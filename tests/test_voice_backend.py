"""Voice input, backend side (03 §4.4, §5.7, §7, §12.1): clip storage, the audio
endpoints and their outcomes, the setup item, the model-visible marker, listener
status, and audio retention in the artifacts walker.

No live model, no microphone: WAVs are generated with stdlib `wave` and transcriptions
are scripted on the fake client."""

from __future__ import annotations

import math
import os
import time

from loom import items
from loom.conversation import context, events, runner
from loom.models.fake import Scripted, ScriptedTranscription
from loom.voice import store as audio_store

MARKER = "[Voice message: transcribed by a speech model, so a word may be misheard. Read for the likely meaning.]"


def tone_wav(seconds: float = 0.4) -> bytes:
    frames = b"".join(
        int(8000 * math.sin(2 * math.pi * 440 * i / 16000)).to_bytes(2, "little", signed=True)
        for i in range(int(16000 * seconds))
    )
    return audio_store.wav_bytes(frames)


def with_transcription(app) -> None:
    app.db.execute(
        "INSERT INTO model_roles(role, preset) VALUES ('transcription', 'fake') "
        "ON CONFLICT(role) DO UPDATE SET preset=excluded.preset"
    )


async def settle(app, cid: str) -> None:
    task = app.tasks.get(f"turn:{cid}")
    if task is not None and not task.done():
        await task


def bus(app) -> list:
    seen: list = []
    app.bus.listen(seen.append)
    return seen


async def upload(client, cid: str, data: bytes, **params):
    return await client.post(f"/api/conversations/{cid}/audio", params=params, content=data,
                             headers={"Content-Type": "audio/wav"})


# ---------------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------------
def test_store_find_read(app):
    desk = app.desk_id()
    wav = tone_wav()
    ref = audio_store.store(app, desk, wav, "audio/x-wav")
    assert audio_store.AUDIO_ID_RE.match(ref["id"]) and ref == {"id": ref["id"], "mime": "audio/wav", "bytes": len(wav)}
    path, mime = audio_store.find(app, desk, ref["id"])
    assert path.name == f"audio_{ref['id']}.wav" and path.parent.name == desk and mime == "audio/wav"
    assert audio_store.read(app, desk, ref["id"]) == (wav, "audio/wav")
    assert audio_store.find(app, desk, "../../etc") is None and audio_store.find(app, desk, "0" * 26) is None


def test_store_rejects_empty_unsupported_and_oversized(app):
    desk = app.desk_id()
    for data, mime in ((b"", "audio/wav"), (b"x", "text/plain"), (b"x" * (audio_store.MAX_AUDIO_BYTES + 1), "audio/wav")):
        try:
            audio_store.store(app, desk, data, mime)
        except ValueError:
            continue
        raise AssertionError(f"accepted {mime} of {len(data)} bytes")


def test_sniff_audio():
    assert audio_store.sniff_audio(tone_wav()) == "audio/wav"
    assert audio_store.sniff_audio(b"OggS\x00\x02" + b"\x00" * 10) == "audio/ogg"
    assert audio_store.sniff_audio(b"ID3\x04" + b"\x00" * 12) == "audio/mpeg"
    assert audio_store.sniff_audio(b"\x1a\x45\xdf\xa3" + b"\x00" * 12) == "audio/webm"
    assert audio_store.sniff_audio(b"RIFF\x00\x00\x00\x00WEBPVP8 ") is None  # a webp is not audio
    assert audio_store.normalise_mime("audio/wav; codecs=1") == "audio/wav"


# ---------------------------------------------------------------------------------
# POST …/audio
# ---------------------------------------------------------------------------------
async def test_send_posts_the_transcript_with_voice_meta_and_marks_it_for_the_model(client, fake):
    app = client.loom
    desk = app.desk_id()
    with_transcription(app)
    fake.transcript("turn the oven on")
    fake.script(Scripted(content="Heating."))
    wav = tone_wav()
    r = await upload(client, desk, wav, action="send", source="hotkey")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["text"] == "turn the oven on" and isinstance(body["inbox_id"], int)
    assert fake.transcribe_calls[0]["audio"] == wav and fake.transcribe_calls[0]["mime"] == "audio/wav"
    assert audio_store.read(app, desk, body["audio_id"]) == (wav, "audio/wav")
    await settle(app, desk)

    user = events.list_events(app, desk, kinds=["user"], limit=10)[-1]
    assert user["content"] == "turn the oven on"  # stored unprefixed
    assert user["meta"]["voice"] == {"audio_id": body["audio_id"], "preset": "fake", "source": "hotkey"}
    sent = [m for m in fake.calls[0].messages if m["role"] == "user" and "oven" in str(m.get("content"))]
    assert sent and sent[-1]["content"] == f"{MARKER}\nturn the oven on"  # in-turn
    replayed = context.messages_from_events(app, desk, [user])
    assert replayed[0]["content"] == f"{MARKER}\nturn the oven on"  # replay


async def test_typed_messages_carry_no_marker(app):
    desk = app.desk_id()
    ev = events.append(app, desk, "user", "typed", {"source": "user"})
    assert context.messages_from_events(app, desk, [events.get(app, ev)])[0]["content"] == "typed"


async def test_draft_emits_voice_draft_and_posts_nothing(client, fake):
    app = client.loom
    desk = app.desk_id()
    with_transcription(app)
    seen = bus(app)
    fake.transcript("a draft")
    r = await upload(client, desk, tone_wav(), action="draft", source="composer")
    assert r.status_code == 200 and r.json()["text"] == "a draft" and "inbox_id" not in r.json()
    draft = [e for e in seen if e["type"] == "voice.draft"]
    assert draft and draft[0]["conversation_id"] == desk
    assert draft[0]["data"] == {"conversation_id": desk, "audio_id": r.json()["audio_id"], "text": "a draft"}
    assert app.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


async def test_empty_transcript_is_dropped(client, fake):
    app = client.loom
    with_transcription(app)
    fake.transcript("   ")
    r = await upload(client, app.desk_id(), tone_wav(), action="send", source="hotkey")
    assert r.status_code == 200 and r.json()["text"] == "" and r.json()["dropped"] is True
    assert app.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


async def test_failed_transcription_records_a_retryable_system_event(client, fake):
    app = client.loom
    desk = app.desk_id()
    with_transcription(app)
    seen = bus(app)
    fake.transcript(ScriptedTranscription(outcome="transient", error="provider busy (HTTP 429): slow down"))
    r = await upload(client, desk, tone_wav(), action="send", source="hotkey")
    assert r.status_code == 502
    audio_id = r.json()["audio_id"]
    assert "429" in r.json()["detail"]
    system = events.list_events(app, desk, kinds=["system"], limit=10)[-1]
    assert system["content"] == "voice message could not be transcribed: provider busy (HTTP 429): slow down"
    assert system["meta"]["voice"] == {"audio_id": audio_id, "failed": True, "action": "send"}
    failed = [e for e in seen if e["type"] == "voice.failed"][0]["data"]
    assert failed["audio_id"] == audio_id and failed["action"] == "send" and failed["event_id"] == system["id"]
    assert any(e["type"] == "event.appended" and e["data"]["id"] == system["id"] for e in seen)

    # Retry transcribes the stored bytes again; the same outcomes apply
    fake.transcript("second time lucky")
    fake.script(Scripted(content="ok"))
    r = await client.post(f"/api/conversations/{desk}/audio/{audio_id}/retry", params={"action": "send"})
    assert r.status_code == 202 and r.json()["text"] == "second time lucky" and r.json()["audio_id"] == audio_id
    assert fake.transcribe_calls[1]["audio"] == fake.transcribe_calls[0]["audio"]
    await settle(app, desk)
    user = events.list_events(app, desk, kinds=["user"], limit=10)[-1]
    assert user["meta"]["voice"]["audio_id"] == audio_id and user["meta"]["voice"]["source"] == "retry"


async def test_no_transcription_model_is_409_with_one_setup_item_resolved_by_the_role(client, fake):
    app = client.loom
    desk = app.desk_id()
    for _ in range(2):
        r = await upload(client, desk, tone_wav(), action="draft", source="composer")
        assert r.status_code == 409 and r.json()["audio_id"] and "transcription" in r.json()["detail"]
    assert not fake.transcribe_calls
    setup = [i for i in items.by_source(app, "setup:transcription") if i["dismissed_at"] is None]
    assert len(setup) == 1 and setup[0]["title"] == "Choose a transcription model" and setup[0]["kind"] == "setup"
    failed = events.list_events(app, desk, kinds=["system"], limit=10)
    assert all(e["meta"]["voice"]["failed"] for e in failed) and len(failed) == 2
    desk_item = items.post(app, "setup", "Choose a model", source="setup")

    r = await client.put("/api/model-roles/transcription", json={"preset": "fake"})
    assert r.status_code == 200
    assert all(i["dismissed_at"] for i in items.by_source(app, "setup:transcription"))
    assert items.get(app, desk_item["id"])["dismissed_at"] is None  # another role's item stays


async def test_send_needs_the_conversation_model(client, fake):
    app = client.loom
    with_transcription(app)
    app.db.execute("UPDATE model_roles SET preset=NULL WHERE role='desk'")
    r = await upload(client, app.desk_id(), tone_wav(), action="send", source="hotkey")
    assert r.status_code == 409 and not fake.transcribe_calls


async def test_upload_rejections(client, fake):
    app = client.loom
    desk = app.desk_id()
    with_transcription(app)
    r = await client.post(f"/api/conversations/{desk}/audio", content=b"hello", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = await client.post(f"/api/conversations/{desk}/audio", content=b"", headers={"Content-Type": "audio/wav"})
    assert r.status_code == 400
    assert (await upload(client, desk, tone_wav(), action="shout")).status_code == 400
    assert (await upload(client, desk, tone_wav(), source="telepathy")).status_code == 400
    assert (await upload(client, "nope", tone_wav())).status_code == 404
    r = await client.post(f"/api/conversations/{desk}/audio/{'0' * 26}/retry")
    assert r.status_code == 404
    assert not fake.transcribe_calls


async def test_get_audio_serves_the_bytes_immutably(client, fake):
    app = client.loom
    desk = app.desk_id()
    wav = tone_wav()
    ref = audio_store.store(app, desk, wav, "audio/wav")
    r = await client.get(f"/api/conversations/{desk}/audio/{ref['id']}")
    assert r.status_code == 200 and r.content == wav and r.headers["content-type"] == "audio/wav"
    assert "immutable" in r.headers["cache-control"]
    assert (await client.get(f"/api/conversations/{desk}/audio/not-an-id")).status_code == 404


# ---------------------------------------------------------------------------------
# listener status
# ---------------------------------------------------------------------------------
async def test_voice_status_defaults_then_reports(client):
    app = client.loom
    r = await client.get("/api/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["state"] == "disabled" and body["running"] is False
    assert body["pid"] is None and body["hotkey"] == "ctrl+alt+space" and body["mode"] == "hold"
    assert body["devices"] == [] and body["device"] == "" and body["error"] is None and body["updated_at"] is None

    app.settings.set(app.db, "voice.enabled", True)
    assert (await client.get("/api/voice/status")).json()["state"] == "stopped"
    seen = bus(app)
    r = await client.post("/api/voice/status", json={
        "state": "error", "hotkey": "ctrl+alt+space", "mode": "hold",
        "error": "hotkey ctrl+alt+space is taken by another application",
        "devices": ["Microphone (USB)", "Headset"], "device": "Headset",
    })
    assert r.status_code == 200
    body = (await client.get("/api/voice/status")).json()
    assert body["state"] == "error" and body["error"].startswith("hotkey") and body["devices"] == ["Microphone (USB)", "Headset"]
    assert body["device"] == "Headset" and body["updated_at"]
    event = [e for e in seen if e["type"] == "voice.status"][0]
    assert event["data"] == r.json() and event["conversation_id"] is None

    await client.post("/api/voice/status", json={"state": "idle"})
    body = (await client.get("/api/voice/status")).json()
    assert body["state"] == "idle" and body["error"] is None


# ---------------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------------
def _age(path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_audio_retention_is_its_own_rule(app):
    desk = app.desk_id()
    app.settings.set(app.db, "voice.audio_retention_days", 30)
    app.settings.set(app.db, "conversation.scratch_idle_days", 7)
    old = audio_store.find(app, desk, audio_store.store(app, desk, tone_wav(), "audio/wav")["id"])[0]
    new = audio_store.find(app, desk, audio_store.store(app, desk, tone_wav(), "audio/wav")["id"])[0]
    week = audio_store.find(app, desk, audio_store.store(app, desk, tone_wav(), "audio/wav")["id"])[0]
    scratch = old.parent / "download.bin"
    scratch.write_bytes(b"x")
    _age(old, 31)
    _age(week, 10)  # past scratch_idle_days, inside audio retention: kept
    _age(scratch, 10)
    stats = app.artifacts.prune()
    assert not old.exists() and new.exists() and week.exists() and not scratch.exists()
    assert stats["audio"] == 1 and stats["files"] == 2


def test_audio_retention_applies_inside_the_archive_grace(app):
    from loom import db
    from loom.ids import ulid

    cid = ulid()
    now = db.now_iso()
    app.db.execute(
        "INSERT INTO conversations(id, kind, title, role, status, created_at, updated_at, archived_at) "
        "VALUES (?, 'thread', 't', 'worker', 'archived', ?, ?, ?)",
        (cid, now, now, now),
    )
    app.settings.set(app.db, "voice.audio_retention_days", 1)
    path = audio_store.find(app, cid, audio_store.store(app, cid, tone_wav(), "audio/wav")["id"])[0]
    _age(path, 2)
    assert app.artifacts.prune()["audio"] == 1 and not path.exists()


async def test_housekeeping_reports_pruned_audio(app):
    from loom.knowledge.maintenance import Maintenance, new_report

    desk = app.desk_id()
    app.settings.set(app.db, "voice.audio_retention_days", 1)
    app.settings.set(app.db, "knowledge.auto_commit_external_edits", False)
    path = audio_store.find(app, desk, audio_store.store(app, desk, tone_wav(), "audio/wav")["id"])[0]
    _age(path, 3)
    report = new_report("test")
    await Maintenance(app)._housekeep(report)
    assert report["pruned_audio"] == 1 and report["pruned_files"] >= 1 and not path.exists()


def test_runner_import_keeps_user_text():  # the runner renders through the same helper
    assert runner.user_text is context.user_text
