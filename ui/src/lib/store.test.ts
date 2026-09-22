import { describe, expect, it } from "vitest";
import { DRAFTS_KEY, initialState, loadDrafts, reduce, saveDrafts, type KeyValueStorage, type State } from "./store";
import type { BusEvent } from "./types";

const CID = "01J000000000000000000DESK";

function ev(type: string, data: unknown, conversation_id: string | null = CID): BusEvent {
  return { type, ts: "2026-09-20T12:00:00Z", conversation_id, data };
}

function feed(events: BusEvent[], from: State = initialState): State {
  return events.reduce(reduce, from);
}

describe("streaming assistant events", () => {
  it("assembles start/delta/end into one assistant event", () => {
    const state = feed([
      ev("message.start", { role: "assistant", step: 1 }),
      ev("message.delta", { channel: "reasoning", text: "let me think" }),
      ev("message.delta", { channel: "content", text: "Hello" }),
      ev("message.delta", { channel: "content", text: ", world" }),
      ev("message.end", { event_id: 7 }),
    ]);
    expect(state.streaming[CID]).toBeUndefined();
    const events = state.events[CID];
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ id: 7, kind: "assistant", content: "Hello, world" });
    expect(events[0].meta).toMatchObject({ reasoning: "let me think" });
  });

  it("buffers content and reasoning separately while streaming", () => {
    const state = feed([
      ev("message.start", { role: "assistant" }),
      ev("message.delta", { channel: "content", text: "a" }),
      ev("message.delta", { channel: "reasoning", text: "b" }),
    ]);
    expect(state.streaming[CID]).toMatchObject({ content: "a", reasoning: "b", active: true });
    expect(state.events[CID]).toBeUndefined();
  });

  it("prefers the persisted row carried by message.end", () => {
    const state = feed([
      ev("message.start", { role: "assistant" }),
      ev("message.delta", { channel: "content", text: "draft" }),
      ev("message.end", {
        event_id: 9,
        event: { id: 9, conversation_id: CID, kind: "assistant", content: "final", meta: {}, ts: "t" },
      }),
    ]);
    expect(state.events[CID][0]).toMatchObject({ id: 9, content: "final" });
  });

  it("keeps the narration before a tool call: the persisted tool_call row carries it as meta.text", () => {
    // What streamed as content lands on the tool_call row's meta.text (runner.py); the
    // transcript renders that above the tool rows, so the update does not vanish.
    const state = feed([
      ev("message.start", { role: "assistant" }),
      ev("message.delta", { channel: "content", text: "Two big jobs. Spinning up threads." }),
      ev("message.end", {
        event_id: 11,
        event: {
          id: 11,
          conversation_id: CID,
          kind: "tool_call",
          content: JSON.stringify([{ id: "c1", name: "conversation_create", arguments: { goal: "x" } }]),
          meta: { text: "Two big jobs. Spinning up threads.", step: 1 },
          ts: "t",
        },
      }),
    ]);
    expect(state.streaming[CID]).toBeUndefined();
    expect(state.events[CID][0]).toMatchObject({ id: 11, kind: "tool_call" });
    expect(state.events[CID][0].meta.text).toBe("Two big jobs. Spinning up threads.");
    expect(state.toolCalls["c1"]).toMatchObject({ name: "conversation_create" });
  });
});

describe("event.appended", () => {
  it("upserts persisted rows by id, in order", () => {
    const state = feed([
      ev("event.appended", { id: 2, conversation_id: CID, kind: "assistant", content: "two", meta: {}, ts: "t" }),
      ev("event.appended", { id: 1, conversation_id: CID, kind: "user", content: "one", meta: {}, ts: "t" }),
      ev("event.appended", { id: 1, conversation_id: CID, kind: "user", content: "one (edited)", meta: {}, ts: "t" }),
    ]);
    expect(state.events[CID].map((e) => e.id)).toEqual([1, 2]);
    expect(state.events[CID][0].content).toBe("one (edited)");
  });
});

describe("tool calls", () => {
  it("pairs tool.call with tool.result by call_id", () => {
    const state = feed([
      ev("tool.call", { call_id: "c1", name: "fs_read", args: { path: "a.md" }, policy: "allow", event_id: 3 }),
      ev("tool.result", { call_id: "c1", ok: true, preview: "contents", artifact_id: "art1", event_id: 4 }),
    ]);
    const call = state.toolCalls["c1"];
    expect(call).toMatchObject({ name: "fs_read", policy: "allow" });
    expect(call.result).toMatchObject({ ok: true, preview: "contents", artifact_id: "art1" });
    expect(state.events[CID].map((e) => e.kind)).toEqual(["tool_call", "tool_result"]);
  });

  it("indexes tool calls found in persisted events", () => {
    const state = feed([
      ev("event.appended", {
        id: 5,
        conversation_id: CID,
        kind: "tool_call",
        content: JSON.stringify([{ id: "c2", name: "shell", arguments: { cmd: "ls" } }]),
        meta: { policy: "classify", reason: "read-only listing" },
        ts: "t",
      }),
      ev("event.appended", {
        id: 6,
        conversation_id: CID,
        kind: "tool_result",
        content: "a.md",
        meta: { call_id: "c2", ok: true },
        ts: "t",
      }),
    ]);
    expect(state.toolCalls["c2"]).toMatchObject({ name: "shell", policy: "classify", reason: "read-only listing" });
    expect(state.toolCalls["c2"].result?.ok).toBe(true);
  });
});

describe("approvals, items, conversations", () => {
  const approval = {
    id: "ap1",
    conversation_id: CID,
    event_id: 3,
    call_id: "c1",
    tool: "shell",
    args: {},
    reason: "writes outside a root",
    options: {},
    status: "pending",
    decision: null,
    decided_at: null,
    created_at: "t",
  };

  it("adds on requested and removes on resolved", () => {
    const requested = reduce(initialState, ev("approval.requested", approval));
    expect(Object.keys(requested.approvals)).toEqual(["ap1"]);
    const resolved = reduce(requested, ev("approval.resolved", { ...approval, status: "allowed" }));
    expect(resolved.approvals).toEqual({});
  });

  it("replaces a conversation row on conversation.updated", () => {
    const created = reduce(
      initialState,
      ev("conversation.created", { id: CID, kind: "desk", status: "idle", title: "" }),
    );
    const updated = reduce(created, ev("conversation.updated", { id: CID, kind: "desk", status: "running", title: "x" }));
    expect(updated.conversations[CID]).toMatchObject({ status: "running", title: "x" });
    expect(Object.keys(updated.conversations)).toHaveLength(1);
  });

  it("counts unseen items", () => {
    const state = feed([
      ev("item.created", { id: "i1", kind: "notification", title: "a", seen_at: null, dismissed_at: null }),
      ev("item.created", { id: "i2", kind: "setup", title: "b", seen_at: null, dismissed_at: null }),
      ev("item.updated", { id: "i2", kind: "setup", title: "b", seen_at: "t", dismissed_at: null }),
    ]);
    expect(state.unseenCount).toBe(1);
  });
});

describe("system events", () => {
  it("settings.changed sets the refetch flag", () => {
    expect(reduce(initialState, ev("settings.changed", { keys: ["general.theme"] }, null)).settingsStale).toBe(true);
  });

  it("models.changed sets the models refetch flag", () => {
    expect(reduce(initialState, ev("models.changed", { kind: "preset", name: "fast" }, null)).modelsStale).toBe(true);
  });

  it("system.queue replaces the queue state", () => {
    const state = reduce(initialState, ev("system.queue", { depth: 2, running: ["x"] }, null));
    expect(state.queue).toEqual({ depth: 2, running: ["x"] });
  });

  it("promote.progress appends stages", () => {
    const state = feed([
      ev("promote.progress", { stage: "snapshot", ok: true, message: "" }, null),
      ev("promote.progress", { stage: "restart", ok: false, message: "boom" }, null),
    ]);
    expect(state.promote.map((p) => p.stage)).toEqual(["snapshot", "restart"]);
    expect(state.promote[1].ok).toBe(false);
  });

  it("maintenance.progress collects the pass, and `start` clears the previous one", () => {
    const first = feed([
      ev("maintenance.progress", { stage: "start", message: "maintenance pass (nightly)" }, null),
      ev("maintenance.progress", { stage: "consolidate", message: "thread: batch 1/2" }, null),
      ev("maintenance.progress", { stage: "done", message: "3 note writes" }, null),
    ]);
    expect(first.maintenanceProgress.map((p) => p.stage)).toEqual(["start", "consolidate", "done"]);
    expect(first.maintenanceStale).toBe(true);

    const second = reduce(first, ev("maintenance.progress", { stage: "start", message: "manual" }, null));
    expect(second.maintenanceProgress).toHaveLength(1);
    expect(second.maintenanceStale).toBe(false);
  });

  it("maintenance.progress flips `running` on a loaded status", () => {
    const loaded: State = {
      ...initialState,
      maintenance: {
        enabled: true,
        running: false,
        reason: null,
        idle_seconds: 900,
        idle_seconds_remaining: 120,
        next_nightly_at: "2026-09-21T03:00:00+00:00",
        last_report: null,
      },
    };
    const running = reduce(loaded, ev("maintenance.progress", { stage: "start", message: "manual" }, null));
    expect(running.maintenance?.running).toBe(true);
    const done = reduce(running, ev("maintenance.progress", { stage: "done", message: "nothing to do" }, null));
    expect(done.maintenance?.running).toBe(false);
  });

  it("schedule.created / updated / deleted keep the table live", () => {
    const row = {
      id: "s1",
      spec: "0 9 * * *",
      target: "desk",
      payload: "standup",
      policy: null,
      created_by: "user",
      enabled: 1,
      next_run_at: "2026-09-21T09:00:00Z",
      last_run_at: null,
      runs: 0,
    };
    const created = reduce(initialState, ev("schedule.created", row, null));
    expect(created.schedules).toHaveLength(1);

    const updated = reduce(created, ev("schedule.updated", { ...row, runs: 1, enabled: 0 }, null));
    expect(updated.schedules).toHaveLength(1);
    expect(updated.schedules[0]).toMatchObject({ runs: 1, enabled: 0 });

    const deleted = reduce(updated, ev("schedule.deleted", row, null));
    expect(deleted.schedules).toEqual([]);
    // a delete for a row we never had changes nothing
    expect(reduce(deleted, ev("schedule.deleted", row, null))).toBe(deleted);
  });

  it("promote.progress prefers the log line's own timestamp", () => {
    const state = reduce(
      initialState,
      ev("promote.progress", { stage: "probe", ok: true, message: "healthy", ts: "2026-09-20T11:00:00Z" }, null),
    );
    expect(state.promote[0].ts).toBe("2026-09-20T11:00:00Z");
  });

  it("notification raises a toast", () => {
    const state = reduce(initialState, ev("notification", { title: "Thread finished", body: "ok", item_id: "i9" }));
    expect(state.toasts).toHaveLength(1);
    expect(state.toasts[0]).toMatchObject({ kind: "notification", title: "Thread finished", item_id: "i9" });
  });

  it("knowledge.changed invalidates the vault tree", () => {
    const state = reduce({ ...initialState, vaultTree: [] }, ev("knowledge.changed", { paths: ["core/self.md"] }, null));
    expect(state.vaultTree).toBeNull();
    expect(state.knowledgeStale).toBe(1);
  });

  it("ignores unknown event types without error", () => {
    const state = reduce(initialState, ev("something.new", { whatever: true }));
    expect(state).toBe(initialState);
  });

  it("ignores malformed rows", () => {
    expect(reduce(initialState, ev("conversation.updated", null))).toBe(initialState);
    expect(reduce(initialState, ev("approval.requested", {}))).toBe(initialState);
    expect(reduce(initialState, ev("tool.call", { name: "x" }))).toBe(initialState);
  });
});

describe("queued messages", () => {
  const posted = (id: number, content: string, source = "user") =>
    ev("inbox.posted", { id, source, content, created_at: "2026-09-21T19:00:00Z", meta: {} });
  const delivered = (eventId: number, inboxId: number) =>
    ev("event.appended", { id: eventId, conversation_id: CID, kind: "user", content: "x", meta: { inbox_id: inboxId, source: "user" }, ts: "t" });

  it("adds a queued row on inbox.posted, sorted by id and deduped", () => {
    const state = feed([posted(12, "second"), posted(11, "first"), posted(12, "second again")]);
    expect(state.queued[CID].map((q) => [q.id, q.content])).toEqual([
      [11, "first"],
      [12, "second"],
    ]);
    expect(state.queued[CID][0]).toMatchObject({ conversation_id: CID, source: "user", created_at: "2026-09-21T19:00:00Z" });
  });

  it("removes the row when its user event lands in the transcript", () => {
    const state = feed([posted(11, "a"), posted(12, "b"), delivered(40, 11)]);
    expect(state.queued[CID].map((q) => q.id)).toEqual([12]);
    expect(state.events[CID].map((e) => e.id)).toEqual([40]);
  });

  it("ignores user events for other inbox rows and non-user events", () => {
    const before = feed([posted(11, "a")]);
    const other = reduce(before, delivered(41, 99));
    expect(other.queued).toBe(before.queued);
    const assistant = reduce(
      before,
      ev("event.appended", { id: 42, conversation_id: CID, kind: "assistant", content: "hi", meta: { inbox_id: 11 }, ts: "t" }),
    );
    expect(assistant.queued[CID].map((q) => q.id)).toEqual([11]);
  });

  it("ignores malformed inbox.posted payloads", () => {
    expect(reduce(initialState, ev("inbox.posted", { source: "user" }))).toBe(initialState);
    expect(reduce(initialState, ev("inbox.posted", { id: 3 }, null))).toBe(initialState);
  });
});

describe("drafts storage", () => {
  function memoryStorage(initial: Record<string, string> = {}): KeyValueStorage & { data: Record<string, string> } {
    const data = { ...initial };
    return {
      data,
      getItem: (k) => (k in data ? data[k] : null),
      setItem: (k, v) => {
        data[k] = v;
      },
    };
  }

  it("round-trips drafts through storage", () => {
    const storage = memoryStorage();
    saveDrafts({ a: "hello", b: "world" }, storage);
    expect(JSON.parse(storage.data[DRAFTS_KEY])).toEqual({ a: "hello", b: "world" });
    expect(loadDrafts(storage)).toEqual({ a: "hello", b: "world" });
  });

  it("treats missing, malformed or blocked storage as no drafts", () => {
    expect(loadDrafts(null)).toEqual({});
    expect(loadDrafts(memoryStorage({ [DRAFTS_KEY]: "not json" }))).toEqual({});
    expect(loadDrafts(memoryStorage({ [DRAFTS_KEY]: JSON.stringify([1, 2]) }))).toEqual({});
    expect(loadDrafts(memoryStorage({ [DRAFTS_KEY]: JSON.stringify({ a: 1, b: "", c: "keep" }) }))).toEqual({ c: "keep" });
    const throwing: KeyValueStorage = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(loadDrafts(throwing)).toEqual({});
    expect(() => saveDrafts({ a: "x" }, throwing)).not.toThrow();
  });
});

describe("tool result images", () => {
  const img = { id: "img_01ARZ3NDEKTSV4RRFFQ69G5FAV", mime: "image/png" };

  it("carries images from tool.result into the event meta and the indexed result", () => {
    const state = feed([
      ev("tool.call", { call_id: "c1", name: "screenshot", args: {}, event_id: 10 }),
      ev("tool.result", { call_id: "c1", name: "screenshot", ok: true, preview: "1440x900 png", event_id: 11, images: [img] }),
    ]);
    expect(state.toolCalls.c1.result?.images).toEqual([img]);
    const result = state.events[CID].find((e) => e.kind === "tool_result");
    expect(result?.meta.images).toEqual([img]);
  });

  it("reads images off persisted tool_result rows so history matches live", () => {
    const state = feed([
      ev("event.appended", { id: 11, conversation_id: CID, kind: "tool_result", content: "x", meta: { call_id: "c1", ok: true, images: [img] }, ts: "t" }),
    ]);
    expect(state.toolCalls.c1.result?.images).toEqual([img]);
    const none = feed([
      ev("event.appended", { id: 12, conversation_id: CID, kind: "tool_result", content: "x", meta: { call_id: "c2", ok: true }, ts: "t" }),
    ]);
    expect(none.toolCalls.c2.result?.images).toBeUndefined();
  });
});

describe("background processes", () => {
  const row = {
    id: "01P1",
    conversation_id: CID,
    title: "dev server",
    command: "npm run dev",
    cwd: "C:/x",
    pid: 4242,
    status: "running",
    exit_code: null,
    error: null,
    log_path: "C:/x/proc_01P1.log",
    started_at: "2026-09-22T10:00:00Z",
    ended_at: null,
  };

  it("upserts rows by id across the four events", () => {
    let state = feed([ev("process.started", row)]);
    expect(state.processes["01P1"]).toMatchObject({ status: "running", pid: 4242 });
    state = feed([ev("process.exited", { ...row, status: "exited", exit_code: 0, ended_at: "2026-09-22T10:05:00Z" })], state);
    expect(state.processes["01P1"]).toMatchObject({ status: "exited", exit_code: 0, title: "dev server" });
    expect(Object.keys(state.processes)).toHaveLength(1);
  });

  it("keeps fields an event leaves out", () => {
    let state = feed([ev("process.started", row)]);
    state = feed([ev("process.stopped", { id: "01P1", status: "stopped", conversation_id: CID })], state);
    expect(state.processes["01P1"]).toMatchObject({ status: "stopped", command: "npm run dev", pid: 4242 });
  });

  it("ignores a process event without an id", () => {
    const state = feed([ev("process.failed", { status: "failed" })]);
    expect(state).toBe(initialState);
  });
});
