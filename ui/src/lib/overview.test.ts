import { describe, expect, it } from "vitest";
import {
  EMPTY_STATE,
  attention,
  cascadeWindows,
  closeWindow,
  cycle,
  focusWindow,
  focusedId,
  lastEventIds,
  minimizeWindow,
  nextSeen,
  openWindow,
  overviewIds,
  parseState,
  placeMissing,
  prune,
  setGeometry,
  stacking,
  tileWindows,
  toggleMaximize,
  unreadIds,
  windowIds,
  type OverviewState,
} from "./overview";
import { fromFrac, overlaps } from "./overviewLayout";
import type { Conversation, ConversationStatus } from "./types";

function conv(id: string, kind: "desk" | "thread", status: ConversationStatus, updated_at: string): Conversation {
  return {
    id,
    kind,
    parent_id: null,
    title: "",
    goal: "",
    pinned_notes: [],
    roots: [],
    depth: 0,
    role: "",
    preset: null,
    status,
    step_budget: null,
    summary: "",
    summary_cutoff: 0,
    token_factor: 1,
    maint_watermark: 0,
    created_at: updated_at,
    updated_at,
    finished_at: null,
    archived_at: null,
    walkthrough: null,
    worktree: null,
  };
}

describe("overviewIds", () => {
  it("puts the desk first", () => {
    const desk = conv("desk", "desk", "idle", "2026-01-01T00:00:00Z");
    const thread = conv("t1", "thread", "running", "2026-01-02T00:00:00Z");
    expect(overviewIds([thread, desk], false)).toEqual(["desk", "t1"]);
  });

  it("orders active threads by updated_at, newest first", () => {
    const desk = conv("desk", "desk", "idle", "2026-01-01T00:00:00Z");
    const older = conv("older", "thread", "running", "2026-01-02T00:00:00Z");
    const newer = conv("newer", "thread", "waiting_approval", "2026-01-03T00:00:00Z");
    const oldest = conv("oldest", "thread", "waiting_input", "2026-01-01T12:00:00Z");
    expect(overviewIds([desk, older, newer, oldest], false)).toEqual(["desk", "newer", "older", "oldest"]);
  });

  it("leaves out finished threads unless asked", () => {
    const desk = conv("desk", "desk", "idle", "2026-01-01T00:00:00Z");
    const active = conv("active", "thread", "running", "2026-01-02T00:00:00Z");
    const done = conv("done", "thread", "done", "2026-01-03T00:00:00Z");
    const failed = conv("failed", "thread", "failed", "2026-01-04T00:00:00Z");
    expect(overviewIds([desk, active, done, failed], false)).toEqual(["desk", "active"]);
    expect(overviewIds([desk, active, done, failed], true)).toEqual(["desk", "active", "failed", "done"]);
  });

  it("never includes archived threads, even when finished ones are asked for", () => {
    const desk = conv("desk", "desk", "idle", "2026-01-01T00:00:00Z");
    const archived = conv("archived", "thread", "archived", "2026-01-05T00:00:00Z");
    expect(overviewIds([desk, archived], false)).toEqual(["desk"]);
    expect(overviewIds([desk, archived], true)).toEqual(["desk"]);
  });
});

const B = { w: 1200, h: 800 };
const desk = conv("desk", "desk", "idle", "2026-01-01T00:00:00Z");
const run = conv("run", "thread", "running", "2026-01-03T00:00:00Z");
const wait = conv("wait", "thread", "waiting_input", "2026-01-02T00:00:00Z");
const done = conv("done", "thread", "done", "2026-01-04T00:00:00Z");
const arch = conv("arch", "thread", "archived", "2026-01-05T00:00:00Z");
const ALL = [desk, run, wait, done, arch];

function st(over: Partial<OverviewState> = {}): OverviewState {
  return { ...EMPTY_STATE, ...over };
}

describe("windowIds", () => {
  it("shows the desk and active threads by themselves", () => {
    expect(windowIds(ALL, st())).toEqual({ shown: ["desk", "run", "wait"], hidden: [] });
  });
  it("adds explicitly opened conversations after them, never archived ones", () => {
    expect(windowIds(ALL, st({ added: ["done", "arch"] })).shown).toEqual(["desk", "run", "wait", "done"]);
  });
  it("keeps closed windows closed and counts the auto ones as hidden", () => {
    const s = closeWindow(st(), "run");
    expect(windowIds(ALL, s)).toEqual({ shown: ["desk", "wait"], hidden: ["run"] });
  });
  it("an explicitly added window survives its thread finishing", () => {
    const s = openWindow(st(), "run");
    const finished = { ...run, status: "done" as const };
    expect(windowIds([desk, finished], s).shown).toEqual(["desk", "run"]);
    expect(windowIds([desk, finished], st()).shown).toEqual(["desk"]);
  });
});

describe("window actions", () => {
  it("open un-closes, pins and raises", () => {
    const s = openWindow(closeWindow(st({ order: ["run", "desk"] }), "run"), "run");
    expect(s.closed).toEqual([]);
    expect(s.added).toEqual(["run"]);
    expect(s.order[s.order.length - 1]).toBe("run");
  });
  it("close keeps the geometry for a later reopen", () => {
    const g = { x: 0.1, y: 0.1, w: 0.3, h: 0.5 };
    const s = closeWindow(st({ geom: { run: g }, added: ["run"], minimized: ["run"] }), "run");
    expect(s.geom.run).toEqual(g);
    expect(s.added).toEqual([]);
    expect(s.minimized).toEqual([]);
  });
  it("focus raises and restores a minimised window; focusing the top window is a no-op", () => {
    const s = minimizeWindow(st({ order: ["desk", "run"] }), "desk");
    const f = focusWindow(s, "desk");
    expect(f.order).toEqual(["run", "desk"]);
    expect(f.minimized).toEqual([]);
    expect(focusWindow(f, "desk")).toBe(f);
  });
  it("maximise toggles and moving a window un-maximises it", () => {
    const m = toggleMaximize(st(), "run");
    expect(m.maximized).toEqual(["run"]);
    expect(toggleMaximize(m, "run").maximized).toEqual([]);
    expect(setGeometry(m, "run", { x: 0, y: 0, w: 0.5, h: 0.5 }).maximized).toEqual([]);
  });
});

describe("stacking and focus", () => {
  it("never-raised windows sit below raised ones", () => {
    expect(stacking(["desk", "run", "wait"], ["desk"])).toEqual({ run: 0, wait: 1, desk: 2 });
  });
  it("focus is the topmost non-minimised shown window", () => {
    const s = st({ order: ["run", "desk"], minimized: ["desk"] });
    expect(focusedId(["desk", "run", "wait"], s)).toBe("run");
    expect(focusedId([], s)).toBe(null);
  });
  it("cycle wraps both ways", () => {
    const ids = ["a", "b", "c"];
    expect(cycle(ids, "c", 1)).toBe("a");
    expect(cycle(ids, "a", -1)).toBe("c");
    expect(cycle(ids, null, 1)).toBe("a");
    expect(cycle([], null, 1)).toBe(null);
  });
});

describe("placement", () => {
  it("tiles everything on a first visit", () => {
    const s = placeMissing(st(), ["desk", "run", "wait"], B);
    const rects = ["desk", "run", "wait"].map((id) => fromFrac(s.geom[id], B));
    expect(overlaps(rects[0], rects[1]) || overlaps(rects[1], rects[2]) || overlaps(rects[0], rects[2])).toBe(false);
  });
  it("places a new window into free space without moving the others", () => {
    const first = placeMissing(st(), ["desk"], B);
    const deskGeom = { ...first.geom.desk, w: 0.4 };
    const s = placeMissing(st({ geom: { desk: deskGeom } }), ["desk", "run"], B);
    expect(s.geom.desk).toEqual(deskGeom);
    expect(overlaps(fromFrac(s.geom.desk, B), fromFrac(s.geom.run, B))).toBe(false);
  });
  it("is a no-op when every window has a place, or before the workspace is measured", () => {
    const s = placeMissing(st(), ["desk"], B);
    expect(placeMissing(s, ["desk"], B)).toBe(s);
    expect(placeMissing(st(), ["desk"], { w: 0, h: 0 })).toEqual(st());
  });
  it("tile and cascade restore minimised and maximised windows", () => {
    const s = st({ minimized: ["desk"], maximized: ["run"] });
    const t = tileWindows(s, ["desk", "run"], B, 2);
    expect(t.minimized).toEqual([]);
    expect(t.maximized).toEqual([]);
    const c = cascadeWindows(s, ["desk", "run"], B);
    expect(c.order).toEqual(["desk", "run"]);
    expect(c.geom.run.x).toBeGreaterThan(c.geom.desk.x);
  });
});

describe("persistence", () => {
  it("parses a stored state, dropping malformed parts", () => {
    const s = parseState({
      geom: { a: { x: 0, y: 0, w: 0.5, h: 0.5 }, bad: { x: 0 } },
      order: ["a", 3, "a"],
      closed: "nope",
    });
    expect(Object.keys(s.geom)).toEqual(["a"]);
    expect(s.order).toEqual(["a"]);
    expect(s.closed).toEqual([]);
    expect(parseState(null)).toEqual(EMPTY_STATE);
  });
  it("prunes gone and archived conversations once the list has loaded", () => {
    const s = st({ geom: { arch: { x: 0, y: 0, w: 1, h: 1 }, run: { x: 0, y: 0, w: 1, h: 1 } }, closed: ["gone", "run"] });
    const p = prune(s, ALL);
    expect(Object.keys(p.geom)).toEqual(["run"]);
    expect(p.closed).toEqual(["run"]);
    expect(prune(s, [])).toBe(s);
    expect(prune(p, ALL)).toBe(p);
  });
});

describe("unread", () => {
  const ev = (id: number) => ({ id, conversation_id: "x", kind: "user" as const, content: "", meta: {}, ts: "" });
  it("takes the newest event id per conversation", () => {
    expect(lastEventIds({ a: [ev(1), ev(5)], b: [] }, ["a", "b", "c"])).toEqual({ a: 5 });
  });
  it("a first sighting is read; later events are unread until focused", () => {
    let seen = nextSeen({}, { a: 5, b: 3 }, null);
    expect(seen).toEqual({ a: 5, b: 3 });
    const last = { a: 7, b: 3 };
    expect([...unreadIds(seen, last, null)]).toEqual(["a"]);
    expect([...unreadIds(seen, last, "a")]).toEqual([]);
    seen = nextSeen(seen, last, "a");
    expect(seen.a).toBe(7);
    expect(nextSeen(seen, last, "a")).toBe(seen);
  });
});

describe("attention", () => {
  it("amber when waiting on the user, sky while running", () => {
    expect(attention("waiting_approval", 0)).toBe("amber");
    expect(attention("waiting_input", 0)).toBe("amber");
    expect(attention("running", 1)).toBe("amber");
    expect(attention("running", 0)).toBe("sky");
    expect(attention("idle", 0)).toBe(null);
  });
});
