import { describe, expect, it } from "vitest";
import { isSafeUrl, renderMarkdown, wikilinkHref } from "./markdown";

describe("wikilinks", () => {
  it("renders [[target]] as a vault link", () => {
    const html = renderMarkdown("see [[projects/loom/note]] for more");
    expect(html).toContain('href="#/vault/projects/loom/note"');
    expect(html).toContain(">projects/loom/note</a>");
  });

  it("uses the alias after the pipe", () => {
    const html = renderMarkdown("see [[projects/loom/note|the note]]");
    expect(html).toContain('href="#/vault/projects/loom/note"');
    expect(html).toContain(">the note</a>");
  });

  it("encodes each path segment", () => {
    expect(wikilinkHref("projects/my note")).toBe("#/vault/projects/my%20note");
  });
});

describe("sanitising", () => {
  it("strips script tags", () => {
    const html = renderMarkdown("hi <script>alert(1)</script>");
    expect(html).not.toMatch(/<script/i);
    expect(html).not.toContain("alert(1)</script>");
  });

  it("strips on* handlers", () => {
    // The handler may survive only as escaped text, never as a live attribute.
    const html = renderMarkdown('<img src="x" onerror="alert(1)">');
    expect(html).not.toMatch(/<[^>]*\bon[a-z]+\s*=/i);
    expect(html).toContain("&lt;img");
  });

  it("refuses javascript: urls", () => {
    expect(isSafeUrl("javascript:alert(1)")).toBe(false);
    expect(isSafeUrl("JaVaScRiPt:alert(1)")).toBe(false);
    expect(isSafeUrl("https://example.com")).toBe(true);
    expect(isSafeUrl("/local/path")).toBe(true);
    const html = renderMarkdown("[click](javascript:alert(1))");
    expect(html).not.toMatch(/javascript:/i);
  });

  it("still renders ordinary markdown", () => {
    const html = renderMarkdown("# Title\n\n- one\n- two\n\n`code`");
    expect(html).toContain("<h1>Title</h1>");
    expect(html).toContain("<li>one</li>");
    expect(html).toContain("<code>code</code>");
  });
});
