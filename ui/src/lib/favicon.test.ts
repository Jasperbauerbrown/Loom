import { describe, expect, it } from "vitest";
import { faviconDataUrl } from "./favicon";

describe("theme-aware favicon", () => {
  it("uses the active palette accent in a self-contained favicon SVG", () => {
    const url = faviconDataUrl("#7c3aed");
    const svg = decodeURIComponent(url.split(",")[1]);

    expect(url).toContain("data:image/svg+xml,");
    expect(svg).toContain('fill="#7c3aed"');
    expect(svg).toContain('stroke-width="5"');
  });

  it("keeps the default theme's computed OKLCH accent", () => {
    const svg = decodeURIComponent(faviconDataUrl("oklch(58.8% .158 241.966)").split(",")[1]);

    expect(svg).toContain('fill="oklch(58.8% .158 241.966)"');
  });

  it("falls back to the default accent for invalid CSS values", () => {
    expect(decodeURIComponent(faviconDataUrl("red; background: url(evil)").split(",")[1])).toContain('fill="#0284c7"');
  });
});
