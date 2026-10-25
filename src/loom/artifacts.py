"""Artifact spill and recall (03 §4.4).

Tool output longer than `conversation.tool_output_cap` is written whole to
`artifacts/<conversation>/<event>.txt` and replaced in the transcript by head + tail +
a pointer line. The full text stays reachable through `artifact_recall(id, offset,
limit)` for as long as the conversation exists. Maintenance prunes (2026-10-05): an
archived conversation's scratch after `conversation.scratch_archived_grace_days`, its
whole folder after `conversation.artifact_retention_days`, and a live conversation's
scratch untouched for `conversation.scratch_idle_days` (see `Store.prune`).

`bound` is the only entry point the dispatcher uses: it decides, spills, and returns the
text to persist.

Images (2026-09-21) live beside the spills as `artifacts/<conversation>/img_<ulid><ext>`
with no table row: the transcript keeps only `{"id", "mime"}` refs on event meta, data
URLs are built at context-assembly time, and every chars/4 estimate stays blind to
base64. Write-before-reference: the file lands before any event points at it.

Voice clips (2026-10-24, `voice/store.py`) are stored the same way as
`artifacts/<conversation>/audio_<ulid>.<ext>`. The walker keeps them like images, and a
fourth rule deletes them after `voice.audio_retention_days` in every folder, archived
or not.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

from . import db
from .ids import ulid

log = logging.getLogger(__name__)

ELLIPSIS = "\n…\n"

IMAGE_MIMES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
_EXT_TO_MIME = {ext: mime for mime, ext in IMAGE_MIMES.items()}
# `img_` + a 26-char Crockford ULID: the strict shape doubles as the traversal guard.
IMAGE_ID_RE = re.compile(r"^img_[0-9A-HJKMNP-TV-Z]{26}$")
AUDIO_PREFIX = "audio_"  # voice clips (voice/store.py); kept like images until their own retention


def sniff_image(data: bytes) -> str | None:
    """The mime of a supported image by its magic bytes, else None."""
    head = bytes(data[:16])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) for PNG, JPEG and GIF from their headers; None when unknown."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = int.from_bytes(data[i + 2 : i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(data[i + 5 : i + 7], "big")
                i += 2 + length
    except Exception:  # a malformed header is not worth a crash: dimensions are decoration
        return None
    return None


class Store:
    """`app.artifacts`."""

    def __init__(self, app: Any):
        self.app = app

    @property
    def root(self) -> Path:
        return Path(self.app.artifacts_dir)

    # -- write ------------------------------------------------------------------
    def spill(self, conversation_id: str, event_id: int | None, text: str) -> dict[str, Any]:
        """Write `text` to disk, insert the row, return it. Persist before act: the file
        lands first, then the row that points at it."""
        artifact_id = ulid()
        folder = self.root / conversation_id
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{event_id}.txt" if event_id is not None else f"{artifact_id}.txt"
        path = folder / name
        if path.exists():  # several calls in one tool_call event
            path = folder / f"{artifact_id}.txt"
        path.write_text(text, encoding="utf-8")

        head_n = int(self.app.settings.get("conversation.artifact_head_chars"))
        tail_n = int(self.app.settings.get("conversation.artifact_tail_chars"))
        row = {
            "id": artifact_id,
            "conversation_id": conversation_id,
            "event_id": event_id,
            "path": str(path).replace("\\", "/"),
            "bytes": len(text.encode("utf-8")),
            "head": text[:head_n],
            "tail": text[-tail_n:] if tail_n else "",
            "created_at": db.now_iso(),
        }
        self.app.db.execute(
            "INSERT INTO artifacts(id, conversation_id, event_id, path, bytes, head, tail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["conversation_id"], row["event_id"], row["path"],
                row["bytes"], row["head"], row["tail"], row["created_at"],
            ),
        )
        return row

    def bound(self, conversation_id: str, event_id: int | None, text: str) -> tuple[str, str | None]:
        """(text_to_persist, artifact_id_or_None). Under the cap nothing is written."""
        text = text if isinstance(text, str) else str(text)
        cap = int(self.app.settings.get("conversation.tool_output_cap"))
        if len(text) <= cap:
            return text, None
        try:
            row = self.spill(conversation_id, event_id, text)
        except OSError as exc:  # disk full, bad path: truncate visibly rather than fail
            log.warning("artifact spill failed (%s); truncating instead", exc)
            return text[:cap] + f"\n[output truncated at {cap} chars; artifact spill failed: {exc}]", None
        pointer = (
            f"[artifact {row['id']}: {len(text)} chars — "
            f"artifact_recall(id=\"{row['id']}\", offset, limit) for more]"
        )
        return f"{row['head']}{ELLIPSIS}{row['tail']}\n{pointer}", row["id"]

    # -- images -----------------------------------------------------------------
    def image_cap_bytes(self) -> int:
        return int(self.app.settings.get("conversation.image_max_mb")) * 1024 * 1024

    def _image_folder(self, conversation_id: str) -> Path:
        return conversation_dir(self.app, conversation_id)

    def store_image(self, conversation_id: str, data: bytes, mime: str) -> dict[str, Any]:
        """Persist image bytes; returns `{"id", "mime", "bytes"}`. `ValueError` for an
        unsupported type or a size over `conversation.image_max_mb`. The file exists
        before the caller writes any event that refers to it."""
        ext = IMAGE_MIMES.get(str(mime or "").lower())
        if ext is None:
            raise ValueError(f"unsupported image type {mime!r} (supported: {', '.join(sorted(IMAGE_MIMES))})")
        cap = self.image_cap_bytes()
        if len(data) > cap:
            raise ValueError(
                f"image is {len(data)} bytes, over the {cap // (1024 * 1024)} MB cap (conversation.image_max_mb)"
            )
        folder = self._image_folder(conversation_id)
        folder.mkdir(parents=True, exist_ok=True)
        image_id = f"img_{ulid()}"
        (folder / f"{image_id}{ext}").write_bytes(bytes(data))
        return {"id": image_id, "mime": str(mime).lower(), "bytes": len(data)}

    def find_image(self, conversation_id: str, image_id: str) -> tuple[Path, str] | None:
        """`(path, mime)` for a stored image, or None for a bad id shape or a missing file."""
        if not IMAGE_ID_RE.match(str(image_id or "")):
            return None
        try:
            folder = self._image_folder(conversation_id)
        except ValueError:
            return None
        for ext, mime in _EXT_TO_MIME.items():
            path = folder / f"{image_id}{ext}"
            if path.is_file():
                return path, mime
        return None

    def read_image(self, conversation_id: str, image_id: str) -> tuple[bytes, str] | None:
        found = self.find_image(conversation_id, image_id)
        if found is None:
            return None
        path, mime = found
        try:
            return path.read_bytes(), mime
        except OSError as exc:
            log.warning("image %s unreadable: %s", image_id, exc)
            return None

    def image_data_url(self, conversation_id: str, image_id: str) -> str | None:
        """The model-facing wire form: `data:<mime>;base64,…`, or None on any miss."""
        got = self.read_image(conversation_id, image_id)
        if got is None:
            return None
        data, mime = got
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    # -- spawn-tree scoping -------------------------------------------------------
    def visible_ids(self, conversation_id: str) -> list[str]:
        """`conversation_id` and every conversation below it in the spawn tree (its
        threads, their sub-threads, …). Never a sibling and never an ancestor: evidence
        flows up to the conversation that spawned the work, not sideways."""
        rows = self.app.db.execute(
            "WITH RECURSIVE tree(id) AS ("
            "  SELECT ? "
            "  UNION "
            "  SELECT c.id FROM conversations c JOIN tree t ON c.parent_id = t.id"
            ") SELECT id FROM tree",
            (conversation_id,),
        ).fetchall()
        return [r["id"] for r in rows]

    def read_in_tree(self, conversation_id: str, artifact_id: str, offset: int = 0, limit: int | None = None) -> str:
        """`read()` when the artifact belongs to `conversation_id` or a conversation below
        it in the spawn tree; otherwise the same shape of error string as `read()`."""
        row = self.get(artifact_id)
        if row is None or row["conversation_id"] not in self.visible_ids(conversation_id):
            return f"error: no artifact {artifact_id} in this conversation or a thread it spawned"
        return self.read(artifact_id, offset, limit)

    def find_image_in_tree(self, conversation_id: str, image_id: str) -> tuple[Path, str] | None:
        """`(path, mime)` for an image stored by `conversation_id` or a conversation below
        it in the spawn tree, else None."""
        if not IMAGE_ID_RE.match(str(image_id or "")):
            return None
        for cid in self.visible_ids(conversation_id):
            found = self.find_image(cid, image_id)
            if found is not None:
                return found
        return None

    # -- read -------------------------------------------------------------------
    def get(self, artifact_id: str) -> dict[str, Any] | None:
        return db.row_to_dict(
            self.app.db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        )

    def list(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = self.app.db.execute(
            "SELECT * FROM artifacts WHERE conversation_id=? ORDER BY id", (conversation_id,)
        ).fetchall()
        return [db.row_to_dict(r) for r in rows]  # type: ignore[misc]

    def text(self, artifact_id: str) -> str | None:
        row = self.get(artifact_id)
        if row is None:
            return None
        try:
            return Path(row["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("artifact %s unreadable: %s", artifact_id, exc)
            return None

    def page_cap(self) -> int:
        """The page size every paged tool uses: `conversation.tool_output_cap`."""
        return int(self.app.settings.get("conversation.tool_output_cap"))

    def read(self, artifact_id: str, offset: int = 0, limit: int | None = None) -> str:
        """A character slice with a paging footer. The page is at most `tool_output_cap`
        characters (the default, and the clamp for any larger `limit`); the footer rides
        on top, and the dispatcher never spills a paged result. Missing artifact → an
        error string; the tool turns it into an error result."""
        row = self.get(artifact_id)
        if row is None:
            return f"error: no artifact {artifact_id}"
        content = self.text(artifact_id)
        if content is None:
            return f"error: artifact {artifact_id} file is missing ({row['path']})"
        total = len(content)
        offset = max(0, int(offset))
        cap = self.page_cap()
        limit = cap if limit is None else max(1, min(int(limit), cap))
        chunk = content[offset : offset + limit]
        end = offset + len(chunk)
        if offset >= total and total:
            return f"[artifact {artifact_id}: offset {offset} is past the end ({total} chars)]"
        if end < total:
            chunk += (
                f"\n[chars {offset}–{end} of {total}; "
                f"artifact_recall(id=\"{artifact_id}\", offset={end}) for more]"
            )
        return chunk

    # -- maintenance ------------------------------------------------------------
    # Three rules, one walker (2026-10-05), plus the audio rule (2026-10-24). Spills and process logs (referenced by rows)
    # and `img_*` (referenced by event meta) are a conversation's real artifacts;
    # everything else in its folder is scratch nothing reads back (TMPDIR, downloads,
    # browser profiles).
    #   archived past `artifact_retention_days`     -> the whole folder and its spill rows;
    #   archived past `scratch_archived_grace_days` -> everything but the real artifacts;
    #   live (the desk included), `scratch_idle_days` > 0 -> scratch with an older mtime;
    #   every folder not purged                     -> top-level `audio_*` older than
    #                                                  `voice.audio_retention_days`.
    # SQLite stays on the calling thread (`_plan`); only the file walk (`_sweep`) may
    # run in a worker thread (`aprune`).
    def prune(self) -> dict[str, int]:
        """Run the rules synchronously. Returns `{"artifacts", "files", "bytes", "failed",
        "audio"}`: spill rows dropped plus images deleted (the historical count), every
        file removed, their bytes, entries that could not be removed, and voice clips
        deleted by their retention (also counted in `files`/`bytes`)."""
        return self._sweep(self._plan())

    async def aprune(self) -> dict[str, int]:
        """`prune()` with the file walk off the event loop."""
        plan = self._plan()
        return await asyncio.to_thread(self._sweep, plan)

    def _plan(self) -> dict[str, Any]:
        settings = self.app.settings
        retention = int(settings.get("conversation.artifact_retention_days"))
        grace = int(settings.get("conversation.scratch_archived_grace_days"))
        idle = int(settings.get("conversation.scratch_idle_days"))
        audio_days = int(settings.get("voice.audio_retention_days"))
        now = _dt.datetime.now(_dt.timezone.utc)
        purge_cut = _iso(now - _dt.timedelta(days=retention))
        grace_cut = _iso(now - _dt.timedelta(days=grace))

        modes: dict[str, str] = {}
        for row in self.app.db.execute("SELECT id, status, archived_at FROM conversations").fetchall():
            archived_at = row["archived_at"]
            if row["status"] != "archived":
                modes[row["id"]] = "idle"
            elif archived_at and archived_at < purge_cut:
                modes[row["id"]] = "purge"
            elif archived_at and archived_at <= grace_cut:
                modes[row["id"]] = "scratch"
            else:
                modes[row["id"]] = "skip"  # inside the grace period: untouched

        keep: dict[str, set[str]] = {}
        for row in self.app.db.execute("SELECT conversation_id, path FROM artifacts").fetchall():
            keep.setdefault(row["conversation_id"], set()).add(Path(row["path"]).name)
        for row in self.app.db.execute(
            "SELECT conversation_id, log_path FROM processes WHERE log_path IS NOT NULL"
        ).fetchall():
            keep.setdefault(row["conversation_id"], set()).add(Path(row["log_path"]).name)

        # Persist before act: the rows go first; a crash mid-walk leaves only files the
        # next pass removes (the folder sweep is keyed on archived_at, not on rows).
        rows_dropped = 0
        for cid, mode in modes.items():
            if mode == "purge":
                cur = self.app.db.execute("DELETE FROM artifacts WHERE conversation_id=?", (cid,))
                rows_dropped += max(0, cur.rowcount or 0)
        return {
            "root": self.root,
            "modes": modes,
            "keep": keep,
            "idle_cutoff": (now - _dt.timedelta(days=idle)).timestamp() if idle > 0 else None,
            "audio_cutoff": (now - _dt.timedelta(days=audio_days)).timestamp(),
            "rows_dropped": rows_dropped,
        }

    @staticmethod
    def _sweep(plan: dict[str, Any]) -> dict[str, int]:
        stats = {"artifacts": int(plan["rows_dropped"]), "files": 0, "bytes": 0, "failed": 0, "audio": 0}
        try:
            folders = [e for e in os.scandir(plan["root"]) if not _is_link(e) and e.is_dir(follow_symlinks=False)]
        except OSError as exc:
            log.warning("artifacts root unreadable: %s", exc)
            return stats
        for entry in folders:
            # A folder with no conversation row (a spawn that failed after writing) is
            # swept like a live one: its scratch ages out, nothing references it.
            mode = plan["modes"].get(entry.name, "idle")
            folder = Path(entry.path)
            keep = frozenset(plan["keep"].get(entry.name, ()))
            if mode == "purge":
                _walk(folder, stats, keep=frozenset(), keep_images=False, cutoff=None, top=True)
                if not _remove(os.rmdir, folder):
                    stats["failed"] += 1
                continue
            _prune_audio(folder, stats, plan["audio_cutoff"])
            if mode == "scratch":
                _walk(folder, stats, keep=keep, keep_images=True, cutoff=None, top=True)
            elif mode == "idle" and plan["idle_cutoff"] is not None:
                _walk(folder, stats, keep=keep, keep_images=True, cutoff=plan["idle_cutoff"], top=True)
        return stats

    # -- disk usage ---------------------------------------------------------------
    def usage(self, limit: int = 10) -> dict[str, Any]:
        """Synchronous `ausage()`."""
        return self._usage_finish(_usage_walk(self.root), limit)

    async def ausage(self, limit: int = 10) -> dict[str, Any]:
        """`{"total_bytes", "total_files", "folders", "conversations": [{"id", "title",
        "kind", "status", "bytes", "files"}]}`: the whole artifacts folder and its
        `limit` largest conversation folders, with titles. The walk runs off the loop."""
        sizes = await asyncio.to_thread(_usage_walk, self.root)
        return self._usage_finish(sizes, limit)

    def _usage_finish(self, sizes: dict[str, tuple[int, int]], limit: int) -> dict[str, Any]:
        largest = sorted(sizes.items(), key=lambda kv: kv[1][0], reverse=True)[: max(0, int(limit))]
        rows = []
        for cid, (nbytes, nfiles) in largest:
            conv = self.app.db.execute("SELECT title, kind, status FROM conversations WHERE id=?", (cid,)).fetchone()
            rows.append({
                "id": cid,
                "title": conv["title"] if conv else None,
                "kind": conv["kind"] if conv else None,
                "status": conv["status"] if conv else None,
                "bytes": nbytes,
                "files": nfiles,
            })
        return {
            "total_bytes": sum(b for b, _ in sizes.values()),
            "total_files": sum(f for _, f in sizes.values()),
            "folders": len(sizes),
            "conversations": rows,
        }


def _iso(moment: _dt.datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_link(entry: os.DirEntry) -> bool:
    """A symlink or (Windows) junction: never followed, only ever unlinked itself."""
    try:
        return entry.is_symlink() or entry.is_junction()
    except OSError:
        return True


def _remove(fn: Any, path: str | Path) -> bool:
    """`fn(path)` (unlink or rmdir); a read-only entry is made writable and retried once.
    False when it still cannot go (locked by a running program, permissions)."""
    try:
        fn(path)
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            fn(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False
    except OSError:
        return False


def _prune_audio(folder: Path, stats: dict[str, int], cutoff: float) -> None:
    """Delete the folder's top-level voice clips whose mtime is older than `cutoff`."""
    try:
        entries = [e for e in os.scandir(folder) if e.name.startswith(AUDIO_PREFIX)]
    except OSError:
        stats["failed"] += 1
        return
    for entry in entries:
        try:
            if _is_link(entry) or not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
        except OSError:
            stats["failed"] += 1
            continue
        if st.st_mtime >= cutoff:
            continue
        if _remove(os.unlink, entry.path):
            stats["files"] += 1
            stats["bytes"] += st.st_size
            stats["audio"] += 1
        else:
            stats["failed"] += 1


def _walk(
    path: Path,
    stats: dict[str, int],
    *,
    keep: frozenset[str],
    keep_images: bool,
    cutoff: float | None,
    top: bool,
) -> bool:
    """Delete what may go under `path`; True when `path` is empty afterwards. `keep` and
    `keep_images` apply at the top level only (spills, logs and images live there; an
    `img_*` deep in a scratch tree is scratch). `cutoff` None deletes regardless of age,
    otherwise only entries whose mtime is older. Links are removed, never entered."""
    try:
        entries = list(os.scandir(path))
    except FileNotFoundError:
        return True
    except OSError:
        stats["failed"] += 1
        return False
    remaining = 0
    for entry in entries:
        if top and (entry.name in keep or (keep_images and entry.name.startswith(("img_", AUDIO_PREFIX)))):
            remaining += 1
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            remaining += 1
            stats["failed"] += 1
            continue
        old = cutoff is None or st.st_mtime < cutoff
        if _is_link(entry):
            if not old:
                remaining += 1
            elif _remove(os.unlink, entry.path):
                stats["files"] += 1
            else:
                remaining += 1
                stats["failed"] += 1
            continue
        if entry.is_dir(follow_symlinks=False):
            before = stats["files"]
            empty = _walk(Path(entry.path), stats, keep=keep, keep_images=keep_images, cutoff=cutoff, top=False)
            # A directory emptied by this sweep goes; an already-empty one only when it is
            # itself old (a fresh TMPDIR about to be written into stays).
            if empty and (stats["files"] > before or old):
                if _remove(os.rmdir, entry.path):
                    continue
                stats["failed"] += 1
            remaining += 1
            continue
        if not old:
            remaining += 1
        elif _remove(os.unlink, entry.path):
            stats["files"] += 1
            stats["bytes"] += st.st_size
            if top and entry.name.startswith("img_"):  # only reachable when purging
                stats["artifacts"] += 1
        else:
            remaining += 1
            stats["failed"] += 1
    return remaining == 0


def _usage_walk(root: Path) -> dict[str, tuple[int, int]]:
    """{folder name: (bytes, files)} for every conversation folder under `root`."""
    sizes: dict[str, tuple[int, int]] = {}
    try:
        entries = list(os.scandir(root))
    except OSError:
        return sizes
    for entry in entries:
        if not _is_link(entry) and entry.is_dir(follow_symlinks=False):
            sizes[entry.name] = _size(Path(entry.path))
    return sizes


def _size(path: Path) -> tuple[int, int]:
    """(bytes, files) under `path`; links count as entries but are never followed."""
    total = files = 0
    stack = [path]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if _is_link(entry):
                    files += 1
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                else:
                    total += entry.stat(follow_symlinks=False).st_size
                    files += 1
            except OSError:
                continue
    return total, files


def conversation_dir(app: Any, conversation_id: str) -> Path:
    """`artifacts/<conversation>`: spills, images, and the conversation's scratch folder —
    where the harness block steers downloads and intermediate files, and where the
    shell's TMPDIR points (2026-09-25). Always inside the allowed roots. Not created
    here; whoever writes first creates it."""
    cid = str(conversation_id or "")
    if not cid or "/" in cid or "\\" in cid or cid in (".", ".."):
        raise ValueError(f"invalid conversation id {conversation_id!r}")
    return Path(app.artifacts_dir) / cid


def install(app: Any) -> None:
    app.artifacts = Store(app)
    Path(app.artifacts_dir).mkdir(parents=True, exist_ok=True)
