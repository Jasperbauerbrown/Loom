"""Shared fixtures (03 §18). Every test gets a temp LOOM_HOME with a migrated DB, a
bootstrapped vault, and a FakeModelClient. No test calls a live model.

Fixtures:
    home      temp APP_HOME path (LOOM_HOME set)
    fake      FakeModelClient (script it before triggering turns)
    app       built App with `fake` attached; async subsystems NOT started
    started   App with `start(app)` run (watcher, scheduler, ...); stopped after
    client    httpx.AsyncClient bound to the FastAPI app (ASGI transport) — added by the
              server package (tests/conftest_server.py plugin) when it lands
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from loom.models.fake import FakeModelClient, Scripted  # noqa: F401  (re-exported for tests)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "loom-home"
    monkeypatch.setenv("LOOM_HOME", str(h))
    # Keep git quiet and deterministic inside the temp vault.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-test@example.invalid")
    return h


@pytest.fixture
def fake() -> FakeModelClient:
    return FakeModelClient()


@pytest.fixture
def app(home: Path, fake: FakeModelClient):
    from loom.app import build_app

    a = build_app(home, models=fake)
    _configure_fake_model(a)
    yield a
    try:
        a.db.close()
    except Exception:
        pass


@pytest.fixture
async def started(app):
    from loom.app import start, stop

    await start(app)
    yield app
    await stop(app)


def _configure_fake_model(app) -> None:  # type: ignore[no-untyped-def]
    """A provider/preset/roles so `has_model()` is true and role resolution works."""
    from loom import db

    now = db.now_iso()
    with db.transaction(app.db):
        app.db.execute(
            "INSERT OR IGNORE INTO providers(name, kind, base_url, params, enabled, created_at, updated_at) "
            "VALUES ('local-ollama', 'ollama', 'http://127.0.0.1:11434', '{}', 1, ?, ?)",
            (now, now),
        )
        app.db.execute(
            "INSERT OR IGNORE INTO presets(name, provider, model, params, enabled, created_at, updated_at) "
            "VALUES ('fake', 'local-ollama', 'fake-model', ?, 1, ?, ?)",
            (db.dumps({"max_tokens": 1024, "effective_ctx": 32768, "temperature": 0.2}), now, now),
        )
        app.db.execute(
            "INSERT OR IGNORE INTO presets(name, provider, model, params, enabled, created_at, updated_at) "
            "VALUES ('fake-embed', 'local-ollama', 'nomic-embed-text', '{}', 1, ?, ?)",
            (now, now),
        )
        for role in ("desk", "worker", "maintenance"):
            app.db.execute("UPDATE model_roles SET preset='fake' WHERE role=?", (role,))
        app.db.execute("UPDATE model_roles SET preset='fake-embed' WHERE role='embedding'")


# -- server fixtures (foundations package) -------------------------------------------
@pytest.fixture
async def client(app):
    """An httpx.AsyncClient over the real ASGI app, with the lifespan run (so every
    package's `start`/`stop` happens exactly as it does under uvicorn). Router tests in
    any package use this; nothing here touches the network."""
    import httpx

    from loom.server.app import create_app

    api = create_app(app)
    async with api.router.lifespan_context(api):
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://loom.test") as http:
            http.loom = app  # type: ignore[attr-defined]
            http.api = api  # type: ignore[attr-defined]
            yield http


@pytest.fixture
async def started_client(client):
    """Alias of `client`: the lifespan is always run. Kept as a separate name so tests
    can say which they mean."""
    return client
