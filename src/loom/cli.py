"""The CLI (03 §14): thin REST clients, no logic of its own.

Everything except `serve` talks to a running backend over `/api`. The base URL is
`--url`, else `$LOOM_URL`, else `http://127.0.0.1:<general.port from the home's
state.db, or 8500>`; the bearer token is `--token`, else `$LOOM_TOKEN`.

`send` streams by polling `GET /conversations/{id}/events?after=` twice a second and
printing what is new, then exits on the conversation's status: 0 for idle/done,
1 for failed, 2 for waiting_input/waiting_approval. Polling rather than a WebSocket
keeps the dependency list at `httpx` (03 §19) and costs nothing at this cadence.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

DEFAULT_PORT = 8500
POLL_SECONDS = 0.5
TERMINAL = {"idle": 0, "done": 0, "failed": 1, "waiting_input": 2, "waiting_approval": 2, "archived": 0}


# ---------------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------------
def home_path(home: str | None) -> Path:
    from .config import app_home

    return Path(home).expanduser() if home else app_home()


def port_from_home(home: Path) -> int:
    """Read `general.port` straight from state.db: the CLI must reach the backend the
    user actually configured, without importing the whole app."""
    state = home / "state.db"
    if not state.exists():
        return DEFAULT_PORT
    try:
        conn = sqlite3.connect(f"file:{state}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='general.port'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return DEFAULT_PORT
    if not row:
        return DEFAULT_PORT
    try:
        return int(json.loads(row[0]))
    except (ValueError, TypeError):
        return DEFAULT_PORT


def base_url(args: argparse.Namespace) -> str:
    if getattr(args, "url", None):
        return str(args.url).rstrip("/")
    env = os.environ.get("LOOM_URL")
    if env:
        return env.rstrip("/")
    return f"http://127.0.0.1:{port_from_home(home_path(getattr(args, 'home', None)))}"


def token_for(args: argparse.Namespace) -> str:
    return str(getattr(args, "token", None) or os.environ.get("LOOM_TOKEN") or "")


class Api:
    """One tiny synchronous REST client; every command is a few calls through it."""

    def __init__(self, url: str, token: str = "", timeout: float = 30.0):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.url = url
        self.http = httpx.Client(base_url=url + "/api", headers=headers, timeout=timeout)

    def close(self) -> None:
        self.http.close()

    def request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            response = self.http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise CliError(f"cannot reach the backend at {self.url}: {exc}", 4) from exc
        if response.status_code == 404:
            raise NotFound(_detail(response) or f"{path} not found")
        if response.status_code >= 400:
            raise CliError(f"{response.status_code}: {_detail(response)}", _code_for(response))
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class NotFound(CliError):
    def __init__(self, message: str):
        super().__init__(message, 4)


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:400]
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return json.dumps(payload)[:400]


def _code_for(response: httpx.Response) -> int:
    if response.status_code == 409 and "no model configured" in _detail(response).lower():
        return 3
    return 1


# ---------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    if not args.no_supervisor:
        return _run_supervisor(args)
    from .server.app import serve

    return serve(
        home=args.home,
        host=args.host,
        port=args.port,
        fake=args.fake,
        open_browser=False if args.no_browser else None,
    )


def supervisor_argv(args: argparse.Namespace) -> list[str]:
    """`loom serve` (no `--no-supervisor`) runs the supervisor with this interpreter, so
    the supervisor in turn starts the backend with an interpreter that can import
    `loom`. Every backend-shaped flag is forwarded."""
    command = [sys.executable, "-m", "supervisor"]
    if args.home:
        command += ["--home", str(args.home)]
    command += ["--port", str(args.port or port_from_home(home_path(args.home)))]
    if args.host:
        command += ["--host", args.host]
    if args.fake:
        command.append("--fake")
    return command


def _run_supervisor(args: argparse.Namespace) -> int:
    """`loom serve` for an end user runs under the supervisor (03 §3.1); development
    uses `--no-supervisor`."""
    import subprocess

    command = supervisor_argv(args)
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        raise CliError(f"could not start the supervisor: {exc}", 4) from exc


def cmd_send(args: argparse.Namespace) -> int:
    api = Api(base_url(args), token_for(args))
    try:
        conversation_id = _target_conversation(api, args)
        try:
            after = _latest_event_id(api, conversation_id)  # show only what this message causes
            api.post(f"/conversations/{conversation_id}/messages", json={"content": args.text})
        except NotFound as exc:
            raise CliError(f"backend does not expose /conversations yet ({exc})", 4) from exc
        return _follow(api, conversation_id, quiet=args.quiet, after=after)
    finally:
        api.close()


def _target_conversation(api: Api, args: argparse.Namespace) -> str:
    if args.thread:
        return str(args.thread)
    try:
        if args.new:
            created = api.post("/conversations", json={"kind": "thread", "goal": args.new})
            return str(created["id"])
        rows = api.get("/conversations", params={"kind": "desk"})
    except NotFound as exc:
        raise CliError(f"backend does not expose /conversations yet ({exc})", 4) from exc
    rows = rows if isinstance(rows, list) else rows.get("conversations", [])
    if not rows:
        raise CliError("no desk conversation found; is the backend bootstrapped?", 4)
    return str(rows[0]["id"])


def _latest_event_id(api: Api, conversation_id: str) -> int:
    """Newest event id, so `send` prints only events caused by this message. The events
    endpoint returns the newest rows when `before` is set, so ask for one."""
    payload = api.get(f"/conversations/{conversation_id}/events", params={"before": 2**31 - 1, "limit": 1})
    events = payload if isinstance(payload, list) else payload.get("events", [])
    return max((int(e.get("id") or 0) for e in events), default=0)


def _follow(api: Api, conversation_id: str, quiet: bool = False, after: int = 0) -> int:
    status = "running"
    printed_any = False
    seen_running = False
    started = time.monotonic()
    while True:
        try:
            payload = api.get(f"/conversations/{conversation_id}/events", params={"after": after, "limit": 200})
        except NotFound as exc:
            raise CliError(f"backend does not expose /conversations yet ({exc})", 4) from exc
        events = payload if isinstance(payload, list) else payload.get("events", [])
        for event in events:
            after = max(after, int(event.get("id") or 0))
            line = _render_event(event, quiet)
            if line:
                print(line, flush=True)
                printed_any = True
        conversation = api.get(f"/conversations/{conversation_id}")
        status = str(conversation.get("status") or "idle")
        seen_running = seen_running or status == "running"
        # A turn takes a moment to start; do not mistake "not started yet" for "finished".
        settled = seen_running or (time.monotonic() - started) >= 3.0
        if status in TERMINAL and not events and settled:
            break
        time.sleep(POLL_SECONDS)
    if status == "waiting_approval":
        print("waiting for an approval decision — `loom approvals`", file=sys.stderr)
    elif status == "waiting_input":
        print("the conversation is waiting for input", file=sys.stderr)
    elif status == "failed" and not printed_any:
        print("the turn failed; see `loom status` and the logs", file=sys.stderr)
    return TERMINAL.get(status, 1)


def _render_event(event: dict[str, Any], quiet: bool) -> str:
    kind = str(event.get("kind") or "")
    content = str(event.get("content") or "")
    if kind == "assistant":
        return content
    if kind == "tool_call" and not quiet:
        # content is the JSON list [{"id","name","arguments"}] (03 §4.2); meta.text is any preamble
        try:
            calls = json.loads(content) if content else []
        except ValueError:
            calls = []
        lines = [f"  · {c.get('name', 'tool')}({_compact(c.get('arguments'))})" for c in calls if isinstance(c, dict)]
        preamble = (event.get("meta") or {}).get("text")
        if preamble:
            lines.insert(0, str(preamble))
        return "\n".join(lines) if lines else "  · tool call"
    if kind == "tool_result" and not quiet:
        return f"  → {_compact(content, 120)}"
    if kind == "system":
        return f"! {content}"
    return ""


def _compact(value: Any, limit: int = 100) -> str:
    text = value if isinstance(value, str) else json.dumps(value or {}, sort_keys=True)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def cmd_threads(args: argparse.Namespace) -> int:
    api = Api(base_url(args), token_for(args))
    try:
        params: dict[str, Any] = {"kind": "thread"}
        rows = api.get("/conversations", params=params)
        rows = rows if isinstance(rows, list) else rows.get("conversations", [])
        if not args.all:
            rows = [r for r in rows if r.get("status") not in ("archived", "done")]
        _table(rows, ("id", "status", "depth", "goal"))
        return 0
    finally:
        api.close()


def cmd_approvals(args: argparse.Namespace) -> int:
    api = Api(base_url(args), token_for(args))
    try:
        if args.allow or args.deny:
            approval_id = args.allow or args.deny
            decision = "allow" if args.allow else "deny"
            row = api.post(f"/approvals/{approval_id}", json={"decision": decision})
            print(f"{approval_id}: {decision}" + (f" ({row.get('status')})" if isinstance(row, dict) else ""))
            return 0
        rows = api.get("/approvals", params={"status": "pending"})
        rows = rows if isinstance(rows, list) else rows.get("approvals", [])
        _table(rows, ("id", "tool", "reason", "conversation_id"))
        return 0
    finally:
        api.close()


def cmd_items(args: argparse.Namespace) -> int:
    api = Api(base_url(args), token_for(args))
    try:
        rows = api.get("/items", params={"unseen": bool(args.unseen)})
        rows = rows if isinstance(rows, list) else rows.get("items", [])
        _table(rows, ("id", "kind", "title", "created_at"))
        return 0
    finally:
        api.close()


def cmd_status(args: argparse.Namespace) -> int:
    api = Api(base_url(args), token_for(args))
    try:
        health = api.get("/system/health")
        queue = api.get("/system/queue")
        print(f"loom {health.get('version')} — {'ok' if health.get('ok') else 'stopping'}, "
              f"up {health.get('uptime_s')}s, {health.get('ws_clients')} surface(s)")
        if not health.get("model_configured"):
            print(f"no model configured — open {base_url(args)}/setup")
        print(f"queue: depth {queue.get('depth')}")
        for entry in queue.get("running") or []:
            print(f"  running {entry.get('model')} on {entry.get('provider')} "
                  f"(p{entry.get('priority')}, {entry.get('purpose')})")
        return 0
    finally:
        api.close()


def cmd_reindex(args: argparse.Namespace) -> int:
    api = Api(base_url(args), token_for(args), timeout=600.0)
    try:
        print(json.dumps(api.post("/system/reindex"), indent=2, sort_keys=True))
        return 0
    finally:
        api.close()


def cmd_vault(args: argparse.Namespace) -> int:
    api = Api(base_url(args), token_for(args))
    try:
        if args.vault_command == "search":
            rows = api.get("/vault/search", params={"q": args.query})
            rows = rows if isinstance(rows, list) else rows.get("results", [])
            _table(rows, ("path", "title", "snippet"))
        else:
            note = api.get("/vault/note", params={"path": args.path})
            print(note.get("content", "") if isinstance(note, dict) else note)
        return 0
    finally:
        api.close()


def cmd_schedules(args: argparse.Namespace) -> int:
    api = Api(base_url(args), token_for(args))
    try:
        rows = api.get("/schedules")
        rows = rows if isinstance(rows, list) else rows.get("schedules", [])
        _table(rows, ("id", "spec", "target", "next_run_at", "enabled"))
        return 0
    finally:
        api.close()


def cmd_promote(args: argparse.Namespace) -> int:
    api = Api(base_url(args), token_for(args), timeout=600.0)
    try:
        body = {"ref": args.ref, "acknowledge_protected": bool(args.acknowledge_protected)}
        print(json.dumps(api.post("/system/promote", json=body), indent=2, sort_keys=True))
        return 0
    finally:
        api.close()


def cmd_rollback(args: argparse.Namespace) -> int:
    api = Api(base_url(args), token_for(args), timeout=600.0)
    try:
        print(json.dumps(api.post("/system/rollback"), indent=2, sort_keys=True))
        return 0
    finally:
        api.close()


def _table(rows: Iterable[dict[str, Any]], columns: tuple[str, ...]) -> None:
    rows = list(rows)
    if not rows:
        print("(nothing)")
        return
    widths = {c: max(len(c), *(len(_cell(r.get(c))) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    for row in rows:
        print("  ".join(_cell(row.get(c)).ljust(widths[c]) for c in columns))


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value)
    text = " ".join(text.split())
    return text if len(text) <= 60 else text[:59] + "…"


# ---------------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loom", description="Loom — local-first assistant.")
    parser.add_argument("--url", help="backend base URL (default $LOOM_URL or http://127.0.0.1:<port>)")
    parser.add_argument("--token", help="bearer token (default $LOOM_TOKEN)")
    parser.add_argument("--home", help="APP_HOME (default $LOOM_HOME or ~/.loom)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the backend")
    serve.add_argument("--port", type=int)
    serve.add_argument("--host")
    serve.add_argument("--no-supervisor", action="store_true", help="serve in this process (development)")
    serve.add_argument("--fake", action="store_true", help="use the fake model client (UI development)")
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(func=cmd_serve)

    send = sub.add_parser("send", help="send a message and follow the turn")
    target = send.add_mutually_exclusive_group()
    target.add_argument("--thread", help="conversation id (default: the desk)")
    target.add_argument("--new", metavar="GOAL", help="create a thread with this goal first")
    send.add_argument("--quiet", action="store_true", help="assistant text only")
    send.add_argument("text")
    send.set_defaults(func=cmd_send)

    threads = sub.add_parser("threads", help="list threads")
    threads.add_argument("--all", action="store_true", help="include done and archived")
    threads.set_defaults(func=cmd_threads)

    approvals = sub.add_parser("approvals", help="list or decide approvals")
    approvals.add_argument("--allow", metavar="ID")
    approvals.add_argument("--deny", metavar="ID")
    approvals.set_defaults(func=cmd_approvals)

    items = sub.add_parser("items", help="the now board")
    items.add_argument("--unseen", action="store_true")
    items.set_defaults(func=cmd_items)

    sub.add_parser("status", help="health and queue").set_defaults(func=cmd_status)
    sub.add_parser("reindex", help="rebuild the vault index").set_defaults(func=cmd_reindex)

    vault = sub.add_parser("vault", help="search or read the vault")
    vault_sub = vault.add_subparsers(dest="vault_command", required=True)
    vault_search = vault_sub.add_parser("search")
    vault_search.add_argument("query")
    vault_read = vault_sub.add_parser("read")
    vault_read.add_argument("path")
    vault.set_defaults(func=cmd_vault)

    sub.add_parser("schedules", help="list schedules").set_defaults(func=cmd_schedules)

    promote = sub.add_parser("promote", help="promote a git ref (plan it first: GET /api/system/promote/plan)")
    promote.add_argument("ref")
    promote.add_argument(
        "--acknowledge-protected",
        action="store_true",
        help="confirm a promotion whose diff touches protected paths (the 409 lists them)",
    )
    promote.set_defaults(func=cmd_promote)

    sub.add_parser("rollback", help="roll back to the previous release").set_defaults(func=cmd_rollback)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.home:
        os.environ["LOOM_HOME"] = str(Path(args.home).expanduser())
    try:
        return int(args.func(args) or 0)
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
