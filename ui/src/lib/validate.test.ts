import { describe, expect, it } from "vitest";
import { validate } from "./validate";
import { controlKind } from "../screens/settings/controls";
import type { SettingsField } from "./types";

function field(overrides: Partial<SettingsField>): SettingsField {
  return {
    key: "group.field",
    group: "group",
    type: "str",
    default: "",
    label: "Field",
    help: "",
    ui: "text",
    min: null,
    max: null,
    choices: [],
    restart: false,
    ...overrides,
  };
}

describe("validate", () => {
  it("accepts a number in range", () => {
    const f = field({ type: "int", ui: "number", min: 1, max: 10 });
    expect(validate(f, 5)).toEqual({ ok: true, value: 5 });
    expect(validate(f, "7")).toEqual({ ok: true, value: 7 });
  });

  it("rejects an out-of-range number", () => {
    const f = field({ type: "int", ui: "number", min: 1, max: 10 });
    expect(validate(f, 11).ok).toBe(false);
    expect(validate(f, 0).ok).toBe(false);
  });

  it("rejects a non-integer for an int field", () => {
    const f = field({ type: "int", ui: "number", min: 0, max: 10 });
    expect(validate(f, 3.5).ok).toBe(false);
  });

  it("accepts a float in range", () => {
    const f = field({ type: "float", ui: "number", min: 0, max: 1 });
    expect(validate(f, 0.55)).toEqual({ ok: true, value: 0.55 });
  });

  it("accepts a valid enum choice and rejects a bad one", () => {
    const f = field({ type: "enum", ui: "select", choices: ["a", "b", "c"] });
    expect(validate(f, "b")).toEqual({ ok: true, value: "b" });
    expect(validate(f, "z").ok).toBe(false);
  });

  it("accepts HH:MM for a time field and rejects a bad format", () => {
    const f = field({ type: "str", ui: "time" });
    expect(validate(f, "03:00")).toEqual({ ok: true, value: "03:00" });
    expect(validate(f, "3:00").ok).toBe(false);
    expect(validate(f, "25:00").ok).toBe(false);
  });

  it("accepts a bool and coerces string variants", () => {
    const f = field({ type: "bool", ui: "toggle" });
    expect(validate(f, true)).toEqual({ ok: true, value: true });
    expect(validate(f, "false")).toEqual({ ok: true, value: false });
    expect(validate(f, "nope").ok).toBe(false);
  });

  it("parses a list field from newline/comma text", () => {
    const f = field({ type: "list", ui: "list" });
    expect(validate(f, "a\nb, c")).toEqual({ ok: true, value: ["a", "b", "c"] });
  });

  it("rejects an empty/blank number", () => {
    const f = field({ type: "int", ui: "number" });
    expect(validate(f, "").ok).toBe(false);
  });
});

describe("controlKind", () => {
  const fixtures: { field: SettingsField; kind: string }[] = [
    { field: field({ key: "general.display_name", type: "str", ui: "text" }), kind: "text" },
    { field: field({ key: "conversation.step_budget_desk", type: "int", ui: "number" }), kind: "number" },
    { field: field({ key: "knowledge.maintenance_enabled", type: "bool", ui: "toggle" }), kind: "toggle" },
    { field: field({ key: "general.theme", type: "enum", ui: "select", choices: ["system", "light", "dark"] }), kind: "select" },
    { field: field({ key: "triggers.notification_targets", type: "list", ui: "list" }), kind: "list" },
    { field: field({ key: "example.folder", type: "str", ui: "path" }), kind: "path" },
    { field: field({ key: "knowledge.maintenance_nightly_at", type: "str", ui: "time" }), kind: "time" },
    { field: field({ key: "general.lan_token", type: "str", ui: "secret" }), kind: "secret" },
  ];

  it("maps each fixture field to exactly one control kind", () => {
    for (const { field: f, kind } of fixtures) {
      expect(controlKind(f)).toBe(kind);
    }
  });
});
