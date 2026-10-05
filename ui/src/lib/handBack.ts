// Maintenance hand-backs (03 §8.6): a `knowledge` item whose `action` carries the exact
// tool call a pass could not make without approval. Apply runs it; the outcome comes
// back on `action.result`.

import type { Item } from "./types";

export interface ApplyResult {
  state: "applying" | "ok" | "error";
  content: string;
  at?: string;
}

/** True when the item carries a tool call the Apply button can run. */
export function canApply(item: Item): boolean {
  const a = item.action ?? {};
  return item.kind === "knowledge" && typeof a.kind === "string" && !!a.args && typeof a.args === "object";
}

/** The recorded outcome of the last Apply, if any. */
export function applyResult(item: Item): ApplyResult | null {
  const r = (item.action ?? {}).result as Partial<ApplyResult> | undefined;
  if (!r || typeof r !== "object" || !r.state) return null;
  return { state: r.state, content: String(r.content ?? ""), at: r.at };
}
