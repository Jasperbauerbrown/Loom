// Markdown rendering: `marked` + a wikilink extension + an allowlist sanitiser
// (03 §21: no DOMPurify). Defence is layered and both layers are cheap:
//   1. every raw-HTML token from the model is escaped, so marked only ever emits the
//      tags it generates itself, and link/image URLs are scheme-checked;
//   2. in the browser the result goes through an allowlist pass over DOMParser, which
//      drops anything unexpected (including `on*` handlers) that slipped through.

import { marked, type Tokens } from "marked";

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const SAFE_SCHEME = /^(https?:|mailto:|tel:|#|\/|\.{0,2}\/)/i;

export function isSafeUrl(url: string): boolean {
  const trimmed = url.trim().replace(/[\u0000-\u001f]/g, "");
  if (!trimmed) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(trimmed)) return SAFE_SCHEME.test(trimmed);
  return true; // relative path or bare fragment
}

/** `[[target|alias]]` → a link into the vault screen. */
export function wikilinkHref(target: string): string {
  const path = target.trim().replace(/^\/+/, "");
  return `#/vault/${path.split("/").map(encodeURIComponent).join("/")}`;
}

interface WikilinkToken extends Tokens.Generic {
  type: "wikilink";
  target: string;
  alias: string;
}

marked.use({
  gfm: true,
  breaks: false,
  extensions: [
    {
      name: "wikilink",
      level: "inline",
      start(src: string) {
        return src.indexOf("[[");
      },
      tokenizer(src: string) {
        const match = /^\[\[([^\]|\n]+)(?:\|([^\]\n]*))?\]\]/.exec(src);
        if (!match) return undefined;
        const token: WikilinkToken = {
          type: "wikilink",
          raw: match[0],
          target: match[1].trim(),
          alias: (match[2] ?? match[1]).trim(),
        };
        return token;
      },
      renderer(token) {
        const t = token as WikilinkToken;
        return `<a class="wikilink" data-wikilink="${escapeHtml(t.target)}" href="${escapeHtml(
          wikilinkHref(t.target),
        )}">${escapeHtml(t.alias)}</a>`;
      },
    },
  ],
  renderer: {
    // Raw HTML from a model is content, not markup.
    html(token: Tokens.HTML | Tokens.Tag) {
      return escapeHtml(token.raw ?? token.text ?? "");
    },
    link(token: Tokens.Link) {
      const href = isSafeUrl(token.href) ? token.href : "#";
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      const external = /^https?:/i.test(href) ? ' target="_blank" rel="noreferrer noopener"' : "";
      return `<a href="${escapeHtml(href)}"${title}${external}>${this.parser.parseInline(token.tokens)}</a>`;
    },
    image(token: Tokens.Image) {
      if (!isSafeUrl(token.href)) return escapeHtml(token.text ?? "");
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      return `<img src="${escapeHtml(token.href)}" alt="${escapeHtml(token.text ?? "")}"${title} />`;
    },
  },
});

const ALLOWED_TAGS = new Set([
  "a", "b", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5", "h6",
  "hr", "i", "img", "input", "li", "ol", "p", "pre", "s", "span", "strong", "sub", "sup",
  "table", "tbody", "td", "th", "thead", "tr", "ul", "div",
]);

const ALLOWED_ATTRS = new Set([
  "href", "src", "alt", "title", "class", "target", "rel", "colspan", "rowspan",
  "start", "type", "checked", "disabled", "data-wikilink", "align",
]);

/** Allowlist pass: unknown elements are unwrapped, unknown/unsafe attributes dropped. */
export function sanitize(html: string): string {
  if (typeof DOMParser === "undefined") return html;
  const doc = new DOMParser().parseFromString(`<div id="root">${html}</div>`, "text/html");
  const root = doc.getElementById("root");
  if (!root) return "";
  const walk = (node: Element) => {
    for (const child of Array.from(node.children)) {
      const tag = child.tagName.toLowerCase();
      if (!ALLOWED_TAGS.has(tag)) {
        // script/style carry no renderable text worth keeping; everything else unwraps.
        if (tag === "script" || tag === "style" || tag === "iframe" || tag === "object") child.remove();
        else {
          walk(child);
          child.replaceWith(...Array.from(child.childNodes));
        }
        continue;
      }
      for (const attr of Array.from(child.attributes)) {
        const name = attr.name.toLowerCase();
        if (!ALLOWED_ATTRS.has(name) || name.startsWith("on")) child.removeAttribute(attr.name);
        else if ((name === "href" || name === "src") && !isSafeUrl(attr.value)) child.removeAttribute(attr.name);
      }
      walk(child);
    }
  };
  walk(root);
  return root.innerHTML;
}

export function renderMarkdown(source: string): string {
  const html = marked.parse(source ?? "", { async: false }) as string;
  return sanitize(html);
}

export function renderInline(source: string): string {
  const html = marked.parseInline(source ?? "", { async: false }) as string;
  return sanitize(html);
}
