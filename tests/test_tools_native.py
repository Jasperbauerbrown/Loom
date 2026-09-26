"""Native tools: the handlers themselves (03 §9.2). Dispatch is covered separately."""

from __future__ import annotations

import os

import httpx
import pytest

from loom.tools import fs_guard
from loom.tools.native import http as http_tool
from loom.tools.native import shell as shell_tool
from loom.tools.registry import ToolContext


@pytest.fixture
def project(app, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    fs_guard.add_root(app, str(root), added_by="test")
    return root


@pytest.fixture
def ctx(app, project):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


async def run(app, ctx, name, args):
    return await app.tools.get(name).handler(ctx, args)


# ---------------------------------------------------------------------------------
# fs_read
# ---------------------------------------------------------------------------------
async def test_fs_read_returns_the_whole_small_file(app, ctx, project):
    (project / "a.txt").write_text("one\ntwo\nthree", encoding="utf-8")
    result = await run(app, ctx, "fs_read", {"path": str(project / "a.txt")})
    assert result.ok and result.content == "1\tone\n2\ttwo\n3\tthree"  # numbered by default
    assert result.meta["lines"] == 3


async def test_fs_read_pages_with_a_footer(app, ctx, project):
    (project / "big.txt").write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
    result = await run(app, ctx, "fs_read", {"path": str(project / "big.txt"), "offset": 0, "limit": 10})
    assert result.content.startswith("1\tline 0")
    assert "[lines 1–10 of 100; fs_read(path, offset=10) for more]" in result.content

    rest = await run(app, ctx, "fs_read", {"path": str(project / "big.txt"), "offset": 10, "limit": 10})
    assert rest.content.startswith("11\tline 10")


async def test_fs_read_reports_missing_binary_and_directories(app, ctx, project):
    assert "no such file" in (await run(app, ctx, "fs_read", {"path": str(project / "nope")})).content
    (project / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    assert "binary" in (await run(app, ctx, "fs_read", {"path": str(project / "bin.dat")})).content
    assert "is a directory" in (await run(app, ctx, "fs_read", {"path": str(project)})).content


async def test_fs_read_past_the_end_says_so(app, ctx, project):
    (project / "a.txt").write_text("one", encoding="utf-8")
    result = await run(app, ctx, "fs_read", {"path": str(project / "a.txt"), "offset": 50})
    assert "past the end" in result.content


async def test_fs_read_caps_the_characters_it_returns(app, ctx, project):
    (project / "wide.txt").write_text("\n".join("x" * 200 for _ in range(200)), encoding="utf-8")
    result = await run(app, ctx, "fs_read", {"path": str(project / "wide.txt"), "limit": 400})
    assert len(result.content) < 9000
    assert "for more]" in result.content


# ---------------------------------------------------------------------------------
# fs_list
# ---------------------------------------------------------------------------------
async def test_fs_list_lists_recursively_and_marks_directories(app, ctx, project):
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("x", encoding="utf-8")
    (project / "README.md").write_text("x", encoding="utf-8")
    (project / ".hidden").write_text("x", encoding="utf-8")

    result = await run(app, ctx, "fs_list", {"path": str(project)})
    lines = result.content.splitlines()
    assert "src/" in lines and "src/main.py" in lines and "README.md" in lines
    assert ".hidden" not in lines

    with_hidden = await run(app, ctx, "fs_list", {"path": str(project), "include_hidden": True})
    assert ".hidden" in with_hidden.content


async def test_fs_list_honours_the_pattern_and_max_results(app, ctx, project):
    for i in range(10):
        (project / f"f{i}.py").write_text("x", encoding="utf-8")
    (project / "notes.md").write_text("x", encoding="utf-8")

    result = await run(app, ctx, "fs_list", {"path": str(project), "pattern": "*.py"})
    assert "notes.md" not in result.content and result.meta["count"] == 10

    capped = await run(app, ctx, "fs_list", {"path": str(project), "max_results": 3})
    assert capped.meta["truncated"] is True and "more were dropped" in capped.content


async def test_fs_list_on_an_empty_or_missing_directory(app, ctx, project):
    (project / "empty").mkdir()
    assert "no entries" in (await run(app, ctx, "fs_list", {"path": str(project / "empty")})).content
    assert "no such directory" in (await run(app, ctx, "fs_list", {"path": str(project / "gone")})).content


# ---------------------------------------------------------------------------------
# fs_search
# ---------------------------------------------------------------------------------
async def test_fs_search_finds_matches_with_context(app, ctx, project):
    (project / "a.py").write_text("import os\n\ndef work():\n    return TODO\n", encoding="utf-8")
    (project / "b.py").write_text("nothing here\n", encoding="utf-8")

    result = await run(app, ctx, "fs_search", {"pattern": "TODO", "path": str(project)})
    assert "a.py:4:>" in result.content
    assert "def work" in result.content  # context line
    assert "b.py" not in result.content
    assert result.meta["matches"] == 1


async def test_fs_search_is_case_insensitive_by_default(app, ctx, project):
    (project / "a.txt").write_text("Todo: sleep", encoding="utf-8")
    assert (await run(app, ctx, "fs_search", {"pattern": "todo", "path": str(project)})).meta["matches"] == 1
    strict = await run(app, ctx, "fs_search", {"pattern": "todo", "path": str(project), "case_sensitive": True})
    assert strict.meta["matches"] == 0


async def test_fs_search_reports_bad_regexes_and_no_matches(app, ctx, project):
    (project / "a.txt").write_text("hello", encoding="utf-8")
    assert "invalid regular expression" in (await run(app, ctx, "fs_search", {"pattern": "a[", "path": str(project)})).content
    assert "no matches" in (await run(app, ctx, "fs_search", {"pattern": "zzz", "path": str(project)})).content


async def test_fs_search_skips_binaries_and_respects_max_results(app, ctx, project):
    (project / "bin.dat").write_bytes(b"\x00hit hit hit")
    (project / "a.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")
    result = await run(app, ctx, "fs_search", {"pattern": "hit", "path": str(project), "max_results": 2})
    assert result.meta["matches"] == 2 and result.meta["truncated"] is True
    assert "bin.dat" not in result.content


# ---------------------------------------------------------------------------------
# fs_write / fs_edit
# ---------------------------------------------------------------------------------
async def test_fs_write_creates_directories_and_reports_bytes(app, ctx, project):
    target = project / "deep" / "nested" / "f.txt"
    result = await run(app, ctx, "fs_write", {"path": str(target), "content": "hello"})
    assert result.ok and result.content == f"wrote 5 bytes to {target.as_posix()}"
    assert target.read_text(encoding="utf-8") == "hello"


async def test_fs_write_refuses_to_create_directories_when_told_not_to(app, ctx, project):
    result = await run(app, ctx, "fs_write", {"path": str(project / "x" / "f.txt"), "content": "c", "create_dirs": False})
    assert not result.ok and "no such directory" in result.content


async def test_fs_edit_replaces_a_unique_occurrence_and_shows_a_diff(app, ctx, project):
    target = project / "a.py"
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(target)})  # the stale-write guard wants a read first
    result = await run(app, ctx, "fs_edit", {"path": str(target), "old": "b = 2", "new": "b = 22"})
    assert result.ok and "1 replacement" in result.content
    assert "-b = 2" in result.content and "+b = 22" in result.content
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 22\nc = 3\n"


async def test_fs_edit_requires_uniqueness_and_says_how_many_it_found(app, ctx, project):
    target = project / "a.py"
    target.write_text("x\nx\nx\n", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(target)})
    result = await run(app, ctx, "fs_edit", {"path": str(target), "old": "x", "new": "y"})
    assert not result.ok and "appears 3 times" in result.content
    assert target.read_text(encoding="utf-8") == "x\nx\nx\n"

    every = await run(app, ctx, "fs_edit", {"path": str(target), "old": "x", "new": "y", "replace_all": True})
    assert every.ok and every.meta["replacements"] == 3
    assert target.read_text(encoding="utf-8") == "y\ny\ny\n"


async def test_fs_edit_reports_text_it_cannot_find(app, ctx, project):
    target = project / "a.py"
    target.write_text("hello", encoding="utf-8")
    await run(app, ctx, "fs_read", {"path": str(target)})
    result = await run(app, ctx, "fs_edit", {"path": str(target), "old": "goodbye", "new": "x"})
    assert not result.ok and "not found" in result.content


# ---------------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------------
def test_resolve_shell_returns_an_argv_prefix(app):
    argv = shell_tool.resolve_shell(app)
    assert isinstance(argv, list) and len(argv) >= 2
    assert argv[0]


def test_resolve_shell_honours_the_setting(app):
    app.settings.set(app.db, "permissions.shell_flavour", "cmd" if os.name == "nt" else "sh")
    argv = shell_tool.resolve_shell(app)
    assert ("cmd" in argv[0].lower()) if os.name == "nt" else argv[-1] == "-c"


# Git Bash is found from where Git is installed, never from a bare `bash.exe` on PATH: on
# Windows that is the WSL launcher in System32 (or the Store stub in WindowsApps), and a
# desk told "Git Bash" once ran in Ubuntu that way (2026-09-22).
def _plant(*files):
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"")


def test_find_git_bash_refuses_the_wsl_launcher(monkeypatch, tmp_path):
    launcher = tmp_path / "Windows" / "System32" / "bash.exe"
    _plant(launcher)
    monkeypatch.setattr(shell_tool, "_registry_git_roots", lambda: [])
    monkeypatch.setattr(shell_tool, "GIT_BASH_CANDIDATES", ())
    monkeypatch.setattr(shell_tool.shutil, "which", lambda name, *a, **k: str(launcher) if name == "bash" else None)
    assert shell_tool.find_git_bash() is None


def test_find_git_bash_follows_the_git_install(monkeypatch, tmp_path):
    root = tmp_path / "Computer Stuff" / "Git"
    bash, git = root / "usr" / "bin" / "bash.exe", root / "cmd" / "git.exe"
    launcher = tmp_path / "Windows" / "System32" / "bash.exe"
    _plant(bash, git, launcher)
    monkeypatch.setattr(shell_tool, "_registry_git_roots", lambda: [])
    monkeypatch.setattr(shell_tool, "GIT_BASH_CANDIDATES", ())
    monkeypatch.setattr(shell_tool.shutil, "which", lambda name, *a, **k: {"git": str(git), "bash": str(launcher)}.get(name))
    assert os.path.normcase(shell_tool.find_git_bash() or "") == os.path.normcase(str(bash))
    # the registry's install root wins when it is set, wherever Git was put
    other = tmp_path / "Elsewhere" / "Git" / "bin" / "bash.exe"
    _plant(other)
    monkeypatch.setattr(shell_tool, "_registry_git_roots", lambda: [str(other.parent.parent)])
    assert os.path.normcase(shell_tool.find_git_bash() or "") == os.path.normcase(str(other))


async def test_shell_round_trips_a_command(app, ctx, project):
    result = await run(app, ctx, "shell", {"command": "echo loom-works", "cwd": str(project)})
    assert result.ok
    assert "loom-works" in result.content
    assert result.content.rstrip().endswith("[exit 0]")
    assert result.meta["exit_code"] == 0


async def test_shell_reports_a_non_zero_exit_and_merges_stderr(app, ctx, project):
    result = await run(app, ctx, "shell", {"command": "echo out; echo err 1>&2; exit 3", "cwd": str(project)})
    if "[exit 3]" not in result.content:  # cmd.exe cannot run that line
        pytest.skip("POSIX shell not available for the exit-code round trip")
    assert not result.ok and result.meta["exit_code"] == 3
    assert "out" in result.content and "err" in result.content


async def test_shell_rejects_an_empty_command_and_a_missing_cwd(app, ctx, project):
    assert "command is empty" in (await run(app, ctx, "shell", {"command": "  "})).content
    result = await run(app, ctx, "shell", {"command": "echo x", "cwd": str(project / "nope")})
    assert "cwd does not exist" in result.content


async def test_shell_times_out_and_says_so(app, ctx, project):
    result = await run(app, ctx, "shell", {"command": "sleep 5", "cwd": str(project), "timeout_s": 0.4})
    assert not result.ok
    assert result.meta["timeout"] is True
    assert "timed out" in result.content


async def test_shell_passes_env_and_stdin(app, ctx, project):
    result = await run(app, ctx, "shell", {"command": "cat", "cwd": str(project), "stdin": "piped-in"})
    if not result.ok:
        pytest.skip("no cat on this machine")
    assert "piped-in" in result.content


async def test_shell_tmpdir_is_the_conversation_scratch_folder(app, ctx, project):
    """TMPDIR points at the conversation's artifacts folder, created on demand, so
    `mktemp` and `tempfile` land where the harness block steers downloads — inside the
    roots, not the machine-wide temp dir (2026-09-25). TEMP/TMP are left alone: Git
    Bash mounts `/tmp` at TEMP, and the guard reads `/tmp` through that mount."""
    from loom.artifacts import conversation_dir

    folder = conversation_dir(app, ctx.conversation_id)
    assert shell_tool.scratch_dir(ctx) == fs_guard.normalise(str(folder))
    assert folder.is_dir()
    assert fs_guard.within_roots(app, ctx.conversation, str(folder))
    result = await run(app, ctx, "shell", {"command": 'echo "tmp=$TMPDIR"', "cwd": str(project)})
    if "tmp=$TMPDIR" in result.content or "tmp=" not in result.content:
        pytest.skip("POSIX shell not available for the round trip")
    shown = result.content.split("tmp=", 1)[1].splitlines()[0].strip()
    assert fs_guard.normalise(shown) == fs_guard.normalise(str(folder))
    # an explicit env still wins
    result = await run(app, ctx, "shell", {"command": 'echo "tmp=$TMPDIR"', "cwd": str(project), "env": {"TMPDIR": str(project)}})
    assert fs_guard.normalise(result.content.split("tmp=", 1)[1].splitlines()[0].strip()) == fs_guard.normalise(str(project))


# The flavour's quirks are prose in prompts/context/shell-<flavour>.md, appended to the
# tool description at registration — not hardcoded, and not re-rendered into the harness
# block every turn (2026-09-22).
def test_shell_description_carries_the_flavour_quirks(app):
    if shell_tool.find_git_bash() is None:
        pytest.skip("Git Bash not installed; its quirks cannot be the resolved flavour")
    app.settings.set(app.db, "permissions.shell_flavour", "gitbash")
    text = shell_tool.describe(app)
    assert "Shell flavour on this machine: gitbash." in text
    assert "//F" in text and "/dev/null" in text


def test_shell_description_omits_quirks_for_a_flavour_without_a_file(app):
    app.settings.set(app.db, "permissions.shell_flavour", "sh")
    text = shell_tool.describe(app)
    assert text.rstrip().endswith("Shell flavour on this machine: sh.")
    assert "//F" not in text


def test_a_missing_flavour_file_is_empty_not_an_error():
    assert shell_tool.flavour_prose("sh") == ""
    assert shell_tool.flavour_prose("no-such-flavour") == ""
    assert "//F" in shell_tool.flavour_prose("gitbash")


def test_the_quirks_are_not_in_the_tools_prose_file():
    from loom import prompts

    assert "//F" not in prompts.load("tools/shell")
    assert shell_tool.describe(None) == prompts.load("tools/shell")


def test_registered_shell_tool_is_composed_with_the_flavour(app):
    assert "Shell flavour on this machine:" in app.tools.get("shell").description


# ---------------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------------
@pytest.fixture
def mock_http(monkeypatch):
    """Replace the client factory with one on a MockTransport; nothing leaves the box."""
    seen: list[httpx.Request] = []

    def install(handler):
        def factory(timeout, follow_redirects):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

        monkeypatch.setattr(http_tool, "client_factory", factory)

    return seen, install


async def test_http_get_formats_status_headers_and_json_body(app, ctx, mock_http):
    seen, install = mock_http

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "items": [1, 2]})

    install(handler)
    result = await run(app, ctx, "http", {"method": "GET", "url": "https://api.example.com/x"})
    assert result.ok
    assert result.content.startswith("HTTP 200 application/json")
    assert '"ok": true' in result.content  # pretty-printed
    assert result.meta["status"] == 200
    assert seen[0].method == "GET"


async def test_http_sends_headers_and_a_json_body(app, ctx, mock_http):
    seen, install = mock_http

    def handler(request):
        seen.append(request)
        return httpx.Response(201, text="created")

    install(handler)
    result = await run(
        app,
        ctx,
        "http",
        {"method": "POST", "url": "https://api.example.com/x", "headers": {"Authorization": "Bearer t"}, "body": {"a": 1}},
    )
    assert result.ok and "created" in result.content
    assert seen[0].headers["authorization"] == "Bearer t"
    assert seen[0].read() == b'{"a":1}'


async def test_http_reports_a_failing_status_without_raising(app, ctx, mock_http):
    _, install = mock_http
    install(lambda request: httpx.Response(404, text="nope"))
    result = await run(app, ctx, "http", {"method": "GET", "url": "https://example.com/missing"})
    assert not result.ok and "HTTP 404" in result.content


async def test_http_network_failure_is_an_error_result(app, ctx, mock_http):
    _, install = mock_http

    def handler(request):
        raise httpx.ConnectError("name or service not known", request=request)

    install(handler)
    result = await run(app, ctx, "http", {"method": "GET", "url": "https://nowhere.invalid"})
    assert not result.ok and "failed" in result.content


async def test_http_rejects_bad_methods_and_empty_urls(app, ctx):
    assert "unsupported method" in (await run(app, ctx, "http", {"method": "TRACE", "url": "https://x"})).content
    assert "url is empty" in (await run(app, ctx, "http", {"method": "GET", "url": ""})).content


# ---------------------------------------------------------------------------------
# tool_search / secret_set / artifact_recall
# ---------------------------------------------------------------------------------
async def test_tool_search_returns_definitions_and_exposes_them_for_the_turn(app, ctx):
    result = await run(app, ctx, "tool_search", {"query": "read a file", "k": 3})
    assert result.ok and result.meta["count"] > 0
    assert set(result.meta["names"]) <= set(app.tools.names())
    assert ctx.extras["exposed"] == set(result.meta["names"])
    assert "parameters" in result.content


async def test_tool_search_says_so_when_nothing_matches(app, ctx):
    result = await run(app, ctx, "tool_search", {"query": "zzzzqqq"})
    assert result.meta["count"] == 0
    assert (await run(app, ctx, "tool_search", {"query": " "})).ok is False


async def test_secret_set_stores_without_echoing_the_value(app, ctx):
    result = await run(app, ctx, "secret_set", {"name": "API_KEY", "value": "sup3r-s3cret"})
    assert result.ok
    assert "sup3r-s3cret" not in result.content
    assert "stored secret API_KEY (12 chars)" in result.content
    assert app.secrets.get("API_KEY") == "sup3r-s3cret"


async def test_secret_set_rejects_bad_names_and_empty_values(app, ctx):
    assert not (await run(app, ctx, "secret_set", {"name": "no spaces!", "value": "x"})).ok
    assert not (await run(app, ctx, "secret_set", {"name": "OK", "value": ""})).ok


async def test_artifact_recall_pages_through_a_spilled_artifact(app, ctx):
    row = app.artifacts.spill(app.desk_id(), 1, "abcdefghij" * 10)
    result = await run(app, ctx, "artifact_recall", {"id": row["id"], "offset": 0, "limit": 20})
    assert result.ok and result.content.startswith("abcdefghijabcdefghij")
    assert "offset=20" in result.content
    assert not (await run(app, ctx, "artifact_recall", {"id": "NOPE"})).ok
    assert not (await run(app, ctx, "artifact_recall", {"id": ""})).ok


# ---------------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------------
def test_every_native_tool_is_registered_with_a_prompt_description(app):
    for name in ("fs_read", "fs_list", "fs_search", "fs_write", "fs_edit", "shell", "http", "tool_search", "secret_set", "artifact_recall"):
        tool = app.tools.get(name)
        assert tool is not None, name
        assert len(tool.description) > 80, name
        assert tool.source == "native"
        assert tool.parameters["type"] == "object"


def test_path_params_are_declared_where_the_guard_needs_them(app):
    assert app.tools.get("fs_write").path_params == ("path",)
    assert app.tools.get("shell").path_params == ("cwd",)
    assert app.tools.get("http").path_params == ()


async def test_fs_read_names_the_base_when_a_relative_path_misses(app, ctx, project):
    result = await run(app, ctx, "fs_read", {"path": "projects/nope.md"})
    assert not result.ok and "no such file" in result.content
    assert "relative paths resolve against the first allowed root" in result.content
    absolute = await run(app, ctx, "fs_read", {"path": str(project / "nope.md")})
    assert "relative paths" not in absolute.content
