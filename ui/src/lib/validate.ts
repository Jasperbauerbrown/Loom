// Client-side mirror of `config.Field.coerce` (src/loom/config.py). The backend
// validates all-or-nothing anyway; this keeps obviously-bad values from ever being sent
// and gives the field an inline error instead of a toast.

import type { SettingsField } from "./types";

export type ValidationResult = { ok: true; value: unknown } | { ok: false; error: string };

export function validate(field: SettingsField, raw: unknown): ValidationResult {
  switch (field.type) {
    case "bool": {
      if (typeof raw === "boolean") return { ok: true, value: raw };
      if (typeof raw === "string" && ["true", "false", "1", "0"].includes(raw.toLowerCase()))
        return { ok: true, value: ["true", "1"].includes(raw.toLowerCase()) };
      return { ok: false, error: "expected true or false" };
    }
    case "int":
    case "float": {
      if (typeof raw === "boolean") return { ok: false, error: "expected a number" };
      if (raw === "" || raw === null || raw === undefined) return { ok: false, error: "expected a number" };
      const n = typeof raw === "number" ? raw : Number(String(raw).trim());
      if (!Number.isFinite(n)) return { ok: false, error: "expected a number" };
      if (field.type === "int" && !Number.isInteger(n)) return { ok: false, error: "expected a whole number" };
      if (field.min !== null && field.min !== undefined && n < field.min)
        return { ok: false, error: `must be at least ${field.min}` };
      if (field.max !== null && field.max !== undefined && n > field.max)
        return { ok: false, error: `must be at most ${field.max}` };
      return { ok: true, value: field.type === "int" ? Math.trunc(n) : n };
    }
    case "enum": {
      const value = String(raw ?? "");
      if (!field.choices.includes(value))
        return { ok: false, error: `must be one of ${field.choices.join(", ")}` };
      return { ok: true, value };
    }
    case "list": {
      if (Array.isArray(raw)) {
        if (!raw.every((x) => typeof x === "string")) return { ok: false, error: "expected a list of strings" };
        return { ok: true, value: raw };
      }
      if (typeof raw === "string") {
        const parts = raw
          .split(/[\n,]/)
          .map((s) => s.trim())
          .filter(Boolean);
        return { ok: true, value: parts };
      }
      return { ok: false, error: "expected a list of strings" };
    }
    case "str": {
      if (raw === null || raw === undefined) return { ok: true, value: "" };
      if (typeof raw !== "string") return { ok: false, error: "expected text" };
      if (field.ui === "time" && raw && !/^([01]\d|2[0-3]):[0-5]\d$/.test(raw))
        return { ok: false, error: "expected HH:MM" };
      return { ok: true, value: raw };
    }
    default:
      return { ok: false, error: `unknown field type ${field.type}` };
  }
}

/** The value a control should start from: the stored value, else the schema default. */
export function fieldValue(field: SettingsField, values: Record<string, unknown>): unknown {
  return field.key in values ? values[field.key] : field.default;
}

/** Text shown in a text-like control for a value of any field type. */
export function toInputValue(field: SettingsField, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (field.type === "list") return Array.isArray(value) ? (value as string[]).join("\n") : String(value);
  return String(value);
}
