import { describe, expect, it } from "vitest";
import {
  GAP,
  MIN_H,
  MIN_W,
  autoColumns,
  cascade,
  clampRect,
  defaultSize,
  fromFrac,
  isFracRect,
  maximizedRect,
  moveRect,
  overlaps,
  placeNew,
  resizeRect,
  restoreUnderPointer,
  snapMove,
  snapResize,
  tile,
  toFrac,
  type Rect,
  type Size,
} from "./overviewLayout";

const B: Size = { w: 1200, h: 800 };

function inside(r: Rect, b: Size) {
  return r.x >= 0 && r.y >= 0 && r.x + r.w <= b.w + 0.001 && r.y + r.h <= b.h + 0.001;
}

describe("clampRect", () => {
  it("keeps a rect inside the workspace", () => {
    expect(clampRect({ x: -50, y: 900, w: 400, h: 300 }, B)).toEqual({ x: 0, y: 500, w: 400, h: 300 });
  });
  it("enforces the minimum size", () => {
    const r = clampRect({ x: 10, y: 10, w: 50, h: 20 }, B);
    expect(r.w).toBe(MIN_W);
    expect(r.h).toBe(MIN_H);
  });
  it("never exceeds the workspace, even below the minimum", () => {
    const tiny = { w: 200, h: 100 };
    expect(clampRect({ x: 0, y: 0, w: 900, h: 900 }, tiny)).toEqual({ x: 0, y: 0, w: 200, h: 100 });
  });
});

describe("moveRect", () => {
  it("moves by the pointer delta", () => {
    expect(moveRect({ x: 100, y: 100, w: 400, h: 300 }, 50, -20, B)).toEqual({ x: 150, y: 80, w: 400, h: 300 });
  });
  it("stops at the edges", () => {
    expect(moveRect({ x: 100, y: 100, w: 400, h: 300 }, 5000, 5000, B)).toEqual({ x: 800, y: 500, w: 400, h: 300 });
  });
});

describe("resizeRect", () => {
  const start = { x: 200, y: 200, w: 400, h: 300 };
  it("east edge changes only the width", () => {
    expect(resizeRect(start, "e", 100, 50, B)).toEqual({ x: 200, y: 200, w: 500, h: 300 });
  });
  it("north-west corner keeps the bottom-right corner fixed", () => {
    const r = resizeRect(start, "nw", -50, -40, B);
    expect(r).toEqual({ x: 150, y: 160, w: 450, h: 340 });
    expect(r.x + r.w).toBe(600);
    expect(r.y + r.h).toBe(500);
  });
  it("cannot shrink below the minimum from either side", () => {
    expect(resizeRect(start, "w", 1000, 0, B).w).toBe(MIN_W);
    expect(resizeRect(start, "w", 1000, 0, B).x + MIN_W).toBe(600);
    expect(resizeRect(start, "s", 0, -1000, B).h).toBe(MIN_H);
  });
  it("cannot grow past the workspace", () => {
    expect(resizeRect(start, "se", 5000, 5000, B)).toEqual({ x: 200, y: 200, w: 1000, h: 600 });
    expect(resizeRect(start, "nw", -5000, -5000, B)).toEqual({ x: 0, y: 0, w: 600, h: 500 });
  });
});

describe("snapMove", () => {
  it("snaps to the inset workspace edge", () => {
    const { rect, guides } = snapMove({ x: 3, y: 300, w: 400, h: 300 }, [], B);
    expect(rect.x).toBe(GAP);
    expect(guides.x).toEqual([GAP]);
  });
  it("snaps the right edge to the far workspace edge", () => {
    const { rect } = snapMove({ x: 795, y: 100, w: 400, h: 300 }, [], B);
    expect(rect.x + rect.w).toBe(B.w - GAP);
  });
  it("snaps beside a neighbouring window, one gap apart", () => {
    const other = { x: 8, y: 8, w: 400, h: 500 };
    const { rect } = snapMove({ x: 420, y: 100, w: 300, h: 300 }, [other], B);
    expect(rect.x).toBe(other.x + other.w + GAP);
  });
  it("aligns top edges with a neighbour", () => {
    const other = { x: 8, y: 50, w: 400, h: 500 };
    const { rect } = snapMove({ x: 416, y: 56, w: 300, h: 300 }, [other], B);
    expect(rect.y).toBe(50);
  });
  it("ignores windows that are far away on the other axis", () => {
    const other = { x: 8, y: 8, w: 400, h: 100 };
    const { rect } = snapMove({ x: 420, y: 500, w: 300, h: 200 }, [other], B);
    expect(rect.x).toBe(420);
  });
  it("leaves a rect alone when nothing is within the threshold", () => {
    const r = { x: 300, y: 300, w: 300, h: 250 };
    expect(snapMove(r, [], B)).toEqual({ rect: r, guides: { x: [], y: [] } });
  });
});

describe("snapResize", () => {
  it("snaps only the dragged edge", () => {
    const { rect } = snapResize({ x: 100, y: 100, w: 1086, h: 300 }, "e", [], B);
    expect(rect.x).toBe(100);
    expect(rect.x + rect.w).toBe(B.w - GAP);
  });
  it("does not snap an edge that is not being dragged", () => {
    const { rect } = snapResize({ x: 3, y: 100, w: 400, h: 300 }, "e", [], B);
    expect(rect.x).toBe(3);
  });
  it("snaps the bottom edge to just above a neighbour", () => {
    const below = { x: 100, y: 500, w: 400, h: 200 };
    const { rect } = snapResize({ x: 100, y: 100, w: 400, h: 395 }, "s", [below], B);
    expect(rect.y + rect.h).toBe(below.y - GAP);
  });
});

describe("placeNew", () => {
  it("puts the first window at the top-left", () => {
    const r = placeNew([], B);
    expect(r.x).toBe(GAP);
    expect(r.y).toBe(GAP);
  });
  it("places beside existing windows without overlap", () => {
    const a = placeNew([], B);
    const b = placeNew([a], B);
    expect(overlaps(a, b)).toBe(false);
    expect(b.x).toBe(a.x + a.w + GAP);
    const c = placeNew([a, b], B);
    expect(overlaps(a, c) || overlaps(b, c)).toBe(false);
    expect(inside(c, B)).toBe(true);
  });
  it("falls back to smaller sizes, then a cascade, when the workspace is full", () => {
    const full = tile(1, B);
    const r = placeNew(full, B);
    expect(inside(r, B)).toBe(true);
    // Everything overlaps a full-screen window; it must still not sit exactly on it.
    expect(r).not.toEqual(full[0]);
  });
  it("in a full tiled workspace, cascades off every existing window's corner", () => {
    const grid = tile(4, B);
    const r = placeNew(grid, B);
    expect(inside(r, B)).toBe(true);
    expect(grid.some((g) => g.x === r.x && g.y === r.y)).toBe(false);
    expect(r.h).toBeLessThan(B.h - 2 * GAP);
  });
  it("uses the free gap left by existing windows", () => {
    const left = { x: GAP, y: GAP, w: 400, h: B.h - 2 * GAP };
    const right = { x: 800, y: GAP, w: 392, h: B.h - 2 * GAP };
    const r = placeNew([left, right], B, { w: 380, h: 400 });
    expect(r.x).toBe(left.x + left.w + GAP);
    expect(overlaps(r, left) || overlaps(r, right)).toBe(false);
  });
});

describe("tile", () => {
  it("returns one rect per window, all inside, none overlapping", () => {
    for (let n = 1; n <= 9; n++) {
      const rects = tile(n, B);
      expect(rects).toHaveLength(n);
      for (const r of rects) expect(inside(r, B)).toBe(true);
      for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) expect(overlaps(rects[i], rects[j])).toBe(false);
    }
  });
  it("a single window fills the workspace less the gap", () => {
    expect(tile(1, B)).toEqual([maximizedRect(B)]);
  });
  it("honours an explicit column count, stretching a short last row", () => {
    const rects = tile(3, B, 2);
    expect(rects[0].y).toBe(rects[1].y);
    expect(rects[2].w).toBe(B.w - 2 * GAP);
  });
  it("one column stacks rows", () => {
    const rects = tile(3, B, 1);
    expect(new Set(rects.map((r) => r.x)).size).toBe(1);
    expect(rects[1].y).toBeGreaterThan(rects[0].y);
  });
  it("auto-columns picks side-by-side for two on a wide screen", () => {
    expect(autoColumns(2, B)).toBe(2);
    expect(autoColumns(1, B)).toBe(1);
    expect(autoColumns(4, B)).toBe(2);
  });
});

describe("cascade", () => {
  it("offsets each window and stays inside", () => {
    const rects = cascade(12, B);
    expect(rects[1].x).toBeGreaterThan(rects[0].x);
    expect(rects[1].y).toBeGreaterThan(rects[0].y);
    for (const r of rects) expect(inside(r, B)).toBe(true);
  });
});

describe("fractions", () => {
  it("round-trips through the same workspace", () => {
    const r = { x: 120, y: 80, w: 480, h: 400 };
    expect(fromFrac(toFrac(r, B), B)).toEqual(r);
  });
  it("scales with a resized workspace and clamps into a small one", () => {
    const f = toFrac({ x: 600, y: 400, w: 600, h: 400 }, B);
    expect(fromFrac(f, { w: 2400, h: 1600 })).toEqual({ x: 1200, y: 800, w: 1200, h: 800 });
    const small = fromFrac(f, { w: 500, h: 400 });
    expect(inside(small, { w: 500, h: 400 })).toBe(true);
    expect(small.w).toBe(MIN_W);
  });
  it("validates stored rects", () => {
    expect(isFracRect({ x: 0, y: 0, w: 0.5, h: 0.5 })).toBe(true);
    expect(isFracRect({ x: 0, y: 0, w: 0, h: 0.5 })).toBe(false);
    expect(isFracRect({ x: "0", y: 0, w: 1, h: 1 })).toBe(false);
    expect(isFracRect(null)).toBe(false);
  });
});

describe("restoreUnderPointer", () => {
  it("keeps the grab point at the same relative position", () => {
    const restore = { x: 0, y: 0, w: 400, h: 300 };
    const r = restoreUnderPointer(restore, 600, B);
    expect(r.w).toBe(400);
    expect(r.x).toBeCloseTo(600 - 400 * ((600 - GAP) / (B.w - 2 * GAP)), 5);
  });
});

describe("defaultSize", () => {
  it("is a full-height column within bounds", () => {
    const s = defaultSize(B);
    expect(s.h).toBe(B.h - 2 * GAP);
    expect(s.w).toBeGreaterThanOrEqual(MIN_W);
    expect(s.w).toBeLessThanOrEqual(B.w);
  });
});
