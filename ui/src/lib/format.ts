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

export function truncate(text: string, n: number): string {
  if (text.length <= n) return text;
  return `${text.slice(0, n)}…`;
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
