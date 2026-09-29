import { describe, expect, it } from "vitest";
import { assignRings, boundingBox, circlePositions, mergeGraphs, radialTreePositions } from "./graphLayout";

const NODES = [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }, { id: "e" }];
// a -- b -- d
// a -- c -- e
const EDGES = [
  { src: "a", dst: "b" },
  { src: "b", dst: "d" },
  { src: "a", dst: "c" },
  { src: "c", dst: "e" },
];

describe("assignRings", () => {
  it("assigns hop distance from the centre over undirected edges", () => {
    const rings = assignRings("a", NODES, EDGES);
    expect(rings.get("a")).toEqual({ ring: 0, parent: null });
    expect(rings.get("b")).toEqual({ ring: 1, parent: "a" });
    expect(rings.get("c")).toEqual({ ring: 1, parent: "a" });
    expect(rings.get("d")).toEqual({ ring: 2, parent: "b" });
    expect(rings.get("e")).toEqual({ ring: 2, parent: "c" });
  });

  it("picks the earliest discoverer in sorted-id order for a diamond", () => {
    // z is reachable from both x and y at the same ring: x sorts first.
    const nodes = [{ id: "center" }, { id: "x" }, { id: "y" }, { id: "z" }];
    const edges = [
      { src: "center", dst: "x" },
      { src: "center", dst: "y" },
      { src: "y", dst: "z" },
      { src: "x", dst: "z" },
    ];
    const rings = assignRings("center", nodes, edges);
    expect(rings.get("z")).toEqual({ ring: 2, parent: "x" });
  });

  it("is empty when the centre is not among the nodes", () => {
    expect(assignRings("nope", NODES, EDGES).size).toBe(0);
  });

  it("leaves disconnected nodes out of the ring map", () => {
    const rings = assignRings("a", [...NODES, { id: "island" }], EDGES);
    expect(rings.has("island")).toBe(false);
  });
});

describe("radialTreePositions", () => {
  it("is deterministic: the same graph always lays out the same way", () => {
    const rings = assignRings("a", NODES, EDGES);
    const p1 = radialTreePositions("a", rings, 100);
    const p2 = radialTreePositions("a", rings, 100);
    expect([...p1.entries()]).toEqual([...p2.entries()]);
  });

  it("places the centre at the origin and each ring at radius k * ringRadius", () => {
    const rings = assignRings("a", NODES, EDGES);
    const pos = radialTreePositions("a", rings, 100);
    expect(pos.get("a")).toEqual({ x: 0, y: 0 });
    const dist = (id: string) => {
      const p = pos.get(id)!;
      return Math.hypot(p.x, p.y);
    };
    expect(dist("b")).toBeCloseTo(100, 6);
    expect(dist("c")).toBeCloseTo(100, 6);
    expect(dist("d")).toBeCloseTo(200, 6);
    expect(dist("e")).toBeCloseTo(200, 6);
  });

  it("keeps each child inside its parent's angular sector", () => {
    // A wide fan: centre with three children, the middle one having two of its own.
    const nodes = [{ id: "c" }, { id: "l" }, { id: "m" }, { id: "r" }, { id: "m1" }, { id: "m2" }];
    const edges = [
      { src: "c", dst: "l" },
      { src: "c", dst: "m" },
      { src: "c", dst: "r" },
      { src: "m", dst: "m1" },
      { src: "m", dst: "m2" },
    ];
    const rings = assignRings("c", nodes, edges);
    const pos = radialTreePositions("c", rings, 100);
    const twoPi = 2 * Math.PI;
    const angleOf = (id: string) => {
      const p = pos.get(id)!;
      // 0 rad is straight up (the module's own convention), unwrapped into [0, 2π) so
      // comparisons never straddle atan2's ±π discontinuity.
      const raw = Math.atan2(p.x, -p.y);
      return ((raw % twoPi) + twoPi) % twoPi;
    };
    // l, m, r (sorted-id order) split the full circle into three equal thirds.
    const third = twoPi / 3;
    expect(angleOf("l")).toBeCloseTo(third / 2, 5);
    expect(angleOf("m")).toBeCloseTo(third + third / 2, 5);
    expect(angleOf("r")).toBeCloseTo(2 * third + third / 2, 5);
    // m1/m2 (m's own children) sit inside m's third, never spilling into l's or r's.
    const mStart = third;
    const mEnd = 2 * third;
    for (const child of ["m1", "m2"]) {
      const a = angleOf(child);
      expect(a).toBeGreaterThanOrEqual(mStart - 1e-9);
      expect(a).toBeLessThanOrEqual(mEnd + 1e-9);
    }
  });
});

describe("circlePositions", () => {
  it("is deterministic and sorts by type then id", () => {
    const nodes = [
      { id: "b.md", type: "note" },
      { id: "a.md", type: "note" },
      { id: "z.md", type: "asset" },
    ];
    const pos = circlePositions(nodes, 200);
    const order = [...pos.keys()];
    expect(order).toEqual(["z.md", "a.md", "b.md"]);
    for (const p of pos.values()) expect(Math.hypot(p.x, p.y)).toBeCloseTo(200, 6);
  });
});

describe("mergeGraphs", () => {
  it("dedups nodes by id, keeping the first, and edges by (src, dst, kind)", () => {
    const base = {
      nodes: [{ id: "a", title: "first" }],
      edges: [{ src: "a", dst: "b", kind: "link" }],
    };
    const extra = {
      nodes: [{ id: "a", title: "second" }, { id: "b", title: "b" }],
      edges: [
        { src: "a", dst: "b", kind: "link" },
        { src: "a", dst: "b", kind: "pinned" },
      ],
    };
    const merged = mergeGraphs(base, extra);
    expect(merged.nodes).toEqual([{ id: "a", title: "first" }, { id: "b", title: "b" }]);
    expect(merged.edges).toEqual([
      { src: "a", dst: "b", kind: "link" },
      { src: "a", dst: "b", kind: "pinned" },
    ]);
  });
});

describe("boundingBox", () => {
  it("pads the box around the given points", () => {
    const positions = new Map([
      ["a", { x: -10, y: -5 }],
      ["b", { x: 10, y: 5 }],
    ]);
    const box = boundingBox(positions, 10);
    expect(box).toEqual({ minX: -20, minY: -15, maxX: 20, maxY: 15, width: 40, height: 30 });
  });

  it("returns a fixed non-collapsing box when there are no points", () => {
    const box = boundingBox(new Map());
    expect(box.width).toBeGreaterThan(0);
    expect(box.height).toBeGreaterThan(0);
  });
});
