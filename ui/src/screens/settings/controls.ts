// Pure mapping from a settings field to the control kind that renders it.
// Kept separate from the component so it can be unit-tested without touching the DOM.

import type { SettingsField } from "../../lib/types";

export type ControlKind = "text" | "number" | "toggle" | "select" | "list" | "path" | "time" | "secret";

/** One control per field. `field.ui` is already authoritative; this just gives it a name
 * and a safe fallback for any value the backend might one day add. */
export function controlKind(field: SettingsField): ControlKind {
  switch (field.ui) {
    case "text":
    case "number":
    case "toggle":
    case "select":
    case "list":
    case "path":
    case "time":
    case "secret":
      return field.ui;
    default:
      return "text";
  }
}
