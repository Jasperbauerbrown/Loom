// Force-directed relaxation for the Graph screen (03 §13.1): a pure function from a
// seed to settled positions, run for a fixed number of cooling steps. No randomness and
// no live simulation — the same graph and seed always land in the same place, so the
// picture is stable across visits and testable without a DOM.

import type { LayoutEdge, LayoutNode, Point } from "./graphLayout";

// --- adjacency ---------------------------------------------------------------------

/** Undirected adjacency over the edges whose endpoints are both known nodes. Self-loops
 *  are dropped: they carry no layout information. */
export function adjacencyOf(nodes: LayoutNode[], edges: LayoutEdge[]): Map<string, Set<string>> {
  const adj = new Map<string, Set<string>>();
  for (const n of nodes) adj.set(n.id, new Set());
  for (const e of edges) {
    if (e.src === e.dst) continue;
    const a = adj.get(e.src);
    const b = adj.get(e.dst);
    if (!a || !b) continue;
    a.add(e.dst);
    b.add(e.src);
  }
  return adj;
}

/** Distinct-neighbour count per node — what node radius and label priority key on. */
export function degreeMap(nodes: LayoutNode[], edges: LayoutEdge[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const [id, set] of adjacencyOf(nodes, edges)) out.set(id, set.size);
  return out;
}

// --- seeds -------------------------------------------------------------------------

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

function byDegreeThenId(adj: Map<string, Set<string>>) {
  return (a: string, b: string) => adj.get(b)!.size - adj.get(a)!.size || (a < b ? -1 : a > b ? 1 : 0);
}

/** A whole-graph seed: nodes in breadth-first order (components largest first, each
 *  walked from its highest-degree node, ties by id) laid on a sunflower spiral, so
 *  linked notes start near each other and the relaxation only has to refine. */
export function spiralSeed(nodes: LayoutNode[], edges: LayoutEdge[], spacing = 40): Map<string, Point> {
  const adj = adjacencyOf(nodes, edges);
  const order = byDegreeThenId(adj);
  const seen = new Set<string>();
  const components: string[][] = [];
  for (const start of [...adj.keys()].sort(order)) {
    if (seen.has(start)) continue;
    const comp: string[] = [start];
    seen.add(start);
    for (let i = 0; i < comp.length; i++) {
      const next = [...adj.get(comp[i])!].filter((id) => !seen.has(id)).sort(order);
      for (const id of next) {
        seen.add(id);
        comp.push(id);
      }
    }
    components.push(comp);
  }
  components.sort((a, b) => b.length - a.length || (a[0] < b[0] ? -1 : 1));
  const positions = new Map<string, Point>();
  let i = 0;
  for (const comp of components) {
    for (const id of comp) {
      const r = spacing * Math.sqrt(i + 0.5);
      const t = i * GOLDEN_ANGLE;
      positions.set(id, { x: r * Math.cos(t), y: r * Math.sin(t) });
      i++;
    }
  }
  return positions;
}

/** An incremental seed: every node already in `previous` keeps its place; each new node
 *  starts a step outside the centroid of its already-placed neighbours (breadth-first,
 *  so a chain of new nodes grows outward), fanned so siblings never coincide. New nodes
 *  with no placed neighbour take their `fallback` position. This is what keeps "expand"
 *  and layer toggles calm: the old picture barely moves. */
export function extendSeed(
  previous: Map<string, Point>,
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  fallback: Map<string, Point>,
  spacing = 60,
): Map<string, Point> {
  const adj = adjacencyOf(nodes, edges);
  const out = new Map<string, Point>();
  for (const n of nodes) {
    const p = previous.get(n.id);
    if (p) out.set(n.id, { x: p.x, y: p.y });
  }
  let pending = nodes
    .map((n) => n.id)
    .filter((id) => !out.has(id))
    .sort();
  // how many new nodes each anchor centroid has already fanned out, so siblings spread
  const fanCount = new Map<string, number>();
  while (pending.length) {
    const still: string[] = [];
    const round = new Map<string, Point>();
    for (const id of pending) {
      const anchors = [...adj.get(id)!].filter((a) => out.has(a)).sort();
      if (!anchors.length) {
        still.push(id);
        continue;
      }
      let cx = 0;
      let cy = 0;
      for (const a of anchors) {
        cx += out.get(a)!.x;
        cy += out.get(a)!.y;
      }
      cx /= anchors.length;
      cy /= anchors.length;
      const key = anchors.join("\u0000");
      const j = fanCount.get(key) ?? 0;
      fanCount.set(key, j + 1);
      const outward = Math.hypot(cx, cy) > 1e-6 ? Math.atan2(cy, cx) : 0;
      // alternate either side of "outward": 0, +, -, ++, --, …
      const side = j === 0 ? 0 : (j % 2 ? 1 : -1) * Math.ceil(j / 2);
      const t = outward + side * 0.5;
      round.set(id, { x: cx + spacing * Math.cos(t), y: cy + spacing * Math.sin(t) });
    }
    if (!round.size) {
      for (const id of still) {
        const f = fallback.get(id) ?? { x: 0, y: 0 };
        out.set(id, { x: f.x, y: f.y });
      }
      break;
    }
    for (const [id, p] of round) out.set(id, p);
    pending = still;
  }
  return out;
}

// --- force-directed relaxation -----------------------------------------------------

export interface ForceOptions {
  /** The rest length of an edge (Fruchterman–Reingold's k). */
  linkDistance?: number;
  /** A fixed step count: the layout is a pure function of its inputs. */
  iterations?: number;
  /** Pull towards the origin; keeps islands and leaves on the page. */
  gravity?: number;
  /** The starting step cap, as a multiple of linkDistance; it cools to ~0. A warm
   *  start (an incremental seed) passes a small value so the picture barely moves. */
  temperature?: number;
  /** Nodes that do not move (the focused centre, nodes the user dragged). */
  fixed?: Map<string, Point>;
  /** Beyond this distance (a multiple of linkDistance) repulsion fades as 1/d³ rather
   *  than 1/d, so islands settle near the main cluster instead of being flung off. */
  range?: number;
  /** Barnes–Hut opening angle: larger is faster and coarser. */
  theta?: number;
}

interface Cell {
  x: number; // square: top-left corner and side
  y: number;
  size: number;
  mass: number;
  sx: number; // coordinate sums → centre of mass
  sy: number;
  body: number; // a leaf's single body index; -1 when empty, internal or aggregated
  kids: (Cell | null)[] | null;
}

function newCell(x: number, y: number, size: number): Cell {
  return { x, y, size, mass: 0, sx: 0, sy: 0, body: -1, kids: null };
}

function insert(cell: Cell, i: number, xs: Float64Array, ys: Float64Array, depth: number): void {
  const px = xs[i];
  const py = ys[i];
  if (cell.mass === 0 && cell.kids === null) {
    cell.body = i;
    cell.mass = 1;
    cell.sx = px;
    cell.sy = py;
    return;
  }
  if (cell.kids === null) {
    if (depth > 24) {
      // (near-)coincident bodies: aggregate rather than subdivide forever
      cell.mass += 1;
      cell.sx += px;
      cell.sy += py;
      cell.body = -1;
      return;
    }
    const old = cell.body;
    cell.body = -1;
    cell.kids = [null, null, null, null];
    if (old >= 0) descend(cell, old, xs, ys, depth);
  }
  cell.mass += 1;
  cell.sx += px;
  cell.sy += py;
  descend(cell, i, xs, ys, depth);
}

function descend(cell: Cell, i: number, xs: Float64Array, ys: Float64Array, depth: number): void {
  const half = cell.size / 2;
  const right = xs[i] >= cell.x + half ? 1 : 0;
  const down = ys[i] >= cell.y + half ? 1 : 0;
  const q = right + 2 * down;
  let kid = cell.kids![q];
  if (!kid) {
    kid = newCell(cell.x + right * half, cell.y + down * half, half);
    cell.kids![q] = kid;
  }
  insert(kid, i, xs, ys, depth + 1);
}

/** Fruchterman–Reingold relaxation — k²/d repulsion (Barnes–Hut), d²/k springs, linear
 *  gravity — for a fixed number of cooling steps from `seed`. Deterministic: there is no
 *  randomness, and coincident nodes separate along an index-derived direction. Nodes
 *  missing from `seed` start at the origin. Fixed nodes never move; with none, the
 *  result is re-centred on its centroid. */
export function forceLayout(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  seed: Map<string, Point>,
  options: ForceOptions = {},
): Map<string, Point> {
  const n = nodes.length;
  if (n === 0) return new Map();
  const k = options.linkDistance ?? 60;
  const iterations = options.iterations ?? (n > 250 ? 200 : 300);
  const gravity = options.gravity ?? 0.05;
  const theta2 = (options.theta ?? 0.9) ** 2;
  const range2 = (k * (options.range ?? 4)) ** 2;
  const fixed = options.fixed ?? new Map<string, Point>();

  const index = new Map<string, number>();
  nodes.forEach((node, i) => index.set(node.id, i));
  const xs = new Float64Array(n);
  const ys = new Float64Array(n);
  const pinned = new Uint8Array(n);
  nodes.forEach((node, i) => {
    const f = fixed.get(node.id);
    const p = f ?? seed.get(node.id) ?? { x: 0, y: 0 };
    xs[i] = p.x;
    ys[i] = p.y;
    if (f) pinned[i] = 1;
  });

  // undirected, deduplicated springs
  const springSeen = new Set<number>();
  const sa: number[] = [];
  const sb: number[] = [];
  for (const e of edges) {
    const a = index.get(e.src);
    const b = index.get(e.dst);
    if (a === undefined || b === undefined || a === b) continue;
    const key = a < b ? a * n + b : b * n + a;
    if (springSeen.has(key)) continue;
    springSeen.add(key);
    sa.push(a);
    sb.push(b);
  }

  const k2 = k * k;
  const g = gravity * Math.sqrt(n);
  const fx = new Float64Array(n);
  const fy = new Float64Array(n);
  const t0 = k * (options.temperature ?? 2);
  const stack: Cell[] = [];

  for (let it = 0; it < iterations; it++) {
    fx.fill(0);
    fy.fill(0);

    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (let i = 0; i < n; i++) {
      if (xs[i] < minX) minX = xs[i];
      if (ys[i] < minY) minY = ys[i];
      if (xs[i] > maxX) maxX = xs[i];
      if (ys[i] > maxY) maxY = ys[i];
    }
    const root = newCell(minX, minY, Math.max(maxX - minX, maxY - minY, 1) * 1.001 + 1e-6);
    for (let i = 0; i < n; i++) insert(root, i, xs, ys, 0);

    // repulsion: k²/d from every other body, far groups as one mass
    for (let i = 0; i < n; i++) {
      const px = xs[i];
      const py = ys[i];
      let ax = 0;
      let ay = 0;
      stack.length = 0;
      stack.push(root);
      while (stack.length) {
        const c = stack.pop()!;
        if (c.mass === 0 || (c.kids === null && c.body === i)) continue;
        let mass = c.mass;
        let cx = c.sx / mass;
        let cy = c.sy / mass;
        const leaf = c.kids === null;
        if (leaf && c.body === -1) {
          // an aggregated leaf may contain i itself
          const selfIn = Math.abs(cx - px) < 1e-6 && Math.abs(cy - py) < 1e-6;
          if (selfIn) mass -= 1;
          if (mass <= 0) continue;
          cx = c.sx / c.mass;
          cy = c.sy / c.mass;
        }
        let ddx = px - cx;
        let ddy = py - cy;
        let d2 = ddx * ddx + ddy * ddy;
        if (leaf || (c.size * c.size) / d2 < theta2) {
          if (d2 < 1e-4) {
            const a = i * GOLDEN_ANGLE;
            ddx = Math.cos(a) * 0.01;
            ddy = Math.sin(a) * 0.01;
            d2 = 1e-4;
          }
          const f = d2 > range2 ? (k2 * mass * range2) / (d2 * d2) : (k2 * mass) / d2;
          ax += ddx * f;
          ay += ddy * f;
        } else {
          for (const kid of c.kids!) if (kid) stack.push(kid);
        }
      }
      fx[i] += ax;
      fy[i] += ay;
    }

    // attraction: d²/k along each spring
    for (let s = 0; s < sa.length; s++) {
      const a = sa[s];
      const b = sb[s];
      const ddx = xs[a] - xs[b];
      const ddy = ys[a] - ys[b];
      const d = Math.sqrt(ddx * ddx + ddy * ddy);
      const f = d / k;
      fx[a] -= ddx * f;
      fy[a] -= ddy * f;
      fx[b] += ddx * f;
      fy[b] += ddy * f;
    }

    // gravity towards the origin, growing with the graph so large vaults stay compact
    for (let i = 0; i < n; i++) {
      fx[i] -= xs[i] * g;
      fy[i] -= ys[i] * g;
    }

    // move, each step capped by the cooling temperature
    const cool = 1 - it / iterations;
    const t = t0 * cool * cool + k * 0.005;
    for (let i = 0; i < n; i++) {
      if (pinned[i]) continue;
      const len = Math.sqrt(fx[i] * fx[i] + fy[i] * fy[i]);
      if (len < 1e-9) continue;
      const step = Math.min(len, t) / len;
      xs[i] += fx[i] * step;
      ys[i] += fy[i] * step;
    }
  }

  let ox = 0;
  let oy = 0;
  if (!fixed.size) {
    for (let i = 0; i < n; i++) {
      ox += xs[i];
      oy += ys[i];
    }
    ox /= n;
    oy /= n;
  }
  const out = new Map<string, Point>();
  nodes.forEach((node, i) => out.set(node.id, { x: xs[i] - ox, y: ys[i] - oy }));
  return out;
}

// --- motion ------------------------------------------------------------------------

/** Ease-in-out cubic on [0, 1]. */
export function easeInOut(t: number): number {
  const c = Math.min(1, Math.max(0, t));
  return c < 0.5 ? 4 * c * c * c : 1 - (-2 * c + 2) ** 3 / 2;
}

/** Positions part-way from `from` to `to` at eased fraction `t`; ids only in `to` are
 *  taken as-is, ids only in `from` are dropped. */
export function interpolate(from: Map<string, Point>, to: Map<string, Point>, t: number): Map<string, Point> {
  const out = new Map<string, Point>();
  for (const [id, b] of to) {
    const a = from.get(id);
    out.set(id, a ? { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t } : b);
  }
  return out;
}
