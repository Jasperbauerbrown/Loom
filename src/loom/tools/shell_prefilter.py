"""Static shell pre-filter (03 §9.4). It can only GRANT.

A command whose every pipeline stage starts with a word on the read-only allowlist, and
which contains no redirection, substitution or newline, and names no protected path, is
obviously harmless; running a model to say so is a waste. Everything else — including
anything this module cannot parse — falls through to the classifier. Nothing here ever
returns "deny": static filters that deny are what models fight (02 §4), and the charter
forbids it.

The splitter is deliberately small and total: it never raises, and any doubt is `False`.
"""

from __future__ import annotations

import os
import re
from typing import Iterable

# Unquoted constructs that put a stage beyond a first-token judgement.
FORBIDDEN = (">", "<", "`", "$(", "${")

# Redirections that write nowhere: a stream duplicated onto another (`2>&1`) or sent to
# the null device (`>/dev/null`, `2>/dev/null`, `&>/dev/null`). They are dropped from
# the stage rather than failing the parse: before 2026-09-29 every `git log … 2>&1`
# cost a classifier call. Bash reads a digit before `>` as the stream only when it is
# a word of its own, so `head -c 2>/dev/null` is `head -c` with stderr dropped — the
# same reading here (`_null_redirect`). `2>nul` is not in the set: on Git Bash it
# creates a file called `nul`.
_NULL_REDIRECT = re.compile(r"(?:[12]?>>?|&>)\s*(?:/dev/null|&[12])(?![^\s;&|])")


def _null_redirect(command: str, i: int) -> int | None:
    """The index just past a harmless redirection starting at `i`, else None."""
    ch = command[i]
    if ch not in "12&>":
        return None
    if ch in "12" and not (i == 0 or command[i - 1].isspace()):
        return None  # part of a word (`head -c 12>…` is not stream 2)
    m = _NULL_REDIRECT.match(command, i)
    if m is None:
        return None
    if ch == "&" and not m.group(0).startswith("&>"):
        return None
    return m.end()


def split_stages(command: str) -> list[str] | None:
    """Split on `&&`, `||`, `;` and `|` outside quotes. None on parse failure: an
    unbalanced quote, a newline, or an unquoted redirection/substitution. A
    redirection that writes nowhere (`2>&1`, `>/dev/null`) is dropped from its stage."""
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
        end = _null_redirect(command, i)
        if end is not None:
            i = end
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


def _key(path: str) -> str:
    p = path.replace("\\", "/").rstrip("/")
    return p.lower() if os.name == "nt" else p


def _looks_like_path(word: str) -> bool:
    return "/" in word or "\\" in word or word.startswith("~") or (len(word) > 1 and word[1] == ":")


def names_protected(words: Iterable[str], protected: Iterable[str] | None, cwd: str | None = None) -> bool:
    """True when any word, taken as a path relative to the shell's `cwd`, is a protected
    path or sits under one, or is a path that cannot be resolved statically (`$VAR`,
    `%VAR%`). A read-only command is not harmless when it reads `secrets.json`;
    protected paths guarded the fs tools but not `cat` (2026-09-22). Not granting is
    not denying: the classifier sees the call. Options (`-x`) are skipped; a bare word
    like `notes.md` is a path in `cwd`, so a command run inside a protected tree is
    judged too."""
    keys = [_key(p) for p in (protected or ()) if p]
    if not keys:
        return False
    from .fs_guard import normalise

    for word in words:
        if not word or word.startswith("-"):
            continue
        if "$" in word or "%" in word:
            if _looks_like_path(word):
                return True
            continue
        try:
            target = _key(normalise(word, cwd=cwd))
        except Exception:
            return True
        for key in keys:
            if target == key or target.startswith(key + "/"):
                return True
    return False


def is_readonly(command: str, allowlist: list[str], protected: Iterable[str] | None = None, cwd: str | None = None) -> bool:
    """True only when every stage is plainly read-only and names no protected path.
    `cwd` is what relative paths in the command resolve against (the shell's working
    directory). Never raises."""
    try:
        if not command or not command.strip():
            return False
        stages = split_stages(command)
        if not stages:
            return False
        singles, pairs = _entries(allowlist)
        seen: list[str] = []
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
            seen.extend(words)
            name = command_name(words[0])
            if name in singles:
                continue
            if len(words) >= 2 and (name, words[1]) in pairs:
                continue
            return False
        if names_protected(seen, protected, cwd):
            return False
        return True
    except Exception:  # a pre-filter that raises would break a turn; doubt means classify
        return False
