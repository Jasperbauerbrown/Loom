// Small formatting helpers. No dependencies; `Intl` does the locale work.

export function cls(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = Date.parse(iso.endsWith("Z") || /[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(then)) return iso;
  const seconds = Math.round((Date.now() - then) / 1000);
  const abs = Math.abs(seconds);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (abs < 60) return rtf.format(-seconds, "second");
  if (abs < 3600) return rtf.format(-Math.round(seconds / 60), "minute");
  if (abs < 86400) return rtf.format(-Math.round(seconds / 3600), "hour");
  if (abs < 2592000) return rtf.format(-Math.round(seconds / 86400), "day");
  return new Date(then).toLocaleDateString();
}

export function localTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = Date.parse(iso.endsWith("Z") || /[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(then)) return iso;
  return new Date(then).toLocaleString();
}

export function bytes(n: number | undefined | null): string {
  if (!n && n !== 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = n;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

/** Compact token/count formatting: 410, 12.3k, 1.2M (a whole number drops the ".0",
 *  so 1000 reads "1k" not "1.0k"). Used for token totals, where the exact number goes
 *  in a tooltip instead. */
export function compactNumber(n: number | undefined | null): string {
  if (n === undefined || n === null || Number.isNaN(n)) return "0";
  const abs = Math.abs(n);
  const units: [number, string][] = [
    [1_000_000_000, "B"],
    [1_000_000, "M"],
    [1_000, "k"],
  ];
  for (const [threshold, suffix] of units) {
    if (abs >= threshold) {
      const rounded = Math.round((n / threshold) * 10) / 10;
      return `${rounded % 1 === 0 ? rounded.toFixed(0) : rounded.toFixed(1)}${suffix}`;
    }
  }
  return String(Math.round(n));
}

export function truncate(text: string, n: number): string {
  if (text.length <= n) return text;
  return `${text.slice(0, n)}…`;
}

/** The word a status badge shows. `waiting_input` is the runner's value for a thread
 *  that ended its turn with plain text and did not call finish (03 §6.7); shown as
 *  "waiting input" it read as "asked a question", which it usually had not — three of
 *  four finished threads sat under that label (2026-09-22). Every other status is its
 *  own name with the underscore spaced. */
export function statusLabel(status: string): string {
  return status === "waiting_input" ? "stopped" : status.replace("_", " ");
}

/** Tooltip for a status badge; only the statuses whose word needs a gloss have one. */
export function statusTitle(status: string): string | undefined {
  if (status === "waiting_input") return "ended its turn without finishing; send a message to continue";
  if (status === "waiting_approval") return "paused on an approval request";
  return undefined;
}

/** A one-line summary of tool arguments for a tool-call row. */
export function argsSummary(args: unknown, max = 120): string {
  if (args === undefined || args === null) return "";
  if (typeof args === "string") return truncate(args, max);
  if (typeof args !== "object") return String(args);
  const parts: string[] = [];
  for (const [k, v] of Object.entries(args as Record<string, unknown>)) {
    const value =
      typeof v === "string" ? v : v === null ? "null" : typeof v === "object" ? JSON.stringify(v) : String(v);
    parts.push(`${k}=${truncate(value.replace(/\s+/g, " "), 60)}`);
  }
  return truncate(parts.join(" "), max);
}

export function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/** Divider label for a `compaction` event, by `meta.kind` (conversation/compaction.py:
 *  `evict`, `fold`, `clear`). The fold count is not in `meta`, so it is read from the
 *  event's own line ("folded 12 events into summary") when it is there. */
export function compactionLabel(meta: Record<string, unknown> | undefined, content?: string): string {
  const kind = typeof meta?.kind === "string" ? meta.kind : "";
  switch (kind) {
    case "clear":
      return "context cleared";
    case "evict":
      return "old tool output stubbed";
    case "fold": {
      const fromMeta = typeof meta?.folded_count === "number" ? meta.folded_count : null;
      const match = /(\d+)\s+events?/.exec(content ?? "");
      const n = fromMeta ?? (match ? Number(match[1]) : null);
      return n === null ? "context compacted" : `context compacted (${n} events summarised)`;
    }
    default:
      return "context compacted";
  }
}

/** A vault path as typed by the person for pinning: trimmed, forward slashes, no
 *  leading slash, `.md` added when missing. Empty in → empty out. */
export function normaliseNotePath(raw: string): string {
  let p = raw.trim().replace(/\\/g, "/").replace(/^\/+/, "");
  if (!p) return "";
  if (!p.toLowerCase().endsWith(".md")) p = `${p}.md`;
  return p;
}

/** Elapsed time between two ISO stamps (the second defaults to now): "12s", "3m 12s",
 *  "2h 05m", "3d 4h". For a process row: uptime while it runs, duration once it ended. */
export function duration(startIso: string | null | undefined, endIso?: string | null): string {
  if (!startIso) return "";
  const parse = (iso: string) => Date.parse(iso.endsWith("Z") || /[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  const start = parse(startIso);
  const end = endIso ? parse(endIso) : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end)) return "";
  const s = Math.max(0, Math.round((end - start) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}
