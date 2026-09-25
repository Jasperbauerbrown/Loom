"""Suspend is a named event, not a provider fault (TD audit F7).

The laptop spent ~60 of the tower-defense run's 136 minutes in Modern Standby. Every
wake made the 120 s stream-idle watchdog fire, and the only trace was a log line blaming
the provider. `_call_model` now measures each call on both clocks: wall-clock time that
the monotonic clock did not see is suspended time, so the failure is re-labelled, the
call is retried for free, and the retry budget is left for real provider errors.
"""

from __future__ import annotations

import pytest

from loom import conversation
from loom.conversation import events, runner
from loom.models.fake import Scripted


class Clock:
    """A stand-in for the `time` module. `advance` moves the wall clock only, which is
    exactly what a suspend looks like from inside the process."""

    def __init__(self) -> None:
        self.wall = 1_700_000_000.0
        self.mono = 10_000.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, wall: float, mono: float = 0.0) -> None:
        self.wall += wall
        self.mono += mono


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(runner, "time", c)
    return c


async def _run(app, cid):
    await runner.run_turn(app, cid)
    task = app.tasks.get(f"turn:{cid}")
    if task is not None and not task.done():
        await task


def _script(fake, clock: Clock, steps: list[tuple[float, Scripted]]) -> list[int]:
    """Drive the turn calls in order; each entry says how far the wall clock jumped
    *during* that call (the machine slept) and what the call then returned."""
    seen: list[int] = []

    def respond(req):
        i = len(seen)
        seen.append(i)
        drift, scripted = steps[min(i, len(steps) - 1)]
        clock.advance(drift)
        return scripted

    fake.on("turn", respond)
    return seen


def _systems(app, cid) -> list[str]:
    return [e["content"] for e in events.list_events(app, cid, kinds=["system"], limit=100)]


# -- the free retry ------------------------------------------------------------------------

async def test_a_call_spanning_a_standby_is_named_and_retried_for_free(app, fake, clock):
    desk = app.desk_id()
    watchdog = Scripted(outcome="transient", error="no data for 120s")
    seen = _script(fake, clock, [
        (20 * 60.0, watchdog),            # 20 minutes of standby, then the watchdog fires
        (0.0, watchdog),                  # an ordinary transient, awake: the real retry
        (0.0, Scripted(content="fine now")),
    ])
    conversation.post(app, desk, "hi")
    await _run(app, desk)

    assert "system resumed after 20 min; retrying" in _systems(app, desk)
    assert len(seen) == 3, "the suspend retry must not have eaten the transient retry"
    assert [e["kind"] for e in events.list_events(app, desk, kinds=["assistant"], limit=10)] == ["assistant"]
    assert events.list_events(app, desk, kinds=["assistant"], limit=10)[0]["content"] == "fine now"
    assert app.conversation(desk)["status"] == "idle"


async def test_drift_under_the_threshold_is_an_ordinary_failure(app, fake, clock):
    """Below SUSPEND_DRIFT_S nothing changes: no event, the ordinary policy applies. This
    is also the shape on a platform whose monotonic clock counts suspended time."""
    desk = app.desk_id()
    app.settings.set(app.db, "conversation.model_error_retries", 0)
    seen = _script(fake, clock, [(float(app.settings.get("conversation.suspend_drift_s")) - 1, Scripted(outcome="transient", error="boom"))])
    conversation.post(app, desk, "hi")
    await _run(app, desk)

    assert len(seen) == 1
    assert not any("system resumed" in s for s in _systems(app, desk))
    assert any("model call failed (transient)" in s for s in _systems(app, desk))


# -- the bound -----------------------------------------------------------------------------

async def test_a_clock_that_keeps_jumping_cannot_spin(app, fake, clock):
    """A repeatedly-jumping clock buys SUSPEND_RETRIES free calls and no more; after that
    the ordinary outcome policy takes the call, and the turn ends."""
    desk = app.desk_id()
    app.settings.set(app.db, "conversation.model_error_retries", 0)
    seen = _script(fake, clock, [(30 * 60.0, Scripted(outcome="transient", error="no data for 120s"))])
    conversation.post(app, desk, "hi")
    await _run(app, desk)

    resumed = [s for s in _systems(app, desk) if s.startswith("system resumed")]
    assert resumed == ["system resumed after 30 min; retrying"] * int(app.settings.get("conversation.suspend_retries"))
    assert len(seen) == int(app.settings.get("conversation.suspend_retries")) + 1
    assert any("model call failed (transient)" in s for s in _systems(app, desk))
    assert app.conversation(desk)["status"] == "idle"


async def test_a_short_suspend_still_reports_at_least_a_minute(app, fake, clock):
    desk = app.desk_id()
    app.settings.set(app.db, "conversation.model_error_retries", 0)
    _script(fake, clock, [
        (float(app.settings.get("conversation.suspend_drift_s")) + 1, Scripted(outcome="transient", error="no data for 120s")),
        (0.0, Scripted(content="back")),
    ])
    conversation.post(app, desk, "hi")
    await _run(app, desk)
    assert "system resumed after 1 min; retrying" in _systems(app, desk)
