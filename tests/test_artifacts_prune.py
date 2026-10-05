"""Artifacts disk use (2026-10-05): scratch pruning in archived and live conversations,
whole-folder retention, counted bytes, and the disk-usage read (03 §4.4)."""

from __future__ import annotations

import datetime as _dt
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from loom import db
from loom.ids import ulid

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _days_ago(n: float) -> str:
    moment = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=n)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _conversation(app, *, status: str = "running", archived_days: float | None = None, title: str = "t") -> str:
    cid = ulid()
    now = db.now_iso()
    app.db.execute(
        "INSERT INTO conversations(id, kind, title, role, status, created_at, updated_at, archived_at) "
        "VALUES (?, 'thread', ?, 'worker', ?, ?, ?, ?)",
        (cid, title, status, now, now, None if archived_days is None else _days_ago(archived_days)),
    )
    return cid


def _write(path: Path, data: bytes = b"x" * 10, *, age_days: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if age_days:
        _age(path, age_days)
    return path


def _age(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def _process_log(app, cid: str, folder: Path) -> Path:
    log = _write(folder / "proc_P1.log", b"log", age_days=30)
    app.db.execute(
        "INSERT INTO processes(id, conversation_id, title, command, cwd, status, log_path, started_at) "
        "VALUES (?, ?, 'p', 'cmd', '.', 'exited', ?, ?)",
        (ulid(), cid, str(log).replace("\\", "/"), db.now_iso()),
    )
    return log


def _set(app, key: str, value) -> None:
    app.settings.set(app.db, key, value)


# -- archived: grace, then retention -----------------------------------------------------
def test_archived_past_grace_keeps_only_spills_logs_and_images(app):
    cid = _conversation(app, status="archived", archived_days=2)
    folder = app.artifacts.root / cid
    spill = app.artifacts.spill(cid, None, "spilled")
    image = app.artifacts.store_image(cid, PNG, "image/png")
    log = _process_log(app, cid, folder)
    _write(folder / "download.zip", b"z" * 100)  # fresh: age does not matter after archive
    _write(folder / "pytest-of-x" / "deep" / "a.txt", b"a" * 50)
    _write(folder / "profile" / "img_cache.bin", b"i" * 25)  # img_ below the top is scratch

    stats = app.artifacts.prune()

    assert stats == {"artifacts": 0, "files": 3, "bytes": 175, "failed": 0}
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        [Path(spill["path"]).name, f"{image['id']}.png", log.name]
    )
    assert app.artifacts.get(spill["id"]) is not None


def test_archived_inside_the_grace_period_is_untouched(app):
    cid = _conversation(app, status="archived", archived_days=0.5)
    scratch = _write(app.artifacts.root / cid / "tmp" / "f.txt", age_days=30)
    assert app.artifacts.prune()["files"] == 0
    assert scratch.exists()


def test_grace_zero_prunes_scratch_right_after_archive(app):
    _set(app, "conversation.scratch_archived_grace_days", 0)
    cid = _conversation(app, status="archived", archived_days=0.001)
    scratch = _write(app.artifacts.root / cid / "f.txt")
    assert app.artifacts.prune()["files"] == 1
    assert not scratch.exists()


def test_archived_past_retention_deletes_the_whole_folder(app):
    _set(app, "conversation.artifact_retention_days", 30)
    cid = _conversation(app, status="archived", archived_days=31)
    folder = app.artifacts.root / cid
    spill = app.artifacts.spill(cid, None, "s" * 7)
    app.artifacts.store_image(cid, PNG, "image/png")
    _process_log(app, cid, folder)
    _write(folder / "tmp" / "nested" / "f.txt", b"f" * 5)

    stats = app.artifacts.prune()

    assert not folder.exists()
    assert app.artifacts.get(spill["id"]) is None
    assert stats["artifacts"] == 2  # the spill row and the image
    assert stats["files"] == 4
    assert stats["bytes"] == 7 + len(PNG) + 3 + 5
    assert stats["failed"] == 0


# -- live conversations, the desk included -------------------------------------------------
def test_live_scratch_untouched_for_idle_days_goes_spills_and_images_stay(app):
    desk = app.desk_id()
    folder = app.artifacts.root / desk
    spill = app.artifacts.spill(desk, None, "spilled")
    _age(Path(spill["path"]), 30)
    image = app.artifacts.store_image(desk, PNG, "image/png")
    _age(folder / f"{image['id']}.png", 30)
    old = _write(folder / "pytest-of-x" / "run-1" / "old.txt", b"o" * 40, age_days=8)
    _age(old.parent, 8)
    fresh = _write(folder / "pytest-of-x" / "run-2" / "fresh.txt", b"n" * 40)
    mixed_old = _write(folder / "dl" / "old.bin", b"m" * 10, age_days=10)
    mixed_new = _write(folder / "dl" / "new.bin", b"m" * 10)
    empty_old = folder / "empty-old"
    empty_old.mkdir()
    _age(empty_old, 9)
    empty_new = folder / "empty-new"
    empty_new.mkdir()

    stats = app.artifacts.prune()

    assert stats == {"artifacts": 0, "files": 2, "bytes": 50, "failed": 0}
    assert Path(spill["path"]).exists()
    assert (folder / f"{image['id']}.png").exists()
    assert not old.exists() and not old.parent.exists()  # emptied directories go
    assert fresh.exists()
    assert not mixed_old.exists() and mixed_new.exists()
    assert not empty_old.exists()
    assert empty_new.exists()  # a fresh empty TMPDIR stays


def test_idle_days_zero_never_prunes_live_scratch(app):
    _set(app, "conversation.scratch_idle_days", 0)
    old = _write(app.artifacts.root / app.desk_id() / "old.txt", age_days=400)
    assert app.artifacts.prune()["files"] == 0
    assert old.exists()


def test_a_folder_without_a_conversation_row_is_swept_like_a_live_one(app):
    old = _write(app.artifacts.root / "orphan" / "old.txt", age_days=30)
    img = _write(app.artifacts.root / "orphan" / f"img_{ulid()}.png", PNG, age_days=30)
    assert app.artifacts.prune()["files"] == 1
    assert not old.exists() and img.exists()


# -- Windows awkwardness ---------------------------------------------------------------------
def test_read_only_files_are_made_writable_and_removed(app):
    cid = _conversation(app, status="archived", archived_days=5)
    ro = _write(app.artifacts.root / cid / "node_modules" / "ro.txt")
    os.chmod(ro, stat.S_IREAD)
    stats = app.artifacts.prune()
    assert stats["files"] == 1 and stats["failed"] == 0
    assert not ro.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="an open file blocks deletion only on Windows")
def test_a_locked_file_is_skipped_and_counted(app):
    cid = _conversation(app, status="archived", archived_days=5)
    locked = _write(app.artifacts.root / cid / "busy" / "locked.txt")
    other = _write(app.artifacts.root / cid / "other.txt")
    with open(locked, "rb"):
        stats = app.artifacts.prune()
    assert stats["failed"] >= 1
    assert locked.exists()
    assert not other.exists()


def _link_dir(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_links_out_of_the_folder_are_removed_never_followed(app, tmp_path):
    outside = tmp_path / "outside"
    keep = _write(outside / "precious.txt")
    _set(app, "conversation.artifact_retention_days", 1)
    cid = _conversation(app, status="archived", archived_days=2)
    folder = app.artifacts.root / cid
    folder.mkdir(parents=True)
    _link_dir(folder / "link", outside)

    app.artifacts.prune()

    assert keep.exists()
    assert not folder.exists()


# -- disk usage ----------------------------------------------------------------------------
def test_usage_reports_the_total_and_the_largest_conversations_with_titles(app):
    small = _conversation(app, title="Small thread")
    big = _conversation(app, title="Big thread")
    _write(app.artifacts.root / small / "a.txt", b"a" * 10)
    _write(app.artifacts.root / big / "deep" / "b.txt", b"b" * 1000)
    _write(app.artifacts.root / big / "c.txt", b"c" * 5)

    usage = app.artifacts.usage(limit=1)

    assert usage["total_bytes"] >= 1015
    assert usage["total_files"] >= 3
    assert usage["conversations"] == [
        {"id": big, "title": "Big thread", "kind": "thread", "status": "running", "bytes": 1005, "files": 2}
    ]


async def test_usage_endpoint(client):
    app = client.loom
    cid = _conversation(app, title="Usage")
    _write(app.artifacts.root / cid / "x.bin", b"x" * 4096)

    r = await client.get("/api/maintenance/artifacts", params={"limit": 5})

    assert r.status_code == 200
    body = r.json()
    assert body["total_bytes"] >= 4096
    assert any(c["id"] == cid and c["title"] == "Usage" and c["bytes"] == 4096 for c in body["conversations"])


async def test_maintenance_report_counts_pruned_files_and_bytes(app, fake):
    cid = _conversation(app, status="archived", archived_days=3)
    _write(app.artifacts.root / cid / "tmp" / "f.txt", b"f" * 123)

    report = await app.maintenance.run("test")

    assert report["pruned_files"] == 1
    assert report["pruned_bytes"] == 123
    assert report["prune_failures"] == 0
