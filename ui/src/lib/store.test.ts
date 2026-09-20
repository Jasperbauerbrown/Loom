import { describe, expect, it } from "vitest";
import { initialState, reduce, type State } from "./store";
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
