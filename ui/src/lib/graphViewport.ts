// Viewport and label math for the Graph screen (03 §13.1). The view is a centre and a
// scale (screen pixels per layout unit); the SVG viewBox is derived from it and the
// element's pixel size, so the viewBox always has the element's aspect ratio and
// pointer math is exact. Pure functions, tested without a DOM.

import type { BBox } from "./graphLayout";

export interface View {
  cx: number;
  cy: number;
  s: number; // pixels per layout unit
}

export interface Size {
  w: number;
  h: number;
}

export const MIN_SCALE = 0.08;
export const MAX_SCALE = 5;

export function clampScale(s: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s));
}

/** The SVG viewBox for a view on an element of `size` pixels. */
export function viewBoxOf(view: View, size: Size): { x: number; y: number; w: number; h: number } {
  const w = Math.max(1, size.w) / view.s;
  const h = Math.max(1, size.h) / view.s;
  return { x: view.cx - w / 2, y: view.cy - h / 2, w, h };
}

/** The view that shows `box` whole, centred, with `pad` pixels of margin; never zoomed
 *  in past `maxScale`, so a two-node graph is not drawn enormous. */
export function fitView(box: BBox, size: Size, pad = 40, maxScale = 1.6): View {
  const aw = Math.max(1, size.w - 2 * pad);
  const ah = Math.max(1, size.h - 2 * pad);
  const s = clampScale(Math.min(aw / Math.max(1, box.width), ah / Math.max(1, box.height), maxScale));
  return { cx: box.minX + box.width / 2, cy: box.minY + box.height / 2, s };
}

/** Screen pixel (relative to the element's top-left) → layout coordinates. */
export function toWorld(view: View, size: Size, px: number, py: number): { x: number; y: number } {
  return { x: view.cx + (px - size.w / 2) / view.s, y: view.cy + (py - size.h / 2) / view.s };
}

/** Zoom by `factor` keeping the layout point under pixel (px, py) fixed on screen. */
export function zoomAround(view: View, size: Size, px: number, py: number, factor: number): View {
  const s = clampScale(view.s * factor);
  const anchor = toWorld(view, size, px, py);
  return {
    s,
    cx: anchor.x - (px - size.w / 2) / s,
    cy: anchor.y - (py - size.h / 2) / s,
  };
}

/** Pan by a screen-pixel drag delta. */
export function panBy(view: View, dxPx: number, dyPx: number): View {
  return { ...view, cx: view.cx - dxPx / view.s, cy: view.cy - dyPx / view.s };
}

/** Views part-way from `a` to `b`: centre linearly, scale geometrically (so a zoom
 *  feels even), at eased fraction t. */
export function lerpView(a: View, b: View, t: number): View {
  return {
    cx: a.cx + (b.cx - a.cx) * t,
    cy: a.cy + (b.cy - a.cy) * t,
    s: a.s * Math.pow(b.s / a.s, t),
  };
}

// --- labels ------------------------------------------------------------------------

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface LabelCandidate extends Box {
  id: string;
  /** further top-left corners to try, in order, when the primary box (x, y) is taken —
   *  e.g. above, right, left of the node; the box keeps its w and h */
  alts?: { x: number; y: number }[];
  /** always shown (centre, selected, hovered and its neighbours); still claims space */
  forced?: boolean;
}

/** Greedy label decluttering: candidates in priority order (the caller sorts), each
 *  placed in the first of its slots (the primary box, then its `alts`) that overlaps
 *  no already-placed label and no obstacle (a node's dot), or dropped when none is
 *  free. Forced labels are always placed (in their first free slot, else the primary).
 *  A uniform grid keeps it near-linear at hundreds of labels. Zooming in spreads the
 *  boxes apart, so more labels fit — that is the whole level-of-detail rule. Returns
 *  the chosen slot per placed id: 0 is the primary box, i > 0 is `alts[i - 1]`. */
export function placeLabels(candidates: LabelCandidate[], obstacles: Box[] = [], cell = 64): Map<string, number> {
  const grid = new Map<string, Box[]>();
  const placed = new Map<string, number>();
  const cellsOf = (b: Box) => {
    const out: string[] = [];
    const x0 = Math.floor(b.x / cell);
    const x1 = Math.floor((b.x + b.w) / cell);
    const y0 = Math.floor(b.y / cell);
    const y1 = Math.floor((b.y + b.h) / cell);
    for (let gx = x0; gx <= x1; gx++) for (let gy = y0; gy <= y1; gy++) out.push(`${gx},${gy}`);
    return out;
  };
  const overlaps = (a: Box, b: Box) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  const free = (b: Box) => {
    for (const key of cellsOf(b)) for (const other of grid.get(key) ?? []) if (overlaps(b, other)) return false;
    return true;
  };
  const claim = (b: Box) => {
    for (const key of cellsOf(b)) {
      const list = grid.get(key);
      if (list) list.push(b);
      else grid.set(key, [b]);
    }
  };
  // obstacles (the nodes themselves) claim space first, so no label covers a dot
  for (const o of obstacles) claim(o);
  for (const c of candidates) {
    const slots: Box[] = [c, ...(c.alts ?? []).map((a) => ({ x: a.x, y: a.y, w: c.w, h: c.h }))];
    let chosen = slots.findIndex(free);
    if (chosen === -1) {
      if (!c.forced) continue;
      chosen = 0;
    }
    placed.set(c.id, chosen);
    claim(slots[chosen]);
  }
  return placed;
}
