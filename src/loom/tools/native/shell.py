"""The `shell` tool (03 §9.2, §21 q2).

One command line, one process, one merged output stream, one exit code. The shell
flavour is a setting: `auto` picks Git Bash on Windows when it is installed (so the same
POSIX command lines work on every machine) and `cmd.exe` otherwise.

A timeout kills the whole process tree, not just the shell — on Windows a killed
`cmd.exe` leaves its children running, which is how a "timed out" build keeps holding a
file lock. Partial output is returned either way: what the command managed to say
before it was killed is usually the interesting part.

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
from ...processes import kill_tree
from ..fs_guard import normalise
from ..registry import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
)


def find_git_bash() -> str | None:
    for candidate in GIT_BASH_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("bash")
    return found if found else None


def resolve_shell(app: Any) -> list[str]:
    """The argv prefix a command line is appended to, e.g.
    `["C:/Program Files/Git/bin/bash.exe", "-lc"]` or `["cmd.exe", "/d", "/c"]`."""
    flavour = str(app.settings.get("permissions.shell_flavour") or "auto")
    if flavour == "auto":
        if os.name == "nt":
            bash = find_git_bash()
            flavour = "gitbash" if bash else "cmd"
        else:
            flavour = "sh"
    if flavour == "gitbash":
        bash = find_git_bash()
        if bash:
            return [bash.replace("\\", "/"), "-lc"]
        log.warning("shell_flavour=gitbash but no bash found; falling back")
        flavour = "cmd" if os.name == "nt" else "sh"
    if flavour == "cmd":
        return [shutil.which("cmd") or "cmd.exe", "/d", "/c"]
    if flavour == "powershell":
        return [shutil.which("powershell") or "powershell.exe", "-NoProfile", "-Command"]
    return [shutil.which("sh") or "/bin/sh", "-c"]


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and everything it started (shared helper in `loom.processes`)."""
    if proc.returncode is not None:
        return
    kill_tree(proc.pid)


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

    timeout = float(args.get("timeout_s") or app.settings.get("conversation.tool_timeout_shell_s"))
    argv = resolve_shell(app) + [command]
    stdin_text = args.get("stdin")

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, ValueError) as exc:
        return ToolResult.error(f"cannot start {argv[0]}: {exc}")

    timed_out = False
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(str(stdin_text).encode("utf-8") if stdin_text is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        timed_out = True
        _kill_tree(proc)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception:
            out = b""
    except asyncio.CancelledError:
        _kill_tree(proc)
        raise

    text = out.decode("utf-8", errors="replace") if out else ""
    if timed_out:
        trailer = f"[timed out after {timeout:g}s; process tree killed]"
        return ToolResult(
            content=(text + ("\n" if text and not text.endswith("\n") else "") + trailer),
            ok=False,
            meta={"timeout": True, "cwd": cwd},
        )
    code = proc.returncode if proc.returncode is not None else -1
    trailer = f"[exit {code}]"
    content = text + ("\n" if text and not text.endswith("\n") else "") + trailer
    return ToolResult(content=content, ok=(code == 0), meta={"exit_code": code, "cwd": cwd})


def tools() -> list[Tool]:
    return [
        Tool(
            name="shell",
            description=prompts.load("tools/shell"),
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
