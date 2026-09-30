import { describe, expect, it } from "vitest";
import { boundingBox } from "./graphLayout";
import {
  MAX_SCALE,
  MIN_SCALE,
  fitView,
  lerpView,
  panBy,
  placeLabels,
  toWorld,
  viewBoxOf,
  zoomAround,
} from "./graphViewport";

const size = { w: 800, h: 400 };

describe("viewport", () => {
  it("derives a viewBox with the element's aspect ratio", () => {
    const vb = viewBoxOf({ cx: 10, cy: 20, s: 2 }, size);
    expect(vb).toEqual({ x: 10 - 200, y: 20 - 100, w: 400, h: 200 });
  });

  it("fits a box whole, centred, capped at maxScale", () => {
    const box = boundingBox(
      new Map([
        ["a", { x: -100, y: -100 }],
        ["b", { x: 300, y: 100 }],
      ]),
      0,
    );
    const v = fitView(box, size, 0, 3);
    expect(v.cx).toBe(100);
    expect(v.cy).toBe(0);
    expect(v.s).toBeCloseTo(2, 6); // min(800/400, 400/200)
    const tiny = fitView(boundingBox(new Map([["a", { x: 0, y: 0 }]]), 1), size, 0, 1.5);
    expect(tiny.s).toBe(1.5);
  });

  it("zooms around the cursor: the point under it stays put", () => {
    const v = { cx: 50, cy: -30, s: 1.2 };
    const before = toWorld(v, size, 600, 100);
    const z = zoomAround(v, size, 600, 100, 1.7);
    const after = toWorld(z, size, 600, 100);
    expect(after.x).toBeCloseTo(before.x, 9);
    expect(after.y).toBeCloseTo(before.y, 9);
    expect(z.s).toBeCloseTo(1.2 * 1.7, 9);
  });

  it("clamps the scale", () => {
    expect(zoomAround({ cx: 0, cy: 0, s: 1 }, size, 0, 0, 1e6).s).toBe(MAX_SCALE);
    expect(zoomAround({ cx: 0, cy: 0, s: 1 }, size, 0, 0, 1e-6).s).toBe(MIN_SCALE);
  });

  it("pans by screen pixels", () => {
    expect(panBy({ cx: 0, cy: 0, s: 2 }, 20, -10)).toEqual({ cx: -10, cy: 5, s: 2 });
  });

  it("interpolates views, scale geometrically", () => {
    const m = lerpView({ cx: 0, cy: 0, s: 1 }, { cx: 10, cy: 20, s: 4 }, 0.5);
    expect(m).toEqual({ cx: 5, cy: 10, s: 2 });
  });
});

describe("placeLabels", () => {
  it("keeps the first of two overlapping labels, and always keeps forced ones", () => {
    const kept = placeLabels([
      { id: "hub", x: 0, y: 0, w: 100, h: 14 },
      { id: "under", x: 50, y: 5, w: 100, h: 14 },
      { id: "far", x: 400, y: 400, w: 100, h: 14 },
      { id: "hovered", x: 10, y: 2, w: 100, h: 14, forced: true },
    ]);
    expect([...kept.keys()].sort()).toEqual(["far", "hovered", "hub"]);
  });

  it("tries the alternative slots in order, and never covers an obstacle", () => {
    const placed = placeLabels(
      [
        { id: "a", x: 0, y: 0, w: 50, h: 10 },
        { id: "b", x: 10, y: 2, w: 50, h: 10, alts: [{ x: 20, y: 5 }, { x: 0, y: 40 }] },
        { id: "c", x: 200, y: 0, w: 50, h: 10 },
      ],
      [{ x: 210, y: 5, w: 6, h: 6 }],
    );
    expect(placed.get("a")).toBe(0);
    expect(placed.get("b")).toBe(2); // primary and first alt overlap "a"
    expect(placed.has("c")).toBe(false); // covers the dot
  });

  it("fits more labels as the same boxes spread apart (zooming in)", () => {
    const row = (gap: number) =>
      Array.from({ length: 10 }, (_, i) => ({ id: `n${i}`, x: i * gap, y: 0, w: 60, h: 14 }));
    expect(placeLabels(row(20)).size).toBeLessThan(placeLabels(row(80)).size);
    expect(placeLabels(row(80)).size).toBe(10);
  });
});
