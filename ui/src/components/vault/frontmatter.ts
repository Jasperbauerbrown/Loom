// Minimal, lossless-for-scalars frontmatter split/join. The backend (knowledge_write)
// is the real YAML parser; this is just enough for the editor's key/value form to
// round-trip simple values without pulling in a YAML dependency (03 §19).

export interface SplitNote {
  frontmatter: string | null; // raw block between the `---` fences, without fences
  body: string;
}

export function splitFrontmatter(content: string): SplitNote {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/.exec(content);
  if (!m) return { frontmatter: null, body: content };
  return { frontmatter: m[1], body: m[2] };
}

function parseScalar(raw: string): unknown {
  const v = raw.trim();
  if (v === "") return "";
  if (v.startsWith("[") && v.endsWith("]")) {
    const inner = v.slice(1, -1).trim();
    if (!inner) return [];
    return inner.split(",").map((s) => s.trim().replace(/^["']|["']$/g, ""));
  }
  if (/^-?\d+(\.\d+)?$/.test(v)) return Number(v);
  if (v === "true") return true;
  if (v === "false") return false;
  return v.replace(/^["']|["']$/g, "");
}

/** true when every value is a plain scalar or a flat array of scalars (form-editable). */
export function isFlatFrontmatter(fm: Record<string, unknown>): boolean {
  return Object.values(fm).every((v) => {
    if (v === null || typeof v === "string" || typeof v === "number" || typeof v === "boolean") return true;
    if (Array.isArray(v)) return v.every((x) => typeof x === "string" || typeof x === "number" || typeof x === "boolean");
    return false;
  });
}

/** Parse a simple `key: value` per-line block (no nesting) into an object. */
export function parseFlatFrontmatter(raw: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim() || line.trim().startsWith("#")) continue;
    const m = /^([^:\s][^:]*):\s?(.*)$/.exec(line);
    if (!m) continue;
    out[m[1].trim()] = parseScalar(m[2]);
  }
  return out;
}

function serializeScalar(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map((x) => String(x)).join(", ")}]`;
  if (typeof v === "string") return /[:#]/.test(v) || v === "" ? JSON.stringify(v) : v;
  return String(v);
}

export function serializeFlatFrontmatter(fm: Record<string, unknown>): string {
  return Object.entries(fm)
    .map(([k, v]) => `${k}: ${serializeScalar(v)}`)
    .join("\n");
}

/** Reassemble a note document from a raw frontmatter block (or null) and body. */
export function joinFrontmatter(frontmatter: string | null, body: string): string {
  if (frontmatter === null || frontmatter.trim() === "") return body;
  return `---\n${frontmatter}\n---\n${body}`;
}
