"""The shell's environment is the machine's, not the virtual environment Loom runs in
(2026-11-09): inherited from `uv run`, every command's `python` was Loom's `.venv`."""

from __future__ import annotations

import os
import sys

from loom.tools.native import shell

SCRIPTS = "Scripts" if os.name == "nt" else "bin"


def test_child_env_drops_the_backend_venv(monkeypatch, tmp_path):
    venv = tmp_path / "venv"
    other = tmp_path / "tools"
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("VIRTUAL_ENV_PROMPT", "loom")
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv / SCRIPTS), str(other), str(venv / SCRIPTS) + os.sep]))
    env = shell.child_env()
    assert "VIRTUAL_ENV" not in env and "VIRTUAL_ENV_PROMPT" not in env
    path_key = next(k for k in env if k.upper() == "PATH")
    assert env[path_key].split(os.pathsep) == [str(other)]
    # the backend's own environment is untouched
    assert os.environ["VIRTUAL_ENV"] == str(venv)


def test_child_env_drops_the_running_interpreters_venv_without_virtual_env(monkeypatch, tmp_path):
    venv = tmp_path / "venv"
    other = tmp_path / "tools"
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv / SCRIPTS), str(other)]))
    env = shell.child_env()
    path_key = next(k for k in env if k.upper() == "PATH")
    assert env[path_key].split(os.pathsep) == [str(other)]


def test_child_env_outside_a_venv_keeps_path(monkeypatch, tmp_path):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    env = shell.child_env()
    path_key = next(k for k in env if k.upper() == "PATH")
    assert env[path_key] == os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
