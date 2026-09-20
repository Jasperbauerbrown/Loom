"""Loader for model-facing prose. All of it lives in prompts/*.md (CLAUDE.md hard rule);
nothing model-facing is hardcoded in Python.

    from loom import prompts
    prompts.load("system")             # prompts/system.md
    prompts.load("tools/fs_read")      # prompts/tools/fs_read.md
    prompts.load("templates/project")  # prompts/templates/project.md

Missing prompt files are a startup error, not a silent empty string.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def prompts_dir() -> Path:
    override = os.environ.get("LOOM_PROMPTS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "prompts"


@lru_cache(maxsize=None)
def load(name: str) -> str:
    path = prompts_dir() / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt file missing: {path}")
    return path.read_text(encoding="utf-8").strip()


def exists(name: str) -> bool:
    return (prompts_dir() / f"{name}.md").exists()


def clear_cache() -> None:
    load.cache_clear()
