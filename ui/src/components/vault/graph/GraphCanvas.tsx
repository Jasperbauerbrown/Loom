// The graph picture (03 §13.1): inline SVG over a pan/zoom view, positions eased from
// one settled layout to the next, labels decluttered by priority, hover and selection
// highlighting a node's neighbourhood. Layout itself is not done here — the caller
// hands in settled positions (`graphForce`); this component only draws and interacts.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { cls, truncate } from "../../../lib/format";
import { boundingBox, type Point } from "../../../lib/graphLayout";
import { adjacencyOf, easeInOut, interpolate } from "../../../lib/graphForce";
import {
  fitView,
  lerpView,
  panBy,
  placeLabels,
  toWorld,
  viewBoxOf,
  zoomAround,
  type Box,
  type LabelCandidate,
  type Size,
  type View,
} from "../../../lib/graphViewport";
import { HUE_FILL, edgeTone, hueOf, statusHue } from "./style";

export type RenderNode = {
  id: string;
  kind: "note" | "conversation";
  title: string;
  type?: string; // note type: the hue
  status?: string; // conversation status: the tint
  goal?: string;
  convKind?: string; // desk | thread
};
export type RenderEdge = { src: string; dst: string; kind?: string };

const MOVE_MS = 520;
const VIEW_MS = 420;
const DRAG_SLOP = 4; // px before a press becomes a drag
const DOUBLE_MS = 400; // two presses on one node within this are a double-click

function usePrefersReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() =>
    typeof window !== "undefined" && window.matchMedia ? window.matchMedia(query).matches : false,
  );
  useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia(query);
    const on = () => setReduced(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return reduced;
}

/** Positions eased from whatever is on screen to `target`. A node new to the picture
 *  starts where one of its neighbours was, so an expansion grows out of the clicked
 *  node rather than popping in. */
function useTweenedPositions(
  target: Map<string, Point>,
  adjacency: Map<string, Set<string>>,
  reduced: boolean,
): Map<string, Point> {
  const [drawn, setDrawn] = useState(target);
  const drawnRef = useRef(target);
  useEffect(() => {
    const from = drawnRef.current;
    if (reduced || from.size === 0 || from === target) {
      drawnRef.current = target;
      setDrawn(target);
      return;
    }
    const start = new Map<string, Point>();
    for (const [id, p] of target) {
      const old = from.get(id);
      if (old) {
        start.set(id, old);
        continue;
      }
      let origin: Point | undefined;
      for (const nb of [...(adjacency.get(id) ?? [])].sort()) {
        origin = from.get(nb);
        if (origin) break;
      }
      start.set(id, origin ?? p);
    }
    let raf = 0;
    const t0 = performance.now();
    const step = (now: number) => {
      const raw = (now - t0) / MOVE_MS;
      const cur = raw >= 1 ? target : interpolate(start, target, easeInOut(raw));
      drawnRef.current = cur;
      setDrawn(cur);
      if (raw < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
    // adjacency is derived from the same graph as target; keying on target suffices
  }, [target, reduced]);
  return drawn;
}

type Drag =
  | { kind: "pan"; x: number; y: number; view: View; moved: boolean }
  | { kind: "node"; id: string; x: number; y: number; dx: number; dy: number; moved: boolean };

function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

function IconButton({ title, onClick, children }: { title: string; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
      className="flex h-7 w-7 items-center justify-center text-neutral-600 transition-colors hover:bg-neutral-100 hover:text-neutral-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60 dark:text-neutral-400 dark:hover:bg-neutral-800 dark:hover:text-neutral-100"
    >
      {children}
    </button>
  );
}

export interface GraphCanvasProps {
  nodes: RenderNode[];
  edges: RenderEdge[];
  /** settled positions from the layout */
  layout: Map<string, Point>;
  /** changes whenever the picture is a different one (mode, centre): refit */
  layoutKey: string;
  centerId: string | null;
  selected: string | null;
  onSelect: (id: string | null) => void;
  /** single click on a node */
  onActivate: (node: RenderNode) => void;
  /** double click on a node */
  onCentre?: (node: RenderNode) => void;
  /** dragged nodes stay where they were dropped (until the layout is reset) */
  pins: Map<string, Point>;
  onPin?: (id: string, p: Point) => void;
  onResetPins?: () => void;
  /** a legend entry under the pointer: highlight that type */
  typeFocus?: string | null;
  compact?: boolean;
  loading?: boolean;
  children?: ReactNode; // overlays (the selected-node card)
}

export function GraphCanvas({
  nodes,
  edges,
  layout,
  layoutKey,
  centerId,
  selected,
  onSelect,
  onActivate,
  onCentre,
  pins,
  onPin,
  onResetPins,
  typeFocus = null,
  compact,
  loading,
  children,
}: GraphCanvasProps) {
  const reduced = usePrefersReducedMotion();
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [size, setSize] = useState<Size>({ w: 0, h: 0 });
  const [view, setViewState] = useState<View>({ cx: 0, cy: 0, s: 1 });
  const viewRef = useRef(view);
  const viewAnim = useRef(0);
  const userMoved = useRef(false);
  const [hovered, setHovered] = useState<string | null>(null);
  const [drag, setDrag] = useState<Drag | null>(null);
  const dragRef = useRef<Drag | null>(null);
  const [dragPos, setDragPos] = useState<{ id: string; p: Point } | null>(null);
  const lastClick = useRef<{ id: string; t: number } | null>(null);

  const setView = useCallback((v: View) => {
    viewRef.current = v;
    setViewState(v);
  }, []);

  const stopViewAnim = useCallback(() => {
    if (viewAnim.current) cancelAnimationFrame(viewAnim.current);
    viewAnim.current = 0;
  }, []);

  const animateView = useCallback(
    (to: View, immediate = false) => {
      stopViewAnim();
      const from = viewRef.current;
      if (immediate || reduced) {
        setView(to);
        return;
      }
      const t0 = performance.now();
      const step = (now: number) => {
        const raw = (now - t0) / VIEW_MS;
        setView(raw >= 1 ? to : lerpView(from, to, easeInOut(raw)));
        viewAnim.current = raw < 1 ? requestAnimationFrame(step) : 0;
      };
      viewAnim.current = requestAnimationFrame(step);
    },
    [reduced, setView, stopViewAnim],
  );
  useEffect(() => stopViewAnim, [stopViewAnim]);

  // element size → the viewBox keeps the element's aspect, so pointer math is exact
  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setSize((s) => (Math.abs(s.w - r.width) < 0.5 && Math.abs(s.h - r.height) < 0.5 ? s : { w: r.width, h: r.height }));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const adjacency = useMemo(() => adjacencyOf(nodes, edges), [nodes, edges]);
  const degree = useMemo(() => {
    const d = new Map<string, number>();
    for (const [id, set] of adjacency) d.set(id, set.size);
    return d;
  }, [adjacency]);
  const maxDegree = useMemo(() => Math.max(1, ...degree.values()), [degree]);

  const tweened = useTweenedPositions(layout, adjacency, reduced);
  const positions = useMemo(() => {
    if (!pins.size && !dragPos) return tweened;
    const out = new Map(tweened);
    for (const [id, p] of pins) if (out.has(id)) out.set(id, p);
    if (dragPos && out.has(dragPos.id)) out.set(dragPos.id, dragPos.p);
    return out;
  }, [tweened, pins, dragPos]);

  const fitTo = useCallback(
    (immediate = false) => {
      if (!size.w || !size.h) return;
      const all = new Map(layout);
      for (const [id, p] of pins) if (all.has(id)) all.set(id, p);
      const box = boundingBox(all, compact ? 26 : 50);
      animateView(fitView(box, size, compact ? 8 : 24, compact ? 1.4 : 1.6), immediate);
    },
    [layout, pins, size, compact, animateView],
  );

  // Refit when the picture changes: always for a new picture (mode, centre, depth), and
  // for a grown one (an expansion, a layer toggle) only while the user has not panned
  // or zoomed — their view is theirs. A resize refits on the same terms, instantly.
  const fitted = useRef<{ key: string; count: number } | null>(null);
  useEffect(() => {
    if (!size.w || !size.h) return;
    const last = fitted.current;
    if (last && last.key === layoutKey && last.count === nodes.length) return;
    fitted.current = { key: layoutKey, count: nodes.length };
    if (!last || last.key !== layoutKey) {
      userMoved.current = false;
      fitTo(!last);
    } else if (!userMoved.current) fitTo();
  }, [layoutKey, nodes.length, size, fitTo]);
  const lastSize = useRef(size);
  useEffect(() => {
    if (lastSize.current.w === size.w && lastSize.current.h === size.h) return;
    const hadSize = lastSize.current.w > 0;
    lastSize.current = size;
    if (hadSize && fitted.current && !userMoved.current) fitTo(true);
  }, [size, fitTo]);

  const zoomBy = useCallback(
    (factor: number) => {
      userMoved.current = true;
      const target = zoomAround(viewRef.current, size, size.w / 2, size.h / 2, factor);
      animateView(target);
    },
    [size, animateView],
  );

  // wheel zoom around the cursor: a native, non-passive listener so preventDefault works
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg || compact) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      stopViewAnim();
      userMoved.current = true;
      const rect = svg.getBoundingClientRect();
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      const factor = Math.exp(-e.deltaY * unit * 0.0015);
      setView(zoomAround(viewRef.current, size, e.clientX - rect.left, e.clientY - rect.top, factor));
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [compact, size, setView, stopViewAnim]);

  // keyboard: Escape clears the selection; + / − / 0 zoom and fit
  useEffect(() => {
    if (compact) return;
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Escape") onSelect(null);
      else if (e.key === "+" || e.key === "=") zoomBy(1.3);
      else if (e.key === "-" || e.key === "_") zoomBy(1 / 1.3);
      else if (e.key === "0") {
        userMoved.current = false;
        fitTo();
      } else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [compact, onSelect, zoomBy, fitTo]);

  function localPoint(e: React.PointerEvent): { x: number; y: number } {
    const rect = svgRef.current!.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function startDrag(d: Drag, e: React.PointerEvent) {
    svgRef.current?.setPointerCapture?.(e.pointerId);
    dragRef.current = d;
    setDrag(d);
  }

  function onBackgroundDown(e: React.PointerEvent<SVGSVGElement>) {
    if (e.button !== 0) return;
    stopViewAnim();
    const p = localPoint(e);
    startDrag({ kind: "pan", x: p.x, y: p.y, view: viewRef.current, moved: false }, e);
  }

  function onNodeDown(e: React.PointerEvent, id: string) {
    if (e.button !== 0) return;
    e.stopPropagation();
    const p = localPoint(e);
    const world = toWorld(viewRef.current, size, p.x, p.y);
    const at = positions.get(id) ?? world;
    startDrag({ kind: "node", id, x: p.x, y: p.y, dx: at.x - world.x, dy: at.y - world.y, moved: false }, e);
  }

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const d = dragRef.current;
    if (!d) return;
    const p = localPoint(e);
    if (!d.moved && Math.hypot(p.x - d.x, p.y - d.y) < DRAG_SLOP) return;
    if (!d.moved) {
      d.moved = true;
      setDrag({ ...d });
    }
    if (d.kind === "pan") {
      userMoved.current = true;
      setView(panBy(d.view, p.x - d.x, p.y - d.y));
    } else if (onPin && !compact) {
      const w = toWorld(viewRef.current, size, p.x, p.y);
      setDragPos({ id: d.id, p: { x: w.x + d.dx, y: w.y + d.dy } });
    }
  }

  function onUp(e: React.PointerEvent<SVGSVGElement>) {
    const d = dragRef.current;
    dragRef.current = null;
    setDrag(null);
    svgRef.current?.releasePointerCapture?.(e.pointerId);
    if (!d) return;
    if (d.kind === "pan") {
      if (!d.moved) onSelect(null);
      return;
    }
    if (!d.moved) {
      // The pointer is captured by the SVG, so the browser's own click/dblclick land on
      // it rather than the node: a second press on the same node soon after is the
      // double-click.
      const node = nodes.find((n) => n.id === d.id);
      const now = performance.now();
      const last = lastClick.current;
      lastClick.current = { id: d.id, t: now };
      if (!node) return;
      if (last && last.id === d.id && now - last.t < DOUBLE_MS && onCentre) {
        lastClick.current = null;
        onCentre(node);
      } else onActivate(node);
      return;
    }
    if (dragPos && dragPos.id === d.id && onPin) onPin(d.id, dragPos.p);
    setDragPos(null);
  }

  // --- what is emphasised --------------------------------------------------------
  const focusId = hovered ?? (drag?.kind === "node" ? drag.id : null) ?? selected;
  const active = useMemo(() => {
    if (typeFocus) {
      const set = new Set<string>();
      for (const n of nodes) if (n.kind === "note" && n.type === typeFocus) set.add(n.id);
      return set;
    }
    if (!focusId || !adjacency.has(focusId)) return null;
    return new Set([focusId, ...adjacency.get(focusId)!]);
  }, [typeFocus, focusId, adjacency, nodes]);

  const s = view.s;
  // Node size is in layout units, but lifted when zoomed out so a whole vault never
  // becomes specks; zoomed in, it scales like everything else.
  const lift = Math.pow(Math.min(1, Math.max(0.3, s)), -0.45);
  const radiusOf = useCallback(
    (id: string) => {
      const d = degree.get(id) ?? 0;
      const base = 4.2 + 7.5 * Math.sqrt(d / maxDegree) + Math.min(2, d * 0.15);
      return (id === centerId ? base + 2.5 : base) * lift;
    },
    [degree, maxDegree, centerId, lift],
  );

  // half-extents of a node's mark, in layout units (a conversation is a pill)
  const extentOf = useCallback(
    (n: RenderNode): { rx: number; ry: number } => {
      if (n.kind === "conversation") {
        const desk = n.convKind === "desk";
        return { rx: ((desk ? 34 : 28) * lift) / 2, ry: ((desk ? 20 : 16) * lift) / 2 };
      }
      const r = radiusOf(n.id);
      return { rx: r, ry: r };
    },
    [radiusOf, lift],
  );

  // --- labels: greedy, priority-ordered, never overlapping ---------------------------
  // Each label tries below its node, then above, right, left; see `placeLabels`.
  const fontPx = compact ? 10.5 : 11.5;
  const labels = useMemo(() => {
    const vb = viewBoxOf(view, size);
    const cands: (LabelCandidate & { rank: number })[] = [];
    const dots: Box[] = [];
    for (const n of nodes) {
      const p = positions.get(n.id);
      if (!p) continue;
      const { rx, ry } = extentOf(n);
      const sx = (p.x - vb.x) * s;
      const sy = (p.y - vb.y) * s;
      if (!active || active.has(n.id)) dots.push({ x: sx - rx * s, y: sy - ry * s, w: 2 * rx * s, h: 2 * ry * s });
      // the centre, the selection and the hovered node always show; a highlighted
      // neighbourhood goes first among the rest, then hubs before leaves
      const near = !!active && active.has(n.id);
      const forced = (n.id === centerId && (!active || near)) || n.id === selected || n.id === hovered;
      if (active && !active.has(n.id) && !forced) continue;
      if (sx < -200 || sy < -40 || sx > size.w + 200 || sy > size.h + 40) continue;
      const text = truncate(n.title || n.id, compact ? 22 : 28);
      const f = n.id === centerId ? fontPx + 1.5 : fontPx;
      const w = text.length * f * 0.56 + 6;
      const h = f * 1.35;
      cands.push({
        id: n.id,
        x: sx - w / 2,
        y: sy + ry * s + 2,
        alts: [
          { x: sx - w / 2, y: sy - ry * s - 2 - h },
          { x: sx + rx * s + 4, y: sy - h / 2 },
          { x: sx - rx * s - 4 - w, y: sy - h / 2 },
        ],
        w,
        h,
        forced,
        rank: forced ? -2e9 : (near ? -1e9 : 0) - (degree.get(n.id) ?? 0),
      });
    }
    cands.sort((a, b) => a.rank - b.rank || (a.id < b.id ? -1 : 1));
    return placeLabels(cands, dots);
  }, [nodes, positions, view, size, s, centerId, selected, hovered, active, extentOf, degree, compact, fontPx]);

  // the last slot each label was drawn in, so a label fading out stays where it was
  const lastSlot = useRef(new Map<string, number>());
  for (const [id, slot] of labels) lastSlot.current.set(id, slot);

  // --- drawing ------------------------------------------------------------------------
  const vb = viewBoxOf(view, size);
  const manyEdges = edges.length > 250;
  const baseEdgeOpacity = manyEdges ? 0.26 : 0.42;
  const px = 1 / s; // one screen pixel in layout units

  const gridStep = 22;
  const gridX = (((-vb.x * s) % gridStep) + gridStep) % gridStep;
  const gridY = (((-vb.y * s) % gridStep) + gridStep) % gridStep;

  const cursor = drag?.moved ? "cursor-grabbing" : compact ? "" : "cursor-grab";

  return (
    <div
      ref={wrapRef}
      className={cls(
        "relative overflow-hidden rounded-lg border border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-950",
        "[--graph-dot:var(--color-neutral-200)] dark:[--graph-dot:var(--color-neutral-800)]",
        compact ? "h-[360px] w-full" : "h-full w-full",
      )}
      style={{
        backgroundImage: "radial-gradient(circle, var(--graph-dot) 1px, transparent 1.3px)",
        backgroundSize: `${gridStep}px ${gridStep}px`,
        backgroundPosition: `${gridX}px ${gridY}px`,
      }}
    >
      {size.w > 0 ? (
        <svg
          ref={svgRef}
          width={size.w}
          height={size.h}
          viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
          className={cls("absolute inset-0 block select-none touch-none", cursor)}
          onPointerDown={compact ? undefined : onBackgroundDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerCancel={onUp}
          role="img"
          aria-label="Graph of notes and their links"
        >
          <g>
            {edges.map((e, i) => {
              const a = positions.get(e.src);
              const b = positions.get(e.dst);
              if (!a || !b) return null;
              const lit = !!active && !typeFocus && (e.src === focusId || e.dst === focusId);
              const inType = !!typeFocus && !!active?.has(e.src) && !!active?.has(e.dst);
              const dim = !!active && !lit && !inType;
              // a gentle arc; the two directions of a mutual link bow to opposite sides
              const lo = e.src < e.dst ? a : b;
              const hi = e.src < e.dst ? b : a;
              const mx = (a.x + b.x) / 2;
              const my = (a.y + b.y) / 2;
              const bend = (e.src < e.dst ? 1 : -1) * 0.1;
              const cx = mx - (hi.y - lo.y) * bend;
              const cy = my + (hi.x - lo.x) * bend;
              return (
                <path
                  key={`${e.src}\u0000${e.dst}\u0000${e.kind ?? ""}\u0000${i}`}
                  d={`M${a.x},${a.y} Q${cx},${cy} ${b.x},${b.y}`}
                  fill="none"
                  stroke="currentColor"
                  vectorEffect="non-scaling-stroke"
                  strokeWidth={lit ? 1.6 : e.kind && e.kind !== "link" ? 1.3 : 1}
                  strokeDasharray={e.kind === "spawned" ? "5 4" : e.kind === "walkthrough" ? "1.5 3" : undefined}
                  strokeLinecap="round"
                  className={cls("transition-[stroke-opacity] duration-200", lit ? "text-sky-500 dark:text-sky-400" : edgeTone(e.kind))}
                  style={{ strokeOpacity: dim ? 0.07 : lit ? 0.9 : e.kind && e.kind !== "link" ? 0.75 : baseEdgeOpacity }}
                />
              );
            })}
          </g>
          <g>
            {nodes.map((n) => {
              const p = positions.get(n.id);
              if (!p) return null;
              const r = radiusOf(n.id);
              const isCenter = n.id === centerId;
              const isSelected = n.id === selected;
              const isHovered = n.id === hovered;
              const dim = !!active && !active.has(n.id);
              const hit = Math.max(r + 2 * px, 9 * px);
              const conv = n.kind === "conversation";
              const w = (n.convKind === "desk" ? 34 : 28) * lift;
              const h = (n.convKind === "desk" ? 20 : 16) * lift;
              return (
                <g
                  key={n.id}
                  transform={`translate(${p.x},${p.y})`}
                  className={cls("transition-opacity duration-200", compact ? "cursor-pointer" : drag ? "" : "cursor-pointer")}
                  style={{ opacity: dim ? 0.16 : 1 }}
                  onPointerDown={(e) => onNodeDown(e, n.id)}
                  onPointerEnter={() => {
                    if (!dragRef.current) setHovered(n.id);
                  }}
                  onPointerLeave={() => setHovered((h) => (h === n.id ? null : h))}
                >
                  <title>{n.title || n.id}</title>
                  <circle r={conv ? Math.max(w, h) / 2 + 3 * px : hit} fill="transparent" />
                  {isCenter ? (
                    <circle
                      r={r + 7 * px + 3}
                      className="fill-sky-500/10 stroke-sky-500/50 dark:fill-sky-400/10 dark:stroke-sky-400/50"
                      strokeWidth={1}
                      vectorEffect="non-scaling-stroke"
                    />
                  ) : null}
                  {isSelected || isHovered ? (
                    <circle
                      r={(conv ? Math.max(w, h) / 2 : r) + 3.5 * px}
                      fill="none"
                      className={isSelected ? "stroke-sky-500 dark:stroke-sky-400" : "stroke-neutral-400 dark:stroke-neutral-500"}
                      strokeWidth={isSelected ? 2 : 1.25}
                      vectorEffect="non-scaling-stroke"
                    />
                  ) : null}
                  {conv ? (
                    <rect
                      x={-w / 2}
                      y={-h / 2}
                      width={w}
                      height={h}
                      rx={h / 2.6}
                      className={cls(statusHue(n.status).fill, "stroke-neutral-50 dark:stroke-neutral-950")}
                      strokeWidth={1.5}
                      vectorEffect="non-scaling-stroke"
                    />
                  ) : (
                    <circle
                      r={r}
                      className={cls(HUE_FILL[hueOf(n.type)], "stroke-neutral-50 dark:stroke-neutral-950")}
                      strokeWidth={1.5}
                      vectorEffect="non-scaling-stroke"
                    />
                  )}
                  {pins.has(n.id) && !compact ? (
                    <circle
                      r={2.2 * px}
                      cx={0}
                      cy={0}
                      className="fill-neutral-50 dark:fill-neutral-950"
                    />
                  ) : null}
                </g>
              );
            })}
          </g>
          <g className="pointer-events-none">
            {nodes.map((n) => {
              const p = positions.get(n.id);
              if (!p) return null;
              const shown = labels.has(n.id);
              const slot = labels.get(n.id) ?? lastSlot.current.get(n.id) ?? 0;
              const isCenter = n.id === centerId;
              const lit = n.id === hovered || n.id === selected;
              const { rx, ry } = extentOf(n);
              const f = (isCenter ? fontPx + 1.5 : fontPx) * px;
              const at =
                slot === 1
                  ? { x: p.x, y: p.y - ry - 2 * px - f * 0.4, anchor: "middle" as const }
                  : slot === 2
                    ? { x: p.x + rx + 4 * px, y: p.y + f * 0.3, anchor: "start" as const }
                    : slot === 3
                      ? { x: p.x - rx - 4 * px, y: p.y + f * 0.3, anchor: "end" as const }
                      : { x: p.x, y: p.y + ry + 2 * px + f * 0.95, anchor: "middle" as const };
              return (
                <text
                  key={n.id}
                  x={at.x}
                  y={at.y}
                  textAnchor={at.anchor}
                  fontSize={f}
                  strokeWidth={3.2 * px}
                  strokeLinejoin="round"
                  className={cls(
                    "stroke-neutral-50 transition-opacity duration-150 dark:stroke-neutral-950",
                    isCenter || lit
                      ? "fill-neutral-900 font-semibold dark:fill-neutral-50"
                      : "fill-neutral-600 dark:fill-neutral-300",
                  )}
                  style={{ paintOrder: "stroke", opacity: shown ? 1 : 0 }}
                >
                  {truncate(n.title || n.id, compact ? 22 : 28)}
                </text>
              );
            })}
          </g>
        </svg>
      ) : null}

      {loading ? (
        <div className="pointer-events-none absolute right-3 top-3 text-neutral-400">
          <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />
        </div>
      ) : null}

      {!compact ? (
        <div className="absolute bottom-3 right-3 flex flex-col overflow-hidden rounded-md border border-neutral-200 bg-white/90 shadow-sm backdrop-blur dark:border-neutral-800 dark:bg-neutral-900/90">
          <IconButton title="Zoom in (+)" onClick={() => zoomBy(1.4)}>
            <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round">
              <path d="M8 3v10M3 8h10" />
            </svg>
          </IconButton>
          <div className="h-px bg-neutral-200 dark:bg-neutral-800" />
          <IconButton title="Zoom out (−)" onClick={() => zoomBy(1 / 1.4)}>
            <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round">
              <path d="M3 8h10" />
            </svg>
          </IconButton>
          <div className="h-px bg-neutral-200 dark:bg-neutral-800" />
          <IconButton
            title="Fit to view (0)"
            onClick={() => {
              userMoved.current = false;
              fitTo();
            }}
          >
            <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
              <path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10" />
            </svg>
          </IconButton>
          {pins.size && onResetPins ? (
            <>
              <div className="h-px bg-neutral-200 dark:bg-neutral-800" />
              <IconButton title={`Release ${pins.size} dragged node${pins.size === 1 ? "" : "s"} and re-run the layout`} onClick={onResetPins}>
                <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5V5h-2.5" />
                </svg>
              </IconButton>
            </>
          ) : null}
        </div>
      ) : null}

      {children}
    </div>
  );
}
