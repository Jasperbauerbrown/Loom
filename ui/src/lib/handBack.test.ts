import { describe, expect, it } from "vitest";
import { applyResult, canApply } from "./handBack";
import type { Item } from "./types";

function item(kind: string, action: Record<string, unknown>): Item {
  return { id: "i", kind, title: "t", body: "", conversation_id: null, source: "maintenance", action, created_at: "", seen_at: null, dismissed_at: null };
}

describe("hand-back apply", () => {
  it("offers Apply only on knowledge items that carry a call", () => {
    expect(canApply(item("knowledge", { kind: "knowledge_write", args: { path: "core/a.md" } }))).toBe(true);
    expect(canApply(item("knowledge", {}))).toBe(false);
    expect(canApply(item("notification", { kind: "open_conversation", conversation_id: "c" }))).toBe(false);
  });

  it("reads the recorded outcome", () => {
    expect(applyResult(item("knowledge", { kind: "knowledge_write", args: {} }))).toBeNull();
    const r = applyResult(item("knowledge", { kind: "knowledge_write", args: {}, result: { state: "error", content: "old text not found" } }));
    expect(r).toEqual({ state: "error", content: "old text not found", at: undefined });
  });
});
