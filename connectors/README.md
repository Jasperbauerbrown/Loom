# Native connectors

Tier 3 of the three integration tiers (charter §5.8; `docs/03-technical-design.md`
§10.2). Use the **lowest tier that works**:

1. **Knowledge only** — a vault note describing the thing and the procedure for using
   it, executed with `shell` / `http`. No code. This is the default.
2. **MCP server** — anything that already speaks MCP. Add it in Settings →
   Integrations; Loom registers its tools as `mcp_<server>_<tool>`.
3. **Native connector** — a package in this folder. It exists only for the two things
   MCP does not provide: **events** (something happens and Loom should take a turn) and
   **surfaces** (another place the user can talk to Loom, such as Discord or Telegram).

## The contract

`connectors/<name>/__init__.py` exports `CONNECTOR`:

```python
from loom.connectors.base import Connector, Event
from loom.tools.registry import Tool, ToolResult

CONNECTOR = Connector(
    name="my_thing",            # the folder name wins if they disagree
    description="One line; it becomes the connector's vault note.",
    config_schema={...},        # JSON Schema subset → the Settings form
    tools=[Tool(...)],          # or a callable: config -> list[Tool]
    events=MySource(),          # optional
    surface=MySurface(),        # optional
    before_tool=my_before,      # optional: hook every tool call before it runs
    after_tool=my_after,        # optional: annotate every result
    setup=lambda config: None,  # optional: {"title", "body"} when the user must act
)
```

The folder name is the identity: it keys the `connectors` row, prefixes the tools
(`my_thing_<tool>`), and names the vault note `connectors/my_thing.md`.

### config_schema

The rendered subset is `properties` with `type` (string / integer / number / boolean /
array / object), `enum`, `default`, `title`, `description`, plus `required`. Nothing
richer, because the Settings form renders exactly this and a setting the UI cannot show
is a setting that does not exist (charter §2.2). `conversation_id` is always accepted:
it routes the connector's events to a thread instead of the desk.

A connector with unset **required** fields loads as `status = needs_config` and posts a
setup item. Everything else discovered here is enabled on sight.

### tools

Ordinary `loom.tools.registry.Tool`s; the loader prefixes the name and sets
`source="connector:<name>"`. Set `read_only=True` for anything without side effects —
that is what gives the tool an `allow` default instead of `ask` (03 §9.3). Handlers are
`async (ctx, args) -> ToolResult` and never raise for expected failures; return
`ToolResult.error("...")` instead.

### events

```python
class MySource:
    async def events(self, config) -> AsyncIterator[Event]:
        while True:
            ...
            yield Event(kind="message", title="...", content="...", meta={})
```

Each event becomes an inbox row and a now-board item through the event router
(03 §11.2); the `triggers.policy_connector` setting decides interrupt vs queue. The
task runs for the life of the connector: cancellation is the normal ending, and an
exception marks the connector `error`, posts an item and restarts with backoff
(5s → 5min).

### surface

```python
class MySurface:
    async def start(self, api, config) -> None: ...   # runs until cancelled
    async def stop(self) -> None: ...
```

`api` is the surface adapter contract (03 §12.3): `send(conversation_id, text,
user_id)`, `subscribe(callback)` (every event of 03 §12.2; returns an unsubscribe),
`answer_approval(id, decision)`, `list_conversations()`. Render what you understand and
ignore the rest.

### hooks

```python
async def my_before(api, config, ctx_summary, tool_name, args) -> dict | None:
    return {"ask": True, "reason": "…", "notes": ["…"]}   # any key optional

async def my_after(api, config, ctx_summary, tool_name, args, result) -> dict | None:
    return {"notes": ["…"]}
```

A before-hook runs once the dispatcher has already allowed the call (03 §9.3 step
4½) and may annotate it or turn the allow into an ask — never deny; a returned
`deny` is logged and counted as an ask. An after-hook annotates the result. Hooks
read copies (`args`, `config`, `result` = ok/content/meta) and a plain
`ctx_summary` dict (conversation id/kind/goal, call id, intent, request text, policy,
decision, reason); they see `{{secret:NAME}}` placeholders, never secrets, and cannot
change what executes. One that raises, times out (`HOOK_TIMEOUT_S`, 10 s) or returns
the wrong type is logged and skipped. Notes reach the model on the result's text as
`[<connector>] <note>` lines. Declaring either adds `hooks` to the connector's facets.

### secrets

Never read a credential from the config in plain text. Put `{{secret:NAME}}` in the
config value and the dispatcher substitutes it immediately before execution; the
transcript keeps the placeholder (03 §4.5).

## Lifecycle

Discovery happens at startup: every folder here with an `__init__.py` gets a row in the
`connectors` table. An import error disables that one connector, records `last_error`
and posts a setup item — it never stops the backend.

Enable/disable, edit the config and re-import from Settings → Integrations, or:

```
GET   /api/connectors            list with rows, schema and tool names
PATCH /api/connectors/{name}     {"enabled": true, "config": {...}}
POST  /api/connectors/{name}/reload
```

`reload` re-imports the module, so editing a connector needs no restart (a restart is
always safe).

## Example

`example_echo/` is the reference: one read-only tool, an optional tick event source, no
surface. It is also what `tests/test_connectors_loader.py` runs against, so keep it
working if you change it.
