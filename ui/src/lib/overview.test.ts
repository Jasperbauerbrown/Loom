import { describe, expect, it } from "vitest";
import { overviewIds } from "./overview";
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
