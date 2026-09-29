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
