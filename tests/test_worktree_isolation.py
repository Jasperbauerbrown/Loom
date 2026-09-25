"""Worktree isolation for a thread (03 §6.7, generalising §15.1).

`conversation_create(isolation="worktree")` gives a thread its own checkout of the
repository it works in, on branch `loom/<id>`, and that checkout *replaces* the source
root: the thread cannot reach the live tree it was branched from. Archiving releases the
checkout when it is clean and keeps it, saying why, when it is not.

Everything here drives the real `git` CLI against a repository built in `tmp_path`; no
model is called (the fake client answers the turns the spawn schedules).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from loom import conversation
from loom.conversation import context, events, worktree
from loom.models.fake import Scripted


# -- helpers ----------------------------------------------------------------------

def _git(cwd: Path | str, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}"
    return proc.stdout


def _repo(tmp_path: Path, name: str = "repo") -> str:
    """A git repository with exactly one commit. The `home` fixture already exports the
    GIT_AUTHOR_*/GIT_COMMITTER_* identity, so no global config is touched."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("live checkout\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "first")
    return str(root).replace("\\", "/")


def _threads(app) -> int:
    return int(app.db.execute("SELECT COUNT(*) FROM conversations WHERE kind='thread'").fetchone()[0])


def _quiet(fake) -> None:
    """Answer whatever the spawn's first turn asks, so tests settle instead of failing
    the turn on an unmatched purpose."""
    fake.on("status", lambda req: Scripted(content='{"state":"waiting","goal":"g","done":"","doing":"","next":"","blockers":""}'))
    fake.default = lambda req: Scripted(content="ok")


async def _settle(app) -> None:
    for _ in range(20):
        pending = [t for n, t in app.tasks.items() if n.startswith("turn:") and not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


# -- spawn ------------------------------------------------------------------------

def test_isolated_thread_gets_its_own_checkout(app, tmp_path):
    repo = _repo(tmp_path)
    other = tmp_path / "assets"
    other.mkdir()
    row = conversation.create_thread(
        app, goal="Change the build.", roots=[repo, str(other)], isolation="worktree", created_by="user"
    )
    path = row["worktree"]
    assert path == worktree.path_for(app, row["id"])
    assert Path(path).is_dir() and (Path(path) / "README.md").read_text(encoding="utf-8") == "live checkout\n"
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() == f"loom/{row['id']}"
    # the row carries the checkout, and the worktree leads the roots with the live tree gone
    assert app.conversation(row["id"])["worktree"] == path
    assert row["roots"][0] == path
    assert not any(worktree.same_path(r, repo) for r in row["roots"])
    assert row["roots"][1] == str(other).replace("\\", "/")  # other roots keep their order


def test_isolation_root_picks_the_repository(app, tmp_path):
    plain = tmp_path / "notes"
    plain.mkdir()
    repo = _repo(tmp_path)
    row = conversation.create_thread(
        app, goal="Change the build.", roots=[str(plain), repo], isolation="worktree",
        isolation_root=repo, created_by="user",
    )
    assert row["roots"][0] == row["worktree"]
    assert row["roots"][1] == str(plain).replace("\\", "/")
    assert not any(worktree.same_path(r, repo) for r in row["roots"])


def test_a_root_that_is_not_a_repository_refuses_and_leaves_nothing(app, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    before = _threads(app)
    with pytest.raises(conversation.ThreadError, match="not inside a git repository"):
        conversation.create_thread(app, goal="Change it.", roots=[str(plain)], isolation="worktree", created_by="user")
    assert _threads(app) == before
    assert not (Path(app.home) / "worktrees").exists()


def test_a_repository_without_a_commit_refuses(app, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init")
    before = _threads(app)
    with pytest.raises(conversation.ThreadError, match="no commit yet"):
        conversation.create_thread(app, goal="Change it.", roots=[str(empty)], isolation="worktree", created_by="user")
    assert _threads(app) == before
    assert not (Path(app.home) / "worktrees" ).exists()


def test_isolation_arguments_are_validated(app, tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(conversation.ThreadError, match="isolation must be 'none' or 'worktree'"):
        conversation.create_thread(app, goal="Change it.", roots=[repo], isolation="container", created_by="user")
    with pytest.raises(conversation.ThreadError, match="worktree isolation needs a root"):
        conversation.create_thread(app, goal="Change it.", isolation="worktree", created_by="user")
    with pytest.raises(conversation.ThreadError, match="is not one of this thread's roots"):
        conversation.create_thread(
            app, goal="Change it.", roots=[repo], isolation="worktree",
            isolation_root=str(tmp_path / "elsewhere"), created_by="user",
        )
    assert _threads(app) == 0
    # isolation=None / "none" is today's behaviour: no checkout, roots untouched
    plain = conversation.create_thread(app, goal="Read it.", roots=[repo], created_by="user")
    assert plain["worktree"] is None and plain["roots"] == [repo]


def test_harness_block_tells_the_thread_where_it_works(app, tmp_path):
    repo = _repo(tmp_path)
    row = conversation.create_thread(app, goal="Change the build.", roots=[repo], isolation="worktree", created_by="user")
    conv = app.conversation(row["id"])
    block = context.filesystem_block(app, conv)
    assert row["worktree"] in block and f"loom/{row['id']}" in block
    assert "branched from" in block
    # and it reaches the model through the one context builder, in the harness block
    harness = context.assemble(app, conv, [], None).harness(app)["content"]
    assert row["worktree"] in harness and f"loom/{row['id']}" in harness
    # a thread without isolation says nothing about worktrees
    plain = conversation.create_thread(app, goal="Read it.", created_by="user")
    assert "worktree" not in context.filesystem_block(app, app.conversation(plain["id"]))


async def test_the_tool_names_the_checkout_and_the_branch(app, tmp_path):
    """What the spawning conversation is told: `conversation_create`'s result says where
    the thread works, so the parent can read the branch back without asking."""
    from loom.conversation.tools import _create
    from loom.tools.registry import ToolContext

    repo = _repo(tmp_path)
    ctx = ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1")
    result = await _create(ctx, {"goal": "Change the build.", "roots": [repo], "isolation": "worktree"})
    assert result.ok
    cid = result.content.split()[2]
    assert app.conversation(cid)["worktree"] in result.content
    assert f"branch loom/{cid}" in result.content
    bad = await _create(ctx, {"goal": "Change the build.", "roots": [str(tmp_path)], "isolation": "worktree"})
    assert not bad.ok and "git repository" in bad.content


# -- REST -------------------------------------------------------------------------

async def test_create_route_accepts_isolation(client, fake, tmp_path):
    app = client.loom
    _quiet(fake)
    repo = _repo(tmp_path)
    r = await client.post(
        "/api/conversations", json={"goal": "Change the build.", "roots": [repo], "isolation": "worktree"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["worktree"] and Path(body["worktree"]).is_dir()
    assert body["roots"][0] == body["worktree"]
    bad = await client.post(
        "/api/conversations", json={"goal": "Change the build.", "roots": [str(tmp_path)], "isolation": "worktree"}
    )
    assert bad.status_code == 400 and "git repository" in bad.json()["detail"]
    await _settle(app)


async def test_archive_removes_a_clean_worktree(client, fake, tmp_path):
    app = client.loom
    _quiet(fake)
    repo = _repo(tmp_path)
    row = conversation.create_thread(app, goal="Change the build.", roots=[repo], isolation="worktree", created_by="user")
    await _settle(app)
    path = row["worktree"]

    r = await client.post(f"/api/conversations/{row['id']}/archive")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "archived" and body["worktree"] is None
    assert "removed" in body["worktree_message"] and path in body["worktree_message"]
    assert not Path(path).exists()
    assert app.conversation(row["id"])["worktree"] is None
    system = [e["content"] for e in events.list_events(app, row["id"], kinds=["system"], limit=100)]
    assert any(path in c and "removed" in c for c in system)
    # the branch is the handback: releasing the checkout never deletes it
    assert f"loom/{row['id']}" in _git(repo, "branch", "--list", f"loom/{row['id']}")


async def test_archive_keeps_a_dirty_worktree_and_says_why(client, fake, tmp_path):
    app = client.loom
    _quiet(fake)
    repo = _repo(tmp_path)
    row = conversation.create_thread(app, goal="Change the build.", roots=[repo], isolation="worktree", created_by="user")
    await _settle(app)
    path = row["worktree"]
    (Path(path) / "work-in-progress.txt").write_text("half a feature\n", encoding="utf-8")

    r = await client.post(f"/api/conversations/{row['id']}/archive")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "archived" and body["worktree"] == path
    assert "kept" in body["worktree_message"] and "uncommitted" in body["worktree_message"]
    assert Path(path).is_dir() and (Path(path) / "work-in-progress.txt").exists()
    system = [e["content"] for e in events.list_events(app, row["id"], kinds=["system"], limit=100)]
    assert any(path in c and "kept" in c for c in system)


async def test_archiving_a_plain_thread_is_unchanged(client, fake, tmp_path):
    app = client.loom
    _quiet(fake)
    row = conversation.create_thread(app, goal="Read the docs.", created_by="user")
    await _settle(app)
    r = await client.post(f"/api/conversations/{row['id']}/archive")
    assert r.status_code == 200 and "worktree_message" not in r.json()
    assert events.list_events(app, row["id"], kinds=["system"], limit=100) == []
