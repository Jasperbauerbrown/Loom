"""In-process event bus: the one stream every surface consumes (charter §5.2, 03 §12.2).

Events are dicts `{"type", "ts", "conversation_id", "data"}`. `emit` is synchronous so
persistence code can emit right after writing a row without awaiting. Subscribers get
an `asyncio.Queue`; slow subscribers are dropped when their queue overflows (a
disconnected socket must never stall the backend).

Event types are the strings listed in 03 §12.2; this module does not validate them,
surfaces ignore what they do not understand.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .db import now_iso

log = logging.getLogger(__name__)

Filter = Literal["all"] | set[str]


@dataclass
class Subscription:
    queue: asyncio.Queue
    conversations: Filter = "all"
    dropped: int = 0

    def wants(self, event: dict[str, Any]) -> bool:
        if self.conversations == "all":
            return True
        cid = event.get("conversation_id")
        return cid is None or cid in self.conversations


@dataclass
class Bus:
    maxsize: int = 2000
    _subs: list[Subscription] = field(default_factory=list)
    _listeners: list[Callable[[dict[str, Any]], None]] = field(default_factory=list)

    def subscribe(self, conversations: Filter = "all") -> Subscription:
        sub = Subscription(asyncio.Queue(maxsize=self.maxsize), conversations)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        try:
            self._subs.remove(sub)
        except ValueError:
            pass

    def listen(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Synchronous in-process listener (router, tests). Exceptions are logged."""
        self._listeners.append(fn)

    def unlisten(self, fn: Callable[[dict[str, Any]], None]) -> None:
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass

    def emit(self, type: str, data: Any = None, conversation_id: str | None = None) -> dict[str, Any]:
        event = {"type": type, "ts": now_iso(), "conversation_id": conversation_id, "data": data}
        for sub in list(self._subs):
            if not sub.wants(event):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                sub.dropped += 1
                if sub.dropped in (1, 100, 1000):
                    log.warning("bus: subscriber overflow (%d dropped)", sub.dropped)
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:
                log.exception("bus listener failed for %s", type)
        return event
