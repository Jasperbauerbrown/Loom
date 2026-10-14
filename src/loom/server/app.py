"""The FastAPI application and the `serve` entry point (03 §3.2, §12, §21.1).

`create_app(app)` is the only place routers, auth, the WebSocket and the built UI are
wired together. Routers come from `server.routers.ROUTERS` by name; a module listed
there that does not import is a startup error, never a silently skipped feature — a
half-mounted API is exactly the UI/backend disagreement the charter forbids.

Auth (03 §21.1): while `general.lan_token` is empty nothing is required anywhere. Once
set, every `/api` request from a non-loopback peer needs `Authorization: Bearer <token>`
and `/ws` needs `?token=`; loopback clients (this machine) never need the token, so
opening the port to the LAN does not change access from here. `GET /api/system/health`
is always open so a supervisor watchdog and a misconfigured surface can both still ask
whether the backend is alive.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import secrets as _secrets
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles

from .. import bootstrap, log as loom_log
from ..app import App, build_app
from ..models import client as model_client
from .routers import ROUTERS
from .ws import Hub

log = logging.getLogger(__name__)

API_PREFIX = "/api"
OPEN_PATHS = ("/api/system/health",)

NO_UI_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Loom</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root {{ color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }}
 body {{ margin: 0; display: grid; place-items: center; min-height: 100vh; padding: 16px; }}
 main {{ max-width: 34rem; }}
 code {{ background: rgba(127,127,127,.18); padding: .15em .4em; border-radius: .3em; }}
 pre {{ background: rgba(127,127,127,.12); padding: .75rem 1rem; border-radius: .5rem; overflow-x: auto; }}
</style></head>
<body><main>
<h1>Loom is running</h1>
<p>The API is up at <code>/api</code>, but the web UI has not been built yet.</p>
<pre>npm --prefix ui install
npm --prefix ui run build</pre>
<p>That writes <code>ui-dist/</code>, which this server serves at <code>/</code>.
Meanwhile <code>{health}</code> and the CLI work.</p>
</main></body></html>
"""


def ui_dist() -> Path | None:
    root = bootstrap.repo_root()
    if root is None:
        return None
    candidate = root / "ui-dist"
    return candidate if (candidate / "index.html").exists() else None


def create_app(app: App) -> FastAPI:
    hub = Hub()

    @contextlib.asynccontextmanager
    async def lifespan(api: FastAPI):
        from ..app import start, stop

        await start(app)
        await model_client.start(app)
        try:
            yield
        finally:
            await hub.close_all()
            await model_client.stop(app)
            await stop(app)

    api = FastAPI(title="Loom", version=app.version, lifespan=lifespan)
    api.state.loom = app
    api.state.hub = hub
    app.extras["hub"] = hub
    loom_log.listen(app)

    @api.middleware("http")
    async def bearer_auth(request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path.startswith(API_PREFIX) and path not in OPEN_PATHS:
            token = str(app.settings.get("general.lan_token") or "")
            if token and not _is_loopback(request.client) and not _authorised(
                request.headers.get("authorization"), request.query_params.get("token"), token
            ):
                return JSONResponse({"detail": "missing or invalid bearer token"}, status_code=401)
        return await call_next(request)

    for name in ROUTERS:
        module = __import__(name, fromlist=["router"])
        api.include_router(module.router, prefix=API_PREFIX)

    @api.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        token = str(app.settings.get("general.lan_token") or "")
        if token and not _is_loopback(websocket.client):
            supplied = websocket.query_params.get("token") or ""
            header = websocket.headers.get("authorization")
            if not _authorised(header, supplied, token):
                await websocket.close(code=4401)
                return
        await hub.serve(websocket, app)

    _mount_ui(api)
    return api


def _is_loopback(client: Any) -> bool:
    """True when the ASGI peer is this machine (127.0.0.0/8 or ::1)."""
    host = getattr(client, "host", None)
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    # a dual-stack (::) socket reports IPv4 peers as ::ffff:x.x.x.x
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool((mapped or addr).is_loopback)


def _authorised(header: str | None, query_token: str | None, expected: str) -> bool:
    supplied = ""
    if header and header.lower().startswith("bearer "):
        supplied = header[7:].strip()
    elif query_token:
        supplied = query_token
    return bool(supplied) and _secrets.compare_digest(supplied, expected)


def _mount_ui(api: FastAPI) -> None:
    dist = ui_dist()
    if dist is None:

        @api.get("/{full_path:path}", include_in_schema=False)
        async def missing_ui(full_path: str) -> Response:
            if full_path.startswith("api/") or full_path == "ws":
                return JSONResponse({"detail": "not found"}, status_code=404)
            return HTMLResponse(NO_UI_HTML.format(health="/api/system/health"))

        return

    api.mount("/assets", StaticFiles(directory=dist / "assets", check_dir=False), name="assets")

    @api.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Response:
        if full_path.startswith("api/") or full_path == "ws":
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = (dist / full_path).resolve() if full_path else dist / "index.html"
        if full_path and candidate.is_file() and str(candidate).startswith(str(dist.resolve())):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")  # SPA fallback


# ---------------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------------
def configure_fake(app: App) -> None:
    """`--fake`: a provider, a preset and the three roles pointing at the fake client,
    whose default responder echoes the last user message. Lets the UI be developed with
    no Ollama running, using exactly the client the tests use."""
    from .. import db
    from ..models.fake import FakeModelClient, Scripted

    now = db.now_iso()
    with db.transaction(app.db):
        app.db.execute(
            "INSERT OR IGNORE INTO providers(name, kind, base_url, params, enabled, created_at, updated_at) "
            "VALUES ('fake', 'ollama', 'http://127.0.0.1:11434', '{}', 1, ?, ?)",
            (now, now),
        )
        app.db.execute(
            "INSERT OR IGNORE INTO presets(name, provider, model, params, enabled, created_at, updated_at) "
            "VALUES ('fake', 'fake', 'fake-model', ?, 1, ?, ?)",
            (db.dumps({"max_tokens": 1024, "effective_ctx": 8192, "temperature": 0.2}), now, now),
        )
        for role in ("desk", "worker", "maintenance"):
            app.db.execute("UPDATE model_roles SET preset='fake' WHERE role=? AND preset IS NULL", (role,))
    client = app.models
    if isinstance(client, FakeModelClient):
        client.models["fake"] = ["fake-model", "nomic-embed-text"]  # so probe/list work too

        def echo(request) -> Scripted:  # type: ignore[no-untyped-def]
            last = ""
            for message in reversed(request.messages):
                if message.get("role") == "user" and isinstance(message.get("content"), str):
                    last = message["content"]
                    break
            return Scripted(content=f"(fake) {last}")

        client.default = echo


def serve(
    home: str | Path | None = None,
    host: str | None = None,
    port: int | None = None,
    fake: bool = False,
    open_browser: bool | None = None,
) -> int:
    """Build, migrate, log and run. Returns a process exit code."""
    import uvicorn

    from ..models.fake import FakeModelClient

    if fake and not home:
        # A fake-model instance must never write its fake preset/roles into the real
        # APP_HOME (it did once, and the real first run then skipped Setup).
        from ..config import app_home

        real = app_home()
        home = str(real.with_name(real.name + "-fake"))
    app = build_app(Path(home) if home else None, models=FakeModelClient() if fake else None)
    loom_log.setup(app.home, str(app.settings.get("system.log_level")))
    if fake:
        configure_fake(app)

    bind = host or str(app.settings.get("general.bind_host"))
    listen_port = int(port if port is not None else app.settings.get("general.port"))
    app.bind_host, app.bind_port = bind, listen_port  # the model reads these in its context block
    url = f"http://{'127.0.0.1' if bind in ('0.0.0.0', '::') else bind}:{listen_port}"

    busy = _port_in_use(bind, listen_port)
    if busy:
        print(
            f"cannot start: {bind}:{listen_port} is already in use{busy}. "
            f"Stop that process or pass --port N.",
            file=sys.stderr,
        )
        return 1

    print(f"Loom {app.version} on {url}" + (f"  [fake model; home {app.home}]" if fake else ""))
    if not app.has_model("desk"):
        print(f"No model configured yet — choose one at {url}/settings/models")
        _post_choose_a_model(app)
    should_open = app.settings.get("general.open_browser") if open_browser is None else open_browser
    if should_open:
        _open_browser(url)

    api = create_app(app)
    config = uvicorn.Config(api, host=bind, port=listen_port, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    server.run()
    return 0


def _port_in_use(host: str, port: int) -> str | None:
    """Returns "" (busy) or " (pid N: name)" when the port cannot be bound, else None.
    Checked before printing the URL or opening the browser so a collision is one clear
    line, not a startup that looks fine and then dies inside uvicorn."""
    import socket

    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return _port_owner(port)
    return None


def _port_owner(port: int) -> str:
    """Best effort " (pid N: name)" for the listener on `port`; "" when unknown."""
    import subprocess

    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                    pid = parts[4]
                    name = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5
                    ).stdout.strip().split(",")[0].strip('"')
                    return f" (pid {pid}: {name or 'unknown'})"
        else:
            out = subprocess.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=5).stdout
            pid = out.strip().splitlines()[0] if out.strip() else ""
            return f" (pid {pid})" if pid else ""
    except Exception:
        pass
    return ""


def _post_choose_a_model(app: App) -> None:
    """No desk model: say so on the now board rather than in a screen that blocks the
    app (decided 2026-09-22). One undismissed item per source, so restarting with the
    model still unset never stacks them up, and dismissing it is the person's answer."""
    from .. import items

    existing = app.db.execute(
        "SELECT 1 FROM items WHERE kind='setup' AND source='setup' AND dismissed_at IS NULL LIMIT 1"
    ).fetchone()
    if existing is not None:
        return
    items.post(
        app,
        "setup",
        "Choose a model",
        "Loom has no model yet. Settings › Models: add a preset, then give it the desk "
        "role — the only required choice. Everything else already has a default. Once "
        "that is done, tell me about yourself, or point me at something to learn.",
        source="setup",
    )


def _open_browser(url: str) -> None:
    import threading
    import webbrowser

    threading.Timer(1.0, lambda: _quiet(webbrowser.open, url)).start()


def _quiet(fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:
        pass
