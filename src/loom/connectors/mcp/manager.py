"""`app.mcp`: the `mcp_servers` rows, their live clients, and their tools (03 §10.1).

One state machine per row, one behaviour per failure class (charter §2.1):

    disconnected → connecting → connected            tools registered, tools_cache written
                             ↘ error                 last_error set, tools unregistered,
                                                     setup item posted, reconnect with
                                                     backoff 5s → 5min while enabled

Nothing here is ever fatal to the backend: `start()` spawns one task per enabled row and
returns immediately, so a server that hangs on startup delays nothing.

Tools are registered as `mcp_<server>_<tool>` with `read_only` / `destructive` taken from
the server's annotations; no `tool_policies` row is inserted — 03 §9.3 rule 3 already
says read-only → allow, anything else → ask, which is the fail-closed default.

Every mutation emits `mcp.status` with the row, so Settings never disagrees with the
backend (charter §2.2).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from ... import db, items
from ...ids import ulid
from ...secrets import SecretMissing
from ...tools.registry import Tool, ToolContext, ToolResult
from .client import McpClient
from .transports import McpError, build_transport

log = logging.getLogger(__name__)

JSON_COLUMNS = ("args", "env", "tools_cache")
BACKOFF_START = 5.0
BACKOFF_MAX = 300.0
NAME_RE = re.compile(r"[^a-z0-9_]+")
TOOLS_BEGIN = "<!-- tools:begin -->"
TOOLS_END = "<!-- tools:end -->"


def slug(name: str) -> str:
    """A server name as it appears in tool names: `[a-z0-9_]` only."""
    return NAME_RE.sub("_", (name or "").strip().lower()).strip("_") or "server"


class Manager:
    def __init__(self, app: Any):
        self.app = app
        self.clients: dict[str, McpClient] = {}  # server id → live client
        self._attempts: dict[str, int] = {}  # server id → consecutive failures

    # -- rows --------------------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        return [self._decode(r) for r in self.app.db.execute("SELECT * FROM mcp_servers ORDER BY name")]

    def row(self, server_id: str) -> dict[str, Any] | None:
        record = self.app.db.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
        return self._decode(record) if record is not None else None

    def row_by_name(self, name: str) -> dict[str, Any] | None:
        record = self.app.db.execute("SELECT * FROM mcp_servers WHERE name=?", (name,)).fetchone()
        return self._decode(record) if record is not None else None

    def _decode(self, record: Any) -> dict[str, Any]:
        row = db.row_to_dict(record) or {}
        row["args"] = db.loads(row.get("args"), [])
        row["env"] = db.loads(row.get("env"), {})
        row["tools_cache"] = db.loads(row.get("tools_cache"), [])
        row["enabled"] = bool(row.get("enabled"))
        row["connected"] = row.get("id") in self.clients
        return row

    def _set(self, server_id: str, **fields: Any) -> dict[str, Any] | None:
        fields["updated_at"] = db.now_iso()
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.app.db.execute(f"UPDATE mcp_servers SET {assignments} WHERE id=?", (*fields.values(), server_id))
        return self.row(server_id)

    def _emit(self, row: dict[str, Any] | None) -> None:
        if row is not None:
            self.app.bus.emit("mcp.status", row)

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        """Connect every enabled row, each in its own task: startup never blocks on a
        server that is slow, missing or wedged."""
        for row in self.rows():
            if row["enabled"]:
                self.app.spawn(f"mcp:{row['id']}", self.connect(row["id"]))

    async def stop(self) -> None:
        for server_id in list(self.clients):
            await self._drop_client(server_id)

    async def connect(self, server_id: str) -> dict[str, Any] | None:
        row = self.row(server_id)
        if row is None:
            raise KeyError(server_id)
        if not row["enabled"]:
            return await self.disconnect(server_id)
        await self._drop_client(server_id)
        self._emit(self._set(server_id, status="connecting", last_error=None))

        try:
            env = self.app.secrets.substitute(row["env"] or {})
        except SecretMissing as exc:
            return self._fail(server_id, f"missing secret {exc.name!r}; add it in Settings → Secrets")

        client: McpClient | None = None
        try:
            transport = build_transport(row, {str(k): str(v) for k, v in (env or {}).items()})
            client = McpClient(transport, name=row["name"], version=str(getattr(self.app, "version", "1")))
            await client.initialize(timeout=self._timeout(row, connect=True))
            tools = await client.list_tools(timeout=self._timeout(row, connect=True))
        except (McpError, TimeoutError, OSError, ValueError) as exc:
            if client is not None:
                await _quiet_close(client)
            return self._fail(server_id, self._reason(exc, client))
        except asyncio.CancelledError:
            if client is not None:
                await _quiet_close(client)
            raise
        except Exception as exc:  # pragma: no cover - defensive: never fatal
            if client is not None:
                await _quiet_close(client)
            return self._fail(server_id, self._reason(exc, client))

        self.clients[server_id] = client
        client.on_tools_changed(lambda sid=server_id: self.app.spawn(f"mcp:refresh:{sid}", self.refresh(sid)))
        self._attempts.pop(server_id, None)
        self._register(row["name"], tools)
        row = self._set(server_id, status="connected", last_error=None, tools_cache=db.dumps(tools), cached_at=db.now_iso())
        self._note(row, tools)
        self._emit(row)
        log.info("mcp: %s connected with %d tool(s)", row["name"], len(tools))
        return row

    async def disconnect(self, server_id: str) -> dict[str, Any] | None:
        row = self.row(server_id)
        if row is None:
            raise KeyError(server_id)
        await self._drop_client(server_id)
        self.app.tools.unregister_source(f"mcp:{slug(row['name'])}")
        self._cancel_reconnect(server_id)
        row = self._set(server_id, status="disconnected", last_error=None)
        self._emit(row)
        return row

    async def reload(self, server_id: str) -> dict[str, Any] | None:
        await self.disconnect(server_id)
        return await self.connect(server_id)

    async def refresh(self, server_id: str) -> dict[str, Any] | None:
        """Re-list tools on a live server (manual refresh, or tools/list_changed)."""
        client = self.clients.get(server_id)
        row = self.row(server_id)
        if row is None:
            raise KeyError(server_id)
        if client is None:
            return await self.connect(server_id)
        try:
            tools = await client.list_tools(timeout=self._timeout(row, connect=True))
        except (McpError, TimeoutError, OSError) as exc:
            return self._fail(server_id, self._reason(exc, client))
        self._register(row["name"], tools)
        row = self._set(server_id, status="connected", last_error=None, tools_cache=db.dumps(tools), cached_at=db.now_iso())
        self._note(row, tools)
        self._emit(row)
        return row

    async def test(self, server_id: str) -> dict[str, Any]:
        """Connect a throwaway client and close it. Never touches the live one."""
        row = self.row(server_id)
        if row is None:
            raise KeyError(server_id)
        started = time.monotonic()
        client: McpClient | None = None
        try:
            env = self.app.secrets.substitute(row["env"] or {})
            transport = build_transport(row, {str(k): str(v) for k, v in (env or {}).items()})
            client = McpClient(transport, name=row["name"], version=str(getattr(self.app, "version", "1")))
            await client.initialize(timeout=self._timeout(row, connect=True))
            tools = await client.list_tools(timeout=self._timeout(row, connect=True))
        except SecretMissing as exc:
            return {"ok": False, "latency_ms": None, "tools": 0, "error": f"missing secret {exc.name!r}"}
        except Exception as exc:
            return {"ok": False, "latency_ms": None, "tools": 0, "error": self._reason(exc, client)}
        finally:
            if client is not None:
                await _quiet_close(client)
        return {
            "ok": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "tools": len(tools),
            "server": client.server_info if client is not None else {},
            "error": None,
        }

    # -- CRUD --------------------------------------------------------------------
    async def create(
        self,
        name: str,
        transport: str = "stdio",
        command: str | None = None,
        args: list[str] | None = None,
        url: str | None = None,
        env: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("a server needs a name")
        if self.row_by_name(name) is not None:
            raise ValueError(f"an MCP server named {name!r} already exists")
        _check_shape(transport, command, url)
        server_id = ulid()
        now = db.now_iso()
        self.app.db.execute(
            "INSERT INTO mcp_servers(id, name, transport, command, args, url, env, enabled, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'disconnected', ?, ?)",
            (server_id, name, transport, command, db.dumps(list(args or [])), url, db.dumps(dict(env or {})), int(bool(enabled)), now, now),
        )
        row = self.row(server_id)
        self._note(row, [])
        self._emit(row)
        if enabled:
            self.app.spawn(f"mcp:{server_id}", self.connect(server_id))
        return row  # type: ignore[return-value]

    async def update(self, server_id: str, **fields: Any) -> dict[str, Any] | None:
        row = self.row(server_id)
        if row is None:
            raise KeyError(server_id)
        allowed = ("name", "transport", "command", "args", "url", "env", "enabled")
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "name" in changes:
            other = self.row_by_name(str(changes["name"]))
            if other is not None and other["id"] != server_id:
                raise ValueError(f"an MCP server named {changes['name']!r} already exists")
        merged = {**row, **changes}
        _check_shape(merged["transport"], merged.get("command"), merged.get("url"))
        stored = dict(changes)
        for column in ("args", "env"):
            if column in stored:
                stored[column] = db.dumps(stored[column])
        if "enabled" in stored:
            stored["enabled"] = int(bool(stored["enabled"]))
        if stored:
            self._set(server_id, **stored)
        # Names prefix the registered tools, so a rename must drop the old ones.
        if "name" in changes:
            self.app.tools.unregister_source(f"mcp:{slug(row['name'])}")
        enabled = bool(merged["enabled"])
        row = await (self.reload(server_id) if enabled else self.disconnect(server_id))
        return row

    async def delete(self, server_id: str) -> bool:
        row = self.row(server_id)
        if row is None:
            return False
        await self.disconnect(server_id)
        self.app.db.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
        self.app.bus.emit("mcp.status", {**row, "status": "deleted", "connected": False})
        return True

    # -- internals ---------------------------------------------------------------
    def _timeout(self, row: dict[str, Any], connect: bool = False) -> float:
        value = float(self.app.settings.get("conversation.tool_timeout_mcp_s") or 300)
        return min(value, 60.0) if connect else value

    def _reason(self, exc: BaseException, client: McpClient | None) -> str:
        text = str(exc) or type(exc).__name__
        transport = getattr(client, "transport", None)
        tail = getattr(transport, "stderr_text", "")
        if tail and tail not in text:
            text = f"{text}\n{tail[-800:]}"
        return text

    def _register(self, server_name: str, tools: list[dict[str, Any]]) -> None:
        source = f"mcp:{slug(server_name)}"
        self.app.tools.unregister_source(source)
        for spec in tools:
            tool = self._tool(server_name, spec)
            if tool is not None:
                self.app.tools.register(tool, replace=True)

    def _tool(self, server_name: str, spec: dict[str, Any]) -> Tool | None:
        raw_name = str(spec.get("name") or "").strip()
        if not raw_name:
            return None
        annotations = spec.get("annotations") if isinstance(spec.get("annotations"), dict) else {}
        schema = spec.get("inputSchema") if isinstance(spec.get("inputSchema"), dict) else {}
        if not schema:
            schema = {"type": "object", "properties": {}}
        description = str(spec.get("description") or spec.get("title") or raw_name)
        server_slug = slug(server_name)
        tool_name = f"mcp_{server_slug}_{slug(raw_name)}"

        async def handler(ctx: ToolContext, args: dict[str, Any], _name: str = raw_name, _server: str = server_name) -> ToolResult:
            server_row = self.row_by_name(_server)
            client = self.clients.get(server_row["id"]) if server_row else None
            if client is None:
                return ToolResult.error(f"MCP server {_server!r} is not connected")
            try:
                out = await client.call_tool(_name, args, timeout=self._timeout(server_row or {}))
            except McpError as exc:
                return ToolResult.error(f"{_server}: {exc.message}", code=exc.code)
            except TimeoutError as exc:
                return ToolResult.error(str(exc), timeout=True)
            except Exception as exc:  # transport died mid-call
                return ToolResult.error(f"{_server}: {exc}")
            return ToolResult(content=out["content"], ok=bool(out["ok"]))

        return Tool(
            name=tool_name,
            description=description,
            parameters=schema,
            handler=handler,
            read_only=bool(annotations.get("readOnlyHint")),
            destructive=bool(annotations.get("destructiveHint")),
            source=f"mcp:{server_slug}",
        )

    def _fail(self, server_id: str, reason: str) -> dict[str, Any] | None:
        row = self.row(server_id)
        if row is None:
            return None
        self.app.tools.unregister_source(f"mcp:{slug(row['name'])}")
        self.clients.pop(server_id, None)
        row = self._set(server_id, status="error", last_error=reason[:4000])
        log.warning("mcp: %s failed: %s", row["name"] if row else server_id, reason.splitlines()[0] if reason else "")
        post_setup_item(
            self.app,
            source=f"mcp:{server_id}",
            title=f"MCP server '{row['name'] if row else server_id}' failed",
            body=reason,
        )
        self._emit(row)
        if row and row["enabled"] and not getattr(self.app, "shutting_down", False):
            self._schedule_reconnect(server_id)
        return row

    def _schedule_reconnect(self, server_id: str) -> None:
        attempt = self._attempts.get(server_id, 0) + 1
        self._attempts[server_id] = attempt
        delay = min(BACKOFF_START * (2 ** (attempt - 1)), BACKOFF_MAX)

        async def later() -> None:
            await asyncio.sleep(delay)
            row = self.row(server_id)
            if row is None or not row["enabled"] or getattr(self.app, "shutting_down", False):
                return
            await self.connect(server_id)

        log.info("mcp: retrying %s in %gs (attempt %d)", server_id, delay, attempt)
        self.app.spawn(f"mcp:retry:{server_id}", later())

    def _cancel_reconnect(self, server_id: str) -> None:
        self._attempts.pop(server_id, None)
        task = self.app.tasks.get(f"mcp:retry:{server_id}")
        if task is not None and not task.done():
            task.cancel()

    async def _drop_client(self, server_id: str) -> None:
        client = self.clients.pop(server_id, None)
        if client is not None:
            await _quiet_close(client)

    def _note(self, row: dict[str, Any] | None, tools: list[dict[str, Any]]) -> None:
        if row is None:
            return
        write_connector_note(self.app, row["name"], "mcp", row.get("description") or "", tools_list(tools, f"mcp_{slug(row['name'])}_"))


async def _quiet_close(client: McpClient) -> None:
    try:
        await client.close()
    except Exception:  # pragma: no cover - closing is best effort
        log.debug("mcp: close failed for %s", client.name, exc_info=True)


def _check_shape(transport: str, command: str | None, url: str | None) -> None:
    if transport not in ("stdio", "http"):
        raise ValueError(f"unknown transport {transport!r}: use stdio or http")
    if transport == "stdio" and not (command or "").strip():
        raise ValueError("a stdio server needs a command")
    if transport == "http" and not (url or "").strip():
        raise ValueError("an http server needs a url")


# ---------------------------------------------------------------------------------
# shared with the connector loader: setup items and the per-integration note (§10.3)
# ---------------------------------------------------------------------------------
def post_setup_item(app: Any, source: str, title: str, body: str) -> dict[str, Any] | None:
    """One undismissed setup item per source: a server retrying every five minutes must
    not bury the now board."""
    existing = app.db.execute(
        "SELECT id FROM items WHERE kind='setup' AND source=? AND dismissed_at IS NULL LIMIT 1", (source,)
    ).fetchone()
    if existing is not None:
        return None
    return items.post(
        app,
        "setup",
        title,
        body,
        source=source,
        action={"kind": "open_settings", "tab": "integrations"},
    )


def tools_list(tools: list[dict[str, Any]], prefix: str = "") -> str:
    """The bulleted tool list that goes between the note's tools markers."""
    lines = []
    for spec in tools:
        name = str(spec.get("name") or "")
        if not name:
            continue
        description = str(spec.get("description") or "").strip().splitlines()[0] if spec.get("description") else ""
        lines.append(f"- `{prefix}{name}`" + (f" — {description}" if description else ""))
    return "\n".join(lines) if lines else "_No tools yet._"


def write_connector_note(app: Any, name: str, kind: str, description: str, tools_md: str) -> str | None:
    """`vault/connectors/<name>.md` from the template, and keep its tool list current.

    Knowledge is markdown truth (03 §4.3): the note is created once and only the block
    between the tools markers is rewritten afterwards, so whatever the agent wrote in
    "When to use" survives every refresh."""
    vault = getattr(app, "vault", None)
    if vault is None:  # pragma: no cover - vault is installed before connectors
        return None
    path = f"connectors/{name}.md"
    try:
        created = not vault.exists(path)
        vault.ensure_note(
            path,
            "connector-note",
            title=name,
            alias=name,
            kind=kind,
            status="configured",
            description=description or f"The {name} {kind} integration.",
            tools=tools_md,
        )
        if not created:
            _replace_tools_block(vault, path, tools_md)
        return path
    except Exception as exc:  # a note is never worth failing a connection over
        log.warning("connectors: could not write the note for %s (%s)", name, exc)
        return None


def _replace_tools_block(vault: Any, path: str, tools_md: str) -> None:
    body = vault.read(path).body
    start = body.find(TOOLS_BEGIN)
    end = body.find(TOOLS_END)
    if start < 0 or end < start:
        return
    old = body[start : end + len(TOOLS_END)]
    new = f"{TOOLS_BEGIN}\n{tools_md}\n{TOOLS_END}"
    if old.strip() != new.strip():
        vault.write(path, new, op="replace", old=old, actor="loom")
