// Vault + conversations graph (03 §13.1). Data, layout and controls live here; the
// picture is `graph/GraphCanvas`. A layout is a deterministic seed — a radial tree
// around the focused note, or a link-ordered spiral for the whole vault — relaxed by a
// fixed-step force pass (`lib/graphForce`); an expansion or a layer toggle re-relaxes
// warm from the positions already on screen, so the picture moves only as much as it
// must. Two call sites share this component: the Vault side panel (`compact`: fixed
// size, depth and an "Open in Graph" link) and the full Graph screen (toolbar, pan and
// zoom, dragging, the conversations layer, a selected-node card, a legend).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ConversationGraphData } from "../../lib/types";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { graphPath, navigate } from "../../lib/router";
import { cls, statusLabel } from "../../lib/format";
import { assignRings, circlePositions, mergeGraphs, radialTreePositions, type Point } from "../../lib/graphLayout";
import { extendSeed, forceLayout, spiralSeed } from "../../lib/graphForce";
import { Empty, Select, Spinner, Toggle } from "../ui";
import { GraphCanvas, type RenderEdge, type RenderNode } from "./graph/GraphCanvas";
import { CenterPicker } from "./graph/CenterPicker";
import { NodeCard } from "./graph/NodeCard";
import { HUE_SWATCH, hueOf, statusHue } from "./graph/style";

const FOCUS_LINK = 78; // rest length of an edge around a focused note
const WHOLE_LINK = 52; // … and across the whole vault, which is denser
const RING_RADIUS = 120; // the focused seed's hop rings
const WHOLE_VAULT_LIMIT = 500;
const NO_PINS: Map<string, Point> = new Map();

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

function toRender(raw: { nodes: { id: string; title?: string; type?: string }[]; edges: { src: string; dst: string }[] }) {
  return {
    nodes: raw.nodes.map((n) => ({ id: n.id, kind: "note" as const, title: n.title || n.id, type: n.type })),
    edges: raw.edges.map((e) => ({ src: e.src, dst: e.dst, kind: "link" })),
  };
}

/** The legend: note types present (hover one to pick its notes out) and, with the
 *  conversations layer on, the status tints and edge kinds. */
function Legend({
  nodes,
  conversations,
  onFocusType,
}: {
  nodes: RenderNode[];
  conversations: boolean;
  onFocusType: (t: string | null) => void;
}) {
  const counts = new Map<string, number>();
  const statuses = new Map<string, string>();
  for (const n of nodes) {
    if (n.kind === "note") counts.set(n.type || "", (counts.get(n.type || "") ?? 0) + 1);
    else {
      const st = statusHue(n.status);
      statuses.set(statusLabel(n.status ?? "idle"), st.swatch);
    }
  }
  const types = [...counts.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1));
  if (!types.length && !statuses.size) return null;
  return (
    <div
      className="absolute bottom-3 left-3 max-w-[16rem] rounded-lg border border-neutral-200 bg-white/90 px-2.5 py-2 text-[11.5px] shadow-sm backdrop-blur dark:border-neutral-800 dark:bg-neutral-900/90"
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="flex flex-col gap-0.5">
        {types.map(([t, c]) => (
          <div
            key={t || "(none)"}
            className="-mx-1 flex cursor-default items-center gap-2 rounded px-1 py-px text-neutral-600 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800"
            onPointerEnter={() => onFocusType(t)}
            onPointerLeave={() => onFocusType(null)}
          >
            <span className={cls("h-2.5 w-2.5 shrink-0 rounded-full", HUE_SWATCH[hueOf(t || undefined)])} />
            <span className="truncate">{t || "untyped"}</span>
            <span className="ml-auto pl-3 tabular-nums text-neutral-400">{c}</span>
          </div>
        ))}
      </div>
      {conversations ? (
        <div className="mt-1.5 border-t border-neutral-200 pt-1.5 text-neutral-500 dark:border-neutral-800">
          <div className="flex flex-wrap gap-x-2.5 gap-y-0.5">
            {[...statuses.entries()].map(([label, swatch]) => (
              <span key={label} className="flex items-center gap-1">
                <span className={cls("h-2 w-3 rounded-[3px]", swatch)} />
                {label}
              </span>
            ))}
          </div>
          <div className="mt-1 flex flex-wrap gap-x-2.5 gap-y-0.5">
            <span className="flex items-center gap-1">
              <svg width="18" height="6" className="text-neutral-500 dark:text-neutral-400">
                <path d="M1 3h16" stroke="currentColor" strokeWidth="1.3" strokeDasharray="4 3" />
              </svg>
              spawned
            </span>
            <span className="flex items-center gap-1">
              <svg width="18" height="6" className="text-violet-500 dark:text-violet-400">
                <path d="M1 3h16" stroke="currentColor" strokeWidth="1.5" />
              </svg>
              pinned
            </span>
            <span className="flex items-center gap-1">
              <svg width="18" height="6" className="text-amber-500 dark:text-amber-400">
                <path d="M1 3h16" stroke="currentColor" strokeWidth="1.5" strokeDasharray="1.5 3" strokeLinecap="round" />
              </svg>
              walkthrough
            </span>
          </div>
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
  const [typeFocus, setTypeFocus] = useState<string | null>(null);
  const [pinState, setPinState] = useState<{ basis: string; map: Map<string, Point> }>(() => ({
    basis: "",
    map: new Map(),
  }));
  const [epoch, setEpoch] = useState(0); // bumps on "release dragged nodes"

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
      setGraph(toRender(raw ?? { nodes: [], edges: [] }));
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
    let cancelled = false;
    (async () => {
      const g = await store.guard(() => api.conversationGraph());
      if (!cancelled) setConvGraph(g ?? { nodes: [], edges: [] });
    })();
    return () => {
      cancelled = true;
    };
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
      convKind: n.kind,
    }));
    const convEdges: RenderEdge[] = convGraph.edges.map((e) => ({ src: e.src, dst: e.dst, kind: e.kind }));
    // a pinned/walkthrough edge may point at a note this graph never fetched — add a
    // stub node for it rather than drop the edge.
    const known = new Set([...base.nodes.map((n) => n.id), ...convNodes.map((n) => n.id)]);
    const stubs: RenderNode[] = [];
    for (const e of convEdges) {
      if ((e.kind === "pinned" || e.kind === "walkthrough") && !known.has(e.dst)) {
        known.add(e.dst);
        stubs.push({ id: e.dst, kind: "note", title: e.dst.split("/").pop()?.replace(/\.md$/, "") || e.dst });
      }
    }
    return mergeGraphs(base, { nodes: [...convNodes, ...stubs], edges: convEdges });
  }, [graph, convGraph]);

  // A different picture (mode, centre, depth) starts a fresh layout and drops the pins;
  // anything else (expand, layer toggle) re-relaxes warm from what is on screen.
  const basis = wholeVault ? "whole" : `focus\u0000${focus}\u0000${depth}`;
  const pins = pinState.basis === basis ? pinState.map : NO_PINS;
  const pinsRef = useRef(pins);
  pinsRef.current = pins;
  const previous = useRef<{ key: string; positions: Map<string, Point> } | null>(null);

  const layout = useMemo(() => {
    const { nodes, edges } = merged;
    if (!nodes.length) return new Map<string, Point>();
    const key = `${basis}\u0000${epoch}`;
    const k = wholeVault ? WHOLE_LINK : FOCUS_LINK;
    // the whole vault: weaker gravity lets clusters breathe, shorter-range repulsion keeps
    // unlinked notes in a ring near the rest instead of at the edge of the world
    const shape = wholeVault ? { gravity: 0.02, range: 2.5 } : {};

    let fallback: Map<string, Point>;
    if (wholeVault) {
      fallback = spiralSeed(nodes, edges, k * 0.75);
    } else {
      const rings = assignRings(focus, nodes, edges);
      fallback = radialTreePositions(focus, rings, RING_RADIUS);
      // anything the ring BFS did not reach (a conversation far from this centre) starts
      // on one outer ring, so nothing silently vanishes
      const leftover = nodes.filter((n) => !fallback.has(n.id));
      if (leftover.length) {
        let maxRing = 0;
        for (const info of rings.values()) maxRing = Math.max(maxRing, info.ring);
        for (const [id, p] of circlePositions(leftover, (maxRing + 1) * RING_RADIUS)) fallback.set(id, p);
      }
    }

    const fixed = new Map<string, Point>();
    const ids = new Set(nodes.map((n) => n.id));
    for (const [id, p] of pinsRef.current) if (ids.has(id)) fixed.set(id, p);
    if (!wholeVault && ids.has(focus) && !fixed.has(focus)) fixed.set(focus, { x: 0, y: 0 });

    const warm = previous.current?.key === key ? previous.current.positions : null;
    const positions = warm
      ? forceLayout(nodes, edges, extendSeed(warm, nodes, edges, fallback, k), {
          linkDistance: k,
          fixed,
          temperature: 0.8,
          iterations: 180,
          ...shape,
        })
      : forceLayout(nodes, edges, fallback, { linkDistance: k, fixed, ...shape });
    previous.current = { key, positions };
    return positions;
    // pins are read through a ref: dragging must not re-run the layout; a drop is
    // already where it should be, and the next re-layout keeps it fixed
  }, [merged, basis, epoch, wholeVault, focus]);

  const centreOn = useCallback((id: string) => {
    setWholeVault(false);
    setFocus(id);
    navigate(graphPath(id));
  }, []);

  async function activate(node: RenderNode) {
    if (compact) {
      if (node.kind === "note") onSelect(node.id);
      return;
    }
    setSelected(node.id);
    if (wholeVault || node.kind !== "note") return;
    const raw = await store.guard(() => api.vaultGraph(node.id, 1));
    if (!raw) return;
    setGraph((g) => mergeGraphs(g ?? { nodes: [], edges: [] }, toRender(raw)));
  }

  function centreNode(node: RenderNode) {
    if (compact) return;
    if (node.kind === "conversation") navigate(`/c/${node.id}`);
    else centreOn(node.id);
  }

  const pin = useCallback(
    (id: string, p: Point) => {
      setPinState((old) => ({ basis, map: new Map(old.basis === basis ? old.map : []).set(id, p) }));
    },
    [basis],
  );
  const resetPins = useCallback(() => {
    setPinState({ basis: "", map: new Map() });
    setEpoch((e) => e + 1);
  }, []);

  const selectedNode = selected ? (merged.nodes.find((n) => n.id === selected) ?? null) : null;
  const noteCount = merged.nodes.filter((n) => n.kind === "note").length;
  const linkCount = merged.edges.filter((e) => !e.kind || e.kind === "link").length;
  const convCount = merged.nodes.length - noteCount;
  const lonely = !wholeVault && merged.nodes.length === 1 && !loading;

  const body =
    !graph && loading ? (
      <div
        className={cls(
          "flex items-center justify-center rounded-lg border border-neutral-200 bg-neutral-50 text-neutral-400 dark:border-neutral-800 dark:bg-neutral-950",
          compact ? "h-[360px]" : "h-full",
        )}
      >
        <Spinner />
      </div>
    ) : merged.nodes.length === 0 ? (
      <div
        className={cls(
          "flex items-center justify-center rounded-lg border border-dashed border-neutral-300 dark:border-neutral-800",
          compact ? "h-[200px]" : "h-full",
        )}
      >
        <Empty>{wholeVault ? "The vault has no notes yet." : "Nothing to draw for this note."}</Empty>
      </div>
    ) : (
      <GraphCanvas
        nodes={merged.nodes}
        edges={merged.edges}
        layout={layout}
        layoutKey={`${basis}\u0000${epoch}`}
        centerId={wholeVault ? null : focus}
        selected={compact ? null : selected}
        onSelect={setSelected}
        onActivate={(n) => void activate(n)}
        onCentre={centreNode}
        pins={pins}
        onPin={compact ? undefined : pin}
        onResetPins={resetPins}
        typeFocus={typeFocus}
        compact={compact}
        loading={loading}
      >
        {lonely ? (
          <div className="pointer-events-none absolute inset-x-0 bottom-12 text-center text-[12px] text-neutral-500">
            No links yet — a <span className="font-mono">[[wikilink]]</span> to or from this note connects it.
          </div>
        ) : null}
        {!compact && selectedNode ? (
          <NodeCard
            node={selectedNode}
            edges={merged.edges}
            isCenter={!wholeVault && selectedNode.id === focus}
            onClose={() => setSelected(null)}
            onCentre={(n) => centreOn(n.id)}
          />
        ) : null}
        {!compact ? <Legend nodes={merged.nodes} conversations={showConversations} onFocusType={setTypeFocus} /> : null}
      </GraphCanvas>
    );

  if (compact) {
    const types = [...new Set(merged.nodes.map((n) => n.type || ""))].sort().slice(0, 8);
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
        {types.length && merged.nodes.length ? (
          <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-neutral-500">
            {types.map((t) => (
              <span key={t || "(none)"} className="flex items-center gap-1">
                <span className={cls("h-2 w-2 rounded-full", HUE_SWATCH[hueOf(t || undefined)])} />
                {t || "untyped"}
              </span>
            ))}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-2.5 flex flex-wrap items-center gap-x-4 gap-y-2">
        <CenterPicker onPick={centreOn} />
        <label className="flex items-center gap-1.5 text-[12px] text-neutral-600 dark:text-neutral-400">
          Depth
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
        </label>
        <label className="flex items-center gap-1.5 text-[12px] text-neutral-600 dark:text-neutral-400">
          <Toggle checked={wholeVault} onChange={setWholeVault} label="Whole vault" />
          Whole vault
        </label>
        <label className="flex items-center gap-1.5 text-[12px] text-neutral-600 dark:text-neutral-400">
          <Toggle checked={showConversations} onChange={setShowConversations} label="Conversations" />
          Conversations
        </label>
        <div className="ml-auto text-[11.5px] tabular-nums text-neutral-500">
          {noteCount} note{noteCount === 1 ? "" : "s"} · {linkCount} link{linkCount === 1 ? "" : "s"}
          {convCount ? ` · ${convCount} conversation${convCount === 1 ? "" : "s"}` : ""}
          {wholeVault && noteCount >= WHOLE_VAULT_LIMIT ? ` (first ${WHOLE_VAULT_LIMIT})` : ""}
        </div>
      </div>
      <div className="min-h-0 flex-1">{body}</div>
      <div className="mt-1.5 text-[11px] text-neutral-400">
        Click to select and expand · double-click to centre · drag a node to move it · scroll to zoom · Esc clears
      </div>
    </div>
  );
}
