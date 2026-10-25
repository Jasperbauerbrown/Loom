"""Voice: audio in (and, later, out) — 03 §5.7.

The listener (`listener.py` logic + `win32.py` shim) owns the global hotkey and the
microphone and runs as a child process of the backend, kept alive by `supervise.py`;
`store.py` keeps clips; the transcription itself is `app.models.transcribe` through the
`transcription` role.

`install` attaches the supervisor as `app.extras["voice_listener"]`; `start` begins
supervising (it spawns only when `voice.enabled` and not a `--fake` serve); `stop` ends
the child. `listener_state(app)` is what `GET /api/voice/status` merges in.
"""

from __future__ import annotations

from typing import Any

EXTRA = "voice_listener"


def install(app) -> None:  # type: ignore[no-untyped-def]
    from .supervise import Supervisor

    app.extras[EXTRA] = Supervisor(app)


async def start(app) -> None:  # type: ignore[no-untyped-def]
    supervisor = app.extras.get(EXTRA)
    if supervisor is not None:
        await supervisor.start()


async def stop(app) -> None:  # type: ignore[no-untyped-def]
    supervisor = app.extras.get(EXTRA)
    if supervisor is not None:
        await supervisor.stop()


def listener_state(app) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """`{running: bool, pid: int | None}` for the supervised listener child."""
    supervisor = app.extras.get(EXTRA)
    if supervisor is None:
        return {"running": False, "pid": None}
    return supervisor.state()
