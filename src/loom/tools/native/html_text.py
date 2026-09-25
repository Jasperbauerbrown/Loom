"""HTML to markdown / plain-text conversion, built on stdlib `html.parser` only.

A page fetched by the `http` tool or scraped by a capability is HTML a model should not
have to read raw: script and style bodies are noise, tag soup wastes tokens, and a table
of `<td>` cells is unreadable without the pipes. This module walks the parse tree once and
renders it twice — once with markdown markers, once stripped to plain text — from the same
traversal, so the two stay consistent by construction rather than by two hand-maintained
code paths.

Failure here is an expected input, not a surprise: pages in the wild are unclosed,
mis-nested, or not HTML at all. `HTMLParser` already tolerates most of that; the one
path that could still misbehave — an exception out of the parser or the renderer —
falls back to a regex tag-strip rather than raising, per the robustness rule. Nothing
else triggers that fallback: angle brackets surviving into the output are ordinary
prose (`a < b`, or a page about `<div>`), and stripping them by regex would delete the
sentence, so the parsed answer is always the better one. The tree is walked with an
explicit stack, not recursion, so pathological nesting depth cannot blow the
interpreter's stack.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

# Elements dropped entirely, element and all descendants: script/style noise plus
# navigational chrome that is never the content a model was asked to read.
DROPPED_ELEMENTS = frozenset(
    {
        "script", "style", "noscript", "template", "svg", "head",
        "nav", "header", "footer", "aside", "form", "iframe",
    }
)

# Elements that force a block break (blank-line separation) around their content.
BLOCK_ELEMENTS = frozenset(
    {
        "p", "div", "section", "article", "main", "table", "tr",
        "ul", "ol", "li", "blockquote", "pre",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }
)

HEADING_LEVELS = {f"h{n}": n for n in range(1, 7)}

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"[ \t\f\v]+")
_BLANK_RUNS_RE = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------------
# tree
# ---------------------------------------------------------------------------------
class _Node:
    """One element or text run. `children` mixes `_Node` and `str`."""

    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str | None, attrs: dict[str, str | None] | None = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Any] = []


class _TreeBuilder(HTMLParser):
    """Builds a `_Node` tree with an explicit stack (no recursion, so depth is not
    bounded by Python's call stack). Unclosed tags are simply left open until end of
    document; stray close tags are ignored — both are the lenient behaviour the spec
    asks for rather than an error."""

    # Tags that never nest content and have no matching end tag in the wild.
    _VOID = frozenset({"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(None)
        self._stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, dict(attrs))
        self._stack[-1].children.append(node)
        if tag not in self._VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(_Node(tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        # Pop back to the matching open tag if one exists anywhere on the stack;
        # otherwise this is a stray close tag and is ignored.
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)

    def error(self, message: str) -> None:  # pragma: no cover - py3.12 never calls this
        pass


def _parse(html: str) -> _Node | None:
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:
        return None
    return builder.root


# ---------------------------------------------------------------------------------
# regex fallback
# ---------------------------------------------------------------------------------
def _strip_tags_fallback(html: str) -> str:
    """Last resort when the parser itself blew up: drop angle-bracket runs, resolve
    entities, collapse whitespace. Loses all structure, but never raises."""
    text = _TAG_RE.sub(" ", html)
    text = unescape(text)
    text = _WS_RE.sub(" ", text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


# ---------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------
class _Renderer:
    """Walks a `_Node` tree with an explicit stack and produces either markdown or
    plain text, depending on `markdown`. The two modes share every structural decision
    (what is a block, what is a list item, what is a table row) and differ only in the
    inline markers emitted around text."""

    def __init__(self, markdown: bool) -> None:
        self.markdown = markdown
        self.out: list[str] = []
        self._quote_marks: list[int] = []

    # -- text helpers --------------------------------------------------------
    def _emit(self, text: str) -> None:
        if text:
            self.out.append(text)

    def _break_block(self) -> None:
        self._emit("\n\n")

    def _line_break(self) -> None:
        self._emit("\n")

    def _escape_inline(self, text: str) -> str:
        if not self.markdown:
            return text
        # Minimal escaping: only a line-leading marker that would otherwise read as
        # a heading/bullet/quote. Mid-line punctuation is left alone for readability.
        def _fix_line(line: str) -> str:
            stripped = line.lstrip(" ")
            lead = len(line) - len(stripped)
            if stripped.startswith(("#", "-", ">")):
                return line[:lead] + "\\" + stripped
            return line
        return "\n".join(_fix_line(ln) for ln in text.split("\n"))

    def _clean_text(self, data: str, in_pre: bool) -> str:
        if in_pre:
            return data
        collapsed = _WS_RE.sub(" ", data.replace("\n", " ").replace("\t", " "))
        return collapsed

    # -- table cell rendering (always inline, own mini-renderer) ------------
    def _cell_text(self, node: _Node) -> str:
        sub = _Renderer(self.markdown)
        sub.render(node, in_pre=False, list_depth=0, ordered_stack=[])
        text = "".join(sub.out)
        text = text.replace("\n", " ").strip()
        text = _WS_RE.sub(" ", text)
        return text.replace("|", "\\|")

    # -- main walk ------------------------------------------------------------
    def render(self, root: _Node, in_pre: bool = False, list_depth: int = 0, ordered_stack: list[bool] | None = None) -> None:
        ordered_stack = ordered_stack if ordered_stack is not None else []
        # Explicit work stack: each frame is (node_or_str, in_pre, list_depth, ordered_stack, phase, extra)
        # Kept simple by delegating to a recursive-looking but iterative helper below.
        self._walk(root, in_pre, list_depth, ordered_stack)

    def _walk(self, node: _Node, in_pre: bool, list_depth: int, ordered_stack: list[bool]) -> None:
        # Explicit stack of pending (children-list, index, context) frames, so nesting
        # depth in the HTML never becomes Python recursion depth.
        Frame = tuple  # (children, idx, in_pre, list_depth, ordered_stack, tag, node)
        stack: list[list] = [[node.children, 0, in_pre, list_depth, ordered_stack, node.tag, node]]
        table_stack: list[dict] = []

        while stack:
            frame = stack[-1]
            children, idx, cur_pre, cur_depth, cur_ordered, tag, owner = frame
            if idx >= len(children):
                stack.pop()
                if owner is not node:
                    self._on_close(owner, table_stack)
                continue
            frame[1] += 1
            child = children[idx]
            if isinstance(child, str):
                text = self._clean_text(child, cur_pre)
                if table_stack and table_stack[-1].get("collecting"):
                    table_stack[-1]["cell_text"].append(child)
                elif text:
                    self._emit(self._escape_inline(text) if not cur_pre else text)
                continue
            ctag = child.tag or ""
            if ctag in DROPPED_ELEMENTS:
                continue
            enter = self._on_open(child, cur_pre, cur_depth, cur_ordered, table_stack)
            if enter is None:
                continue
            next_pre, next_depth, next_ordered = enter
            stack.append([child.children, 0, next_pre, next_depth, next_ordered, ctag, child])

    # -- open/close handlers --------------------------------------------------
    def _on_open(self, node: _Node, in_pre: bool, list_depth: int, ordered_stack: list[bool], table_stack: list[dict]) -> tuple[bool, int, list[bool]] | None:
        tag = node.tag or ""

        if tag == "br":
            self._line_break()
            return None
        if tag == "hr":
            self._break_block()
            self._emit("---" if self.markdown else "")
            self._break_block()
            return None
        if tag == "img":
            if self.markdown:
                src = (node.attrs.get("src") or "").strip()
                alt = (node.attrs.get("alt") or "").strip()
                if src:
                    self._emit(f"![{alt}]({src})")
                elif alt:
                    self._emit(alt)
            else:
                alt = (node.attrs.get("alt") or "").strip()
                if alt:
                    self._emit(alt)
            return None

        if tag in HEADING_LEVELS:
            self._break_block()
            if self.markdown:
                self._emit("#" * HEADING_LEVELS[tag] + " ")
            return (in_pre, list_depth, ordered_stack)
        if tag == "p" or tag in ("div", "section", "article", "main"):
            self._break_block()
            return (in_pre, list_depth, ordered_stack)
        if tag == "blockquote":
            self._break_block()
            self._quote_marks.append(len(self.out))
            return (in_pre, list_depth, ordered_stack)
        if tag == "pre":
            self._break_block()
            if self.markdown:
                self._emit("```\n")
            return (True, list_depth, ordered_stack)
        if tag == "code" and not in_pre:
            if self.markdown:
                self._emit("`")
            return (in_pre, list_depth, ordered_stack)
        if tag in ("strong", "b"):
            if self.markdown:
                self._emit("**")
            return (in_pre, list_depth, ordered_stack)
        if tag in ("em", "i"):
            if self.markdown:
                self._emit("*")
            return (in_pre, list_depth, ordered_stack)
        if tag in ("ul", "ol"):
            self._break_block()
            return (in_pre, list_depth + 1, ordered_stack + [tag == "ol"])
        if tag == "li":
            self._line_break()
            indent = "  " * max(0, list_depth - 1)
            if self.markdown:
                marker = "1. " if (ordered_stack and ordered_stack[-1]) else "- "
                self._emit(indent + marker)
            else:
                self._emit(indent + "")
            return (in_pre, list_depth, ordered_stack)
        if tag == "a":
            href = (node.attrs.get("href") or "").strip()
            text = self._collect_text(node)
            if not self.markdown or not href or href.lower().startswith("javascript:"):
                if text:
                    self._emit(text)
                return None
            if not text:
                return None
            self._emit(f"[{text}]({href})")
            return None
        if tag == "table":
            self._break_block()
            table_stack.append({"rows": [], "header_done": False})
            return (in_pre, list_depth, ordered_stack)
        if tag == "thead" or tag == "tbody" or tag == "tfoot":
            return (in_pre, list_depth, ordered_stack)
        if tag == "tr":
            row_cells: list[tuple[str, bool]] = []
            table_stack[-1]["current_row"] = row_cells
            return (in_pre, list_depth, ordered_stack)
        if tag in ("td", "th"):
            text = self._cell_text(node)
            table_stack[-1]["current_row"].append((text, tag == "th"))
            return None

        # Unknown / unhandled inline element: recurse into content, no markers.
        return (in_pre, list_depth, ordered_stack)

    def _on_close(self, node: _Node, table_stack: list[dict]) -> None:
        tag = node.tag or ""
        if tag in HEADING_LEVELS:
            self._break_block()
            return
        if tag == "blockquote":
            mark = self._quote_marks.pop() if self._quote_marks else 0
            inner = "".join(self.out[mark:]).strip("\n")
            del self.out[mark:]
            if self.markdown and inner:
                inner = "\n".join(f"> {ln}" if ln else ">" for ln in inner.split("\n"))
            self._emit(inner)
            self._break_block()
            return
        if tag in ("p", "div", "section", "article", "main"):
            self._break_block()
            return
        if tag == "pre":
            if self.markdown:
                self._emit("\n```")
            self._break_block()
            return
        if tag == "code" and self.markdown:
            self._emit("`")
            return
        if tag in ("strong", "b") and self.markdown:
            self._emit("**")
            return
        if tag in ("em", "i") and self.markdown:
            self._emit("*")
            return
        if tag in ("ul", "ol"):
            self._break_block()
            return
        if tag == "li":
            return
        if tag == "tr":
            row = table_stack[-1].pop("current_row", [])
            if row:
                table_stack[-1]["rows"].append(row)
            return
        if tag == "table":
            table = table_stack.pop()
            self._emit(self._render_table(table["rows"]))
            self._break_block()
            return

    def _collect_text(self, node: _Node) -> str:
        """Plain inline text of a subtree (used for `<a>` link text)."""
        sub = _Renderer(self.markdown)
        sub.render(node, in_pre=False)
        text = "".join(sub.out).strip()
        return _WS_RE.sub(" ", text.replace("\n", " "))

    def _render_table(self, rows: list[list[tuple[str, bool]]]) -> str:
        if not rows:
            return ""
        lines: list[str] = []
        header_idx = None
        if rows and rows[0] and all(is_th for _, is_th in rows[0]):
            header_idx = 0
        for i, row in enumerate(rows):
            cells = [text for text, _ in row]
            lines.append("| " + " | ".join(cells) + " |")
            if i == (header_idx if header_idx is not None else 0):
                lines.append("| " + " | ".join("---" for _ in cells) + " |")
        return "\n".join(lines)


# ---------------------------------------------------------------------------------
# list-item numbering fix-up (ordered lists want 1., 2., 3. — track counts properly)
# ---------------------------------------------------------------------------------
# The renderer above marks every `<li>` in an `<ol>` with a flat "1. "; correcting each
# item's number would need per-level counters threaded through the walk. Rather than
# complicate the explicit-stack walker further, a light post-pass renumbers consecutive
# ordered-list markers at the same indent.
def _renumber_ordered_lists(text: str) -> str:
    lines = text.split("\n")
    counters: dict[int, int] = {}
    result = []
    for line in lines:
        m = re.match(r"^( *)1\. (.*)$", line)
        if not m:
            # A non-list line at indent 0 resets all counters (new context).
            if line.strip() and not line.startswith(" "):
                counters.clear()
            result.append(line)
            continue
        indent = len(m.group(1))
        counters[indent] = counters.get(indent, 0) + 1
        for deeper in [k for k in counters if k > indent]:
            del counters[deeper]
        result.append(f"{m.group(1)}{counters[indent]}. {m.group(2)}")
    return "\n".join(result)


# ---------------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------------
def _finalise(rendered: str) -> str:
    text = _BLANK_RUNS_RE.sub("\n\n", rendered)
    lines = [ln.rstrip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANK_RUNS_RE.sub("\n\n", text)
    return text.strip()


def _convert(html: str, markdown: bool) -> str:
    if not html:
        return ""
    root = _parse(html)
    if root is None:
        return _finalise(_strip_tags_fallback(html))
    try:
        renderer = _Renderer(markdown)
        renderer.render(root)
        rendered = "".join(renderer.out)
        if markdown:
            rendered = _renumber_ordered_lists(rendered)
        return _finalise(rendered)
    except Exception:
        return _finalise(_strip_tags_fallback(html))


def html_to_markdown(html: str) -> str:
    return _convert(html, markdown=True)


def html_to_text(html: str) -> str:
    return _convert(html, markdown=False)
