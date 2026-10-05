"""Cancellation (03 §6.4): one signal per conversation, and one way to await under it.

`POST /conversations/{id}/cancel` sets the conversation's signal (`app.cancel_flags.add`).
Every await a turn spends real time in — the model call (queue wait, prompt processing,
streaming), the tool dispatch (judge, handler), a compaction — goes through `race`,
which runs the work beside the signal and, when the signal wins, cancels the work and
raises `Cancelled`. Cancelling the work is what stops it: the queue drops the waiter,
the HTTP stream closes, `shell` kills its process tree. There is no per-tool path.

The runner discards the signal when its turn ends, so a cancel never leaks into the next
turn. What each race interrupted is noted, so the turn's end can say what was stopped.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable


class Cancelled(Exception):
    """The conversation's cancel signal won the race. `value` is what the interrupted
    work answered with when it chose to answer on cancellation (a `shell` call returns
    what the command printed before its tree was killed), else None."""

    def __init__(self, value: Any = None) -> None:
        super().__init__("cancelled by user")
        self.value = value


class CancelFlags:
    """Per-conversation cancel signals. Set-like (`add`, `discard`, `in`) so callers that
    only flag or test keep reading naturally; `event` is what `race` awaits."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._stopped: dict[str, list[str]] = {}

    def event(self, conversation_id: str) -> asyncio.Event:
        ev = self._events.get(conversation_id)
        if ev is None:
            ev = self._events[conversation_id] = asyncio.Event()
        return ev

    def add(self, conversation_id: str) -> None:
        self.event(conversation_id).set()

    def discard(self, conversation_id: str) -> None:
        self._events.pop(conversation_id, None)
        self._stopped.pop(conversation_id, None)

    def __contains__(self, conversation_id: object) -> bool:
        ev = self._events.get(conversation_id) if isinstance(conversation_id, str) else None
        return ev is not None and ev.is_set()

    def note(self, conversation_id: str, what: str) -> None:
        self._stopped.setdefault(conversation_id, []).append(what)

    def stopped(self, conversation_id: str) -> list[str]:
        """What races interrupted in this conversation since its signal was last cleared."""
        return list(self._stopped.get(conversation_id, []))


async def race(flags: CancelFlags | None, conversation_id: str | None, work: Awaitable[Any], what: str | None = None) -> Any:
    """Await `work` unless the conversation's cancel signal fires first.

    Signal first: `work` is cancelled and awaited to its end (so a killed process is
    reaped before this returns), `what` is noted, and `Cancelled` is raised carrying
    whatever `work` answered. Signal already set: `work` never starts. The caller itself
    cancelled (shutdown, an outer timeout or an outer race): `work` is cancelled the same
    way and the `CancelledError` propagates; if that cascade came from the signal,
    `what` is still noted — the innermost labelled await is the one that says what it
    was doing."""
    if flags is None or not conversation_id:
        return await work
    signal = flags.event(conversation_id)
    if signal.is_set():
        if inspect.iscoroutine(work):
            work.close()  # never started; no "never awaited" warning
        raise Cancelled()
    # Started eagerly: the work runs up to its first suspension right here, exactly as a
    # plain `await` would, so racing it never reorders it behind other ready tasks.
    if inspect.iscoroutine(work):
        task: asyncio.Future = asyncio.Task(work, loop=asyncio.get_running_loop(), eager_start=True)
    else:
        task = asyncio.ensure_future(work)
    if task.done():
        return task.result()  # finished without suspending: no wait, no extra loop turn
    waiter = asyncio.ensure_future(signal.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        waiter.cancel()
        await _stop(task)
        if signal.is_set() and what:
            flags.note(conversation_id, what)
        raise
    if task.done():
        waiter.cancel()
        return task.result()
    await _stop(task)
    if what:
        flags.note(conversation_id, what)
    value = None
    if not task.cancelled() and task.exception() is None:
        value = task.result()
    raise Cancelled(value)


async def _stop(task: asyncio.Future) -> None:
    """Cancel `task` and wait for it to finish (a killed process tree is reaped before
    anyone moves on). Being cancelled meanwhile does not abandon the wait; it is
    re-raised once the task is done."""
    task.cancel()
    interrupted = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            interrupted = True
    if not task.cancelled():
        task.exception()  # retrieved: a crash while being cancelled is not "never retrieved"
    if interrupted:
        raise asyncio.CancelledError
