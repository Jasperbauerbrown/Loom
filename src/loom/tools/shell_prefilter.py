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


# -- protected_reference: the desk auto-approve's check ------------------------------
# `names_protected` serves the pre-filter, whose miss costs one classifier call, so it
# leans to "protected" on anything it cannot resolve. Desk auto-approve's miss costs a
# click and is meant to cost nothing, so it asks the opposite question: does the raw
# text contain an actual reference to a protected path? (2026-10-05)
_WORD_BREAK = re.compile(r"[\s;|&<>()=,`]+")
_VAR = re.compile(r"\$\{(\w+)\}|\$(\w+)|%(\w+)%")
_TILDE = re.compile(r"(?:^|(?<=[\s;|&<>()=,`]))~(?=/|$|[\s;|&<>()=,`])")
_URL = re.compile(r"^[a-z][a-z0-9+.-]+:/", re.I)  # a scheme of 2+ letters; `c:/` is a drive
_PATH_CONT = "._-+@~#"
_GLOB = re.compile(r"[*?\[]")


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None and name == "HOME":
        value = os.path.expanduser("~")  # Git Bash sets HOME; the Windows process may not
    return value


def _flatten(text: str) -> str:
    """The command as one comparable string: known variables and `~` expanded (an
    unknown one stays as written, so it can never match), quotes dropped, every
    backslash run read as one `/`, slash runs collapsed, case folded on Windows."""
    text = _VAR.sub(lambda m: _env(m.group(1) or m.group(2) or m.group(3)) or m.group(0), text)
    home = os.path.expanduser("~")
    text = _TILDE.sub(lambda m: home.replace("\\", "/"), text)
    text = text.replace('"', "").replace("'", "")
    text = re.sub(r"[\\/]+", "/", text)
    return text.lower() if os.name == "nt" else text


def _spellings(path: str) -> set[str]:
    """How a shell may write one absolute path: as stored, and through the MSYS /
    Cygwin / WSL drive mounts (`/c/...`)."""
    p = _key(path)
    out = {p}
    m = re.match(r"^([a-zA-Z]):/(.*)$", p)
    if m:
        drive, rest = m.group(1).lower(), m.group(2)
        out |= {f"/{drive}/{rest}", f"/cygdrive/{drive}/{rest}", f"/mnt/{drive}/{rest}"}
    return out


def _bounded(text: str, start: int, end: int) -> bool:
    """`text[start:end]` is a whole path, or the head of a longer path under it."""
    before = text[start - 1] if start > 0 else " "
    if before.isalnum() or before in _PATH_CONT:
        return False
    if end >= len(text):
        return True
    after = text[end]
    return after == "/" or not (after.isalnum() or after in _PATH_CONT)


def _glob_regex(pattern: str) -> re.Pattern[str] | None:
    """A shell glob as a regex in which `*` and `?` stay inside one path segment."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            close = pattern.find("]", i + 1)
            if close < 0:
                out.append(re.escape(ch))
            else:
                body = pattern[i + 1 : close].replace("\\", "\\\\")
                out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                i = close
        else:
            out.append(re.escape(ch))
        i += 1
    try:
        return re.compile("".join(out))
    except re.error:
        return None


_STAGE_BREAK = re.compile(r"[;|&\r\n]+")
# A stage that deletes or moves takes a protected path with it when it names any folder
# above one (`rm -rf ~/.loom`), so there an ancestor counts as a reference too. Matched
# on a word's command name and on the last dotted part (`shutil.rmtree`, `os.unlink`).
_DESTRUCTIVE = frozenset(
    {"rm", "rmdir", "mv", "del", "erase", "rd", "move", "remove-item", "ri", "move-item", "mi",
     "rmtree", "removedirs", "unlink", "rimraf", "shred"}
)


def _destructive(words: list[str]) -> bool:
    """The stage deletes or moves: a delete/move command anywhere in it, `git clean`,
    or `find … -delete`."""
    for i, word in enumerate(words):
        name = command_name(word).lower()
        if name in _DESTRUCTIVE or name.rsplit(".", 1)[-1] in _DESTRUCTIVE or word == "-delete":
            return True
        if name == "git" and i + 1 < len(words) and words[i + 1] == "clean":
            return True
    return False


def _ancestors(key: str) -> list[str]:
    parts = key.split("/")
    return ["/".join(parts[:i]) or "/" for i in range(1, len(parts))]


def _hits(target: str, keys: list[str], ancestors: bool = False) -> str | None:
    """The protected key `target` is, or sits under; a glob also hits a key it matches.
    With `ancestors`, a target above a key (or a glob matching a folder above one) hits."""
    if _GLOB.search(target):
        literal = _GLOB.split(target, 1)[0].rsplit("/", 1)[0]
        regex = _glob_regex(target)
        for key in keys:
            if literal == key or literal.startswith(key + "/") or (regex is not None and regex.fullmatch(key)):
                return key
            if ancestors and regex is not None and any(regex.fullmatch(a) for a in _ancestors(key)):
                return key
        return None
    for key in keys:
        if target == key or target.startswith(key + "/"):
            return key
        if ancestors and key.startswith(target.rstrip("/") + "/"):
            return key
    return None


def protected_reference(text: str, protected: Iterable[str] | None, cwd: str | None = None) -> str | None:
    """The first protected path the raw `text` refers to, or None. Needs no parse —
    quotes, heredocs, chains and substitutions are all just text — so there is no
    "cannot tell" answer; the dispatcher reads a raise as one, and holds.

    Either of two readings counts, each needing a protected path (or one under it):
    (1) every absolute spelling of a protected path, searched as a bounded
    substring, which survives quoting and spaces; (2) every word, split on whitespace
    and shell punctuation, resolved like a path against `cwd` and against each
    `cd`/`pushd` target the text names before it, globs included. In a stage that
    deletes or moves (`rm`, `mv`, `Remove-Item`, `rmtree`, `git clean`, …) a folder
    above a protected path counts too: `rm -rf ~/.loom` takes `secrets.json` with it,
    while `ls ~` names nothing. A `$VAR` or
    `%VAR%` set in the environment is expanded; one that is not, an opaque quoted blob
    or a URL names nothing. `cwd=None` skips relative words (a caller whose working
    directory is not the shell's)."""
    originals = {_key(p): p for p in (protected or ()) if p}
    keys = list(originals)
    if not keys or not text:
        return None
    flat = _flatten(text)
    for key in keys:
        for spelling in _spellings(key):
            at = flat.find(spelling)
            while at >= 0:
                if _bounded(flat, at, at + len(spelling)):
                    return originals[key]
                at = flat.find(spelling, at + 1)
    from .fs_guard import normalise

    bases: list[str] = [str(cwd)] if cwd else []
    for stage in _STAGE_BREAK.split(flat):
        words = _WORD_BREAK.split(stage)
        ancestors = _destructive(words)
        prev = ""
        for word in words:
            if not word or word.startswith("-") or "$" in word or "%" in word or "{" in word or _URL.match(word):
                prev = word
                continue
            absolute = word.startswith(("/", "~")) or (len(word) > 1 and word[1] == ":")
            if not absolute and not bases:
                prev = word
                continue
            for base in (bases[-1:] or [None]) if prev in ("cd", "pushd") else (bases or [None]):
                try:
                    target = _key(normalise(word, cwd=base))
                except Exception:
                    continue
                hit = _hits(target, keys, ancestors)
                if hit:
                    return originals[hit]
            if prev in ("cd", "pushd"):
                try:
                    bases.append(normalise(word, cwd=bases[-1] if bases else None))
                except Exception:
                    pass
            prev = word
    return None


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
        if protected_reference(command, protected, cwd):  # also follows `cd` and spellings `names_protected` misses
            return False
        return True
    except Exception:  # a pre-filter that raises would break a turn; doubt means classify
        return False
