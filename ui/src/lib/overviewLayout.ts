// Window geometry for the Overview workspace (03 §13.1): pure functions so every drag,
// resize, snap, placement and arrangement is unit-tested without a DOM. Pixel rects are
// relative to the workspace's top-left corner; what is persisted is the same rect as
// fractions of the workspace, so a layout survives resizing the browser.

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Size {
  w: number;
  h: number;
}

/** Which edge (or corner) a resize grabs: compass points. */
export type Edge = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

/** Space between tiled windows and around the workspace edge; also the snap offset
 *  when one window is placed beside another. */
export const GAP = 8;
/** How close (px) an edge must come to a target before it snaps. */
export const SNAP = 10;
export const MIN_W = 300;
export const MIN_H = 220;

export interface Guides {
  x: number[];
  y: number[];
}

const NO_GUIDES: Guides = { x: [], y: [] };

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(Math.max(n, lo), Math.max(lo, hi));
}

/** The minimum size, shrunk when the workspace itself is smaller than it. */
export function minSize(b: Size): Size {
  return { w: Math.min(MIN_W, b.w), h: Math.min(MIN_H, b.h) };
}

/** Keep a rect inside the workspace and at least the minimum size. */
export function clampRect(r: Rect, b: Size): Rect {
  const min = minSize(b);
  const w = clamp(r.w, min.w, b.w);
  const h = clamp(r.h, min.h, b.h);
  return { x: clamp(r.x, 0, b.w - w), y: clamp(r.y, 0, b.h - h), w, h };
}

/** A window dragged by (dx, dy) from where the drag began. */
export function moveRect(start: Rect, dx: number, dy: number, b: Size): Rect {
  return clampRect({ ...start, x: start.x + dx, y: start.y + dy }, b);
}

/** A window resized by dragging `edge` by (dx, dy): the opposite edges stay put. */
export function resizeRect(start: Rect, edge: Edge, dx: number, dy: number, b: Size): Rect {
  const min = minSize(b);
  let left = start.x;
  let right = start.x + start.w;
  let top = start.y;
  let bottom = start.y + start.h;
  if (edge.includes("w")) left = clamp(left + dx, 0, right - min.w);
  if (edge.includes("e")) right = clamp(right + dx, left + min.w, b.w);
  if (edge.includes("n")) top = clamp(top + dy, 0, bottom - min.h);
  if (edge.includes("s")) bottom = clamp(bottom + dy, top + min.h, b.h);
  return { x: left, y: top, w: right - left, h: bottom - top };
}

/** Do two spans come within `slack` of each other (touching or overlapping)? */
function near(a0: number, a1: number, b0: number, b1: number, slack: number): boolean {
  return a0 <= b1 + slack && b0 <= a1 + slack;
}

interface Targets {
  /** positions a window's low edge (left/top) may snap to */
  low: number[];
  /** positions a window's high edge (right/bottom) may snap to */
  high: number[];
}

/** Snap targets along one axis: the workspace edges (inset by GAP) and, for every
 *  neighbouring window, alignment with its edges and adjacency one GAP beside it. */
function axisTargets(r: Rect, others: Rect[], b: Size, axis: "x" | "y"): Targets {
  const size = axis === "x" ? b.w : b.h;
  const low = [GAP];
  const high = [size - GAP];
  for (const o of others) {
    const neighbour =
      axis === "x"
        ? near(r.y, r.y + r.h, o.y, o.y + o.h, GAP + SNAP)
        : near(r.x, r.x + r.w, o.x, o.x + o.w, GAP + SNAP);
    if (!neighbour) continue;
    const o0 = axis === "x" ? o.x : o.y;
    const o1 = axis === "x" ? o.x + o.w : o.y + o.h;
    low.push(o0, o1 + GAP);
    high.push(o1, o0 - GAP);
  }
  return { low, high };
}

/** The smallest shift (within `threshold`) that puts `value` on one of `targets`. */
function nearestShift(value: number, targets: number[], threshold: number): number | null {
  let best: number | null = null;
  for (const t of targets) {
    const d = t - value;
    if (Math.abs(d) <= threshold && (best === null || Math.abs(d) < Math.abs(best))) best = d;
  }
  return best;
}

/** Snap a moved window: whichever of its edges is nearest a target pulls the whole
 *  window onto it, per axis. Returns the snapped rect and the guide lines to draw. */
export function snapMove(
  r: Rect,
  others: Rect[],
  b: Size,
  threshold: number = SNAP,
): { rect: Rect; guides: Guides } {
  const guides: Guides = { x: [], y: [] };
  const out = { ...r };
  for (const axis of ["x", "y"] as const) {
    const t = axisTargets(r, others, b, axis);
    const lo = axis === "x" ? r.x : r.y;
    const len = axis === "x" ? r.w : r.h;
    const sLow = nearestShift(lo, t.low, threshold);
    const sHigh = nearestShift(lo + len, t.high, threshold);
    let shift: number | null = null;
    let edge = 0;
    if (sLow !== null && (sHigh === null || Math.abs(sLow) <= Math.abs(sHigh))) {
      shift = sLow;
      edge = lo + sLow;
    } else if (sHigh !== null) {
      shift = sHigh;
      edge = lo + len + sHigh;
    }
    if (shift !== null) {
      out[axis] = lo + shift;
      guides[axis].push(edge);
    }
  }
  const rect = clampRect(out, b);
  return { rect, guides: rect.x === out.x && rect.y === out.y ? guides : NO_GUIDES };
}

/** Snap a resized window: only the edges being dragged move. */
export function snapResize(
  r: Rect,
  edge: Edge,
  others: Rect[],
  b: Size,
  threshold: number = SNAP,
): { rect: Rect; guides: Guides } {
  const min = minSize(b);
  const guides: Guides = { x: [], y: [] };
  let left = r.x;
  let right = r.x + r.w;
  let top = r.y;
  let bottom = r.y + r.h;
  const tx = axisTargets(r, others, b, "x");
  const ty = axisTargets(r, others, b, "y");
  if (edge.includes("w")) {
    const s = nearestShift(left, tx.low, threshold);
    if (s !== null && right - (left + s) >= min.w) {
      left += s;
      guides.x.push(left);
    }
  }
  if (edge.includes("e")) {
    const s = nearestShift(right, tx.high, threshold);
    if (s !== null && right + s - left >= min.w) {
      right += s;
      guides.x.push(right);
    }
  }
  if (edge.includes("n")) {
    const s = nearestShift(top, ty.low, threshold);
    if (s !== null && bottom - (top + s) >= min.h) {
      top += s;
      guides.y.push(top);
    }
  }
  if (edge.includes("s")) {
    const s = nearestShift(bottom, ty.high, threshold);
    if (s !== null && bottom + s - top >= min.h) {
      bottom += s;
      guides.y.push(bottom);
    }
  }
  return { rect: clampRect({ x: left, y: top, w: right - left, h: bottom - top }, b), guides };
}

/** Strict overlap (edges that merely touch do not count). */
export function overlaps(a: Rect, c: Rect): boolean {
  return a.x < c.x + c.w - 0.5 && c.x < a.x + a.w - 0.5 && a.y < c.y + c.h - 0.5 && c.y < a.y + a.h - 0.5;
}

/** The size a newly opened window starts at: a full-height column about a third of
 *  the workspace wide. */
export function defaultSize(b: Size): Size {
  const min = minSize(b);
  return {
    w: clamp(Math.round((b.w - 4 * GAP) / 3), min.w, Math.min(560, b.w - 2 * GAP)),
    h: Math.max(min.h, b.h - 2 * GAP),
  };
}

/** Where a new window goes: the first free spot (top-to-bottom, left-to-right, beside
 *  or below the existing windows) where it fits without overlapping any; trying the
 *  default size, then half height, then the minimum. When the workspace is full it
 *  cascades from the top-left, so it is never hidden exactly behind another. */
export function placeNew(existing: Rect[], b: Size, size: Size = defaultSize(b)): Rect {
  const min = minSize(b);
  const sizes: Size[] = [size, { w: size.w, h: Math.max(min.h, Math.round((b.h - 3 * GAP) / 2)) }, min];
  const xs = [GAP, ...existing.map((r) => r.x + r.w + GAP)].sort((a, c) => a - c);
  const ys = [GAP, ...existing.map((r) => r.y + r.h + GAP)].sort((a, c) => a - c);
  for (const s of sizes) {
    for (const y of ys) {
      for (const x of xs) {
        const r = { x, y, w: s.w, h: s.h };
        if (x + s.w > b.w + 0.5 || y + s.h > b.h + 0.5) continue;
        if (!existing.some((e) => overlaps(e, r))) return r;
      }
    }
  }
  // No free spot: a shorter window on the cascade diagonal, at the first step whose
  // corner no existing window already occupies (so it never hides exactly behind one).
  const fs = { w: size.w, h: Math.max(min.h, Math.min(size.h, Math.round(b.h * 0.7))) };
  const step = 32;
  const room = Math.max(1, Math.floor(Math.min(b.w - fs.w - GAP, b.h - fs.h - GAP) / step) + 1);
  const taken = (x: number, y: number) => existing.some((e) => Math.abs(e.x - x) < 4 && Math.abs(e.y - y) < 4);
  let k = 0;
  while (k < room - 1 && taken(GAP + k * step, GAP + k * step)) k++;
  return clampRect({ x: GAP + k * step, y: GAP + k * step, w: fs.w, h: fs.h }, b);
}

/** Column count for an automatic tile: the grid whose cells are nearest a
 *  comfortable reading shape, preferring full rows, never below the minimum size
 *  when some count fits it. */
export function autoColumns(n: number, b: Size): number {
  if (n <= 1) return 1;
  let best = 1;
  let bestScore = Infinity;
  for (let cols = 1; cols <= n; cols++) {
    const rows = Math.ceil(n / cols);
    const cw = (b.w - GAP * (cols + 1)) / cols;
    const ch = (b.h - GAP * (rows + 1)) / rows;
    const fits = cw >= MIN_W && ch >= MIN_H;
    const aspect = Math.max(cw, 1) / Math.max(ch, 1);
    const empty = cols * rows - n;
    const score = Math.abs(Math.log(aspect / 1.2)) + (0.4 * empty) / cols + (fits ? 0 : 10);
    if (score < bestScore) {
      bestScore = score;
      best = cols;
    }
  }
  return best;
}

/** Tile `n` windows into a grid of `columns` (auto when omitted). Rows share the height
 *  equally; a short last row stretches its windows across the full width. */
export function tile(n: number, b: Size, columns?: number): Rect[] {
  if (n <= 0) return [];
  const cols = Math.max(1, Math.min(n, columns ?? autoColumns(n, b)));
  const rows = Math.ceil(n / cols);
  const ch = (b.h - GAP * (rows + 1)) / rows;
  const out: Rect[] = [];
  for (let row = 0; row < rows; row++) {
    const k = row < rows - 1 ? cols : n - cols * (rows - 1);
    const cw = (b.w - GAP * (k + 1)) / k;
    for (let i = 0; i < k; i++) {
      const r = {
        x: Math.round(GAP + i * (cw + GAP)),
        y: Math.round(GAP + row * (ch + GAP)),
        w: Math.round(cw),
        h: Math.round(ch),
      };
      out.push(clampRect(r, b));
    }
  }
  return out;
}

/** Cascade `n` windows diagonally from the top-left, wrapping when out of room. */
export function cascade(n: number, b: Size): Rect[] {
  const min = minSize(b);
  const w = Math.max(min.w, Math.round(b.w * 0.55));
  const h = Math.max(min.h, Math.round(b.h * 0.7));
  const step = 30;
  const room = Math.max(1, Math.floor(Math.min(b.w - w - GAP, b.h - h - GAP) / step) + 1);
  const out: Rect[] = [];
  for (let i = 0; i < n; i++) {
    const k = i % room;
    const wrap = Math.floor(i / room);
    out.push(clampRect({ x: GAP + k * step + wrap * step * 2, y: GAP + k * step, w, h }, b));
  }
  return out;
}

/** The rect of a maximised window. */
export function maximizedRect(b: Size): Rect {
  return { x: GAP, y: GAP, w: Math.max(0, b.w - 2 * GAP), h: Math.max(0, b.h - 2 * GAP) };
}

/** Dragging a maximised window restores it under the pointer, keeping the grab point
 *  at the same relative spot across the title bar. `px` is the pointer's x in the
 *  workspace. */
export function restoreUnderPointer(restore: Rect, px: number, b: Size): Rect {
  const max = maximizedRect(b);
  const frac = max.w > 0 ? clamp((px - max.x) / max.w, 0, 1) : 0.5;
  return clampRect({ x: px - restore.w * frac, y: GAP, w: restore.w, h: restore.h }, b);
}

/** Pixels → fractions of the workspace (what is stored). */
export function toFrac(r: Rect, b: Size): Rect {
  if (b.w <= 0 || b.h <= 0) return { x: 0, y: 0, w: 1, h: 1 };
  return { x: r.x / b.w, y: r.y / b.h, w: r.w / b.w, h: r.h / b.h };
}

/** Fractions → pixels, clamped into the workspace (a stored layout from a larger
 *  window, or a hand-edited one, still lands fully visible). */
export function fromFrac(f: Rect, b: Size): Rect {
  return clampRect({ x: f.x * b.w, y: f.y * b.h, w: f.w * b.w, h: f.h * b.h }, b);
}

/** Is this a usable stored fraction rect? */
export function isFracRect(v: unknown): v is Rect {
  if (!v || typeof v !== "object") return false;
  const r = v as Record<string, unknown>;
  return ["x", "y", "w", "h"].every((k) => typeof r[k] === "number" && Number.isFinite(r[k] as number)) &&
    (r.w as number) > 0 &&
    (r.h as number) > 0;
}
