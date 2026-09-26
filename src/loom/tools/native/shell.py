"""The `shell` tool (03 §9.2, §21 q2).

One command line, one process, one merged output stream, one exit code. The shell
flavour is a setting: `auto` picks Git Bash on Windows when it is installed (so the same
POSIX command lines work on every machine) and `cmd.exe` otherwise. Each flavour's quirks
live in `prompts/context/shell-<flavour>.md` and are appended to this tool's description
at registration.

A timeout kills the whole process tree, not just the shell — on Windows a killed
`cmd.exe` leaves its children running, which is how a "timed out" build keeps holding a
file lock. Partial output is returned either way: what the command managed to say
before it was killed is usually the interesting part, so stdout is drained by a shielded
reader task rather than `communicate()`, whose buffer a cancellation throws away.

What the containment ends is named in the result (`[ended 1 process the command left
running: game.exe pid 5732]`, 2026-09-24). The rule was already in the prose and the
desk still launched a game in a foreground call four times, chasing wrong hypotheses for
40 minutes, because nothing in the result said what had happened to it.

Secrets are already substituted by the dispatcher; `env` values may therefore contain
real credentials and are never echoed back.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from ... import prompts
from ...artifacts import conversation_dir
from ...processes import (
    assign,
    close,
    create_job,
    group_pids,
    job_pids,
    kill_group,
    kill_tree,
    pid_alive,
    process_name,
    terminate_job,
)
from ..fs_guard import normalise
from ..registry import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
)


# Directories whose `bash.exe` is not a shell: Windows' WSL launcher lives in System32 and
# the Store stub in WindowsApps, and a PATH lookup for `bash` lands on one of them first.
_WSL_LAUNCHER_DIRS = ("system32", "windowsapps")


def _registry_git_roots() -> list[str]:
    """Git for Windows records its install root under `SOFTWARE\\GitForWindows`, wherever
    the person put it."""
    if os.name != "nt":
        return []
    import winreg  # Windows only

    roots: list[str] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for flag in (0, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows", 0, winreg.KEY_READ | flag) as key:
                    value = str(winreg.QueryValueEx(key, "InstallPath")[0])
            except OSError:
                continue
            if value and value not in roots:
                roots.append(value)
    return roots


def _is_wsl_launcher(path: str) -> bool:
    parts = path.replace("\\", "/").lower().split("/")
    return any(p in _WSL_LAUNCHER_DIRS for p in parts[:-1])


def find_git_bash() -> str | None:
    """Git's bash, found from where Git is installed: the registry, the standard folders,
    then the `git` on PATH (`<root>/cmd/git.exe` or `<root>/mingw64/bin/git.exe`). A bare
    `bash.exe` on PATH is the last resort and never accepted when it is the WSL launcher:
    on 2026-09-22 a desk told its shell was Git Bash ran in Ubuntu, because Git lived in a
    non-standard folder and System32 came first on PATH."""
    roots = _registry_git_roots()
    roots += [os.path.dirname(os.path.dirname(c)) for c in GIT_BASH_CANDIDATES]
    git = shutil.which("git")
    if git:
        parent = os.path.dirname(os.path.realpath(git))  # <root>/cmd or <root>/mingw64/bin
        roots += [os.path.dirname(parent), os.path.dirname(os.path.dirname(parent))]
    seen: set[str] = set()
    for root in roots:
        if not root or root in seen:
            continue
        seen.add(root)
        for rel in (("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
            candidate = os.path.join(root, *rel)
            if os.path.isfile(candidate) and not _is_wsl_launcher(candidate):
                return candidate
    found = shutil.which("bash")
    return found if found and not _is_wsl_launcher(found) else None


FLAVOURS = ("gitbash", "cmd", "powershell", "sh")


def resolve_flavour(app: Any) -> str:
    """The effective flavour: the setting, with `auto` resolved and a missing Git Bash
    falling back the same way `resolve_shell` does."""
    flavour = str(app.settings.get("permissions.shell_flavour") or "auto")
    if flavour == "auto":
        flavour = ("gitbash" if find_git_bash() else "cmd") if os.name == "nt" else "sh"
    if flavour == "gitbash" and not find_git_bash():
        log.warning("shell_flavour=gitbash but no bash found; falling back")
        flavour = "cmd" if os.name == "nt" else "sh"
    return flavour if flavour in FLAVOURS else "sh"


def flavour_prose(flavour: str) -> str:
    """What the model must know about this flavour to use it without failing against it,
    from `prompts/context/shell-<flavour>.md`. A flavour with nothing to say has no file,
    and that is not an error."""
    name = f"context/shell-{flavour}"
    return prompts.load(name) if prompts.exists(name) else ""


def describe(app: Any = None) -> str:
    """This tool's description: its own prose, then the flavour resolved on this machine
    and its quirks. Composed at registration rather than rendered into the harness block
    every turn — the quirks are static per flavour, and the tool schema is what the model
    reads while it writes a command (2026-09-22). `permissions.shell_flavour` therefore
    needs a restart: `resolve_shell` would change behaviour while a registered description
    still named the old flavour."""
    text = prompts.load("tools/shell")
    if app is None:
        return text
    flavour = resolve_flavour(app)
    header = f"Shell flavour on this machine: {flavour}."
    prose = flavour_prose(flavour)
    return f"{text}\n\n{header}\n{prose}" if prose else f"{text}\n\n{header}"


def resolve_shell(app: Any) -> list[str]:
    """The argv prefix a command line is appended to, e.g.
    `["C:/Program Files/Git/bin/bash.exe", "-lc"]` or `["cmd.exe", "/d", "/c"]`."""
    flavour = resolve_flavour(app)
    if flavour == "gitbash":
        bash = find_git_bash()
        if bash:
            return [bash.replace("\\", "/"), "-lc"]
        flavour = "cmd" if os.name == "nt" else "sh"
    if flavour == "cmd":
        return [shutil.which("cmd") or "cmd.exe", "/d", "/c"]
    if flavour == "powershell":
        return [shutil.which("powershell") or "powershell.exe", "-NoProfile", "-Command"]
    return [shutil.which("sh") or "/bin/sh", "-c"]


MAX_ENDED_NAMES = 8  # beyond this the list says ", and N more"


def effective_timeout(app: Any, args: dict[str, Any]) -> float:
    """The deadline this call runs under: its own `timeout_s` clamped by
    `conversation.tool_timeout_max_s`, else the configured shell timeout. The same value
    `dispatch.timeout_for` computes, whose `wait_for` sits a grace margin above it — so
    this deadline fires first and the output produced before the kill survives."""
    try:
        asked = 0.0 if isinstance(args.get("timeout_s"), bool) else float(args.get("timeout_s"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        asked = 0.0
    if asked > 0:
        return min(asked, float(app.settings.get("conversation.tool_timeout_max_s")))
    return float(app.settings.get("conversation.tool_timeout_shell_s"))


def survivors(proc: asyncio.subprocess.Process, job: int | None) -> list[dict[str, Any]]:
    """What the command left running, as `{"pid", "name"}`, lowest pid first. Snapshot it
    *before* the kill: for a kill-on-close job, dropping the handle is the kill. Best
    effort — an enumeration that fails just means no trailer."""
    found: list[dict[str, Any]] = []
    try:
        pairs = [(pid, "") for pid in job_pids(job)] if os.name == "nt" else group_pids(proc.pid)
        for pid, name in sorted(pairs):
            if not pid or int(pid) == int(proc.pid) or not pid_alive(pid):
                continue
            found.append({"pid": int(pid), "name": name or process_name(pid)})
    except Exception as exc:  # pragma: no cover - platform dependent
        log.debug("could not list what the command left running: %s", exc)
    return found


def ended_trailer(ended: list[dict[str, Any]]) -> str:
    """The line that makes the kill-on-close rule concrete. Empty when nothing was left
    running, which is the ordinary case."""
    if not ended:
        return ""
    shown = ended[:MAX_ENDED_NAMES]
    names = ", ".join(f"{e['name']} pid {e['pid']}" if e.get("name") else f"pid {e['pid']}" for e in shown)
    if len(ended) > len(shown):
        names += f", and {len(ended) - len(shown)} more"
    noun = "process" if len(ended) == 1 else "processes"
    return f"[ended {len(ended)} {noun} the command left running: {names}]"


def _kill_tree(proc: asyncio.subprocess.Process, job: int | None = None) -> None:
    """Kill the process and everything it started. The Job Object reaches descendants
    whose parent already exited; the pid tree kill is belt and braces (`loom.processes`)."""
    terminate_job(job)
    if proc.returncode is not None:
        return
    kill_tree(proc.pid)


def scratch_dir(ctx: ToolContext) -> str | None:
    """The conversation's artifacts folder, created, as the command's TMPDIR: `mktemp`,
    Python's `tempfile` and the like land where the harness block already steers
    downloads (2026-09-25). Only TMPDIR — TEMP and TMP stay the user's, because Git
    Bash mounts `/tmp` at TEMP (`usertemp`) and `fs_guard.normalise` reads `/tmp`
    through one per-process mount table; moving it per conversation would make the
    guard and the shell disagree about `/tmp` again. None when it cannot be made."""
    try:
        folder = conversation_dir(ctx.app, ctx.conversation_id)
        folder.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("shell: no scratch folder for %s: %s", ctx.conversation.get("id"), exc)
        return None
    return normalise(str(folder))


async def shell_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    command = str(args.get("command") or "").strip()
    if not command:
        return ToolResult.error("command is empty")
    app = ctx.app
    roots = ctx.roots()
    cwd_arg = args.get("cwd")
    cwd = normalise(str(cwd_arg), cwd=roots[0] if roots else None) if cwd_arg else (roots[0] if roots else os.getcwd())
    if not Path(cwd).is_dir():
        return ToolResult.error(f"cwd does not exist: {cwd}")

    env = dict(os.environ)
    scratch = scratch_dir(ctx)
    if scratch:
        env["TMPDIR"] = scratch
    extra = args.get("env")
    if isinstance(extra, dict):
        env.update({str(k): str(v) for k, v in extra.items()})

    if args.get("background"):
        # A program that must keep running (03 §9.6): a row, a log file, a watcher; the
        # call returns at once and the conversation hears about the exit later.
        row = await app.processes.start(ctx.conversation_id, command, cwd, env=env, title=str(args.get("title") or ""))
        if row.get("status") != "running":
            return ToolResult.error(row.get("error") or "could not start the process", process_id=row.get("id"))
        pid_ = row["id"]
        return ToolResult(
            content=(
                f'started process {pid_} "{row["title"]}" (pid {row["pid"]}); log: {row["log_path"]}\n'
                f'Read its output with process(action="logs", id="{pid_}"), end it with process(action="stop", id="{pid_}"). '
                "You will get a message in this conversation when it exits."
            ),
            meta={"process_id": pid_, "pid": row["pid"], "cwd": cwd},
        )

    timeout = effective_timeout(app, args)
    argv = resolve_shell(app) + [command]
    stdin_text = args.get("stdin")

    # Everything a foreground call starts ends with the call: a kill-on-close Job Object
    # on Windows (descendants whose parent exited included), a process group elsewhere.
    # Orphans of a timed-out `node … &` had kept a port for hours (2026-09-22).
    job = create_job(kill_on_close=True)
    kwargs: dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,
        )
    except (OSError, ValueError) as exc:
        close(job)
        return ToolResult.error(f"cannot start {argv[0]}: {exc}")
    if job is not None and not assign(job, proc.pid):
        close(job)
        job = None

    # Drained into a list, not with `communicate()`: a cancelled `communicate()` discards
    # everything it had already buffered, and what a killed command said before it died
    # is the interesting part (2026-09-24).
    chunks: list[bytes] = []

    async def drain() -> None:
        while proc.stdout is not None:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return
            chunks.append(chunk)

    reader = asyncio.ensure_future(drain())
    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(str(stdin_text).encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # a command that never reads its input is not an error
        try:
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    timed_out = False
    ended: list[dict[str, Any]] = []
    try:
        # Shielded: the timeout must not cancel the reader, which still has to drain the
        # pipe that the kill is about to close.
        await asyncio.wait_for(asyncio.shield(reader), timeout=timeout)
        await proc.wait()
    except asyncio.TimeoutError:
        timed_out = True
        ended = survivors(proc, job)  # before the kill: the job is about to be emptied
        _kill_tree(proc, job)
        try:
            await asyncio.wait_for(reader, timeout=5)  # finishes as the kill closes the pipe
        except Exception:
            reader.cancel()  # whatever it collected is already in `chunks`
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)  # reap the child we just killed
        except Exception:
            pass
    except asyncio.CancelledError:
        _kill_tree(proc, job)
        reader.cancel()
        raise
    else:
        ended = survivors(proc, job)
        kill_group(proc.pid)  # POSIX: the group outlives the shell; on Windows close(job) does it
    finally:
        close(job)  # kill-on-close: whatever the command left behind ends here

    text = b"".join(chunks).decode("utf-8", errors="replace")
    code = proc.returncode if proc.returncode is not None else -1
    trailer = f"[timed out after {timeout:g}s; process tree killed]" if timed_out else f"[exit {code}]"
    content = text + ("\n" if text and not text.endswith("\n") else "") + trailer
    line = ended_trailer(ended)
    if line:
        content += "\n" + line
    meta: dict[str, Any] = {"timeout": True, "cwd": cwd} if timed_out else {"exit_code": code, "cwd": cwd}
    if ended:
        meta["ended"] = ended
    return ToolResult(content=content, ok=(not timed_out and code == 0), meta=meta)


def tools(app: Any = None) -> list[Tool]:
    return [
        Tool(
            name="shell",
            description=describe(app),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command line to run."},
                    "cwd": {"type": "string", "description": "Working directory; must be in an allowed root."},
                    "timeout_s": {"type": "number", "description": "Seconds before the process tree is killed."},
                    "env": {"type": "object", "description": "Extra environment variables."},
                    "stdin": {"type": "string", "description": "Text piped to the process."},
                    "background": {"type": "boolean", "description": "Start it as a background process and return at once; manage it with the process tool."},
                    "title": {"type": "string", "description": "Short label for a background process."},
                },
                "required": ["command"],
            },
            handler=shell_handler,
            path_params=("cwd",),
        )
    ]
