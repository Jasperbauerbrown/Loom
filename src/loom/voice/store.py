"""Voice clips on disk (03 §4.4, §5.7).

A clip lives beside the conversation's images as `artifacts/<conv>/audio_<ulid>.<ext>`
with no table row; the bare ULID is the clip's id and its strict shape is the traversal
guard. Persist before act: the file is written before anything transcribes it or any
event points at it, so a failed transcription can be retried from the stored bytes.

Loom itself only ever produces WAV PCM16 mono 16 kHz (the listener and the browser
both), but any audio type a transcription endpoint commonly accepts is stored and
passed through untouched. The cap is a fixed 25 MB — OpenAI's upload limit, the
tightest of the hosted endpoints; 120 s of 16 kHz PCM16 is about 3.8 MB.

Retention (`voice.audio_retention_days`) is applied by the artifacts walker that
housekeeping runs (`Store.prune`), which keeps `audio_*` files until then.
"""

from __future__ import annotations

import io
import logging
import re
import wave
from pathlib import Path
from typing import Any

from ..artifacts import AUDIO_PREFIX, conversation_dir
from ..ids import ulid

log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 25 * 1024 * 1024
SAMPLE_RATE = 16_000

AUDIO_MIMES = {
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/flac": ".flac",
}
_ALIASES = {
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/mp3": "audio/mpeg",
    "audio/x-m4a": "audio/mp4",
    "audio/x-flac": "audio/flac",
}
_EXT_TO_MIME = {ext: mime for mime, ext in AUDIO_MIMES.items()}
AUDIO_ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def normalise_mime(declared: str | None) -> str | None:
    """A Content-Type header (parameters dropped, aliases folded) as a supported audio
    mime, else None."""
    mime = (declared or "").split(";")[0].strip().lower()
    mime = _ALIASES.get(mime, mime)
    return mime if mime in AUDIO_MIMES else None


def sniff_audio(data: bytes) -> str | None:
    """The mime of a supported audio container by its magic bytes, else None."""
    head = bytes(data[:16])
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"fLaC":
        return "audio/flac"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    if head[4:8] == b"ftyp":
        return "audio/mp4"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    return None


def store(app: Any, conversation_id: str, data: bytes, mime: str) -> dict[str, Any]:
    """Write the clip; returns `{"id", "mime", "bytes"}`. `ValueError` for an empty
    clip, an unsupported type or one over `MAX_AUDIO_BYTES`."""
    mime = normalise_mime(mime) or ""
    ext = AUDIO_MIMES.get(mime)
    if ext is None:
        raise ValueError(f"unsupported audio type (supported: {', '.join(sorted(AUDIO_MIMES))})")
    if not data:
        raise ValueError("audio is empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise ValueError(f"audio is {len(data)} bytes, over the {MAX_AUDIO_BYTES // (1024 * 1024)} MB cap")
    folder = conversation_dir(app, conversation_id)
    folder.mkdir(parents=True, exist_ok=True)
    audio_id = ulid()
    (folder / f"{AUDIO_PREFIX}{audio_id}{ext}").write_bytes(bytes(data))
    return {"id": audio_id, "mime": mime, "bytes": len(data)}


def find(app: Any, conversation_id: str, audio_id: str) -> tuple[Path, str] | None:
    """`(path, mime)` for a stored clip, or None for a bad id shape or a missing file."""
    if not AUDIO_ID_RE.match(str(audio_id or "")):
        return None
    try:
        folder = conversation_dir(app, conversation_id)
    except ValueError:
        return None
    for ext, mime in _EXT_TO_MIME.items():
        path = folder / f"{AUDIO_PREFIX}{audio_id}{ext}"
        if path.is_file():
            return path, mime
    return None


def read(app: Any, conversation_id: str, audio_id: str) -> tuple[bytes, str] | None:
    found = find(app, conversation_id, audio_id)
    if found is None:
        return None
    path, mime = found
    try:
        return path.read_bytes(), mime
    except OSError as exc:
        log.warning("audio %s unreadable: %s", audio_id, exc)
        return None


def wav_bytes(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """Mono PCM16 samples as a WAV file — the one format Loom produces."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def silence_wav(seconds: float, rate: int = SAMPLE_RATE) -> bytes:
    """What a transcription preset's Test sends."""
    return wav_bytes(b"\x00\x00" * int(rate * seconds), rate)
