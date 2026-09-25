"""The shell result names what the call's containment killed (03 §9.2, §9.6; audit F3).

Everything a foreground call starts ends with the call — a kill-on-close Job Object on
Windows, the process group elsewhere. The rule was in the prose and the result said
nothing, so the desk launched a game in a foreground call four times and spent 40
minutes on the wrong hypotheses. Also here: the partial output a timed-out call must
return, which a cancelled `communicate()` used to throw away.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from loom import processes
from loom.tools import dispatch, fs_guard
from loom.tools.native import shell as shell_tool
from loom.tools.registry import ToolContext

PY = sys.executable.replace("\\", "/")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Job Objects are a Windows mechanism")


@pytest.fixture
def project(app, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    fs_guard.add_root(app, str(root), added_by="test")
    return root


@pytest.fixture
def ctx(app, project):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


def _cmd(code: str) -> str:
    """A one-line python child through the configured shell."""
    return f'"{PY}" -c "{code}"'


def _needs_quoted_argv(app) -> None:
    if shell_tool.resolve_flavour(app) == "powershell":
        pytest.skip("this flavour cannot express a quoted argv on one line")


# ---------------------------------------------------------------------------------
# the trailer itself (pure function)
# ---------------------------------------------------------------------------------
def test_nothing_left_running_has_no_trailer():
    assert shell_tool.ended_trailer([]) == ""


def test_one_process_is_named_in_the_singular():
    assert shell_tool.ended_trailer([{"pid": 5732, "name": "tower-defense-graphical.exe"}]) == (
        "[ended 1 process the command left running: tower-defense-graphical.exe pid 5732]"
    )


def test_several_processes_are_listed_in_order():
    ended = [{"pid": 1234, "name": "node.exe"}, {"pid": 5732, "name": "game.exe"}]
    assert shell_tool.ended_trailer(ended) == (
        "[ended 2 processes the command left running: node.exe pid 1234, game.exe pid 5732]"
    )


def test_a_name_that_cannot_be_read_prints_as_the_pid_alone():
    assert shell_tool.ended_trailer([{"pid": 5732, "name": ""}]) == (
        "[ended 1 process the command left running: pid 5732]"
    )
    assert shell_tool.ended_trailer([{"pid": 5732}]) == (
        "[ended 1 process the command left running: pid 5732]"
    )


def test_a_long_list_is_capped():
    ended = [{"pid": 1000 + i, "name": f"p{i}.exe"} for i in range(11)]
    line = shell_tool.ended_trailer(ended)
    assert line.startswith("[ended 11 processes the command left running: p0.exe pid 1000, ")
    assert line.endswith("p7.exe pid 1007, and 3 more]")
    assert "p8.exe" not in line


# ---------------------------------------------------------------------------------
# the enumeration helpers: best effort, never raise
# ---------------------------------------------------------------------------------
def test_the_helpers_answer_safely_on_every_platform():
    assert processes.job_pids(None) == []
    assert processes.process_name(None) == ""
    assert processes.process_name(0) == ""
    assert processes.group_pids(None) == []
    assert processes.group_pids(0) == []
    processes.kill_group(None)  # a no-op, not an exception


@windows_only
def test_windows_has_no_process_groups_to_enumerate_or_kill():
    assert processes.group_pids(os.getpid()) == []
    processes.kill_group(os.getpid())  # a no-op on Windows; the job does the killing
    assert processes.pid_alive(os.getpid())


@windows_only
def test_job_pids_and_process_name_see_a_real_child():
    job = processes.create_job(kill_on_close=True)
    if job is None:
        pytest.skip("no Job Object available on this machine")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert processes.assign(job, child.pid)
        assert child.pid in processes.job_pids(job)
        assert processes.process_name(child.pid).lower().endswith("python.exe")
    finally:
        processes.close(job)  # kill-on-close: this is the kill
        child.kill()
        child.wait(timeout=10)


# ---------------------------------------------------------------------------------
# through a real call
# ---------------------------------------------------------------------------------
async def test_a_plain_command_leaves_nothing_and_says_nothing(app, ctx, project):
    result = await dispatch.execute(app, ctx, app.tools.get("shell"), {"command": "echo hello", "cwd": str(project)})
    assert result.ok and "hello" in result.content
    assert result.content.rstrip().endswith("[exit 0]")
    assert "[ended" not in result.content and "ended" not in result.meta


DETACHER = """\
import os, subprocess, sys
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "detached.pid"), "w") as fh:
    fh.write(str(g.pid))
print("launched", g.pid, flush=True)
"""


@windows_only
async def test_the_result_names_what_the_call_left_running(app, ctx, project):
    """The audited shape: the command starts something that outlives it, exits cleanly,
    and the kill-on-close job ends the survivor as the call returns. The result has to
    say so — that is the whole finding."""
    probe = processes.create_job(kill_on_close=False)
    if probe is None:
        pytest.skip("no Job Object available on this machine")
    processes.close(probe)
    _needs_quoted_argv(app)

    script = project / "detacher.py"
    script.write_text(DETACHER, encoding="utf-8")
    result = await dispatch.execute(
        app, ctx, app.tools.get("shell"),
        {"command": f'"{PY}" "{str(script).replace(chr(92), "/")}"', "cwd": str(project), "timeout_s": 30},
    )
    pid = int((project / "detached.pid").read_text().strip())
    try:
        assert result.ok, result.content
        lines = result.content.strip().splitlines()
        assert lines[-2] == "[exit 0]"  # the trailer goes on its own line, after the exit
        assert lines[-1].startswith("[ended ") and "the command left running: " in lines[-1]
        assert f"pid {pid}" in lines[-1]
        # One name per survivor. A venv python.exe is a trampoline for the real
        # interpreter, so the detached child can legitimately be a pair.
        assert pid in [e["pid"] for e in result.meta["ended"]]
        assert all(e["name"].lower().endswith("python.exe") for e in result.meta["ended"])
    finally:
        processes.kill_tree(pid)


async def test_a_timed_out_call_returns_what_the_command_said_first(app, ctx, project):
    """The 2026-09-24 bug: `communicate()` was cancelled by the timeout and discarded
    everything it had buffered, so a timed-out call came back empty."""
    _needs_quoted_argv(app)
    result = await dispatch.execute(
        app, ctx, app.tools.get("shell"),
        {"command": _cmd("print('before-the-kill', flush=True); import time; time.sleep(60)"),
         "cwd": str(project), "timeout_s": 2},
    )
    if "before-the-kill" not in result.content and result.meta.get("timeout") is not True:
        pytest.skip("this flavour could not run a quoted python one-liner")
    assert not result.ok and result.meta["timeout"] is True
    assert "before-the-kill" in result.content
    assert "[timed out after 2s; process tree killed]" in result.content
    if os.name == "nt":  # the sleeping child was still in the job when the deadline hit
        assert "the command left running: " in result.content
        assert result.content.index("[timed out after") < result.content.index("[ended ")
        # Git Bash re-execs itself and a venv python is a trampoline, so the tree under
        # a timed-out call is legitimately more than one process. All of it is killed.
        assert any(e["name"].lower().endswith("python.exe") for e in result.meta["ended"])
