"""Static shell pre-filter (03 §9.4). It can only GRANT.

A command whose every pipeline stage starts with a word on the read-only allowlist, and
which contains no redirection, substitution or newline, is obviously harmless; running
a model to say so is a waste. Everything else — including anything this module cannot
parse — falls through to the classifier. Nothing here ever returns "deny": static
filters that deny are what models fight (02 §4), and the charter forbids it.

The splitter is deliberately small and total: it never raises, and any doubt is `False`.
"""

from __future__ import annotations

import os
from typing import Iterable

# Unquoted constructs that put a stage beyond a first-token judgement.
FORBIDDEN = (">", "<", "`", "$(", "${")


def split_stages(command: str) -> list[str] | None:
    """Split on `&&`, `||`, `;` and `|` outside quotes. None on parse failure: an
    unbalanced quote, a newline, or an unquoted redirection/substitution."""
    if "\n" in command or "\r" in command:
        return None
    stages: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(ch)
                buf.append(command[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:  # escaped character outside quotes
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        pair = command[i : i + 2]
        if pair in ("&&", "||"):
            stages.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in (";", "|", "&"):
            stages.append("".join(buf))
            buf = []
            i += 1
            continue
        if ch in ("<", ">", "`"):
            return None
        if pair in ("$(", "${"):
            return None
        buf.append(ch)
        i += 1
    if quote:
        return None
    stages.append("".join(buf))
    return [s.strip() for s in stages]


def split_words(stage: str) -> list[str] | None:
    """Whitespace split respecting quotes; quotes are stripped from the words."""
    words: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    started = False
    for ch in stage:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            started = True
            continue
        if ch.isspace():
            if buf or started:
                words.append("".join(buf))
                buf = []
                started = False
            continue
        buf.append(ch)
    if quote:
        return None
    if buf or started:
        words.append("".join(buf))
    return words


def _is_assignment(word: str) -> bool:
    name, sep, _ = word.partition("=")
    if not sep or not name:
        return False
    return (name[0].isalpha() or name[0] == "_") and all(c.isalnum() or c == "_" for c in name)


def command_name(word: str) -> str:
    """Basename without a `.exe` suffix, lowercased on Windows."""
    base = word.replace("\\", "/").rsplit("/", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.lower() if os.name == "nt" else base


def _entries(allowlist: Iterable[str]) -> tuple[set[str], set[tuple[str, str]]]:
    singles: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for raw in allowlist or ():
        parts = str(raw).split()
        if len(parts) == 1:
            singles.add(command_name(parts[0]))
        elif len(parts) == 2:
            pairs.add((command_name(parts[0]), parts[1]))
    return singles, pairs


def is_readonly(command: str, allowlist: list[str]) -> bool:
    """True only when every stage is plainly read-only. Never raises."""
    try:
        if not command or not command.strip():
            return False
        stages = split_stages(command)
        if not stages:
            return False
        singles, pairs = _entries(allowlist)
        for stage in stages:
            if not stage:
                return False
            if any(token in stage for token in FORBIDDEN):
                return False
            words = split_words(stage)
            if not words:
                return False
            while words and _is_assignment(words[0]):
                words = words[1:]
            if not words:
                return False
            name = command_name(words[0])
            if name in singles:
                continue
            if len(words) >= 2 and (name, words[1]) in pairs:
                continue
            return False
        return True
    except Exception:  # a pre-filter that raises would break a turn; doubt means classify
        return False
