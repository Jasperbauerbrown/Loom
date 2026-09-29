// Vault + conversations graph (03 §13.1): inline SVG, deterministic radial-tree /
// circle layouts (`../../lib/graphLayout`), no physics simulation. Two call sites share
// this one component: the Vault side panel (`compact`, fixed size, depth + an
// "Open in Graph" link only) and the full Graph screen (toolbar, pan/zoom, the
// conversations layer, a selected-node card).

import { useEffect, useMemo, useRef, useState } from "react";
import type { ConversationGraphData, VaultSearchHit } from "../../lib/types";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { graphPath, navigate, notePath } from "../../lib/router";
import { cls, truncate } from "../../lib/format";
import {
  assignRings,
  boundingBox,
  circlePositions,
  mergeGraphs,
  radialTreePositions,
  type Point,
} from "../../lib/graphLayout";
import { Button, Empty, Input, Select, Spinner, Toggle } from "../ui";

type RenderNode = {
  id: string;
  kind: "note" | "conversation";
  title: string;
  type?: string; // note type: the hue
  status?: string; // conversation status: the tint
  goal?: string;
};
type RenderEdge = { src: string; dst: string; kind?: string };

const NOTE_HUES = ["sky", "emerald", "amber", "violet", "rose", "teal"] as const;
const HUE_FILL: Record<string, string> = {
  sky: "fill-sky-500",
  emerald: "fill-emerald-500",
  amber: "fill-amber-500",
  violet: "fill-violet-500",
  rose: "fill-rose-500",
  teal: "fill-teal-500",
};
const HUE_SWATCH: Record<string, string> = {
  sky: "bg-sky-500",
  emerald: "bg-emerald-500",
  amber: "bg-amber-500",
  violet: "bg-violet-500",
  rose: "bg-rose-500",
  teal: "bg-teal-500",
};
const NEUTRAL_FILL = "fill-neutral-400 dark:fill-neutral-500";

function statusFill(status?: string): string {
  switch (status) {
    case "running":
      return "fill-sky-500";
    case "waiting_approval":
    case "waiting_input":
      return "fill-amber-500";
    case "done":
      return "fill-emerald-500";
    case "failed":
      return "fill-red-500";
    default:
      return NEUTRAL_FILL;
  }
}

const RING_RADIUS = 110;
const WHOLE_RADIUS = 260;
const WHOLE_VAULT_LIMIT = 500;
const COMPACT_W = 480;
const COMPACT_H = 360;

const CENTER_KEY = "loom.graph.center";
const CONVERSATIONS_KEY = "loom.graph.conversations";

function rememberCenter(path: string) {
  try {
    localStorage.setItem(CENTER_KEY, path);
  } catch {
    /* private mode: the next visit simply starts from the vault's first note */
  }
}

/** The last graph centre remembered in this browser, for the Graph screen's own
 *  fallback when it opens with no path. */
export function recallGraphCenter(): string | null {
  try {
    return localStorage.getItem(CENTER_KEY);
  } catch {
    return null;
  }
}

function recallConversationsLayer(): boolean {
  try {
    return localStorage.getItem(CONVERSATIONS_KEY) === "1";
  } catch {
    return false;
  }
}

function rememberConversationsLayer(on: boolean) {
  try {
    if (on) localStorage.setItem(CONVERSATIONS_KEY, "1");
    else localStorage.removeItem(CONVERSATIONS_KEY);
  } catch {
    /* private mode: the toggle simply does not persist */
  }
}

interface ViewBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** A small search-to-set-centre box for the full screen's toolbar. */
function CenterPicker({ onPick }: { onPick: (path: string) => void }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<VaultSearchHit[]>([]);

  useEffect(() => {
    if (!query.trim()) {
      setHits([]);
      return;
    }
    let cancelled = false;
    const t = setTimeout(async () => {
      const rows = await store.guard(() => api.vaultSearch(query));
      if (!cancelled) setHits((rows ?? []).slice(0, 8));
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query]);

  function pick(path: string) {
    onPick(path);
    setQuery("");
    setHits([]);
  }

  return (
    <div className="relative">
      <Input
        value={query}
        placeholder="Centre on…"
        className="w-48"
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && hits.length) pick(hits[0].path);
        }}
      />
      {hits.length ? (
        <div className="absolute z-10 mt-1 w-56 overflow-y-auto rounded-md border border-neutral-200 bg-white text-left shadow-md dark:border-neutral-800 dark:bg-neutral-900">
          {hits.map((h) => (
            <button
              key={h.path}
              type="button"
              className="block w-full truncate px-2 py-1 text-left text-[12px] hover:bg-neutral-100 dark:hover:bg-neutral-800"
              onClick={() => pick(h.path)}
            >
              {h.title || h.path} <span className="text-neutral-500">{h.path}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function GraphView({
  center,
  onSelect,
  compact,
}: {
  center: string;
  onSelect: (path: string) => void;
  compact?: boolean;
}) {
  const [focus, setFocus] = useState(center);
  useEffect(() => setFocus(center), [center]);

  const [depth, setDepth] = useState(1);
  const [wholeVault, setWholeVault] = useState(false);
  const [showConversations, setShowConversations] = useState(() => (compact ? false : recallConversationsLayer()));
  const [graph, setGraph] = useState<{ nodes: RenderNode[]; edges: RenderEdge[] } | null>(null);
  const [convGraph, setConvGraph] = useState<ConversationGraphData | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [viewBox, setViewBox] = useState<ViewBox>({ x: -160, y: -120, w: 320, h: 240 });
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ x: number; y: number; vb: ViewBox } | null>(null);
  const nodeCountRef = useRef(0);

  // the base (vault) graph: refetched whenever the mode, centre or depth changes
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      const raw = wholeVault
        ? await store.guard(() => api.vaultGraph(undefined, 1, WHOLE_VAULT_LIMIT))
        : await store.guard(() => api.vaultGraph(focus, depth));
      if (cancelled) return;
      setLoading(false);
      const g = raw ?? { nodes: [], edges: [] };
      setGraph({
        nodes: g.nodes.map((n) => ({ id: n.id, kind: "note" as const, title: n.title || n.id, type: n.type })),
        edges: g.edges.map((e) => ({ src: e.src, dst: e.dst, kind: "link" })),
      });
      setSelected(null);
    })();
    return () => {
      cancelled = true;
    };
  }, [focus, depth, wholeVault]);

  // the conversations layer: an independent fetch, merged in below
  useEffect(() => {
    if (!showConversations) {
      setConvGraph(null);
      return;
    }
    (async () => {
      const g = await store.guard(() => api.conversationGraph());
      setConvGraph(g ?? { nodes: [], edges: [] });
    })();
  }, [showConversations]);

  useEffect(() => rememberConversationsLayer(showConversations), [showConversations]);
  useEffect(() => {
    if (!compact && !wholeVault && focus) rememberCenter(focus);
  }, [focus, wholeVault, compact]);

  const merged = useMemo(() => {
    const base = graph ?? { nodes: [], edges: [] };
    if (!convGraph) return base;
    const convNodes: RenderNode[] = convGraph.nodes.map((n) => ({
      id: n.id,
      kind: "conversation",
      title: n.title || n.goal || n.id,
      status: n.status,
      goal: n.goal,
    }));
    const convEdges: RenderEdge[] = convGraph.edges.map((e) => ({ src: e.src, dst: e.dst, kind: e.kind }));
    // a pinned/walkthrough edge may point at a note this graph never fetched — add a
    // stub node for it rather than drop the edge.
    const known = new Set([...base.nodes.map((n) => n.id), ...convNodes.map((n) => n.id)]);
    const stubs: RenderNode[] = [];
    for (const e of convEdges) {
      if ((e.kind === "pinned" || e.kind === "walkthrough") && !known.has(e.dst)) {
        known.add(e.dst);
        stubs.push({ id: e.dst, kind: "note", title: e.dst.split("/").pop() || e.dst });
      }
    }
    return mergeGraphs(base, { nodes: [...convNodes, ...stubs], edges: convEdges });
  }, [graph, convGraph]);

  const positions = useMemo(() => {
    if (!merged.nodes.length) return new Map<string, Point>();
    if (wholeVault) {
      return circlePositions(
        merged.nodes.map((n) => ({ id: n.id, type: n.kind === "conversation" ? "￿conversation" : (n.type ?? "") })),
        WHOLE_RADIUS,
      );
    }
    const rings = assignRings(focus, merged.nodes, merged.edges);
    const pos = radialTreePositions(focus, rings, RING_RADIUS);
    // anything the ring BFS did not reach (e.g. a conversation far from this centre)
    // still has to go somewhere: one outer ring, so nothing silently vanishes.
    const leftover = merged.nodes.filter((n) => !pos.has(n.id));
    if (leftover.length) {
      let maxRing = 0;
      for (const info of rings.values()) maxRing = Math.max(maxRing, info.ring);
      const outer = circlePositions(leftover, (maxRing + 1) * RING_RADIUS);
      for (const [id, p] of outer) pos.set(id, p);
    }
    return pos;
  }, [merged, wholeVault, focus]);

  function fit() {
    const box = boundingBox(positions, compact ? 30 : 60);
    setViewBox({ x: box.minX, y: box.minY, w: box.width, h: box.height });
  }

  // auto-fit whenever the node count changes (first load, expand, layer toggle) —
  // manual pan/zoom in between is left alone.
  useEffect(() => {
    if (merged.nodes.length !== nodeCountRef.current) {
      nodeCountRef.current = merged.nodes.length;
      const box = boundingBox(positions, compact ? 30 : 60);
      setViewBox({ x: box.minX, y: box.minY, w: box.width, h: box.height });
    }
  }, [merged.nodes.length, positions, compact]);

  async function handleNodeClick(node: RenderNode) {
    if (compact) {
      onSelect(node.id);
      return;
    }
    setSelected(node.id);
    if (wholeVault) {
      setWholeVault(false);
      setFocus(node.id);
      navigate(graphPath(node.id));
      return;
    }
    if (node.kind === "note") {
      const raw = await store.guard(() => api.vaultGraph(node.id, 1));
      if (!raw) return;
      const extra = {
        nodes: raw.nodes.map((n) => ({ id: n.id, kind: "note" as const, title: n.title || n.id, type: n.type })),
        edges: raw.edges.map((e) => ({ src: e.src, dst: e.dst, kind: "link" })),
      };
      setGraph((g) => mergeGraphs(g ?? { nodes: [], edges: [] }, extra));
    }
  }

  function onPointerDown(e: React.PointerEvent<SVGSVGElement>) {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    dragRef.current = { x: e.clientX, y: e.clientY, vb: viewBox };
  }
  function onPointerMove(e: React.PointerEvent<SVGSVGElement>) {
    const drag = dragRef.current;
    if (!drag || !svgRef.current) return;
    const rect = svgRef.current.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dx = ((e.clientX - drag.x) * drag.vb.w) / rect.width;
    const dy = ((e.clientY - drag.y) * drag.vb.h) / rect.height;
    setViewBox({ ...drag.vb, x: drag.vb.x - dx, y: drag.vb.y - dy });
  }
  function onPointerUp() {
    dragRef.current = null;
  }
  function onWheel(e: React.WheelEvent<SVGSVGElement>) {
    if (!svgRef.current) return;
    e.preventDefault();
    const rect = svgRef.current.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const px = (e.clientX - rect.left) / rect.width;
    const py = (e.clientY - rect.top) / rect.height;
    const factor = e.deltaY > 0 ? 1.1 : 0.9;
    const newW = Math.max(80, Math.min(4000, viewBox.w * factor));
    const newH = Math.max(60, Math.min(3000, viewBox.h * factor));
    const cx = viewBox.x + px * viewBox.w;
    const cy = viewBox.y + py * viewBox.h;
    setViewBox({ x: cx - px * newW, y: cy - py * newH, w: newW, h: newH });
  }

  const typeOrder = useMemo(() => {
    const types = new Set<string>();
    for (const n of merged.nodes) if (n.kind === "note" && n.type) types.add(n.type);
    return [...types].sort();
  }, [merged.nodes]);

  function hueFill(type?: string): string {
    if (!type) return NEUTRAL_FILL;
    const i = typeOrder.indexOf(type);
    return i === -1 ? NEUTRAL_FILL : HUE_FILL[NOTE_HUES[i % NOTE_HUES.length]];
  }

  const selectedNode = selected ? (merged.nodes.find((n) => n.id === selected) ?? null) : null;

  const body =
    loading && !merged.nodes.length ? (
      <Spinner />
    ) : merged.nodes.length === 0 ? (
      <Empty>No graph data.</Empty>
    ) : (
      <div className={compact ? "relative" : "relative h-full min-h-0"}>
        <svg
          ref={svgRef}
          viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
          className={cls(
            "rounded-md border border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-950",
            compact ? "w-full" : "h-full w-full cursor-grab touch-none active:cursor-grabbing",
          )}
          style={compact ? { width: COMPACT_W, height: COMPACT_H, maxWidth: "100%" } : undefined}
          onPointerDown={compact ? undefined : onPointerDown}
          onPointerMove={compact ? undefined : onPointerMove}
          onPointerUp={compact ? undefined : onPointerUp}
          onPointerLeave={compact ? undefined : onPointerUp}
          onWheel={compact ? undefined : onWheel}
        >
          <g>
            {merged.edges.map((e, i) => {
              const a = positions.get(e.src);
              const b = positions.get(e.dst);
              if (!a || !b) return null;
              const dim = hovered !== null && hovered !== e.src && hovered !== e.dst;
              const stroke =
                e.kind === "pinned"
                  ? "text-violet-500"
                  : e.kind === "walkthrough"
                    ? "text-amber-500"
                    : e.kind === "spawned"
                      ? "text-sky-500"
                      : "text-neutral-400 dark:text-neutral-600";
              return (
                <line
                  key={i}
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  stroke="currentColor"
                  strokeOpacity={dim ? 0.08 : 0.3}
                  strokeDasharray={e.kind === "spawned" ? "4 3" : undefined}
                  className={stroke}
                />
              );
            })}
          </g>
          <g>
            {merged.nodes.map((n) => {
              const p = positions.get(n.id);
              if (!p) return null;
              const isCenter = !wholeVault && n.id === focus;
              const isSelected = selected === n.id;
              const isNeighbour =
                hovered !== null &&
                hovered !== n.id &&
                merged.edges.some((e) => (e.src === hovered && e.dst === n.id) || (e.dst === hovered && e.src === n.id));
              const dim = hovered !== null && hovered !== n.id && !isNeighbour;
              const label = truncate(n.title || n.id, compact ? 22 : 26);
              return (
                <g
                  key={n.id}
                  transform={`translate(${p.x},${p.y})`}
                  className="cursor-pointer"
                  opacity={dim ? 0.35 : 1}
                  onMouseEnter={() => setHovered(n.id)}
                  onMouseLeave={() => setHovered((h) => (h === n.id ? null : h))}
                  onClick={() => void handleNodeClick(n)}
                >
                  <title>{n.title || n.id}</title>
                  {n.kind === "conversation" ? (
                    <rect
                      x={-16}
                      y={-9}
                      width={32}
                      height={18}
                      rx={4}
                      className={statusFill(n.status)}
                      stroke={isSelected ? "currentColor" : undefined}
                      strokeWidth={isSelected ? 2 : 0}
                    />
                  ) : (
                    <circle
                      r={isCenter ? 9 : isSelected ? 8 : 6}
                      className={hueFill(n.type)}
                      stroke={isSelected ? "currentColor" : undefined}
                      strokeWidth={isSelected ? 2 : 0}
                    />
                  )}
                  <text
                    x={0}
                    y={n.kind === "conversation" ? -13 : isCenter ? -14 : -10}
                    textAnchor="middle"
                    className="fill-neutral-700 text-[9px] dark:fill-neutral-300"
                  >
                    {label}
                  </text>
                </g>
              );
            })}
          </g>
        </svg>
        {!compact && selectedNode ? (
          <div className="absolute bottom-3 left-3 max-w-xs rounded-md border border-neutral-200 bg-white p-2.5 text-[12px] shadow-md dark:border-neutral-800 dark:bg-neutral-900">
            <div className="mb-1.5 truncate font-medium text-neutral-800 dark:text-neutral-200">{selectedNode.title}</div>
            <div className="flex gap-2">
              {selectedNode.kind === "note" ? (
                <>
                  <Button size="sm" onClick={() => navigate(graphPath(selectedNode.id))}>
                    Centre here
                  </Button>
                  <Button size="sm" onClick={() => navigate(notePath(selectedNode.id))}>
                    Open note
                  </Button>
                </>
              ) : (
                <Button size="sm" onClick={() => navigate(`/c/${selectedNode.id}`)}>
                  Open
                </Button>
              )}
            </div>
          </div>
        ) : null}
      </div>
    );

  if (compact) {
    return (
      <div>
        <div className="mb-2 flex items-center justify-between gap-2">
          <a href={graphPath(focus)} className="text-[12px] text-sky-700 hover:underline dark:text-sky-400">
            Open in Graph
          </a>
          <div className="flex items-center gap-2">
            <span className="text-[11.5px] text-neutral-500">Depth</span>
            <Select className="w-16" value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
              <option value={1}>1</option>
              <option value={2}>2</option>
              <option value={3}>3</option>
            </Select>
          </div>
        </div>
        {body}
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-2 flex flex-wrap items-center gap-3 border-b border-neutral-200 pb-2 dark:border-neutral-800">
        <CenterPicker
          onPick={(p) => {
            setWholeVault(false);
            setFocus(p);
            navigate(graphPath(p));
          }}
        />
        <span className="text-[11.5px] text-neutral-500">Depth</span>
        <Select
          className="w-16"
          value={depth}
          disabled={wholeVault}
          onChange={(e) => setDepth(Number(e.target.value))}
        >
          <option value={1}>1</option>
          <option value={2}>2</option>
          <option value={3}>3</option>
        </Select>
        <label className="flex items-center gap-1.5 text-[12px] text-neutral-600 dark:text-neutral-400">
          <Toggle checked={wholeVault} onChange={setWholeVault} label="Whole vault" />
          Whole vault
        </label>
        <label className="flex items-center gap-1.5 text-[12px] text-neutral-600 dark:text-neutral-400">
          <Toggle checked={showConversations} onChange={setShowConversations} label="Conversations" />
          Conversations
        </label>
        <Button size="sm" onClick={fit}>
          Fit
        </Button>
        <div className="ml-auto flex flex-wrap items-center gap-2.5 text-[11px] text-neutral-500">
          {typeOrder.map((t, i) => (
            <span key={t} className="flex items-center gap-1">
              <span className={cls("h-2.5 w-2.5 rounded-full", HUE_SWATCH[NOTE_HUES[i % NOTE_HUES.length]])} />
              {t}
            </span>
          ))}
          {showConversations ? (
            <span className="flex items-center gap-1">
              <span className="h-2.5 w-3.5 rounded-[3px] bg-neutral-400 dark:bg-neutral-600" />
              conversation
            </span>
          ) : null}
        </div>
      </div>
      <div className="min-h-0 flex-1">{body}</div>
    </div>
  );
}
