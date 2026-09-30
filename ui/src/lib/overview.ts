// The Overview workspace's window state (03 §13.1), pure so it can be unit-tested
// without mounting anything: which conversations have windows, their stacking order,
// minimised / maximised / closed flags, stored geometry, and unread tracking.
//
// Which windows show: the desk first, then active threads (running, waiting_approval,
// waiting_input) newest `updated_at` first — these appear on their own — then any
// conversation the user explicitly opened (a finished thread, say), newest first.
// A closed window stays closed until reopened, even if its thread becomes active again.
// Archived conversations never have a window.

import type { Conversation, ConversationStatus, ConvEvent } from "./types";
import { cascade, fromFrac, isFracRect, placeNew, tile, toFrac, type Rect, type Size } from "./overviewLayout";

const ACTIVE_STATUSES = new Set(["running", "waiting_approval", "waiting_input"]);
const FINISHED_STATUSES = new Set(["done", "failed"]);

function byUpdatedDesc(a: Conversation, b: Conversation): number {
  if (a.updated_at === b.updated_at) return 0;
  return a.updated_at < b.updated_at ? 1 : -1;
}

/** Ordered ids of the conversations that appear by themselves: the desk, then active
 *  threads; with `showFinished`, done/failed threads after them. */
export function overviewIds(conversations: Conversation[], showFinished: boolean): string[] {
  const desk = conversations.find((c) => c.kind === "desk");
  const active = conversations.filter((c) => c.kind === "thread" && ACTIVE_STATUSES.has(c.status)).sort(byUpdatedDesc);
  const finished = showFinished
    ? conversations.filter((c) => c.kind === "thread" && FINISHED_STATUSES.has(c.status)).sort(byUpdatedDesc)
    : [];

  const ids: string[] = [];
  if (desk) ids.push(desk.id);
  for (const c of active) ids.push(c.id);
  for (const c of finished) ids.push(c.id);
  return ids;
}

// --- window state -------------------------------------------------------------

export interface OverviewState {
  /** stored geometry per conversation, as fractions of the workspace */
  geom: Record<string, Rect>;
  /** stacking order, bottom first: the last shown, non-minimised id has focus */
  order: string[];
  minimized: string[];
  maximized: string[];
  /** closed by the user: no window until reopened */
  closed: string[];
  /** opened explicitly: keeps a window even when not active (finished threads) */
  added: string[];
}

export const EMPTY_STATE: OverviewState = { geom: {}, order: [], minimized: [], maximized: [], closed: [], added: [] };

function stringList(v: unknown): string[] {
  return Array.isArray(v) ? Array.from(new Set(v.filter((x): x is string => typeof x === "string"))) : [];
}

/** A stored state, validated field by field: anything malformed is dropped, never an
 *  error (it is a per-browser convenience). */
export function parseState(raw: unknown): OverviewState {
  if (!raw || typeof raw !== "object") return EMPTY_STATE;
  const r = raw as Record<string, unknown>;
  const geom: Record<string, Rect> = {};
  if (r.geom && typeof r.geom === "object") {
    for (const [id, g] of Object.entries(r.geom as Record<string, unknown>)) {
      if (isFracRect(g)) geom[id] = { x: g.x, y: g.y, w: g.w, h: g.h };
    }
  }
  return {
    geom,
    order: stringList(r.order),
    minimized: stringList(r.minimized),
    maximized: stringList(r.maximized),
    closed: stringList(r.closed),
    added: stringList(r.added),
  };
}

/** The windows to render (in overview order) and the auto windows the user closed. */
export function windowIds(conversations: Conversation[], st: OverviewState): { shown: string[]; hidden: string[] } {
  const auto = overviewIds(conversations, false);
  const autoSet = new Set(auto);
  const closed = new Set(st.closed);
  const addedSet = new Set(st.added);
  const added = conversations
    .filter((c) => addedSet.has(c.id) && !autoSet.has(c.id) && c.status !== "archived")
    .sort(byUpdatedDesc)
    .map((c) => c.id);
  return {
    shown: [...auto, ...added].filter((id) => !closed.has(id)),
    hidden: auto.filter((id) => closed.has(id)),
  };
}

const without = (list: string[], id: string) => (list.includes(id) ? list.filter((x) => x !== id) : list);
const withAdded = (list: string[], id: string) => (list.includes(id) ? list : [...list, id]);
const raised = (order: string[], id: string) =>
  order[order.length - 1] === id ? order : [...order.filter((x) => x !== id), id];

/** Bring a window to the front (restoring it if minimised). */
export function focusWindow(st: OverviewState, id: string): OverviewState {
  const order = raised(st.order, id);
  const minimized = without(st.minimized, id);
  if (order === st.order && minimized === st.minimized) return st;
  return { ...st, order, minimized };
}

/** Open a window for any conversation: un-close it, pin it, bring it to the front. */
export function openWindow(st: OverviewState, id: string): OverviewState {
  return { ...focusWindow(st, id), closed: without(st.closed, id), added: withAdded(st.added, id) };
}

/** Close a window; its geometry is kept so reopening puts it back where it was. */
export function closeWindow(st: OverviewState, id: string): OverviewState {
  return {
    ...st,
    order: without(st.order, id),
    minimized: without(st.minimized, id),
    maximized: without(st.maximized, id),
    added: without(st.added, id),
    closed: withAdded(st.closed, id),
  };
}

export function minimizeWindow(st: OverviewState, id: string): OverviewState {
  if (st.minimized.includes(id)) return st;
  return { ...st, minimized: [...st.minimized, id] };
}

export function toggleMaximize(st: OverviewState, id: string): OverviewState {
  const on = st.maximized.includes(id);
  const next = focusWindow(st, id);
  return { ...next, maximized: on ? without(st.maximized, id) : [...st.maximized, id] };
}

/** Commit a move or resize: new geometry, and the window is no longer maximised. */
export function setGeometry(st: OverviewState, id: string, frac: Rect): OverviewState {
  return { ...st, geom: { ...st.geom, [id]: frac }, maximized: without(st.maximized, id) };
}

/** Apply an arrangement (tile, cascade): every listed window gets its rect and is
 *  restored from minimised or maximised. */
export function arrange(st: OverviewState, rects: Record<string, Rect>): OverviewState {
  const ids = new Set(Object.keys(rects));
  return {
    ...st,
    geom: { ...st.geom, ...rects },
    minimized: st.minimized.filter((x) => !ids.has(x)),
    maximized: st.maximized.filter((x) => !ids.has(x)),
  };
}

/** Give every shown window without stored geometry a place: when none has any (a
 *  first visit, or after Reset) tile them all; otherwise each new one goes into the
 *  first free spot beside the visible windows. */
export function placeMissing(st: OverviewState, shown: string[], b: Size): OverviewState {
  const missing = shown.filter((id) => !st.geom[id]);
  if (missing.length === 0 || b.w <= 0 || b.h <= 0) return st;
  const geom = { ...st.geom };
  if (missing.length === shown.length) {
    tile(shown.length, b).forEach((r, i) => (geom[shown[i]] = toFrac(r, b)));
    return { ...st, geom };
  }
  const minimized = new Set(st.minimized);
  const placed = shown.filter((id) => st.geom[id] && !minimized.has(id)).map((id) => fromFrac(st.geom[id], b));
  for (const id of missing) {
    const r = placeNew(placed, b);
    placed.push(r);
    geom[id] = toFrac(r, b);
  }
  return { ...st, geom };
}

/** Tile the given windows (overview order) into `columns`, or an automatic grid. */
export function tileWindows(st: OverviewState, ids: string[], b: Size, columns?: number): OverviewState {
  const rects = tile(ids.length, b, columns);
  return arrange(st, Object.fromEntries(ids.map((id, i) => [id, toFrac(rects[i], b)])));
}

/** Cascade the given windows, stacking them in that order (the last on top). */
export function cascadeWindows(st: OverviewState, ids: string[], b: Size): OverviewState {
  const rects = cascade(ids.length, b);
  const next = arrange(st, Object.fromEntries(ids.map((id, i) => [id, toFrac(rects[i], b)])));
  const set = new Set(ids);
  return { ...next, order: [...next.order.filter((id) => !set.has(id)), ...ids] };
}

/** Forget ids whose conversation is gone or archived. Only meaningful once the
 *  conversation list has loaded (it always holds the desk), else it is a no-op. */
export function prune(st: OverviewState, conversations: Conversation[]): OverviewState {
  if (!conversations.some((c) => c.kind === "desk")) return st;
  const live = new Set(conversations.filter((c) => c.status !== "archived").map((c) => c.id));
  const keep = (list: string[]) => {
    const next = list.filter((id) => live.has(id));
    return next.length === list.length ? list : next;
  };
  const geomIds = Object.keys(st.geom);
  const geomLive = geomIds.filter((id) => live.has(id));
  const next: OverviewState = {
    geom: geomLive.length === geomIds.length ? st.geom : Object.fromEntries(geomLive.map((id) => [id, st.geom[id]])),
    order: keep(st.order),
    minimized: keep(st.minimized),
    maximized: keep(st.maximized),
    closed: keep(st.closed),
    added: keep(st.added),
  };
  const same = (Object.keys(next) as (keyof OverviewState)[]).every((k) => next[k] === st[k]);
  return same ? st : next;
}

/** Stacking position of each shown window (0 = bottom); ids never raised go below
 *  the rest, in overview order. */
export function stacking(shown: string[], order: string[]): Record<string, number> {
  const shownSet = new Set(shown);
  const inOrder = order.filter((id) => shownSet.has(id));
  const orderSet = new Set(inOrder);
  const all = [...shown.filter((id) => !orderSet.has(id)), ...inOrder];
  return Object.fromEntries(all.map((id, i) => [id, i]));
}

/** The focused window: the topmost shown one that is not minimised. */
export function focusedId(shown: string[], st: OverviewState): string | null {
  const z = stacking(shown, st.order);
  const minimized = new Set(st.minimized);
  let best: string | null = null;
  for (const id of shown) {
    if (minimized.has(id)) continue;
    if (best === null || z[id] > z[best]) best = id;
  }
  return best;
}

/** The next (or previous) window in overview order for keyboard focus cycling. */
export function cycle(shown: string[], current: string | null, dir: 1 | -1): string | null {
  if (shown.length === 0) return null;
  const i = current ? shown.indexOf(current) : -1;
  if (i < 0) return dir === 1 ? shown[0] : shown[shown.length - 1];
  return shown[(i + dir + shown.length) % shown.length];
}

// --- unread -------------------------------------------------------------------

/** The newest event id per conversation, for the ids asked about. */
export function lastEventIds(events: Record<string, ConvEvent[] | undefined>, ids: string[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const id of ids) {
    const list = events[id];
    if (list && list.length) out[id] = list[list.length - 1].id;
  }
  return out;
}

/** Advance what has been seen: a conversation first observed counts as read (opening
 *  the Overview is not news), and the focused window is read up to its newest event.
 *  Returns `seen` itself when nothing changed. */
export function nextSeen(
  seen: Record<string, number>,
  last: Record<string, number>,
  focused: string | null,
): Record<string, number> {
  let out = seen;
  for (const [id, n] of Object.entries(last)) {
    if (seen[id] === undefined || (id === focused && seen[id] !== n)) {
      if (out === seen) out = { ...seen };
      out[id] = n;
    }
  }
  return out;
}

/** Windows with events newer than the reader last saw them. */
export function unreadIds(seen: Record<string, number>, last: Record<string, number>, focused: string | null): Set<string> {
  const out = new Set<string>();
  for (const [id, n] of Object.entries(last)) {
    if (id !== focused && seen[id] !== undefined && n > seen[id]) out.add(id);
  }
  return out;
}

/** The attention cue a window wears: amber when it waits on the user, sky while it
 *  runs, none otherwise. */
export function attention(status: ConversationStatus | undefined, pendingApprovals: number): "amber" | "sky" | null {
  if (pendingApprovals > 0 || status === "waiting_approval" || status === "waiting_input") return "amber";
  if (status === "running") return "sky";
  return null;
}
