import { describe, expect, it } from "vitest";
import {
  adjacencyOf,
  degreeMap,
  easeInOut,
  extendSeed,
  forceLayout,
  interpolate,
  spiralSeed,
} from "./graphForce";
import type { LayoutEdge, LayoutNode, Point } from "./graphLayout";

const dist = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y);

/** Two dense clusters of `size` joined by a single bridge edge, plus some islands. */
function twoClusters(size = 12, islands = 3) {
  const nodes: LayoutNode[] = [];
  const edges: LayoutEdge[] = [];
  for (const c of ["a", "b"]) {
    for (let i = 0; i < size; i++) nodes.push({ id: `${c}${i}` });
    for (let i = 0; i < size; i++)
      for (let j = i + 1; j < size; j++) if ((i + j) % 3 === 0 || j === i + 1) edges.push({ src: `${c}${i}`, dst: `${c}${j}` });
  }
  edges.push({ src: "a0", dst: "b0" });
  for (let i = 0; i < islands; i++) nodes.push({ id: `z${i}` });
  return { nodes, edges };
}

/** A deterministic pseudo-random vault: `n` notes, ~2 links each, a few hubs. */
function bigVault(n: number) {
  const nodes: LayoutNode[] = [];
  const edges: LayoutEdge[] = [];
  let x = 12345;
  const rnd = () => ((x = (x * 1103515245 + 12345) % 2147483648) / 2147483648);
  for (let i = 0; i < n; i++) nodes.push({ id: `n${String(i).padStart(4, "0")}` });
  for (let i = 1; i < n; i++) {
    const hub = Math.floor(rnd() * Math.min(i, 20));
    edges.push({ src: nodes[i].id, dst: nodes[rnd() < 0.5 ? hub : Math.floor(rnd() * i)].id });
    if (rnd() < 0.4) edges.push({ src: nodes[i].id, dst: nodes[Math.floor(rnd() * i)].id });
  }
  return { nodes, edges };
}

describe("adjacency and degree", () => {
  it("is undirected, ignores self-loops, unknown endpoints and duplicates", () => {
    const nodes = [{ id: "a" }, { id: "b" }, { id: "c" }];
    const edges = [
      { src: "a", dst: "b" },
      { src: "b", dst: "a" },
      { src: "a", dst: "a" },
      { src: "a", dst: "ghost" },
    ];
    const adj = adjacencyOf(nodes, edges);
    expect([...adj.get("a")!]).toEqual(["b"]);
    expect([...adj.get("b")!]).toEqual(["a"]);
    expect(adj.get("c")!.size).toBe(0);
    expect(Object.fromEntries(degreeMap(nodes, edges))).toEqual({ a: 1, b: 1, c: 0 });
  });
});

describe("spiralSeed", () => {
  it("places every node once, hub first at the middle, deterministically", () => {
    const { nodes, edges } = twoClusters();
    const p1 = spiralSeed(nodes, edges);
    const p2 = spiralSeed([...nodes].reverse(), edges);
    expect(p1.size).toBe(nodes.length);
    for (const [id, p] of p1) expect(p2.get(id)).toEqual(p);
    const deg = degreeMap(nodes, edges);
    const nearest = [...p1.entries()].sort((a, b) => Math.hypot(a[1].x, a[1].y) - Math.hypot(b[1].x, b[1].y))[0][0];
    const maxDeg = Math.max(...deg.values());
    expect(deg.get(nearest)).toBe(maxDeg);
  });
});

describe("extendSeed", () => {
  it("keeps old positions and puts new nodes near their placed neighbours", () => {
    const previous = new Map<string, Point>([
      ["a", { x: 0, y: 0 }],
      ["b", { x: 100, y: 0 }],
    ]);
    const nodes = [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }, { id: "lonely" }];
    const edges = [
      { src: "b", dst: "c" },
      { src: "c", dst: "d" },
    ];
    const fallback = new Map<string, Point>([["lonely", { x: -500, y: -500 }]]);
    const out = extendSeed(previous, nodes, edges, fallback, 50);
    expect(out.get("a")).toEqual({ x: 0, y: 0 });
    expect(out.get("b")).toEqual({ x: 100, y: 0 });
    expect(dist(out.get("c")!, out.get("b")!)).toBeCloseTo(50, 6);
    expect(dist(out.get("d")!, out.get("c")!)).toBeCloseTo(50, 6);
    expect(out.get("lonely")).toEqual({ x: -500, y: -500 });
  });

  it("fans siblings of one anchor apart", () => {
    const previous = new Map<string, Point>([["hub", { x: 10, y: 0 }]]);
    const nodes = [{ id: "hub" }, { id: "k1" }, { id: "k2" }, { id: "k3" }];
    const edges = nodes.slice(1).map((n) => ({ src: "hub", dst: n.id }));
    const out = extendSeed(previous, nodes, edges, new Map(), 50);
    expect(dist(out.get("k1")!, out.get("k2")!)).toBeGreaterThan(10);
    expect(dist(out.get("k2")!, out.get("k3")!)).toBeGreaterThan(10);
  });
});

describe("forceLayout", () => {
  it("is deterministic", () => {
    const { nodes, edges } = twoClusters();
    const seed = spiralSeed(nodes, edges);
    const a = forceLayout(nodes, edges, seed);
    const b = forceLayout(nodes, edges, seed);
    expect([...a.entries()]).toEqual([...b.entries()]);
  });

  it("clusters by links: same-cluster pairs sit closer than cross-cluster pairs", () => {
    const { nodes, edges } = twoClusters();
    const pos = forceLayout(nodes, edges, spiralSeed(nodes, edges));
    const centroid = (c: string) => {
      const pts = nodes.filter((n) => n.id.startsWith(c)).map((n) => pos.get(n.id)!);
      return { x: pts.reduce((s, p) => s + p.x, 0) / pts.length, y: pts.reduce((s, p) => s + p.y, 0) / pts.length };
    };
    const ca = centroid("a");
    const cb = centroid("b");
    const spread = (c: string, m: Point) =>
      Math.max(...nodes.filter((n) => n.id.startsWith(c)).map((n) => dist(pos.get(n.id)!, m)));
    // the two clusters are separated by more than either cluster's own radius
    expect(dist(ca, cb)).toBeGreaterThan(Math.max(spread("a", ca), spread("b", cb)));
  });

  it("keeps nodes apart and every position finite", () => {
    const { nodes, edges } = twoClusters();
    const pos = forceLayout(nodes, edges, spiralSeed(nodes, edges), { linkDistance: 60 });
    const pts = [...pos.values()];
    for (const p of pts) expect(Number.isFinite(p.x) && Number.isFinite(p.y)).toBe(true);
    let min = Infinity;
    for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) min = Math.min(min, dist(pts[i], pts[j]));
    expect(min).toBeGreaterThan(8);
  });

  it("never moves fixed nodes", () => {
    const { nodes, edges } = twoClusters(6, 1);
    const fixed = new Map<string, Point>([
      ["a0", { x: 0, y: 0 }],
      ["b3", { x: 400, y: 50 }],
    ]);
    const pos = forceLayout(nodes, edges, spiralSeed(nodes, edges), { fixed });
    expect(pos.get("a0")).toEqual({ x: 0, y: 0 });
    expect(pos.get("b3")).toEqual({ x: 400, y: 50 });
  });

  it("separates coincident seeds and handles empty and single graphs", () => {
    const nodes = [{ id: "a" }, { id: "b" }, { id: "c" }];
    const pos = forceLayout(nodes, [], new Map());
    expect(dist(pos.get("a")!, pos.get("b")!)).toBeGreaterThan(10);
    expect(forceLayout([], [], new Map()).size).toBe(0);
    const one = forceLayout([{ id: "x" }], [], new Map([["x", { x: 5, y: 5 }]]));
    expect(one.get("x")).toEqual({ x: 0, y: 0 });
  });

  it("lays out 500 notes quickly", () => {
    const { nodes, edges } = bigVault(500);
    const seed = spiralSeed(nodes, edges);
    const t = performance.now();
    const pos = forceLayout(nodes, edges, seed);
    const ms = performance.now() - t;
    expect(pos.size).toBe(500);
    for (const p of pos.values()) expect(Number.isFinite(p.x)).toBe(true);
    // generous bound for a slow CI box; measured locally it is far lower
    expect(ms).toBeLessThan(2500);
  });
});

describe("motion helpers", () => {
  it("eases from 0 to 1 and clamps", () => {
    expect(easeInOut(0)).toBe(0);
    expect(easeInOut(1)).toBe(1);
    expect(easeInOut(0.5)).toBeCloseTo(0.5, 6);
    expect(easeInOut(-1)).toBe(0);
    expect(easeInOut(2)).toBe(1);
  });

  it("interpolates shared ids and takes new ids as-is", () => {
    const from = new Map([
      ["a", { x: 0, y: 0 }],
      ["gone", { x: 1, y: 1 }],
    ]);
    const to = new Map([
      ["a", { x: 10, y: 20 }],
      ["new", { x: 5, y: 5 }],
    ]);
    const mid = interpolate(from, to, 0.5);
    expect(mid.get("a")).toEqual({ x: 5, y: 10 });
    expect(mid.get("new")).toEqual({ x: 5, y: 5 });
    expect(mid.has("gone")).toBe(false);
  });
});
