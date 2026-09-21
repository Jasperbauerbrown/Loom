"""Conversations, transcript events, messages, cancel, archive, occupancy, artifacts
(03 §12.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ... import conversation as conv_pkg
from ... import db
from ...artifacts import IMAGE_MIMES, sniff_image
from ...conversation import events, inbox, status
from ...conversation.context import occupancy
from ..deps import get_app

router = APIRouter()


def _status_line(app, row: dict[str, Any]) -> str:  # type: ignore[no-untyped-def]
    if row.get("kind") != "thread":
        return ""
    try:
        fields, _owner, _sink = status.read(app, row)
    except Exception:
        return ""
    if not fields:
        return ""
    return (fields.get("doing") or fields.get("state") or "").strip()[:200]


def _status_payload(app, row: dict[str, Any]) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    fields, owner, source = status.read(app, row)
    return {"fields": fields, "owner": owner, "source": source}


def _row(app, conversation_id: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    row = events.public_row(app, conversation_id)
    if row is None:
        raise HTTPException(404, f"conversation {conversation_id} not found")
    row["status_line"] = _status_line(app, row)
    return row


@router.get("/conversations")
async def list_conversations(
    kind: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
    app=Depends(get_app),
) -> list[dict[str, Any]]:
    sql = "SELECT id FROM conversations"
    where, params = [], []
    if kind:
        where.append("kind=?")
        params.append(kind)
    if status:
        where.append("status=?")
        params.append(status)
    elif not include_archived:
        where.append("status!='archived'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE kind WHEN 'desk' THEN 0 ELSE 1 END, updated_at DESC"
    return [_row(app, r["id"]) for r in app.db.execute(sql, params)]


class CreateThread(BaseModel):
    kind: str = "thread"
    goal: str
    scope_note: str | None = None
    project_name: str | None = None
    roots: list[str] | None = None
    preset: str | None = None
    title: str | None = None
    parent_id: str | None = None
    excerpt: str | None = None


@router.post("/conversations", status_code=201)
async def create_conversation(body: CreateThread, app=Depends(get_app)) -> dict[str, Any]:
    if body.kind != "thread":
        raise HTTPException(400, "only threads can be created; the desk always exists")
    if not app.has_model("worker"):
        raise HTTPException(409, "no model configured for the worker role — open Settings › Models")
    try:
        row = conv_pkg.create_thread(
            app, goal=body.goal, parent_id=body.parent_id, scope_note=body.scope_note, project_name=body.project_name,
            roots=body.roots, preset=body.preset, title=body.title, excerpt=body.excerpt, created_by="user",
        )
    except conv_pkg.ThreadError as exc:
        raise HTTPException(400, str(exc))
    return _row(app, row["id"])


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, app=Depends(get_app)) -> dict[str, Any]:
    return _row(app, conversation_id)


class PatchConversation(BaseModel):
    title: str | None = None
    preset: str | None = None
    step_budget: int | None = None
    clear_preset: bool = False


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(conversation_id: str, body: PatchConversation, app=Depends(get_app)) -> dict[str, Any]:
    _row(app, conversation_id)
    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title.strip()
    if body.clear_preset:
        fields["preset"] = None
    elif body.preset is not None:
        p = app.db.execute("SELECT enabled FROM presets WHERE name=?", (body.preset,)).fetchone()
        if p is None or not p["enabled"]:
            raise HTTPException(400, f"preset {body.preset!r} does not exist or is disabled")
        fields["preset"] = body.preset
    if body.step_budget is not None:
        if body.step_budget < 1:
            raise HTTPException(400, "step_budget must be ≥ 1")
        fields["step_budget"] = body.step_budget
    if fields:
        fields["updated_at"] = db.now_iso()
        events.update(app, conversation_id, **fields)
        app.bus.emit("conversation.updated", events.public_row(app, conversation_id), conversation_id)
    return _row(app, conversation_id)


@router.get("/conversations/{conversation_id}/events")
async def list_events(
    conversation_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = Query(200, ge=1, le=2000),
    kinds: str | None = None,
    tail: bool = False,
    app=Depends(get_app),
) -> list[dict[str, Any]]:
    """Ascending page of events. `after` follows a turn (the CLI), `before` pages back
    through history, and `tail=1` returns the newest `limit` — what a surface opening a
    transcript wants. Without `tail` or `before` the page is the oldest `limit`, which
    is why the web UI showed a 300-event desk from its beginning (2026-09-21)."""
    _row(app, conversation_id)
    kind_list = [k for k in (kinds or "").split(",") if k] or None
    if before is not None or tail:
        rows = events.list_events(app, conversation_id, after=after, before=before, limit=limit, kinds=kind_list, descending=True)
        rows.reverse()
        return rows
    return events.list_events(app, conversation_id, after=after, limit=limit, kinds=kind_list)


class StatusBody(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)


@router.get("/conversations/{conversation_id}/status")
async def get_status(conversation_id: str, app=Depends(get_app)) -> dict[str, Any]:
    """The thread's status wherever it lives: the scope note's block (`source: note`) or
    its transcript's status events (`source: transcript`, a task)."""
    return _status_payload(app, _row(app, conversation_id))


@router.put("/conversations/{conversation_id}/status")
async def put_status(conversation_id: str, body: StatusBody, app=Depends(get_app)) -> dict[str, Any]:
    """The person edits the plan through the same writer the thread uses; a note block
    keeps its owner, a transcript event records `user` as its author."""
    row = _row(app, conversation_id)
    if row["kind"] != "thread":
        raise HTTPException(400, "the desk publishes no status")
    if not body.fields:
        raise HTTPException(400, "fields is empty")
    try:
        status.write(app, row, body.fields, owner="user")
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return _status_payload(app, row)


@router.get("/conversations/{conversation_id}/inbox")
async def list_inbox(conversation_id: str, app=Depends(get_app)) -> list[dict[str, Any]]:
    """Messages posted but not yet rendered into the transcript — what surfaces show as
    *queued* while a turn runs or waits for an approval."""
    _row(app, conversation_id)
    return inbox.pending_rows(app, conversation_id)


class Message(BaseModel):
    content: str = ""
    images: list[str] = Field(default_factory=list)  # image ids from POST …/images


@router.post("/conversations/{conversation_id}/messages", status_code=202)
async def send_message(conversation_id: str, body: Message, app=Depends(get_app)) -> dict[str, Any]:
    row = _row(app, conversation_id)
    refs: list[dict[str, str]] = []
    for image_id in body.images:
        found = app.artifacts.find_image(conversation_id, image_id) if app.artifacts is not None else None
        if found is None:
            raise HTTPException(400, f"unknown image {image_id!r} for this conversation — upload it first")
        refs.append({"id": image_id, "mime": found[1]})
    if not body.content.strip() and not refs:
        raise HTTPException(400, "content is empty")
    if not app.has_model(row["role"]):
        raise HTTPException(409, "no model configured — open Settings › Models (or the Setup screen)")
    inbox_id = conv_pkg.post(app, conversation_id, body.content, source="user", meta={"images": refs} if refs else None)
    return {"inbox_id": inbox_id, "conversation_id": conversation_id}


@router.post("/conversations/{conversation_id}/images")
async def upload_image(conversation_id: str, request: Request, app=Depends(get_app)) -> dict[str, Any]:
    """Raw image bytes in the body (no multipart: one image, one request, no extra
    dependency). The type is sniffed from the bytes, then the Content-Type header.
    Returns `{id, mime, bytes}`; the id goes into `POST …/messages` `images`."""
    _row(app, conversation_id)
    if app.artifacts is None:
        raise HTTPException(503, "artifact store unavailable")
    cap = app.artifacts.image_cap_bytes()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > cap:
            raise HTTPException(413, f"image exceeds the {cap // (1024 * 1024)} MB cap (conversation.image_max_mb)")
    declared = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    mime = sniff_image(bytes(body)) or (declared if declared in IMAGE_MIMES else None)
    if mime is None:
        raise HTTPException(415, f"not a supported image (content-type {declared or 'missing'!r}; supported: {', '.join(sorted(IMAGE_MIMES))})")
    try:
        return app.artifacts.store_image(conversation_id, bytes(body), mime)
    except ValueError as exc:
        raise HTTPException(413 if "cap" in str(exc) else 415, str(exc))


@router.get("/conversations/{conversation_id}/images/{image_id}")
async def get_image(conversation_id: str, image_id: str, app=Depends(get_app)) -> Response:
    """The stored bytes. Ids are ULID-minted and content is immutable, so caching is forever."""
    _row(app, conversation_id)
    got = app.artifacts.read_image(conversation_id, image_id) if app.artifacts is not None else None
    if got is None:
        raise HTTPException(404, "image not found")
    data, mime = got
    return Response(content=data, media_type=mime, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.post("/conversations/{conversation_id}/cancel")
async def cancel(conversation_id: str, app=Depends(get_app)) -> dict[str, Any]:
    row = _row(app, conversation_id)
    running = row["status"] == "running"
    if running:
        app.cancel_flags.add(conversation_id)
    return {"cancelling": running, "status": row["status"]}


@router.post("/conversations/{conversation_id}/clear")
async def clear_context(conversation_id: str, app=Depends(get_app)) -> dict[str, Any]:
    """Start the context fresh: nothing before now is replayed to the model. The
    transcript is kept (visible below a divider, searchable); 409 while a turn runs."""
    from ...conversation import compaction

    _row(app, conversation_id)
    try:
        compaction.clear(app, conversation_id, actor="user")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return _row(app, conversation_id)


@router.post("/conversations/{conversation_id}/archive")
async def archive(conversation_id: str, app=Depends(get_app)) -> dict[str, Any]:
    row = _row(app, conversation_id)
    if row["kind"] == "desk":
        raise HTTPException(400, "the desk cannot be archived")
    if row["status"] in ("running", "waiting_approval"):
        raise HTTPException(409, f"conversation is {row['status']}; cancel it first")
    events.set_status(app, conversation_id, "archived", archived_at=db.now_iso())
    return _row(app, conversation_id)


@router.get("/conversations/{conversation_id}/occupancy")
async def get_occupancy(conversation_id: str, app=Depends(get_app)) -> dict[str, Any]:
    row = _row(app, conversation_id)
    return occupancy(app, row)


@router.get("/conversations/{conversation_id}/artifacts")
async def list_artifacts(conversation_id: str, app=Depends(get_app)) -> list[dict[str, Any]]:
    _row(app, conversation_id)
    return app.artifacts.list(conversation_id)


@router.get("/artifacts/{artifact_id}")
async def read_artifact(artifact_id: str, offset: int = 0, limit: int = Query(8000, ge=1, le=200000), app=Depends(get_app)) -> dict[str, Any]:
    row = app.artifacts.get(artifact_id)
    if row is None:
        raise HTTPException(404, f"artifact {artifact_id} not found")
    return {**row, "offset": offset, "limit": limit, "content": app.artifacts.read(artifact_id, offset, limit)}
