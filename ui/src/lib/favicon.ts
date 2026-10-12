/** Update the tab icon from the active palette's primary accent. The SVG data URL is
 *  self-contained because browsers render favicons outside the page's CSS cascade. */
export function faviconDataUrl(accent: string): string {
  const isHex = /^#[\da-f]{3}(?:[\da-f]{3})?$/i.test(accent);
  const isOklch = /^oklch\([\d.%\s/+\-]+\)$/i.test(accent);
  const color = isHex || isOklch ? accent : "#0284c7";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect x="3" y="3" width="58" height="58" rx="17" fill="${color}"/><path d="M20 16v25c0 5 3 8 8 8h5M44 16v25c0 5-3 8-8 8h-5M20 25h24M20 39h24" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/><circle cx="20" cy="25" r="4" fill="#fff"/><circle cx="44" cy="39" r="4" fill="#fff"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

export function setFaviconAccent(accent: string) {
  if (typeof document === "undefined") return;
  const link = document.getElementById("loom-favicon") as HTMLLinkElement | null;
  if (link) link.href = faviconDataUrl(accent);
}
