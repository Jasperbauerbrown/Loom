"""Voice input: audio clips in, transcripts out, and the listener's status (03 §5.7,
§12.1).

One codepath for every clip, whoever sent it (the hotkey listener, the composer's mic
button) and whether it is new or a retry: store → transcribe → deliver.

    ok, action=send     the transcript is posted to the conversation like a typed
                        message, `meta.voice = {audio_id, preset, source}` → 202
    ok, action=draft    bus `voice.draft {conversation_id, audio_id, text}` → 200
    empty transcript    nothing posted → 200 `{audio_id, text: "", dropped: true}`
    failed              a `system` event (`meta.voice = {audio_id, failed, action}`, the
                        transcript's Retry) and bus `voice.failed` → 502, or 409 when no
                        model holds the transcription role (which also posts the
                        de-duplicated "Choose a transcription model" setup item)

Bodies are the raw audio bytes, as with images: one clip, one request, no multipart.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from ... import conversation as conv_pkg
from ... import db, items, voice
from ...conversation import events
from ...models import roles
from ...voice import store as audio_store
from ..deps import get_app

router = APIRouter(tags=["voice"])

ACTIONS = ("send", "draft")
SOURCES = ("hotkey", "composer")
SETUP_SOURCE = "setup:transcription"
STATUS_KEY = "voice.status"


def _conversation(app, conversation_id: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    row = events.public_row(app, conversation_id)
    if row is None:
        raise HTTPException(404, f"conversation {conversation_id} not found")
    return row


def _check(action: str, source: str | None = None) -> None:
    if action not in ACTIONS:
        raise HTTPException(400, f"action must be one of {list(ACTIONS)}")
    if source is not None and source not in SOURCES:
        raise HTTPException(400, f"source must be one of {list(SOURCES)}")


@router.post("/conversations/{conversation_id}/audio")
async def upload_audio(
    conversation_id: str, request: Request, action: str = "draft", source: str = "composer", app=Depends(get_app)
) -> Response:
    """Raw audio bytes (WAV PCM16 mono 16 kHz is what Loom produces; the type is sniffed,
    then taken from Content-Type). 413 over 25 MB, 415 not audio, 400 empty."""
    _check(action, source)
    row = _conversation(app, conversation_id)
    if action == "send" and not app.has_model(row["role"]):
        raise HTTPException(409, "no model configured — open Settings › Models")
    cap = audio_store.MAX_AUDIO_BYTES
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > cap:
            raise HTTPException(413, f"audio exceeds the {cap // (1024 * 1024)} MB cap")
    if not body:
        raise HTTPException(400, "audio is empty")
    declared = request.headers.get("content-type")
    mime = audio_store.sniff_audio(bytes(body)) or audio_store.normalise_mime(declared)
    if mime is None:
        supported = ", ".join(sorted(audio_store.AUDIO_MIMES))
        raise HTTPException(415, f"not supported audio (content-type {declared or 'missing'!r}; supported: {supported})")
    try:
        ref = audio_store.store(app, conversation_id, bytes(body), mime)  # persist before act
    except ValueError as exc:
        raise HTTPException(415, str(exc))
    return await _transcribe_and_deliver(app, conversation_id, ref["id"], bytes(body), mime, action, source)


@router.post("/conversations/{conversation_id}/audio/{audio_id}/retry")
async def retry_audio(conversation_id: str, audio_id: str, action: str = "draft", app=Depends(get_app)) -> Response:
    """Transcribe a stored clip again; the same outcomes as a new upload."""
    _check(action)
    row = _conversation(app, conversation_id)
    if action == "send" and not app.has_model(row["role"]):
        raise HTTPException(409, "no model configured — open Settings › Models")
    got = audio_store.read(app, conversation_id, audio_id)
    if got is None:
        raise HTTPException(404, "audio not found (deleted by voice.audio_retention_days, or never stored)")
    data, mime = got
    return await _transcribe_and_deliver(app, conversation_id, audio_id, data, mime, action, "retry")


@router.get("/conversations/{conversation_id}/audio/{audio_id}")
async def get_audio(conversation_id: str, audio_id: str, app=Depends(get_app)) -> Response:
    """The stored clip. Ids are ULID-minted and content is immutable, so caching is forever."""
    _conversation(app, conversation_id)
    got = audio_store.read(app, conversation_id, audio_id)
    if got is None:
        raise HTTPException(404, "audio not found")
    data, mime = got
    return Response(content=data, media_type=mime, headers={"Cache-Control": "public, max-age=31536000, immutable"})


async def _transcribe_and_deliver(
    app, conversation_id: str, audio_id: str, data: bytes, mime: str, action: str, source: str  # type: ignore[no-untyped-def]
) -> Response:
    if not app.has_model("transcription"):
        _post_setup_item(app)
        error = f"no model configured for role transcription — open {roles.settings_url(app)}"
        _record_failure(app, conversation_id, audio_id, action, error)
        return JSONResponse({"detail": error, "audio_id": audio_id}, status_code=409)
    preset = roles.role_preset(app, "transcription")
    result = await app.models.transcribe(data, mime)
    if not result.ok:
        error = result.error or f"transcription {result.outcome}"
        _record_failure(app, conversation_id, audio_id, action, error)
        return JSONResponse({"detail": error, "audio_id": audio_id}, status_code=502)
    text = (result.text or "").strip()
    if not text:
        return JSONResponse({"audio_id": audio_id, "text": "", "dropped": True})
    if action == "send":
        meta = {"voice": {"audio_id": audio_id, "preset": preset, "source": source}}
        inbox_id = conv_pkg.post(app, conversation_id, text, source="user", meta=meta)
        return JSONResponse({"audio_id": audio_id, "text": text, "inbox_id": inbox_id}, status_code=202)
    app.bus.emit("voice.draft", {"conversation_id": conversation_id, "audio_id": audio_id, "text": text}, conversation_id)
    return JSONResponse({"audio_id": audio_id, "text": text})


def _record_failure(app, conversation_id: str, audio_id: str, action: str, error: str) -> None:  # type: ignore[no-untyped-def]
    """The one failure behaviour: a `system` event the transcript renders with a Retry
    (it is never replayed to the model), then `voice.failed` for whoever is listening."""
    meta = {"voice": {"audio_id": audio_id, "failed": True, "action": action}}
    ev = events.append(app, conversation_id, "system", f"voice message could not be transcribed: {error}", meta)
    app.bus.emit("event.appended", events.get(app, ev), conversation_id)
    app.bus.emit(
        "voice.failed",
        {"conversation_id": conversation_id, "audio_id": audio_id, "action": action, "error": error, "event_id": ev},
        conversation_id,
    )


def _post_setup_item(app) -> None:  # type: ignore[no-untyped-def]
    """One undismissed item at a time; setting the role answers it (models router)."""
    existing = app.db.execute(
        "SELECT 1 FROM items WHERE kind='setup' AND source=? AND dismissed_at IS NULL LIMIT 1", (SETUP_SOURCE,)
    ).fetchone()
    if existing is not None:
        return
    items.post(
        app,
        "setup",
        "Choose a transcription model",
        "A voice clip arrived but no model holds the transcription role. Settings › Models: "
        "add a preset on an OpenAI-compatible provider that serves /v1/audio/transcriptions "
        "(a local whisper server, OpenAI, Groq, OpenRouter) and give it the transcription "
        "role. The clip is kept: Retry it from the transcript.",
        source=SETUP_SOURCE,
    )


# ---------------------------------------------------------------------------------
# listener status
# ---------------------------------------------------------------------------------
class StatusIn(BaseModel):
    """What the listener reports. Unknown keys are kept, so a newer listener can say
    more without a backend change."""

    model_config = ConfigDict(extra="allow")

    state: str
    hotkey: str | None = None
    mode: str | None = None
    error: str | None = None
    devices: list[str] | None = None
    device: str | None = None
    updated_at: str | None = None


def status_payload(app) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """`{enabled, running, pid, state, hotkey, mode, error, devices, device, updated_at}`:
    the listener's last report over the settings, plus the supervised child's state.
    `state` is `disabled` while voice.enabled is off and `stopped` before any report."""
    settings = app.settings
    reported: dict[str, Any] = dict(app.extras.get(STATUS_KEY) or {})
    enabled = bool(settings.get("voice.enabled"))
    out: dict[str, Any] = {
        **reported,
        "enabled": enabled,
        "state": "disabled" if not enabled else (reported.get("state") or "stopped"),
        "hotkey": reported.get("hotkey") or settings.get("voice.hotkey"),
        "mode": reported.get("mode") or settings.get("voice.mode"),
        "error": reported.get("error"),
        "devices": list(reported.get("devices") or []),
        "device": reported.get("device") if reported.get("device") is not None else settings.get("voice.input_device"),
        "updated_at": reported.get("updated_at"),
    }
    listener = voice.listener_state(app)
    out["running"] = bool(listener.get("running"))
    out["pid"] = listener.get("pid")
    return out


@router.get("/voice/status")
async def get_status(app=Depends(get_app)) -> dict[str, Any]:
    return status_payload(app)


@router.post("/voice/status")
async def post_status(body: StatusIn, app=Depends(get_app)) -> dict[str, Any]:
    """The listener reports here on start, on every state change and on errors. Kept in
    memory only (a restart starts a new listener, which reports again)."""
    report = body.model_dump(exclude_none=True)
    report.pop("running", None)
    report.pop("pid", None)
    report.setdefault("updated_at", db.now_iso())
    if body.error is None:
        report["error"] = None  # a report without an error clears the last one
    app.extras[STATUS_KEY] = report
    payload = status_payload(app)
    app.bus.emit("voice.status", payload)
    return payload
