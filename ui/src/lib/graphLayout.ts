// Pure layout math for the Graph screen (03 §13.1). A layout is a deterministic seed
// (a radial tree around a focused note, or a link-ordered sunflower spiral for the
// whole vault) followed by a fixed number of force-directed relaxation steps
// (`forceLayout` in `./graphForce`): no randomness, no live simulation, the same graph
// always lands in the same place. This module holds the seeds, merging and boxes; both
// are dependency-free and framework-free so they test without touching React or the DOM.

export interface LayoutNode {
  id: string;
  [key: string]: unknown;
}

export interface LayoutEdge {
  src: string;
  dst: string;
  kind?: string;
}

export interface Point {
  x: number;
  y: number;
}

export interface MergeableGraph<N extends LayoutNode = LayoutNode, E extends LayoutEdge = LayoutEdge> {
  nodes: N[];
  edges: E[];
}

/** BFS ring/parent assignment from `center` over the edge set treated as undirected —
 *  a link or a spawn/pin/walkthrough edge all count as adjacency for layout purposes.
 *  Ties (a node reachable from more than one ring-k node) resolve to the earliest
 *  discoverer in sorted-id order, so the result is deterministic for a given graph. */
export function assignRings(
  center: string,
  nodes: LayoutNode[],
  edges: LayoutEdge[],
): Map<string, { ring: number; parent: string | null }> {
  const ids = new Set(nodes.map((n) => n.id));
  const adjacency = new Map<string, Set<string>>();
  const link = (a: string, b: string) => {
    if (!adjacency.has(a)) adjacency.set(a, new Set());
    adjacency.get(a)!.add(b);
  };
  for (const e of edges) {
    if (!ids.has(e.src) || !ids.has(e.dst)) continue;
    link(e.src, e.dst);
    link(e.dst, e.src);
  }

  const result = new Map<string, { ring: number; parent: string | null }>();
  if (!ids.has(center)) return result;
  result.set(center, { ring: 0, parent: null });
  let frontier = [center];
  let ring = 0;
  while (frontier.length) {
    ring++;
    const next: string[] = [];
    for (const id of [...frontier].sort()) {
      const neighbours = [...(adjacency.get(id) ?? [])].sort();
      for (const n of neighbours) {
        if (result.has(n)) continue;
        result.set(n, { ring, parent: id });
        next.push(n);
      }
    }
    frontier = next;
  }
  return result;
}

/** Radial-tree positions: the centre at the origin, each ring k at radius `ringRadius *
 *  k`, and each node placed at the mid-angle of the angular sector its parent handed it
 *  — the parent's own sector, subdivided equally among its children in sorted-id order.
 *  Nodes the ring map does not reach (disconnected from the centre) are omitted; a
 *  caller that wants them shown falls back to `circlePositions` for those ids. */
export function radialTreePositions(
  center: string,
  ringInfo: Map<string, { ring: number; parent: string | null }>,
  ringRadius = 110,
): Map<string, Point> {
  const childrenOf = new Map<string, string[]>();
  for (const [id, info] of ringInfo) {
    if (info.parent === null) continue;
    if (!childrenOf.has(info.parent)) childrenOf.set(info.parent, []);
    childrenOf.get(info.parent)!.push(id);
  }
  for (const kids of childrenOf.values()) kids.sort();

  const sectors = new Map<string, { start: number; end: number }>();
  const positions = new Map<string, Point>();

  function place(id: string, start: number, end: number) {
    sectors.set(id, { start, end });
    const info = ringInfo.get(id);
    const ring = info ? info.ring : 0;
    if (ring === 0) {
      positions.set(id, { x: 0, y: 0 });
    } else {
      const mid = (start + end) / 2 - Math.PI / 2; // 0 rad = straight up
      const r = ring * ringRadius;
      positions.set(id, { x: r * Math.cos(mid), y: r * Math.sin(mid) });
    }
    const kids = childrenOf.get(id) ?? [];
    if (!kids.length) return;
    const width = (end - start) / kids.length;
    kids.forEach((child, i) => place(child, start + i * width, start + (i + 1) * width));
  }

  if (ringInfo.has(center)) place(center, 0, 2 * Math.PI);
  return positions;
}

/** Nodes on a single circle, sorted by type then path/id — the whole-vault layout. No
 *  hierarchy, so there is nothing to subdivide. */
export function circlePositions<N extends LayoutNode>(nodes: N[], radius: number): Map<string, Point> {
  const sorted = [...nodes].sort((a, b) => {
    const ta = String(a.type ?? "");
    const tb = String(b.type ?? "");
    if (ta !== tb) return ta < tb ? -1 : 1;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
  const positions = new Map<string, Point>();
  const n = sorted.length;
  sorted.forEach((node, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, n) - Math.PI / 2;
    positions.set(node.id, { x: radius * Math.cos(angle), y: radius * Math.sin(angle) });
  });
  return positions;
}

/** Union of two graphs: nodes dedup by id (the first occurrence's fields win — a
 *  re-fetch of an already-known node never overwrites what is already drawn), edges
 *  dedup by (src, dst, kind). Used both for "expand" (merge in a clicked node's
 *  neighbours) and for combining the vault and conversations layers. */
export function mergeGraphs<N extends LayoutNode, E extends LayoutEdge>(
  base: MergeableGraph<N, E>,
  extra: MergeableGraph<N, E>,
): MergeableGraph<N, E> {
  const nodes = [...base.nodes];
  const seenNodes = new Set(nodes.map((n) => n.id));
  for (const n of extra.nodes) {
    if (seenNodes.has(n.id)) continue;
    seenNodes.add(n.id);
    nodes.push(n);
  }
  const edges = [...base.edges];
  const seenEdges = new Set(edges.map((e) => `${e.src}\u0000${e.dst}\u0000${e.kind ?? ""}`));
  for (const e of extra.edges) {
    const key = `${e.src}\u0000${e.dst}\u0000${e.kind ?? ""}`;
    if (seenEdges.has(key)) continue;
    seenEdges.add(key);
    edges.push(e);
  }
  return { nodes, edges };
}

export interface BBox {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
  width: number;
  height: number;
}

/** The bounding box of a set of positions, padded — what "Fit" resets the viewBox to.
 *  A single point (or none) gets a fixed minimum box so the viewBox never collapses. */
export function boundingBox(positions: Map<string, Point>, padding = 40): BBox {
  const pts = [...positions.values()];
  if (!pts.length) return { minX: -160, minY: -120, maxX: 160, maxY: 120, width: 320, height: 240 };
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of pts) {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x);
    maxY = Math.max(maxY, p.y);
  }
  minX -= padding;
  minY -= padding;
  maxX += padding;
  maxY += padding;
  const width = Math.max(1, maxX - minX);
  const height = Math.max(1, maxY - minY);
  return { minX, minY, maxX, maxY, width, height };
}
