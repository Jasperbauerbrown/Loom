"""JSON-RPC 2.0 transports for the scratch-built MCP client (03 §10.1).

Two transports, one interface, so `McpClient` never asks which it is talking to:

    await t.open()                       # spawn / prepare
    result = await t.request(method, params, timeout)   # correlated, raises McpError
    await t.notify(method, params)
    t.on_notification(cb)                # notifications/tools/list_changed etc.
    await t.close()

`request` correlates by id, raises `McpError(code, message)` for a JSON-RPC error
response and `TimeoutError` when the server does not answer in time.

Server→client *requests* (`sampling/createMessage`, `elicitation/create`) are answered
with JSON-RPC error -32601 by the one general codepath below: v1 supports none of them
and a silent drop would hang the server (03 §10.1).

**stdio**: newline-delimited JSON frames on the child's stdin/stdout. stderr is drained
into a bounded ring (last 50 lines) which `last_error` quotes, so a server that dies on
startup explains itself. Windows spawns with CREATE_NO_WINDOW; `command` is resolved
through `shutil.which` so a bare `npx` works.

**streamable HTTP** (spec revisions 2025-03-26 / 2025-06-18, subset): every request is a
POST with `Accept: application/json, text/event-stream`; the response is either one JSON
message or an SSE stream which is read until the matching response arrives (notifications
in between are dispatched). `Mcp-Session-Id` from any response is echoed on later
requests. *Not in v1, documented deviation:* the optional standalone GET listening
stream — without it a server can only push notifications while a request of ours is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections import deque
from typing import Any, AsyncIterator, Callable

import httpx

log = logging.getLogger(__name__)

JSONRPC = "2.0"
METHOD_NOT_FOUND = -32601
STDERR_LINES = 50
MAX_LINE = 16 * 1024 * 1024


class McpError(Exception):
    """A JSON-RPC error response, or a transport failure shaped like one."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class Transport:
    """Shared id correlation, notification dispatch and server-request refusal."""

    def __init__(self) -> None:
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._on_notification: Callable[[str, dict[str, Any]], None] | None = None
        self.closed = False

    # -- public -----------------------------------------------------------------
    def on_notification(self, callback: Callable[[str, dict[str, Any]], None] | None) -> None:
        self._on_notification = callback

    async def open(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def send(self, message: dict[str, Any]) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        raise NotImplementedError

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.send(_frame(method, params))

    # -- helpers ----------------------------------------------------------------
    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Route one parsed frame. Returns a reply to send back, if any."""
        if not isinstance(message, dict):
            return None
        if "method" in message:
            if message.get("id") is None:
                self._notify_up(str(message.get("method")), message.get("params") or {})
                return None
            return {
                "jsonrpc": JSONRPC,
                "id": message["id"],
                "error": {"code": METHOD_NOT_FOUND, "message": f"{message['method']} is not supported by this client"},
            }
        key = message.get("id")
        future = self._pending.pop(key, None) if isinstance(key, int) else None
        if future is None or future.done():
            return None
        error = message.get("error")
        if error:
            future.set_exception(McpError(int(error.get("code", 0)), str(error.get("message", "error")), error.get("data")))
        else:
            future.set_result(message.get("result"))
        return None

    def _notify_up(self, method: str, params: dict[str, Any]) -> None:
        if self._on_notification is None:
            return
        try:
            self._on_notification(method, params)
        except Exception:
            log.exception("mcp: notification handler failed for %s", method)

    def _fail_pending(self, exc: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


def _frame(method: str, params: dict[str, Any] | None, request_id: int | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": JSONRPC, "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return message


# ---------------------------------------------------------------------------------
# stdio
# ---------------------------------------------------------------------------------
class StdioTransport(Transport):
    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ):
        super().__init__()
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self.stderr_ring: deque[str] = deque(maxlen=STDERR_LINES)
        self._pump: asyncio.Task | None = None
        self._drain: asyncio.Task | None = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StdioTransport({self.command!r}, args={self.args!r})"

    async def open(self) -> None:
        exe = shutil.which(self.command) or self.command
        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.env.items()})
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":  # keep a console server from flashing a window
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            self.process = await asyncio.create_subprocess_exec(
                exe,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.cwd,
                limit=MAX_LINE,
                **kwargs,
            )
        except OSError as exc:
            raise McpError(-32000, f"cannot start {self.command!r}: {exc}") from exc
        self._pump = asyncio.get_running_loop().create_task(self._run(), name=f"mcp-stdio:{self.command}")
        self._drain = asyncio.get_running_loop().create_task(self._drain_stderr(), name=f"mcp-stderr:{self.command}")

    async def recv(self) -> AsyncIterator[dict[str, Any]]:
        """Parsed messages from the child's stdout, in order, until it closes."""
        assert self.process is not None and self.process.stdout is not None
        while True:
            try:
                line = await self.process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:  # frame past the limit
                log.warning("mcp: oversized frame from %s (%s)", self.command, exc)
                continue
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                yield json.loads(text)
            except json.JSONDecodeError:
                log.warning("mcp: non-JSON line from %s: %s", self.command, text[:200])

    async def _run(self) -> None:
        try:
            async for message in self.recv():
                reply = self._dispatch(message)
                if reply is not None:
                    await self.send(reply)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("mcp: stdio pump for %s failed: %r", self.command, exc)
        finally:
            self._fail_pending(McpError(-32000, self._exit_reason()))

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            self.stderr_ring.append(line.decode("utf-8", errors="replace").rstrip())

    def _exit_reason(self) -> str:
        code = self.process.returncode if self.process is not None else None
        tail = " | ".join(list(self.stderr_ring)[-5:])
        base = "server exited" if code is None else f"server exited with code {code}"
        return f"{base}: {tail}" if tail else base

    @property
    def stderr_text(self) -> str:
        return "\n".join(self.stderr_ring)

    async def send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.stdin.is_closing():
            raise McpError(-32000, self._exit_reason())
        process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise McpError(-32000, self._exit_reason()) from exc

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        request_id = self._new_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.send(_frame(method, params, request_id))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{method} timed out after {timeout:g}s") from exc
        finally:
            self._pending.pop(request_id, None)

    async def close(self) -> None:
        self.closed = True
        process, self.process = self.process, None
        for task in (self._pump, self._drain):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._pump = self._drain = None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(Exception):
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
        with contextlib.suppress(ProcessLookupError, OSError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except (asyncio.TimeoutError, TimeoutError):
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        self._fail_pending(McpError(-32000, "transport closed"))


# ---------------------------------------------------------------------------------
# streamable HTTP
# ---------------------------------------------------------------------------------
class HttpTransport(Transport):
    ACCEPT = "application/json, text/event-stream"

    def __init__(self, url: str, headers: dict[str, str] | None = None, client: httpx.AsyncClient | None = None):
        super().__init__()
        self.url = url
        self.headers = {k: str(v) for k, v in (headers or {}).items()}
        self.session_id: str | None = None
        self._client = client
        self._owns_client = client is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HttpTransport({self.url!r})"

    async def open(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    def _request_headers(self) -> dict[str, str]:
        headers = {"Accept": self.ACCEPT, "Content-Type": "application/json", **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def send(self, message: dict[str, Any]) -> None:
        """One-way frame (a notification or our error reply): POST and drop the body."""
        if self._client is None:
            await self.open()
        assert self._client is not None
        response = await self._client.post(self.url, json=message, headers=self._request_headers())
        self._remember_session(response)
        await response.aclose()

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        if self._client is None:
            await self.open()
        assert self._client is not None
        request_id = self._new_id()
        body = _frame(method, params, request_id)
        try:
            return await asyncio.wait_for(self._post(body, request_id), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{method} timed out after {timeout:g}s") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _post(self, body: dict[str, Any], request_id: int) -> Any:
        assert self._client is not None
        try:
            async with self._client.stream("POST", self.url, json=body, headers=self._request_headers()) as response:
                self._remember_session(response)
                if response.status_code >= 400:
                    await response.aread()
                    raise McpError(-32000, f"HTTP {response.status_code} from {self.url}")
                kind = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                if kind == "text/event-stream":
                    return await self._read_sse(response, request_id)
                payload = json.loads((await response.aread()).decode("utf-8") or "null")
                return self._settle(payload, request_id)
        except httpx.HTTPError as exc:
            raise McpError(-32000, f"{type(exc).__name__}: {exc}") from exc

    async def _read_sse(self, response: httpx.Response, request_id: int) -> Any:
        """Read `data:` events until the response with our id arrives (spec: the POST's
        stream carries that response plus any notifications the server wants first)."""
        data: list[str] = []
        async for raw in response.aiter_lines():
            line = raw.rstrip("\r")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
                continue
            if line:  # event:/id:/retry:/comment — nothing this subset needs
                continue
            if not data:
                continue
            chunk, data = "\n".join(data), []
            try:
                message = json.loads(chunk)
            except json.JSONDecodeError:
                log.warning("mcp: non-JSON SSE payload from %s: %s", self.url, chunk[:200])
                continue
            done, value = self._maybe_settle(message, request_id)
            if done:
                return value
        raise McpError(-32000, "server closed the stream before answering")

    def _maybe_settle(self, message: dict[str, Any], request_id: int) -> tuple[bool, Any]:
        if isinstance(message, dict) and message.get("id") == request_id and ("result" in message or "error" in message):
            return True, self._settle(message, request_id)
        reply = self._dispatch(message)
        if reply is not None:  # a server→client request: refuse it out of band
            asyncio.get_running_loop().create_task(self._reply(reply))
        return False, None

    async def _reply(self, message: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self.send(message)

    def _settle(self, message: Any, request_id: int) -> Any:
        if not isinstance(message, dict):
            raise McpError(-32000, "malformed response (not a JSON-RPC object)")
        if message.get("id") != request_id:
            self._dispatch(message)
            raise McpError(-32000, "response id did not match the request")
        error = message.get("error")
        if error:
            raise McpError(int(error.get("code", 0)), str(error.get("message", "error")), error.get("data"))
        return message.get("result")

    def _remember_session(self, response: httpx.Response) -> None:
        session = response.headers.get("mcp-session-id")
        if session:
            self.session_id = session

    async def close(self) -> None:
        self.closed = True
        self._fail_pending(McpError(-32000, "transport closed"))
        if self._client is not None and self._owns_client:
            with contextlib.suppress(Exception):
                await self._client.aclose()
        self._client = None


def build_transport(row: dict[str, Any], env: dict[str, str]) -> Transport:
    """One transport from an `mcp_servers` row whose env/url already carry real secrets.

    For `http` rows the env map is the request header set (the row has nowhere else to
    put an auth header, and `{{secret:...}}` substitution is identical)."""
    kind = (row.get("transport") or "stdio").lower()
    if kind == "stdio":
        command = (row.get("command") or "").strip()
        if not command:
            raise McpError(-32602, "stdio server has no command")
        return StdioTransport(command, row.get("args") or [], env, row.get("cwd") or None)
    if kind == "http":
        url = (row.get("url") or "").strip()
        if not url:
            raise McpError(-32602, "http server has no url")
        return HttpTransport(url, env)
    raise McpError(-32602, f"unknown transport {kind!r}: use stdio or http")
