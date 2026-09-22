"""Markdown note parsing: frontmatter, wikilinks, aliases, status blocks, dates.

Pure functions only — no app, no database, no filesystem. Everything here is a
deterministic text transform so it can be unit-tested directly and reused by the vault
writer, the indexer and the maintenance pass alike (03 §4.3, §8.1).

Failure is an expected input (charter §2.1): a malformed note parses into
`Parsed(frontmatter={}, body=<whole text>, error="...")` rather than raising, so the
indexer can record it as `type=invalid` and carry on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml

# --------------------------------------------------------------------------------
# Frontmatter
# --------------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


@dataclass
class Parsed:
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    error: str | None = None


def parse(text: str) -> Parsed:
    """Split a note into YAML frontmatter and body.

    No leading `---` block → the whole text is the body (not an error). A block that is
    not valid YAML, or that is not a mapping, sets `error` and leaves the *whole* text as
    the body so nothing is ever lost."""
    if text is None:
        return Parsed({}, "", None)
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return Parsed({}, text, None)
    raw = match.group(1)
    body = text[match.end() :]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return Parsed({}, text, f"invalid frontmatter: {_one_line(exc)}")
    if data is None:
        return Parsed({}, body, None)
    if not isinstance(data, dict):
        return Parsed({}, text, "invalid frontmatter: expected a mapping")
    return Parsed(data, body, None)


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())


class _Dumper(yaml.SafeDumper):
    """Flow style for short scalar lists (`aliases: [a, b]`), block style otherwise."""


def _represent_list(dumper: yaml.SafeDumper, data: list[Any]) -> Any:
    flow = bool(data) and all(isinstance(x, (str, int, float, bool)) for x in data) and len(repr(data)) <= 120
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_Dumper.add_representer(list, _represent_list)


def emit(frontmatter: dict[str, Any], body: str) -> str:
    """Re-emit a note. Empty frontmatter emits the body alone."""
    body = body or ""
    if not body.endswith("\n"):
        body += "\n"
    if not frontmatter:
        return body.lstrip("\n")
    dumped = yaml.dump(
        dict(frontmatter),
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    return f"---\n{dumped}---\n\n{body.lstrip(chr(10))}"


# --------------------------------------------------------------------------------
# Wikilinks and aliases
# --------------------------------------------------------------------------------

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def wikilinks(body: str) -> list[str]:
    """Link *targets* in order of appearance, `#heading` and `|alias` stripped.
    Duplicates are preserved; the indexer de-duplicates."""
    return [m.group(1).strip() for m in WIKILINK_RE.finditer(body or "") if m.group(1).strip()]


_ALIAS_PUNCT_RE = re.compile(r"[^\w\s\-_.]", re.UNICODE)


def normalise_alias(s: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation except inner `-`, `_`, `.`."""
    s = (s or "").lower()
    s = _ALIAS_PUNCT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip("-_.")


# --------------------------------------------------------------------------------
# Status block (03 §4.3)
# --------------------------------------------------------------------------------

STATUS_BEGIN = "<!-- status:begin owner=%s -->"
STATUS_END = "<!-- status:end -->"
STATUS_HEADING = "## Status"
STATUS_FIELDS: tuple[str, ...] = ("state", "goal", "done", "doing", "next", "blockers", "updated")
STATUS_STATES: tuple[str, ...] = ("planning", "working", "blocked", "waiting", "done")

_STATUS_BLOCK_RE = re.compile(
    r"<!--[ \t]*status:begin[ \t]+owner=(?P<owner>\S*)[ \t]*-->\r?\n?(?P<body>.*?)<!--[ \t]*status:end[ \t]*-->",
    re.S,
)
_STATUS_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*)$")


def parse_status(body: str) -> tuple[dict[str, str] | None, str | None]:
    """`(fields, owner)` for the first status block, or `(None, None)` when absent.
    Multi-line (bulleted) values come back as newline-joined strings."""
    match = _STATUS_BLOCK_RE.search(body or "")
    if match is None:
        return None, None
    fields: dict[str, str] = {}
    key: str | None = None
    for raw in match.group("body").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _STATUS_KEY_RE.match(line)
        if m and not line.startswith((" ", "\t", "-")):
            key = m.group(1).strip().lower()
            fields[key] = m.group(2).strip()
            continue
        if key is None:
            continue
        piece = line.strip()
        if piece.startswith("- "):
            piece = piece[2:].strip()
        elif piece == "-":
            piece = ""
        fields[key] = f"{fields[key]}\n{piece}".strip() if fields[key] else piece
    return fields, match.group("owner")


def status_blocks(body: str) -> list[str]:
    """The full text of every delimited status block in `body`, in order — what
    `Vault.write(protect_status=True)` requires a write to leave byte-identical."""
    return [m.group(0) for m in _STATUS_BLOCK_RE.finditer(body or "")]


def render_status(owner: str, fields: dict[str, Any]) -> str:
    """The delimited block, keys in `STATUS_FIELDS` order; unknown keys follow, sorted.
    Multi-line and list values render as indented bullets."""
    lines = [STATUS_BEGIN % (owner or "")]
    ordered = list(STATUS_FIELDS) + sorted(k for k in fields if k not in STATUS_FIELDS)
    for key in ordered:
        if key not in fields:
            continue
        value = fields[key]
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            parts = [str(v).strip() for v in value if str(v).strip()]
        else:
            parts = [p.strip() for p in str(value).splitlines() if p.strip()]
        if not parts:
            continue
        if len(parts) == 1:
            lines.append(f"{key}: {parts[0]}")
        else:
            lines.append(f"{key}:")
            lines.extend(f"  - {p.lstrip('- ').strip()}" for p in parts)
    lines.append(STATUS_END)
    return "\n".join(lines)


def replace_status(body: str, owner: str, fields: dict[str, Any]) -> str:
    """Replace the delimited block in place. When there is no block, append it under an
    existing `## Status` heading, or append the whole section when that is missing too."""
    body = body or ""
    block = render_status(owner, fields)
    match = _STATUS_BLOCK_RE.search(body)
    if match is not None:
        return body[: match.start()] + block + body[match.end() :]
    heading = re.search(r"^[ \t]*##[ \t]+Status[ \t]*$", body, re.M)
    if heading is not None:
        cut = heading.end()
        return body[:cut] + "\n" + block + "\n" + body[cut:].lstrip("\n")
    prefix = body.rstrip("\n")
    joiner = "\n\n" if prefix else ""
    return f"{prefix}{joiner}{STATUS_HEADING}\n{block}\n"


# --------------------------------------------------------------------------------
# Relative dates (03 §4.3: rejected by knowledge_write)
# --------------------------------------------------------------------------------

# One tuple so the list is trivial to extend. Each entry is a regex fragment; they are
# joined into a single word-bounded, case-insensitive alternation.
RELATIVE_DATE_PATTERNS: tuple[str, ...] = (
    r"yesterday",
    r"today",
    r"tomorrow",
    r"last\s+(?:week|month|year)",
    r"next\s+(?:week|month|year)",
    r"this\s+(?:week|month|year)",
    r"\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago",
    r"in\s+\d+\s+(?:day|days|week|weeks|month|months|year|years)",
)

_RELATIVE_DATE_RE = re.compile(r"\b(?:" + "|".join(RELATIVE_DATE_PATTERNS) + r")\b", re.I)


def has_relative_dates(text: str) -> str | None:
    """The offending phrase, or None. Dates in notes must be absolute: a note outlives
    the turn that wrote it, and "tomorrow" is a lie the next morning."""
    match = _RELATIVE_DATE_RE.search(text or "")
    return match.group(0) if match else None


# --------------------------------------------------------------------------------
# Paths and titles
# --------------------------------------------------------------------------------


def title_from_path(path: str) -> str:
    """`devices/ender-3.md` → `Ender 3`; `procedures/pla-print/SKILL.md` → `Pla print`."""
    p = PurePosixPath(str(path).replace("\\", "/"))
    stem = p.stem
    if stem.upper() == "SKILL" and p.parent.name:
        stem = p.parent.name
    words = re.sub(r"[-_]+", " ", stem).strip()
    words = re.sub(r"\s+", " ", words)
    return words[:1].upper() + words[1:] if words else stem


def slug(title: str) -> str:
    """A filename stem: lowercase, hyphen-separated, no punctuation."""
    s = (title or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "untitled"
