"""Listener supervision (03 §5.7) and the listener's protected paths (§15.3).

The spawn is always the fake below: no test starts a real `loom listen`."""

from __future__ import annotations

import asyncio
import sys

import pytest

from loom import bootstrap, db
from loom import voice
from loom.tools import fs_guard
from loom.voice import supervise
from loom.voice.supervise import Supervisor


class FakeChild:
    _next_pid = 4000

    def __init__(self, argv, env):
        FakeChild._next_pid += 1
        self.pid = FakeChild._next_pid
        self.argv, self.env = argv, env
        self.exit: asyncio.Future = asyncio.get_running_loop().create_future()
        self.killed = False

    async def wait(self) -> int:
        return await asyncio.shield(self.exit)

    def kill(self) -> None:
        self.killed = True
        if not self.exit.done():
            self.exit.set_result(-9)

    def crash(self, code: int = 1) -> None:
        self.exit.set_result(code)


class Spawner:
    def __init__(self):
        self.children: list[FakeChild] = []
        self.fail = 0

    async def __call__(self, argv, env):
        if self.fail:
            self.fail -= 1
            raise OSError("no python")
        child = FakeChild(argv, env)
        self.children.append(child)
        return child


async def settle(cond=lambda: True, rounds: int = 200) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)
        if cond():
            return
    await asyncio.sleep(0.01)
    assert cond()


def change(app, **patch) -> None:
    keys = app.settings.update(app.db, {k.replace("__", "."): v for k, v in patch.items()})
    app.bus.emit("settings.changed", {"keys": keys})


@pytest.fixture
def waits(monkeypatch):
    """Record each backoff wait instead of sleeping it (a wake still ends it)."""
    seen: list[float | None] = []
    real = Supervisor._wait_wake

    async def fast(self, timeout):
        seen.append(timeout)
        await real(self, None if timeout is None else 0)

    monkeypatch.setattr(Supervisor, "_wait_wake", fast)
    return seen


@pytest.fixture
async def sup(app):
    spawner = Spawner()
    s = Supervisor(app, spawn=spawner, allowed=True)
    s.spawner = spawner  # type: ignore[attr-defined]
    await s.start()
    yield s
    await s.stop()


# -- when it runs ---------------------------------------------------------------------
async def test_a_fake_serve_never_starts_a_listener(app):
    voice.install(app)
    s = app.extras[voice.EXTRA]
    assert s.allowed is False  # the app fixture's client is the FakeModelClient, as in --fake
    spawner = Spawner()
    s.spawn = spawner
    app.settings.update(app.db, {"voice.enabled": True})
    await voice.start(app)
    try:
        await settle()
        assert spawner.children == [] and voice.listener_state(app) == {"running": False, "pid": None}
    finally:
        await voice.stop(app)


async def test_disabled_by_default_and_started_when_enabled(app, sup):
    await settle()
    assert sup.spawner.children == []
    change(app, voice__enabled=True)
    await settle(lambda: len(sup.spawner.children) == 1)
    child = sup.spawner.children[0]
    assert sup.state() == {"running": True, "pid": child.pid}


async def test_the_command_line_and_environment(app, sup):
    app.bind_host, app.bind_port = "0.0.0.0", 8611
    change(app, voice__enabled=True)
    await settle(lambda: len(sup.spawner.children) == 1)
    child = sup.spawner.children[0]
    assert child.argv == [sys.executable, "-m", "loom", "listen", "--url", "http://127.0.0.1:8611"]
    assert child.env["LOOM_HOME"] == str(app.home)
    assert "LOOM_TOKEN" not in child.env

    change(app, general__lan_token="s3cret")  # a token change restarts it, token in env only
    await settle(lambda: len(sup.spawner.children) == 2)
    assert child.killed
    second = sup.spawner.children[1]
    assert second.env["LOOM_TOKEN"] == "s3cret"
    assert not any("s3cret" in a for a in second.argv)


def test_listener_url(app):
    app.bind_host, app.bind_port = "", 0
    assert supervise.listener_url(app) == f"http://127.0.0.1:{app.settings.get('general.port')}"
    app.bind_host, app.bind_port = "192.168.1.5", 8500
    assert supervise.listener_url(app) == "http://192.168.1.5:8500"
    app.bind_host = "::1"
    assert supervise.listener_url(app) == "http://[::1]:8500"
    app.bind_host = "::"
    assert supervise.listener_url(app) == "http://127.0.0.1:8500"


# -- restarts -------------------------------------------------------------------------
async def test_an_unexpected_exit_restarts_with_doubling_backoff(app, sup, waits):
    change(app, voice__enabled=True)
    for n in range(1, 9):
        await settle(lambda: len(sup.spawner.children) == n)
        sup.spawner.children[-1].crash(1)
    await settle(lambda: len(sup.spawner.children) == 9)
    backoffs = [w for w in waits if w is not None]
    assert backoffs[:8] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    assert sup.restarts == 8 and sup.last_exit == 1


async def test_exit_zero_waits_for_a_settings_change(app, sup, waits):
    change(app, voice__enabled=True)
    await settle(lambda: len(sup.spawner.children) == 1)
    sup.spawner.children[0].crash(0)  # unsupported / another listener / invalid hotkey
    await settle()
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(sup.spawner.children) == 1 and sup.state()["running"] is False
    change(app, voice__hotkey="ctrl+shift+v")
    await settle(lambda: len(sup.spawner.children) == 2)


async def test_a_voice_setting_change_restarts_and_others_do_not(app, sup):
    change(app, voice__enabled=True)
    await settle(lambda: len(sup.spawner.children) == 1)
    first = sup.spawner.children[0]
    change(app, system__log_level="DEBUG")
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(sup.spawner.children) == 1 and not first.killed
    change(app, voice__mode="toggle")
    await settle(lambda: len(sup.spawner.children) == 2)
    assert first.killed and sup.state()["pid"] == sup.spawner.children[1].pid


async def test_disabling_stops_it(app, sup):
    change(app, voice__enabled=True)
    await settle(lambda: len(sup.spawner.children) == 1)
    change(app, voice__enabled=False)
    await settle(lambda: sup.spawner.children[0].killed)
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(sup.spawner.children) == 1 and sup.state() == {"running": False, "pid": None}


async def test_stop_kills_the_child(app):
    spawner = Spawner()
    s = Supervisor(app, spawn=spawner, allowed=True)
    app.settings.update(app.db, {"voice.enabled": True})
    await s.start()
    await settle(lambda: len(spawner.children) == 1)
    await s.stop()
    assert spawner.children[0].killed and s.task is None and s.state()["running"] is False
    change(app, voice__mode="toggle")  # no longer listening to the bus
    await asyncio.sleep(0)
    assert len(spawner.children) == 1


async def test_a_spawn_that_fails_is_retried(app, sup, waits):
    sup.spawner.fail = 2
    change(app, voice__enabled=True)
    await settle(lambda: len(sup.spawner.children) == 1)
    assert [w for w in waits if w is not None][:2] == [1.0, 2.0]


async def test_shutting_down_never_starts_one(app, sup):
    app.shutting_down = True
    change(app, voice__enabled=True)
    for _ in range(20):
        await asyncio.sleep(0)
    assert sup.spawner.children == []


# -- protected paths ------------------------------------------------------------------
def _voice_rows(app) -> dict[str, str]:
    wanted = {fs_guard.normalise(p) for p in bootstrap.default_voice_protected_paths(bootstrap.checkout_root())}
    return {p: m for p, m in fs_guard.protected_rows(app) if p in wanted}


def test_a_fresh_install_protects_the_listener_files(app):
    rows = _voice_rows(app)
    assert len(rows) == 2 and set(rows.values()) == {"all"}
    assert all(p.endswith(("src/loom/voice/listener.py", "src/loom/voice/win32.py")) for p in rows)


def test_the_rows_arrive_once_with_migration_0011(app):
    for p in list(_voice_rows(app)):
        fs_guard.remove_protected(app, p)
    bootstrap.ensure(app)  # an ordinary restart
    assert _voice_rows(app) == {}
    bootstrap.ensure(app, [11])  # the start that applies 0011 on an existing install
    assert len(_voice_rows(app)) == 2


def test_migration_0011_changes_no_schema(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    for number, path in db.list_migrations():
        if number > 10:
            break
        with db.transaction(conn):
            for statement in db.split_statements(path.read_text(encoding="utf-8")):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={number}")
    before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    assert 11 in db.migrate(conn)
    assert conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before
    conn.close()


def test_every_role_has_a_row(app):
    from loom.models.roles import ROLES

    rows = {r["role"] for r in app.db.execute("SELECT role FROM model_roles")}
    assert set(ROLES) <= rows and "transcription" in rows


# -- the real spawn, with a stand-in program (never `loom listen`) ----------------------
async def test_the_real_spawn_logs_and_reports_the_exit_code(app):
    import os

    spawn = supervise.make_spawn(app)
    child = await spawn([sys.executable, "-c", "print('stand-in'); raise SystemExit(3)"], dict(os.environ))
    assert child.pid and await asyncio.wait_for(child.wait(), 30) == 3
    assert "stand-in" in (app.logs_dir / supervise.LOG_NAME).read_text(encoding="utf-8")


async def test_the_real_spawn_can_be_killed(app):
    import os

    spawn = supervise.make_spawn(app)
    child = await spawn([sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ))
    if os.name == "nt":
        assert child.job is not None  # in a kill-on-close job
    child.kill()
    assert await asyncio.wait_for(child.wait(), 30) != 0
