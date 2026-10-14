// Preset helpers shared by the Models settings screen.

/** The default name for a preset duplicated from `source`: `<source>-copy`, then
 *  `<source>-copy-2`, `-copy-3`… until one is not taken. Pure; the server still
 *  refuses a clash with 409 if the person types a taken name. */
export function copyName(source: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  const base = `${source}-copy`;
  if (!used.has(base)) return base;
  for (let n = 2; ; n++) {
    const candidate = `${base}-${n}`;
    if (!used.has(candidate)) return candidate;
  }
}

export type ReasoningMode = "" | "off" | "on" | "low" | "medium" | "high" | "budget";

/** Every value the model client understands (`models/client.py`): unset (the provider's
 *  own default; nothing is sent), off, on, an effort level, or `budget:<tokens>`. Any
 *  other string is read the way the client reads it — as medium effort — so the dialog
 *  never shows one thing while the backend does another. */
export function parseReasoning(v: unknown): { mode: ReasoningMode; budget: string } {
  if (typeof v !== "string" || !v) return { mode: "", budget: "" };
  if (v === "off" || v === "on" || v === "low" || v === "medium" || v === "high") return { mode: v, budget: "" };
  const m = /^budget:(\d+)$/.exec(v);
  if (m) return { mode: "budget", budget: m[1] };
  return { mode: "medium", budget: "" };
}

/** The inverse: what is saved. Unset saves nothing, so the provider default applies. */
export function formatReasoning(mode: ReasoningMode, budget: string): string | undefined {
  if (mode === "") return undefined;
  return mode === "budget" ? `budget:${budget || "0"}` : mode;
}
