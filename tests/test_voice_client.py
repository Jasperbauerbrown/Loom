"""Transcription through the model client (03 §5.7): the multipart request, one outcome
per failure class, the Ollama refusal, the role lookup, the transcription model list and
the preset Test.

Every transport call goes through httpx.MockTransport — no network, no live model."""

from __future__ import annotations

import io
import wave

import httpx

from loom import db
from loom.models import client as model_client
from loom.models.client import TRANSCRIPTION_OLLAMA, Client
from loom.models.fake import ScriptedTranscription
from loom.voice.store import silence_wav, wav_bytes


def tone_wav(seconds: float = 0.5) -> bytes:
    import math

    frames = b"".join(
        int(8000 * math.sin(2 * math.pi * 440 * i / 16000)).to_bytes(2, "little", signed=True)
        for i in range(int(16000 * seconds))
    )
    return wav_bytes(frames)


def hosted(app, *, params: dict | None = None, base: str = "https://api.example.test/v1", role: bool = True) -> None:
    now = db.now_iso()
    app.db.execute(
        "INSERT OR REPLACE INTO providers(name, kind, base_url, api_key_ref, params, enabled, created_at, updated_at) "
        "VALUES ('hosted', 'openai_compatible', ?, NULL, '{}', 1, ?, ?)",
        (base, now, now),
    )
    app.db.execute(
        "INSERT OR REPLACE INTO presets(name, provider, model, params, enabled, created_at, updated_at) "
        "VALUES ('whisper', 'hosted', 'whisper-large-v3', ?, 1, ?, ?)",
        (db.dumps(params or {}), now, now),
    )
    if role:
        app.db.execute(
            "INSERT INTO model_roles(role, preset) VALUES ('transcription', 'whisper') "
            "ON CONFLICT(role) DO UPDATE SET preset=excluded.preset"
        )


def real(app, handler) -> Client:
    client = Client(app, transport=httpx.MockTransport(handler))
    app.models = client
    return client


def form_fields(request: httpx.Request) -> dict[str, str | bytes]:
    """Parse the multipart body the client sent (stdlib email parser)."""
    from email.parser import BytesParser
    from email.policy import HTTP

    header = request.headers["content-type"].encode()
    message = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + header + b"\r\n\r\n" + request.content)
    out: dict[str, str | bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        out[name] = payload if part.get_filename() else payload.decode()
        if part.get_filename():
            out[f"{name}.filename"] = part.get_filename()
            out[f"{name}.type"] = part.get_content_type()
    return out


async def test_transcribe_sends_multipart_with_preset_params(app):
    hosted(app, params={"language": "en", "prompt": "Loom, Ollama", "temperature": 0, "timeout_s": 30})
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["fields"] = form_fields(request)
        return httpx.Response(200, json={"text": "  hello loom  "})

    audio = tone_wav()
    got = await real(app, handler).transcribe(audio, "audio/wav")
    assert got.ok and got.text == "hello loom" and got.model == "whisper-large-v3" and got.error is None
    assert seen["url"] == "https://api.example.test/v1/audio/transcriptions"  # no doubled /v1
    fields = seen["fields"]
    assert fields["model"] == "whisper-large-v3" and fields["response_format"] == "json"
    assert fields["language"] == "en" and fields["prompt"] == "Loom, Ollama" and fields["temperature"] == "0.0"
    assert fields["file"] == audio and fields["file.filename"] == "audio.wav" and fields["file.type"] == "audio/wav"


async def test_transcribe_omits_unset_params(app):
    hosted(app)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["fields"] = form_fields(request)
        return httpx.Response(200, json={"text": "x"})

    await real(app, handler).transcribe(tone_wav(), "audio/wav")
    assert set(seen["fields"]) == {"model", "response_format", "file", "file.filename", "file.type"}


async def test_transcribe_outcomes_never_raise(app):
    hosted(app)
    cases = [
        (lambda r: httpx.Response(401, text="bad key"), "fatal", "authentication failed"),
        (lambda r: httpx.Response(404, text="nope"), "fatal", "not found"),
        (lambda r: httpx.Response(429, text="slow down"), "transient", "busy"),
        (lambda r: httpx.Response(503, text="down"), "transient", "provider error"),
        (lambda r: httpx.Response(200, text="<html>"), "transient", "malformed"),
        (lambda r: httpx.Response(200, json={"segments": []}), "transient", "no `text`"),
    ]
    for handler, outcome, needle in cases:
        got = await real(app, handler).transcribe(tone_wav(), "audio/wav")
        assert (got.outcome, got.text) == (outcome, ""), (outcome, got)
        assert needle in (got.error or ""), got.error


async def test_transcribe_network_error_is_transient(app):
    hosted(app)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    got = await real(app, handler).transcribe(tone_wav(), "audio/wav")
    assert got.outcome == "transient" and "ConnectError" in got.error


async def test_transcribe_on_ollama_is_fatal_without_a_request(app):
    calls = []
    app.db.execute("INSERT OR REPLACE INTO model_roles(role, preset) VALUES ('transcription', 'fake')")
    got = await real(app, lambda r: calls.append(r) or httpx.Response(200)).transcribe(tone_wav(), "audio/wav")
    assert got.outcome == "fatal" and got.error == TRANSCRIPTION_OLLAMA and not calls


async def test_transcribe_without_role_names_the_settings_url(app):
    hosted(app, role=False)
    got = await real(app, lambda r: httpx.Response(200, json={"text": "x"})).transcribe(tone_wav(), "audio/wav")
    assert got.outcome == "fatal" and "transcription" in got.error and "/settings/models" in got.error


async def test_transcribe_explicit_preset_wins_over_role(app):
    hosted(app, role=False)
    got = await real(app, lambda r: httpx.Response(200, json={"text": "ok"})).transcribe(
        tone_wav(), "audio/wav", preset="whisper"
    )
    assert got.ok and got.text == "ok"


async def test_list_models_filters_transcription_on_openai_compatible(app):
    hosted(app)
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "/api/tags" in str(request.url):
            return httpx.Response(200, json={"models": [{"name": "qwen"}]})
        return httpx.Response(200, json={"data": [{"id": "openai/whisper-1"}]})

    client = real(app, handler)
    assert await client.list_models("hosted", role="transcription") == ["openai/whisper-1"]
    assert await client.list_models("hosted") == ["openai/whisper-1"]
    assert await client.list_models("local-ollama", role="transcription") == ["qwen"]
    assert urls[0] == "https://api.example.test/v1/models?output_modalities=transcription"
    assert urls[1] == "https://api.example.test/v1/models"
    assert "output_modalities" not in urls[2]


async def test_models_endpoint_passes_role(client):
    app = client.loom
    hosted(app)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "whisper-1"}]})

    real(app, handler)
    r = await client.get("/api/models", params={"provider": "hosted", "role": "transcription"})
    assert r.status_code == 200 and r.json()["providers"][0]["models"] == ["whisper-1"]
    assert seen == ["https://api.example.test/v1/models?output_modalities=transcription"]


async def test_preset_test_sends_one_second_of_silence(app, fake):
    hosted(app)
    fake.transcript("")
    report = await model_client.test_preset(app, "whisper")
    assert report["outcome"] == "ok" and report["error"] is None
    assert "empty transcript" in report["content"]
    sent = fake.transcribe_calls[0]
    assert sent["mime"] == "audio/wav" and sent["preset"] == "whisper"
    with wave.open(io.BytesIO(sent["audio"])) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 16000)
    assert sent["audio"] == silence_wav(1.0)


async def test_preset_test_reports_a_failed_transcription(app, fake):
    hosted(app)
    fake.transcript(ScriptedTranscription(outcome="fatal", error="authentication failed (HTTP 401): bad key"))
    report = await model_client.test_preset(app, "whisper")
    assert report["outcome"] == "fatal" and "401" in report["error"] and report["content"] == ""


async def test_preset_test_of_a_chat_preset_is_unchanged(app, fake):
    hosted(app)
    fake.reply("pong")
    report = await model_client.test_preset(app, "fake")
    assert report["outcome"] == "ok" and report["content"] == "pong" and not fake.transcribe_calls


async def test_fake_client_scripts_and_fails_loudly(fake):
    fake.transcript("one", ScriptedTranscription(outcome="transient", error="busy"))
    assert (await fake.transcribe(b"a", "audio/wav")).text == "one"
    assert (await fake.transcribe(b"b", "audio/wav")).outcome == "transient"
    try:
        await fake.transcribe(b"c", "audio/wav")
    except AssertionError as exc:
        assert "no scripted transcription" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unscripted transcription must fail loudly")
