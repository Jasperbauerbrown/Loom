"""Vault endpoints (03 §12.1): the browser, editor, search, history and graph the UI
renders, plus the status block a thread's plan panel writes through.

Everything here goes through `app.vault` / `app.index`, so the UI and the model see the
same vault under the same rules (charter §2.2). Mutations emit `knowledge.changed` —
the vault does it on write, and history operations do it here — so every open surface
refreshes without polling.

Every handler is `async def`: Starlette runs a sync handler in a worker thread, and the
SQLite connection is the event loop thread's alone (decision 2026-09-20). Vault and index
calls therefore run inline; only git subprocesses go to a thread, through `git.a_*`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...app import App
from ...knowledge.vault import VaultError
from ..deps import get_app

router = APIRouter(tags=["vault"])


def _guard(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class NoteBody(BaseModel):
    path: str
    content: str


class RevertBody(BaseModel):
    commit: str
    path: str | None = None


class StatusBody(BaseModel):
    path: str
    fields: dict[str, Any] = Field(default_factory=dict)
    owner: str | None = None


# -- browsing -----------------------------------------------------------------------
@router.get("/vault/tree")
async def tree(app: App = Depends(get_app)) -> dict[str, Any]:
    return app.vault.tree()


@router.get("/vault/note")
async def get_note(path: str, app: App = Depends(get_app)) -> dict[str, Any]:
    note = _guard(app.vault.read, path)
    out = note.to_json()
    out["links"] = app.index.links(note.path, "out")
    out["backlinks"] = app.index.links(note.path, "in")
    return out


@router.put("/vault/note")
async def put_note(body: NoteBody, app: App = Depends(get_app)) -> dict[str, Any]:
    note = _guard(app.vault.put_raw, body.path, body.content, "user")
    out = note.to_json()
    out["links"] = app.index.links(note.path, "out")
    out["backlinks"] = app.index.links(note.path, "in")
    return out


@router.delete("/vault/note")
async def delete_note(path: str, app: App = Depends(get_app)) -> dict[str, Any]:
    return {"path": _guard(app.vault.delete, path, "user"), "deleted": True}


@router.get("/vault/search")
async def search(q: str, app: App = Depends(get_app), type: str | None = None, k: int = 8) -> list[dict[str, Any]]:
    return await app.index.search(q, type, max(1, min(50, int(k))))


# -- history (03 §8.6: recent changes with one-click revert) -------------------------
@router.get("/vault/history")
async def history(path: str, app: App = Depends(get_app), n: int = 50) -> list[dict[str, Any]]:
    rel = _guard(app.vault.norm, path)
    return await app.vault.git.a_log(rel, n)


@router.get("/vault/recent-changes")
async def recent_changes(app: App = Depends(get_app), n: int = 50) -> list[dict[str, Any]]:
    return await app.vault.git.a_log(None, n)


@router.get("/vault/diff")
async def diff(sha: str, app: App = Depends(get_app), path: str | None = None) -> dict[str, Any]:
    rel = _guard(app.vault.norm, path) if path else None
    return {"sha": sha, "path": rel, "diff": await app.vault.git.a_diff(sha, rel)}


@router.post("/vault/revert")
async def revert(body: RevertBody, app: App = Depends(get_app)) -> dict[str, Any]:
    rel = _guard(app.vault.norm, body.path) if body.path else None
    sha = await app.vault.git.a_revert(body.commit, rel)
    if sha is None:
        raise HTTPException(status_code=400, detail=f"could not revert {body.commit}")
    if rel:
        paths = [rel]
    else:
        head = await app.vault.git.a_log(None, 1)
        paths = list(head[0]["paths"]) if head else []
    app.index.update(paths)  # SQLite stays on the event-loop thread
    app.bus.emit("knowledge.changed", {"paths": paths})
    return {"commit": sha, "paths": paths}


# -- graph, meters, wanted notes ----------------------------------------------------
@router.get("/vault/graph")
async def graph(
    app: App = Depends(get_app), center: str | None = None, depth: int = 1, limit: int = 200
) -> dict[str, Any]:
    rel = _guard(app.vault.norm, center) if center else None
    return app.index.graph(rel, depth, limit)


@router.get("/vault/core")
async def core(app: App = Depends(get_app)) -> list[dict[str, Any]]:
    return app.index.core_fill()


@router.get("/vault/wanted")
async def wanted(app: App = Depends(get_app)) -> list[dict[str, Any]]:
    return app.index.dangling()


# -- procedures ---------------------------------------------------------------------
@router.get("/vault/procedures")
async def procedures(app: App = Depends(get_app)) -> list[dict[str, Any]]:
    return app.vault.procedures()


@router.post("/vault/procedures/{name}/approve")
async def approve_procedure(name: str, app: App = Depends(get_app)) -> dict[str, Any]:
    return _guard(app.vault.approve_procedure, name)


# -- status block (the thread plan panel) -------------------------------------------
@router.get("/vault/status")
async def get_status(path: str, app: App = Depends(get_app)) -> dict[str, Any]:
    fields, owner = _guard(app.vault.read_status, path)
    return {"path": _guard(app.vault.norm, path), "fields": fields or {}, "owner": owner, "present": fields is not None}


@router.put("/vault/status")
async def put_status(body: StatusBody, app: App = Depends(get_app)) -> dict[str, Any]:
    _fields, owner = _guard(app.vault.read_status, body.path)
    note = _guard(app.vault.write_status, body.path, body.owner or owner or "", body.fields)
    fields, new_owner = _guard(app.vault.read_status, note.path)
    return {"path": note.path, "fields": fields or {}, "owner": new_owner, "present": fields is not None}
