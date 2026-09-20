"""Per-provider priority queue (03 §5.3). Every model call passes through it.

    async with app.models.queue.slot("local-ollama", 0, {"purpose": "turn"}):
        ...one HTTP call...

Priority 0 interactive, 1 background, 2 maintenance; FIFO within a priority. A provider
serves `concurrency` calls at a time (default 1 for Ollama, 4 for hosted), so preemption
is at call boundaries only — the queue never cancels work, it only orders it.

Depth and occupancy are exposed on the bus as `system.queue` and at
`GET /api/system/queue`, which is what the UI header renders.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..db import now_iso


@dataclass(order=True)
class _Waiter:
    priority: int
    seq: int
    future: asyncio.Future = field(compare=False, default=None)  # type: ignore[assignment]


@dataclass
class _Running:
    provider: str
    purpose: str
    conversation_id: str | None
    priority: int
    since: str
    preset: str = ""
    model: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "purpose": self.purpose,
            "conversation_id": self.conversation_id,
            "priority": self.priority,
            "since": self.since,
            "preset": self.preset,
            "model": self.model,
        }


class _Lane:
    """One provider's waiting list and running set."""

    def __init__(self, concurrency: int):
        self.concurrency = max(1, int(concurrency))
        self.waiting: list[_Waiter] = []
        self.running: list[_Running] = []
        # Slots handed to a waiter whose coroutine has not resumed yet. Counting them
        # keeps one event-loop turn from granting the same slot twice.
        self.granted: int = 0


class ModelQueue:
    def __init__(self, app=None):  # type: ignore[no-untyped-def]
        self.app = app
        self._lanes: dict[str, _Lane] = {}
        self._seq = itertools.count()

    # -- lanes ------------------------------------------------------------------
    def _lane(self, provider: str, concurrency: int | None = None) -> _Lane:
        lane = self._lanes.get(provider)
        if lane is None:
            lane = self._lanes[provider] = _Lane(concurrency if concurrency is not None else 1)
        elif concurrency is not None and int(concurrency) != lane.concurrency:
            lane.concurrency = max(1, int(concurrency))
            self._pump(provider)
        return lane

    def set_concurrency(self, provider: str, concurrency: int) -> None:
        self._lane(provider, concurrency)

    # -- the one acquisition path -----------------------------------------------
    @contextlib.asynccontextmanager
    async def slot(
        self,
        provider: str,
        priority: int = 0,
        info: dict[str, Any] | None = None,
        concurrency: int | None = None,
    ) -> AsyncIterator[None]:
        info = dict(info or {})
        lane = self._lane(provider, concurrency)
        waiter: _Waiter | None = None
        if len(lane.running) + lane.granted >= lane.concurrency:
            waiter = _Waiter(int(priority), next(self._seq), asyncio.get_running_loop().create_future())
            lane.waiting.append(waiter)
            lane.waiting.sort()
            self._emit()
            try:
                await waiter.future
            except asyncio.CancelledError:
                with contextlib.suppress(ValueError):
                    lane.waiting.remove(waiter)
                if waiter.future.done() and not waiter.future.cancelled():
                    lane.granted -= 1
                    self._pump(provider)
                self._emit()
                raise
            lane.granted -= 1
        entry = _Running(
            provider=provider,
            purpose=str(info.get("purpose") or "turn"),
            conversation_id=info.get("conversation_id"),
            priority=int(priority),
            since=now_iso(),
            preset=str(info.get("preset") or ""),
            model=str(info.get("model") or ""),
        )
        lane.running.append(entry)
        self._emit()
        try:
            yield
        finally:
            with contextlib.suppress(ValueError):
                lane.running.remove(entry)
            self._pump(provider)
            self._emit()

    def _pump(self, provider: str) -> None:
        lane = self._lanes.get(provider)
        if lane is None:
            return
        while lane.waiting and len(lane.running) + lane.granted < lane.concurrency:
            waiter = lane.waiting.pop(0)
            if waiter.future.done():
                continue
            waiter.future.set_result(None)
            lane.granted += 1

    # -- introspection -----------------------------------------------------------
    def state(self) -> dict[str, Any]:
        running = [r.to_json() for lane in self._lanes.values() for r in lane.running]
        running.sort(key=lambda r: (r["since"], r["provider"]))
        depth = sum(len(lane.waiting) for lane in self._lanes.values())
        return {"depth": depth, "running": running}

    def _emit(self) -> None:
        if self.app is None:
            return
        state = self.state()
        if state == getattr(self, "_last_state", None):
            return
        self._last_state = state
        self.app.bus.emit("system.queue", state)
