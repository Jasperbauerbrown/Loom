"""Vault watcher: polling, because polling is the simplest mechanism that covers every
case (03 §8.8, charter §2.1).

`os.scandir` + mtime/size against the `notes` table, every `knowledge.vault_poll_seconds`.
Thousands of files scan in milliseconds, there is no `watchfiles` dependency, no native
event API differences between Windows/macOS/Linux, and an edit made while the backend
was down is caught by the same code as an edit made while it runs.

Two things happen on a change: the index is updated and `knowledge.changed` is emitted
so open UI views refresh. A newly appeared, unapproved `procedures/<name>/SKILL.md` also
posts one *setup* item — third-party skills may carry code, so the user approves them
(03 §4.3).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from .. import db, items

log = logging.getLogger(__name__)

_SKIP_DIRS = {".git", ".obsidian", "__pycache__", ".trash"}


class Watcher:
    def __init__(self, app: Any):
        self.app = app
        self.running = False
        self._announced: set[str] = set()  # procedure names already on the now board

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.app.spawn("vault-watcher", self._loop())

    def stop(self) -> None:
        self.running = False
        task = self.app.tasks.get("vault-watcher")
        if task is not None and not task.done():
            task.cancel()

    async def _loop(self) -> None:
        while self.running and not self.app.shutting_down:
            try:
                interval = float(self.app.settings.get("knowledge.vault_poll_seconds"))
            except Exception:
                interval = 2.0
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                self.scan_once()
            except asyncio.CancelledError:
                return
            except Exception:  # a scan failure must never end the watcher
                log.exception("vault watcher: scan failed")

    # -- scanning ---------------------------------------------------------------
    def snapshot(self) -> dict[str, tuple[float, int]]:
        """Every markdown file in the vault → (mtime, size)."""
        root = Path(self.app.vault_dir)
        out: dict[str, tuple[float, int]] = {}

        def walk(directory: Path, rel: str) -> None:
            try:
                entries = list(os.scandir(directory))
            except OSError:
                return
            for entry in entries:
                if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                    continue
                child = f"{rel}/{entry.name}" if rel else entry.name
                try:
                    if entry.is_dir():
                        walk(Path(entry.path), child)
                    elif entry.name.endswith(".md"):
                        stat = entry.stat()
                        out[child] = (stat.st_mtime, stat.st_size)
                except OSError:
                    continue

        walk(root, "")
        return out

    def scan_once(self) -> list[str]:
        """One poll: index what changed, announce it, return the changed paths."""
        current = self.snapshot()
        known = {
            row["path"]: (row["mtime"], row["size"])
            for row in self.app.db.execute("SELECT path, mtime, size FROM notes")
        }
        changed: list[str] = []
        for path, (mtime, size) in current.items():
            before = known.get(path)
            if before is None or abs(before[0] - mtime) > 0.001 or before[1] != size:
                changed.append(path)
        removed = [p for p in known if p not in current]
        touched = sorted(changed + removed)
        if not touched:
            return []
        self.app.index.update(touched)
        for path in changed:
            self._maybe_announce_procedure(path)
        self.app.bus.emit("knowledge.changed", {"paths": touched})
        return touched

    # -- procedure approval (03 §4.3) -------------------------------------------
    def _maybe_announce_procedure(self, path: str) -> None:
        if not (path.startswith("procedures/") and path.endswith("/SKILL.md")):
            return
        name = path.split("/")[1]
        if name in self._announced:
            return
        row = self.app.vault.find_procedure(name)
        if row is None or row["approved"]:
            return
        self._announced.add(name)
        if self._already_posted(name):
            return
        items.post(
            self.app,
            "setup",
            f"New procedure '{row['name']}' needs approval",
            (row["description"] or "").strip()
            or "A procedure appeared in the vault from outside Loom. Procedures may carry "
            "scripts, so it stays unloadable until you approve it.",
            source=f"vault:{path}",
            action={"kind": "approve_procedure", "name": row["name"], "path": path},
        )

    def _already_posted(self, name: str) -> bool:
        for row in self.app.db.execute(
            "SELECT action FROM items WHERE kind='setup' AND dismissed_at IS NULL"
        ):
            action = db.loads(row["action"], {}) or {}
            if action.get("kind") == "approve_procedure" and action.get("name") == name:
                return True
        return False
