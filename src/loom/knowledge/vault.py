"""The vault: markdown truth (03 §4.3, charter §5.4).

`APP_HOME/vault/` is a git repository of Obsidian-compatible notes. The folder is the
entity type, frontmatter carries title/aliases/type, `[[wikilinks]]` carry the graph.
The user may edit any of it in Obsidian at any moment; the SQLite index is a cache
rebuilt from these files, never the other way round.

Everything that mutates the vault goes through `Vault` so that one set of rules holds
for every writer (model, user, maintenance): frontmatter is re-emitted, `modified` is
stamped, aliases are lower-cased and de-duplicated, relative dates are refused, the
write is atomic, git records it, the index is updated and `knowledge.changed` is
emitted. Path arguments are vault-relative; `..` and absolute paths are refused here, so
tools that take vault paths need no filesystem guard.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .. import db, items, prompts
from . import notes
from .git import Git

log = logging.getLogger(__name__)

# Folder → note type. A folder not listed keeps its own name as the type.
FOLDER_TYPES: dict[str, str] = {
    "core": "core",
    "devices": "device",
    "projects": "project",
    "people": "person",
    "connectors": "connector",
    "courses": "course",
    "topics": "topic",
    "procedures": "procedure",
    "log": "log",
}

TYPE_FOLDERS: tuple[str, ...] = (
    "core",
    "devices",
    "projects",
    "people",
    "connectors",
    "courses",
    "topics",
    "procedures",
    "log",
)

CORE_NOTES: dict[str, str] = {
    "core/about-me.md": "core-about-me",
    "core/preferences.md": "core-preferences",
    "core/conventions.md": "core-conventions",
}

GITIGNORE = """# Loom vault
.obsidian/workspace*
.obsidian/cache/
"""

OPS: tuple[str, ...] = ("create", "append", "replace", "rethink")

_SKIP_DIRS = {".git", ".obsidian", "__pycache__", ".trash"}
_PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")


class VaultError(Exception):
    """An expected, explainable failure: it becomes a tool error result verbatim."""


@dataclass
class Note:
    path: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    raw: str = ""
    title: str = ""
    type: str = ""
    aliases: list[str] = field(default_factory=list)
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "frontmatter": self.frontmatter,
            "body": self.body,
            "raw": self.raw,
            "title": self.title,
            "type": self.type,
            "aliases": self.aliases,
            "error": self.error,
        }


def folder_type(path: str) -> str:
    parts = PurePosixPath(path).parts
    top = parts[0] if parts else ""
    return FOLDER_TYPES.get(top, top or "note")


def clean_aliases(values: Any) -> list[str]:
    """Lower-cased, whitespace-collapsed, de-duplicated, order preserved (03 §4.3)."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value)).strip().lower()
        if text and text not in out:
            out.append(text)
    return out


class Vault:
    def __init__(self, app: Any):
        self.app = app
        self.root: Path = Path(app.vault_dir)
        self.git = Git(self.root)

    # -- paths ------------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        """Vault-relative path → absolute path inside the vault. Anything that escapes
        the vault is refused (this is the guard for every vault-path tool argument)."""
        raw = str(path or "").strip().replace("\\", "/")
        if not raw:
            raise VaultError("path is required")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            raise VaultError(f"path must be vault-relative, not absolute: {path}")
        parts = [p for p in PurePosixPath(raw).parts if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise VaultError(f"path must stay inside the vault: {path}")
        if not parts:
            raise VaultError("path is required")
        return self.root.joinpath(*parts)

    def rel(self, path: Path) -> str:
        p = Path(path)
        if not p.is_absolute():
            return p.as_posix()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return Path(os.path.relpath(p, self.root)).as_posix()

    def norm(self, path: str) -> str:
        """The canonical vault-relative spelling of `path` (validated)."""
        return self.rel(self.resolve(path))

    def exists(self, path: str) -> bool:
        try:
            return self.resolve(path).is_file()
        except VaultError:
            return False

    # -- bootstrap --------------------------------------------------------------
    def bootstrap(self) -> None:
        """Idempotent: git repo, type folders, core notes, .gitignore, first commit."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.git.init()
        except Exception as exc:  # git missing: the vault still works, history does not
            log.warning("vault: git init failed (%s); continuing without history", exc)
        for folder in TYPE_FOLDERS:
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(GITIGNORE, encoding="utf-8")
        for path, template in CORE_NOTES.items():
            target = self.resolve(path)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(self.template(template), encoding="utf-8")
        try:
            self.git.commit("loom: bootstrap vault")
        except Exception as exc:
            log.warning("vault: bootstrap commit failed (%s)", exc)

    def template(self, name: str, **values: Any) -> str:
        """Load `prompts/templates/<name>.md` and fill `{{placeholders}}`; unknown
        placeholders resolve to an empty string rather than leaking braces."""
        text = prompts.load(f"templates/{name}")
        return _PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), "")), text) + "\n"

    # -- reading ----------------------------------------------------------------
    def read(self, path: str) -> Note:
        target = self.resolve(path)
        rel = self.norm(path)
        if not target.is_file():
            raise VaultError(f"no such note: {rel}")
        raw = target.read_text(encoding="utf-8", errors="replace")
        return self._note(rel, raw)

    def _note(self, rel: str, raw: str) -> Note:
        parsed = notes.parse(raw)
        fm = parsed.frontmatter
        title = str(fm.get("title") or fm.get("name") or notes.title_from_path(rel))
        ntype = "invalid" if parsed.error else str(fm.get("type") or folder_type(rel))
        return Note(
            path=rel,
            frontmatter=fm,
            body=parsed.body,
            raw=raw,
            title=title,
            type=ntype,
            aliases=clean_aliases(fm.get("aliases")),
            error=parsed.error,
        )

    def list(self, prefix: str = "", suffix: str = ".md") -> list[str]:
        """Vault-relative paths under `prefix`, sorted. Hidden folders are skipped."""
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if suffix and not name.endswith(suffix):
                    continue
                rel = Path(dirpath, name).relative_to(self.root).as_posix()
                if prefix and not rel.startswith(prefix):
                    continue
                out.append(rel)
        return sorted(out)

    def tree(self) -> dict[str, Any]:
        """Nested `{name, path, type: dir|note, children}` for the vault browser."""

        def walk(directory: Path, rel: str) -> dict[str, Any]:
            children: list[dict[str, Any]] = []
            try:
                entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                entries = []
            for entry in entries:
                if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                    continue
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                if entry.is_dir():
                    children.append(walk(entry, child_rel))
                elif entry.suffix == ".md":
                    children.append({"name": entry.name, "path": child_rel, "type": "note", "children": []})
            return {"name": directory.name if rel else "vault", "path": rel, "type": "dir", "children": children}

        return walk(self.root, "")

    # -- writing ----------------------------------------------------------------
    def write(
        self,
        path: str,
        content: str,
        *,
        op: str = "create",
        old: str | None = None,
        frontmatter: dict[str, Any] | None = None,
        reason: str | None = None,
        actor: str = "loom",
    ) -> Note:
        """The one write path for notes (03 §4.3, §8.4).

        `create` refuses an existing note; `append` adds a blank line then the content;
        `replace` swaps a unique `old` snippet; `rethink` rewrites the body and requires
        a `reason`, which lands on the now board. Relative dates are refused. The write
        is atomic, committed, indexed and announced."""
        if op not in OPS:
            raise VaultError(f"unknown op {op!r}; use one of {', '.join(OPS)}")
        target = self.resolve(path)
        rel = self.norm(path)
        content = content or ""
        # A body that opens with its own frontmatter block — the model copying the shape
        # it reads back — is folded in rather than nested: its keys merge beneath the
        # explicit `frontmatter` argument and the body starts after it (2026-09-21, two
        # live notes ended up with two blocks).
        embedded = notes.parse(content)
        if embedded.frontmatter and not embedded.error:
            content = embedded.body.lstrip("\n")
            frontmatter = {**{k: v for k, v in embedded.frontmatter.items() if k != "modified"}, **(frontmatter or {})}
        exists = target.is_file()

        if op == "create" and exists:
            raise VaultError(f"{rel} already exists; use append, replace or rethink")
        if op != "create" and not exists:
            raise VaultError(f"{rel} does not exist; use create")
        if op == "rethink" and not (reason or "").strip():
            raise VaultError("rethink requires a reason (what was wrong with the old note)")

        current = self._note(rel, target.read_text(encoding="utf-8", errors="replace")) if exists else None
        old_body = current.body if current else ""

        if op == "create":
            body = content
        elif op == "append":
            body = f"{old_body.rstrip()}\n\n{content.strip()}\n" if old_body.strip() else content
        elif op == "replace":
            if old is None or old == "":
                raise VaultError("replace requires `old`: the exact text to swap out")
            found = old_body.count(old)
            if found == 0:
                raise VaultError(f"`old` not found in {rel}; read the note and copy the text exactly")
            if found > 1:
                raise VaultError(f"`old` appears {found} times in {rel}; include more context to make it unique")
            body = old_body.replace(old, content, 1)
        else:  # rethink
            body = content

        offender = notes.has_relative_dates(content)
        if offender:
            raise VaultError(f"use absolute dates: found '{offender}'")

        merged = dict(current.frontmatter) if current else {}
        for key, value in (frontmatter or {}).items():
            merged[key] = value
        self._stamp(rel, merged, actor)

        self._atomic_write(target, notes.emit(merged, body))
        self._after_write(rel, f"{actor}: {op} {rel}")

        if op == "rethink":
            items.post(
                self.app,
                "knowledge",
                f"Rewrote {rel}",
                reason or "",
                source="knowledge_write",
                action={"kind": "open_note", "path": rel},
            )
        return self.read(rel)

    def put_raw(self, path: str, text: str, actor: str = "user") -> Note:
        """Save a note exactly as edited (vault UI, Obsidian-style editing). Frontmatter
        is re-parsed and `modified` re-stamped; a malformed note is still saved — the
        index records it as invalid and the UI shows the error."""
        target = self.resolve(path)
        rel = self.norm(path)
        parsed = notes.parse(text or "")
        if parsed.error:
            body = text or ""
            out = body
        else:
            fm = dict(parsed.frontmatter)
            self._stamp(rel, fm, actor)
            out = notes.emit(fm, parsed.body)
        self._atomic_write(target, out)
        self._after_write(rel, f"{actor}: edit {rel}")
        return self.read(rel)

    def delete(self, path: str, actor: str = "user") -> str:
        target = self.resolve(path)
        rel = self.norm(path)
        if not target.is_file():
            raise VaultError(f"no such note: {rel}")
        target.unlink()
        try:
            self.git.commit(f"{actor}: delete {rel}", [rel])
        except Exception as exc:
            log.warning("vault: commit failed for %s (%s)", rel, exc)
        if self.app.index is not None:
            self.app.index.update([rel])
        self.app.bus.emit("knowledge.changed", {"paths": [rel]})
        return rel

    def delete_folder(self, path: str, actor: str = "user") -> list[str]:
        """Remove a folder and everything under it, as one git commit (revertible from
        Recent changes). `core/` is refused: those notes are always in context and the
        bootstrap would only recreate them. Type folders may go; bootstrap recreates the
        empty skeleton on the next start."""
        import shutil

        target = self.resolve(path)
        rel = self.norm(path).strip("/")
        if not rel or rel == ".":
            raise VaultError("refusing to delete the vault root")
        if rel == "core" or rel.startswith("core/"):
            raise VaultError("core/ holds the always-in-context notes; edit them instead of deleting the folder")
        if not target.is_dir():
            raise VaultError(f"no such folder: {rel}")
        removed = sorted(
            self.rel(p) for p in target.rglob("*") if p.is_file() and ".git" not in p.parts
        )
        shutil.rmtree(target)
        try:
            self.git.commit(f"{actor}: delete folder {rel}/ ({len(removed)} files)", None)
        except Exception as exc:
            log.warning("vault: commit failed for %s/ (%s)", rel, exc)
        if self.app.index is not None and removed:
            self.app.index.update(removed)
        self.app.bus.emit("knowledge.changed", {"paths": removed or [rel]})
        return removed

    def _stamp(self, rel: str, fm: dict[str, Any], actor: str) -> None:
        """Harness-owned frontmatter: title, type, aliases, modified (03 §4.3)."""
        fm["title"] = str(fm.get("title") or notes.title_from_path(rel))
        fm["type"] = str(fm.get("type") or folder_type(rel))
        aliases = clean_aliases(fm.get("aliases"))
        if aliases or "aliases" in fm:
            fm["aliases"] = aliases
        if fm["type"] == "procedure" and rel.endswith("/SKILL.md"):
            fm.setdefault("name", PurePosixPath(rel).parent.name)
            fm.setdefault("description", "")
            # A procedure Loom wrote itself needs no approval gate: the code in it is
            # Loom's own (03 §4.3 — approval exists for third-party skills).
            if actor == "loom" and not fm.get("approved"):
                fm["approved"] = db.now_iso()
        fm["modified"] = db.now_iso()

    def _atomic_write(self, target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".loom-tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, target)

    def _after_write(self, rel: str, message: str) -> None:
        try:
            self.git.commit(message, [rel])
        except Exception as exc:
            log.warning("vault: commit failed for %s (%s)", rel, exc)
        if self.app.index is not None:
            self.app.index.update([rel])
        self.app.bus.emit("knowledge.changed", {"paths": [rel]})

    # -- status block -----------------------------------------------------------
    def read_status(self, path: str) -> tuple[dict[str, str] | None, str | None]:
        return notes.parse_status(self.read(path).body)

    def write_status(self, path: str, owner: str, fields: dict[str, Any]) -> Note:
        """Merge `fields` into the note's status block and stamp `updated`. The block is
        the thread's plan (charter §5.3); the user may edit it in place between turns."""
        note = self.read(path)
        current, existing_owner = notes.parse_status(note.body)
        merged: dict[str, Any] = dict(current or {})
        for key, value in (fields or {}).items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        merged["updated"] = db.now_iso()
        body = notes.replace_status(note.body, owner or existing_owner or "", merged)
        fm = dict(note.frontmatter)
        self._stamp(note.path, fm, "loom")
        self._atomic_write(self.resolve(note.path), notes.emit(fm, body))
        self._after_write(note.path, f"loom: status {note.path}")
        return self.read(note.path)

    # -- procedures (Agent Skills; 03 §4.3) -------------------------------------
    def procedure_path(self, name: str) -> str:
        return f"procedures/{notes.slug(name)}/SKILL.md"

    def procedures(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        root = self.root / "procedures"
        if not root.is_dir():
            return out
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            skill = entry / "SKILL.md"
            if not entry.is_dir() or not skill.is_file():
                continue
            rel = self.rel(skill)
            try:
                note = self.read(rel)
            except VaultError:
                continue
            fm = note.frontmatter
            out.append(
                {
                    "name": str(fm.get("name") or entry.name),
                    "description": str(fm.get("description") or ""),
                    "path": rel,
                    "approved": bool(fm.get("approved")),
                    "approved_at": fm.get("approved") if isinstance(fm.get("approved"), str) else None,
                    "error": note.error,
                }
            )
        return out

    def find_procedure(self, name: str) -> dict[str, Any] | None:
        wanted = notes.normalise_alias(name)
        for row in self.procedures():
            if notes.normalise_alias(row["name"]) == wanted or notes.normalise_alias(
                PurePosixPath(row["path"]).parent.name
            ) == wanted:
                return row
        return None

    def approve_procedure(self, name: str) -> dict[str, Any]:
        row = self.find_procedure(name)
        if row is None:
            raise VaultError(f"no procedure named {name!r}")
        note = self.read(row["path"])
        fm = dict(note.frontmatter)
        fm["approved"] = db.now_iso()
        self._stamp(note.path, fm, "user")
        self._atomic_write(self.resolve(note.path), notes.emit(fm, note.body))
        self._after_write(note.path, f"user: approve procedure {row['name']}")
        out = self.find_procedure(name)
        return out if out is not None else row

    def load_procedure(self, name: str) -> str:
        row = self.find_procedure(name)
        if row is None:
            known = ", ".join(p["name"] for p in self.procedures()) or "none"
            raise VaultError(f"no procedure named {name!r} (have: {known})")
        if not row["approved"]:
            raise VaultError(
                f"procedure {row['name']!r} is not approved yet; it was added from outside Loom "
                "and the user must approve it on the now board before it can be loaded"
            )
        return self.read(row["path"]).body.strip()

    # -- entity helpers ---------------------------------------------------------
    def ensure_project_note(self, name: str, root: str | None = None, aliases: list[str] | None = None) -> str:
        """The project note a thread scopes to (03 §6.7). Idempotent: an existing note
        with this slug is returned untouched. `aliases` are the names people will use
        for it ("3d map", "map project"); the project name is always one."""
        stem = notes.slug(name)
        path = f"projects/{stem}.md"
        if self.exists(path):
            return path
        folder = root or str(self.app.settings.projects_root() / stem)
        text = self.template("project", title=name, alias=name.strip().lower(), root=str(folder).replace("\\", "/"))
        parsed = notes.parse(text)
        fm = dict(parsed.frontmatter)
        fm["aliases"] = clean_aliases([name, *(aliases or [])])
        fm["root"] = str(folder).replace("\\", "/")
        self.write(path, parsed.body, op="create", frontmatter=fm, actor="loom")
        return path

    def ensure_note(self, path: str, template: str, **values: Any) -> str:
        """Create a note from a template if it is missing; return its path."""
        if self.exists(path):
            return self.norm(path)
        text = self.template(template, **values)
        parsed = notes.parse(text)
        self.write(path, parsed.body, op="create", frontmatter=dict(parsed.frontmatter), actor="loom")
        return self.norm(path)
