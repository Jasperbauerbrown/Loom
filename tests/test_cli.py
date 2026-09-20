"""CLI: argument parsing and base-URL/token resolution. The commands themselves are
thin REST calls (03 §14), covered by the router tests."""

from __future__ import annotations

import json
import sqlite3

import pytest

from loom import cli


def parse(*argv: str):
    return cli.build_parser().parse_args(list(argv))


def test_send_defaults_to_the_desk():
    args = parse("send", "hello there")
    assert args.command == "send" and args.text == "hello there"
    assert args.thread is None and args.new is None and args.func is cli.cmd_send


def test_send_thread_and_new_are_mutually_exclusive():
    assert parse("send", "--thread", "01J8", "hi").thread == "01J8"
    assert parse("send", "--new", "build a bot", "hi").new == "build a bot"
    with pytest.raises(SystemExit):
        parse("send", "--thread", "01J8", "--new", "goal", "hi")


def test_serve_flags():
    args = parse("serve", "--port", "9000", "--no-supervisor", "--fake", "--no-browser")
    assert (args.port, args.no_supervisor, args.fake, args.no_browser) == (9000, True, True, True)
    assert parse("serve").no_supervisor is False  # end users get the supervisor


def test_every_documented_subcommand_parses():
    assert parse("threads", "--all").all is True
    assert parse("approvals", "--allow", "a1").allow == "a1"
    assert parse("approvals", "--deny", "a1").deny == "a1"
    assert parse("items").func is cli.cmd_items
    assert parse("status").func is cli.cmd_status
    assert parse("reindex").func is cli.cmd_reindex
    assert parse("vault", "search", "printer").query == "printer"
    assert parse("vault", "read", "devices/ender-3.md").path == "devices/ender-3.md"
    assert parse("schedules").func is cli.cmd_schedules
    assert parse("promote", "abc123").ref == "abc123"
    assert parse("rollback").func is cli.cmd_rollback
    with pytest.raises(SystemExit):
        parse()


def test_base_url_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("LOOM_URL", raising=False)
    monkeypatch.setenv("LOOM_HOME", str(tmp_path))
    assert cli.base_url(parse("status")) == "http://127.0.0.1:8500"

    monkeypatch.setenv("LOOM_URL", "http://box.lan:9999/")
    assert cli.base_url(parse("status")) == "http://box.lan:9999"

    args = parse("--url", "http://explicit:1/", "status")
    assert cli.base_url(args) == "http://explicit:1"


def test_base_url_reads_the_port_from_the_home_database(monkeypatch, tmp_path):
    monkeypatch.delenv("LOOM_URL", raising=False)
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO settings VALUES ('general.port', ?, 'now')", (json.dumps(8777),))
    conn.commit()
    conn.close()
    assert cli.port_from_home(tmp_path) == 8777
    assert cli.base_url(parse("--home", str(tmp_path), "status")) == "http://127.0.0.1:8777"


def test_port_from_home_survives_a_missing_or_broken_database(tmp_path):
    assert cli.port_from_home(tmp_path) == cli.DEFAULT_PORT
    (tmp_path / "state.db").write_bytes(b"not a database")
    assert cli.port_from_home(tmp_path) == cli.DEFAULT_PORT


def test_token_precedence(monkeypatch):
    monkeypatch.delenv("LOOM_TOKEN", raising=False)
    assert cli.token_for(parse("status")) == ""
    monkeypatch.setenv("LOOM_TOKEN", "from-env")
    assert cli.token_for(parse("status")) == "from-env"
    assert cli.token_for(parse("--token", "explicit", "status")) == "explicit"


def test_event_rendering():
    assert cli._render_event({"kind": "assistant", "content": "hi"}, False) == "hi"
    assert cli._render_event({"kind": "thinking", "content": "hmm"}, False) == ""
    assert cli._render_event({"kind": "system", "content": "failed"}, False) == "! failed"
    # tool_call content is the persisted JSON list of calls (03 §4.2); meta.text is a preamble
    content = '[{"id": "c1", "name": "shell", "arguments": {"command": "ls"}}]'
    line = cli._render_event({"kind": "tool_call", "content": content, "meta": {"text": ""}}, False)
    assert line.startswith("  · shell(") and "ls" in line
    two = cli._render_event({"kind": "tool_call", "content": content, "meta": {"text": "Checking."}}, False)
    assert two.splitlines()[0] == "Checking." and two.splitlines()[1].startswith("  · shell(")
    assert cli._render_event({"kind": "tool_call", "content": content, "meta": {}}, True) == ""


def test_exit_codes_match_the_documented_statuses():
    assert cli.TERMINAL["idle"] == 0 and cli.TERMINAL["done"] == 0
    assert cli.TERMINAL["failed"] == 1
    assert cli.TERMINAL["waiting_approval"] == 2 and cli.TERMINAL["waiting_input"] == 2


def test_missing_backend_is_a_message_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("LOOM_URL", "http://127.0.0.1:9")  # discard port: nothing listens
    code = cli.main(["send", "hello"])
    assert code == 4
    assert "cannot reach the backend" in capsys.readouterr().err


def test_a_backend_without_conversations_says_so(monkeypatch, capsys):
    import httpx

    class Stub(cli.Api):
        def request(self, method, path, **kw):
            raise cli.NotFound("conversations not found")

    monkeypatch.setattr(cli, "Api", Stub)
    monkeypatch.setenv("LOOM_URL", "http://127.0.0.1:8500")
    code = cli.main(["send", "hello"])
    assert code == 4
    assert "does not expose /conversations yet" in capsys.readouterr().err


def test_no_model_configured_exits_three(monkeypatch, capsys):
    import httpx

    class Stub(cli.Api):
        def request(self, method, path, **kw):
            if path == "/conversations":
                return [{"id": "desk1"}]
            response = httpx.Response(409, json={"detail": "no model configured for role desk — open http://…"})
            raise cli.CliError(f"409: {cli._detail(response)}", cli._code_for(response))

    monkeypatch.setattr(cli, "Api", Stub)
    monkeypatch.setenv("LOOM_URL", "http://127.0.0.1:8500")
    assert cli.main(["send", "hello"]) == 3
    assert "no model configured" in capsys.readouterr().err
