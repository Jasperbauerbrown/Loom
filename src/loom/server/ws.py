"""`/ws`: one multiplexed socket per surface (03 §12.2).

The client may send `{"subscribe": {"conversations": ["01J…"] | "all"}}` at any time
(default: all) and `{"type": "ping"}` to keep the line warm. The server forwards bus
events verbatim as `{type, ts, conversation_id, data}` — surfaces render what they
understand and ignore the rest (charter §5.2).

One bus subscription per socket, dropped on disconnect. A slow client overflows its own
queue and loses events (the Bus says so); it never stalls the backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

log = logging.getLogger(__name__)


class Hub:
    """Tracks open sockets so `/system/health` can report how many surfaces are live."""

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()

    def connected(self) -> int:
        return len(self._sockets)

    async def serve(self, websocket: WebSocket, app) -> None:  # type: ignore[no-untyped-def]
        await websocket.accept()
        self._sockets.add(websocket)
        sub = app.bus.subscribe("all")
        pump = asyncio.create_task(self._pump(websocket, sub))
        try:
            while True:
                raw = await websocket.receive_text()
                await self._handle(websocket, sub, raw)
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            pass  # socket closed under us
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            app.bus.unsubscribe(sub)
            self._sockets.discard(websocket)
            if websocket.client_state is not WebSocketState.DISCONNECTED:
                with contextlib.suppress(Exception):
                    await websocket.close()

    async def _handle(self, websocket: WebSocket, sub, raw: str) -> None:  # type: ignore[no-untyped-def]
        try:
            message = json.loads(raw)
        except ValueError:
            await self._send(websocket, {"type": "error", "data": {"message": "expected JSON"}})
            return
        if not isinstance(message, dict):
            return
        if message.get("type") == "ping":
            await self._send(websocket, {"type": "pong", "data": None})
            return
        subscribe = message.get("subscribe")
        if isinstance(subscribe, dict):
            conversations = subscribe.get("conversations", "all")
            sub.conversations = "all" if conversations == "all" else set(conversations or [])
            await self._send(
                websocket,
                {
                    "type": "subscribed",
                    "data": {"conversations": "all" if sub.conversations == "all" else sorted(sub.conversations)},
                },
            )

    async def _pump(self, websocket: WebSocket, sub) -> None:  # type: ignore[no-untyped-def]
        while True:
            event = await sub.queue.get()
            try:
                await self._send(websocket, event)
            except Exception:
                return

    async def _send(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        if websocket.client_state is not WebSocketState.CONNECTED:
            raise RuntimeError("socket is not connected")
        await websocket.send_text(json.dumps(payload, default=str))

    async def close_all(self) -> None:
        for websocket in list(self._sockets):
            with contextlib.suppress(Exception):
                await websocket.close()
        self._sockets.clear()
