"""Settings: typed schema + the `settings` table + APP_HOME resolution.

Every scalar setting in the product is declared here as a `Field`. The UI renders the
Settings screen from `schema()`; there is no setting without a UI (charter §2.2).
Row-shaped configuration (providers, presets, roles, tool policies, roots, protected
paths, MCP servers, connectors, schedules) lives in its own table and endpoints.

Usage:
    settings = Settings.load(conn)          # defaults overlaid with stored rows
    settings.get("conversation.step_budget_desk")
    settings.set(conn, "general.timezone", "Europe/Amsterdam")   # validates, persists
    Settings.schema()                        # list[dict] for GET /api/settings
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db


def app_home() -> Path:
    """APP_HOME: `$LOOM_HOME` if set, else `~/.loom`."""
    raw = os.environ.get("LOOM_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".loom"


def detect_timezone() -> str:
    """Best-effort IANA timezone name from the OS; falls back to UTC."""
    try:
        import zoneinfo  # noqa: F401

        tz = os.environ.get("TZ")
        if tz:
            return tz
        localtime = Path("/etc/localtime")
        if localtime.is_symlink():
            parts = localtime.resolve().parts
            if "zoneinfo" in parts:
                return "/".join(parts[parts.index("zoneinfo") + 1 :])
        if os.name == "nt":
            try:
                import subprocess

                out = subprocess.run(
                    ["tzutil", "/g"], capture_output=True, text=True, timeout=5
                ).stdout.strip()
                return _WINDOWS_TZ.get(out, "UTC") if out else "UTC"
            except Exception:
                return "UTC"
    except Exception:
        pass
    return "UTC"


# Small mapping of common Windows zone names → IANA. Extend as needed; unknown → UTC and
# the Setup screen asks the user to confirm anyway.
_WINDOWS_TZ = {
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York",
    "US Mountain Standard Time": "America/Phoenix",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Amsterdam",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "Russian Standard Time": "Europe/Moscow",
    "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "New Zealand Standard Time": "Pacific/Auckland",
    "UTC": "UTC",
}


@dataclass(frozen=True)
class Field:
    key: str  # dotted: "<group>.<name>"
    type: str  # str | int | float | bool | list | enum
    default: Any
    label: str
    help: str = ""
    ui: str = "text"  # text | number | toggle | select | list | path | time | secret
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()
    restart: bool = False  # true if a change needs a backend restart to apply

    @property
    def group(self) -> str:
        return self.key.split(".", 1)[0]

    def coerce(self, value: Any) -> Any:
        """Validate and coerce a raw (json-decoded) value. Raises ValueError."""
        t = self.type
        if t == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false", "1", "0"):
                return value.lower() in ("true", "1")
            raise ValueError(f"{self.key}: expected bool")
        if t == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError(f"{self.key}: expected int")
            v = int(value)
            if self.min is not None and v < self.min or self.max is not None and v > self.max:
                raise ValueError(f"{self.key}: out of range [{self.min}, {self.max}]")
            return v
        if t == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError(f"{self.key}: expected number")
            v = float(value)
            if self.min is not None and v < self.min or self.max is not None and v > self.max:
                raise ValueError(f"{self.key}: out of range [{self.min}, {self.max}]")
            return v
        if t == "enum":
            if value not in self.choices:
                raise ValueError(f"{self.key}: must be one of {list(self.choices)}")
            return value
        if t == "list":
            if isinstance(value, str):
                value = [s for s in (x.strip() for x in value.replace("\n", ",").split(",")) if s]
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError(f"{self.key}: expected list of strings")
            return value
        if t == "str":
            if value is None:
                return ""
            if not isinstance(value, str):
                raise ValueError(f"{self.key}: expected string")
            return value
        raise ValueError(f"{self.key}: unknown type {t}")

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "group": self.group,
            "type": self.type,
            "default": self.default,
            "label": self.label,
            "help": self.help,
            "ui": self.ui,
            "min": self.min,
            "max": self.max,
            "choices": list(self.choices),
            "restart": self.restart,
        }


GROUPS: dict[str, str] = {
    "general": "General",
    "models": "Models",
    "conversation": "Conversation",
    "knowledge": "Knowledge",
    "permissions": "Permissions",
    "tools": "Tools",
    "triggers": "Triggers",
    "system": "System",
}

DEFAULT_SHELL_READONLY = [
    "ls", "dir", "cat", "type", "head", "tail", "less", "more", "wc", "grep", "rg",
    "find", "where", "which", "echo", "pwd", "printenv", "env", "tree", "stat", "file",
    "git status", "git log", "git diff", "git show", "git branch", "git remote",
    "python --version", "node --version", "npm ls", "uv pip list", "pip list", "sleep",
]

FIELDS: list[Field] = [
    # General
    Field("general.display_name", "str", "", "Your name", "How Loom addresses you.", "text"),
    Field("general.timezone", "str", "UTC", "Timezone", "IANA name, e.g. America/Denver.", "text"),
    Field("general.bind_host", "str", "127.0.0.1", "Bind address", "Use 0.0.0.0 to serve on the LAN (set a token).", "text", restart=True),
    Field("general.port", "int", 8500, "Port", "", "number", min=1024, max=65535, restart=True),
    Field("general.lan_token", "str", "", "LAN bearer token", "Required on every request when bound beyond loopback. Empty = loopback only.", "secret"),
    Field("general.theme", "enum", "system", "Theme", "Light or dark; system follows the OS.", "select", choices=("system", "light", "dark")),
    Field("general.palette", "enum", "default", "Palette", "Colour palette; the theme picks its light or dark side.", "select", choices=("default", "slate", "warm", "forest", "nord", "mono", "grape", "ocean", "ember", "moss")),
    Field("general.open_browser", "bool", True, "Open browser on start", "", "toggle"),
    # Models (rows live in providers/presets/model_roles; scalars here)
    Field("models.fallback_role", "enum", "", "Fallback role", "Role to try when a call fails fatally (auth, model missing).", "select", choices=("", "desk", "worker", "maintenance", "utility")),
    Field("models.background_max_tokens", "int", 2048, "Background max tokens", "Cap on generation for background (priority 1–2) calls so the desk waits at most one bounded generation.", "number", min=64, max=65536),
    Field("models.embeddings_enabled", "bool", True, "Embeddings", "Off until an embedding preset exists; retrieval falls back to alias + FTS.", "toggle"),
    # Conversation
    Field("conversation.step_budget_desk", "int", 25, "Desk step budget", "Tool steps per desk turn.", "number", min=1, max=500),
    Field("conversation.step_budget_thread", "int", 60, "Thread step budget", "Tool steps per thread turn; threads see the remaining count.", "number", min=1, max=1000),
    Field("conversation.compact_threshold", "float", 0.75, "Compaction threshold", "Fraction of the effective context at which compaction runs.", "number", min=0.3, max=0.95),
    Field("conversation.forced_compactions_per_turn", "int", 2, "Forced compactions per turn", "", "number", min=0, max=10),
    Field("conversation.model_error_retries", "int", 1, "Model error retries", "Retries for transient model errors.", "number", min=0, max=5),
    Field("conversation.reasoning_replay", "bool", True, "Replay reasoning within a turn", "Feed each step's reasoning into later steps of the same turn.", "toggle"),
    Field("conversation.reasoning_replay_max_chars", "int", 4000, "Reasoning replay cap (chars)", "Longer reasoning replays its last N characters, the part that led to the step's calls.", "number", min=0, max=100000),
    Field("conversation.tool_output_cap", "int", 8000, "Tool output cap (chars)", "Longer outputs are spilled to artifacts.", "number", min=500, max=200000),
    Field("conversation.artifact_head_chars", "int", 2000, "Artifact preview head (chars)", "", "number", min=0, max=50000),
    Field("conversation.artifact_tail_chars", "int", 1000, "Artifact preview tail (chars)", "", "number", min=0, max=50000),
    Field("conversation.artifact_retention_days", "int", 90, "Artifact retention (days)", "For archived conversations only.", "number", min=1, max=3650),
    Field("conversation.image_max_mb", "int", 10, "Image size cap (MB)", "Uploads, fs_read images and screenshots above this are refused.", "number", min=1, max=100),
    Field("conversation.thread_archive_days", "int", 14, "Archive finished threads after (days)", "", "number", min=1, max=3650),
    Field("conversation.max_thread_depth", "int", 3, "Max thread depth", "", "number", min=1, max=10),
    Field("conversation.tool_expose_max", "int", 48, "Max tools exposed per turn", "Above this, non-native tools load via tool_search.", "number", min=8, max=512),
    Field("conversation.pinned_note_chars", "int", 6000, "Pinned note cap (chars)", "Each note a conversation pins is shown up to this many characters per turn; the rest is a knowledge_read away.", "number", min=500, max=50000),
    Field("conversation.tool_timeout_shell_s", "int", 120, "Shell timeout (s)", "", "number", min=1, max=3600),
    Field("conversation.tool_timeout_http_s", "int", 60, "HTTP timeout (s)", "", "number", min=1, max=3600),
    Field("conversation.tool_timeout_mcp_s", "int", 300, "MCP tool timeout (s)", "", "number", min=1, max=3600),
    Field("conversation.tool_timeout_max_s", "int", 900, "Tool timeout ceiling (s)", "A call's own timeout_s argument (shell, http) is honoured up to this.", "number", min=1, max=7200),
    Field("conversation.suspend_drift_s", "int", 60, "Suspend detection (s)", "A failed model call whose wall clock ran this far ahead of the monotonic clock spanned a standby; it is retried without counting as an error.", "number", min=5, max=3600),
    Field("conversation.suspend_retries", "int", 2, "Suspend retries", "Free retries per turn after a detected standby.", "number", min=0, max=10),
    Field("conversation.now_board_max_threads", "int", 10, "Now board: threads shown", "", "number", min=1, max=50),
    Field("conversation.shutdown_grace_s", "int", 10, "Shutdown grace (s)", "Time in-flight tool calls get to finish.", "number", min=0, max=120),
    # Knowledge
    Field("knowledge.preload_budget_tokens", "int", 3000, "Pre-load budget (tokens)", "Per turn, for resolved entities and their first-hop links.", "number", min=200, max=50000),
    Field("knowledge.preload_max_entities", "int", 3, "Pre-load max entities", "", "number", min=1, max=20),
    Field("knowledge.retrieval_threshold", "float", 0.55, "Retrieval cosine threshold", "", "number", min=0.0, max=1.0),
    Field("knowledge.core_block_chars", "int", 1500, "Core block cap (chars)", "Each core/ note is truncated to this with a visible marker.", "number", min=200, max=20000),
    Field("knowledge.vault_poll_seconds", "float", 2.0, "Vault poll interval (s)", "", "number", min=0.5, max=60),
    Field("knowledge.maintenance_idle_seconds", "int", 120, "Maintenance after idle (s)", "", "number", min=10, max=86400),
    Field("knowledge.maintenance_nightly_at", "str", "03:00", "Maintenance nightly at", "HH:MM local time.", "time"),
    Field("knowledge.maintenance_enabled", "bool", True, "Maintenance enabled", "", "toggle"),
    Field("knowledge.curation_similarity", "float", 0.92, "Curation similarity threshold", "Cosine above which two notes are merge candidates.", "number", min=0.5, max=1.0),
    Field("knowledge.auto_commit_external_edits", "bool", True, "Auto-commit external edits", "Commit Obsidian/user edits to the vault repo on the next maintenance pass.", "toggle"),
    # Permissions (per-tool modes, roots, protected paths are rows)
    Field("permissions.shell_readonly_commands", "list", DEFAULT_SHELL_READONLY, "Shell read-only commands", "Pipeline stages whose first token is here run without a classifier call. Never denies.", "list"),
    Field("permissions.shell_flavour", "enum", "auto", "Shell flavour", "auto = Git Bash if present, else cmd (Windows) or sh.", "select", choices=("auto", "gitbash", "cmd", "powershell", "sh"), restart=True),
    Field("permissions.classifier_role", "enum", "utility", "Classifier role", "Model role used for classify decisions; utility falls back to the conversation's role.", "select", choices=("utility", "desk", "worker", "maintenance")),
    Field("permissions.desk_auto_approve", "bool", False, "Desk auto-approve", "The desk runs every call without asking or classifying. Protected paths still ask; threads are unaffected.", "toggle"),
    Field("permissions.classifier_timeout_s", "int", 45, "Classifier timeout (s)", "A verdict that takes longer than this becomes an ask (one retry for a busy provider).", "number", min=5, max=600),
    # Tools
    Field("tools.web_search_provider", "enum", "", "Web search provider", "Which index the web_search tool queries. SearXNG is self-hosted and needs no key; Brave, Exa and Tavily each need an API key. While this is empty the tool answers 'not configured' and says what to set.", "select", choices=("", "searxng", "brave", "exa", "tavily")),
    Field("tools.web_search_url", "str", "", "SearXNG URL", "SearXNG only: the base URL of the instance, e.g. http://127.0.0.1:8888. Its JSON API must be enabled.", "text"),
    Field("tools.web_search_secret", "str", "web_search", "API key secret name", "Brave, Exa and Tavily only: the name of the stored secret holding the key — not the key itself. Paste the key into Integrations → Secrets under this name, or ask Loom to store it.", "text"),
    Field("tools.web_search_results", "int", 5, "Results per search", "How many results web_search returns when it is not asked for a number.", "number", min=1, max=20),
    # Triggers
    Field("triggers.policy_thread_event", "enum", "interrupt", "Thread events", "", "select", choices=("interrupt", "queue")),
    Field("triggers.policy_schedule", "enum", "queue", "Schedule firings", "", "select", choices=("interrupt", "queue")),
    Field("triggers.policy_connector", "enum", "queue", "Connector events", "", "select", choices=("interrupt", "queue")),
    Field("triggers.max_active_schedules", "int", 25, "Max active schedules", "", "number", min=0, max=1000),
    Field("triggers.min_interval_minutes", "int", 5, "Min schedule interval (minutes)", "", "number", min=1, max=1440),
    Field("triggers.schedule_catchup_hours", "int", 6, "Catch-up window (hours)", "Missed firings inside this window fire once at startup.", "number", min=0, max=168),
    Field("triggers.scheduler_tick_seconds", "int", 30, "Scheduler tick (s)", "", "number", min=5, max=600),
    Field("triggers.notification_targets", "list", ["web"], "Notification targets", "Surfaces that receive interrupt notifications (web, plus connector surface names).", "list"),
    # System
    Field("system.log_level", "enum", "INFO", "Log level", "", "select", choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    Field("system.auto_rollback_crashes", "int", 5, "Auto-rollback after crashes", "Crashes within the window on a fresh release trigger rollback.", "number", min=1, max=100),
    Field("system.auto_rollback_window_minutes", "int", 10, "Auto-rollback window (minutes)", "", "number", min=1, max=1440),
    Field("system.promotion_watchdog_seconds", "int", 120, "Post-promotion watchdog (s)", "", "number", min=10, max=3600),
    Field("system.promotion_test_timeout_s", "int", 1800, "Promotion test-suite timeout (s)", "The supervisor runs the full suite inside the new release before the swap; a run slower than this fails the promotion.", "number", min=60, max=7200),
    Field("system.snapshot_retention", "int", 10, "Snapshots kept", "", "number", min=1, max=100),
]

FIELD_MAP: dict[str, Field] = {f.key: f for f in FIELDS}


@dataclass
class Settings:
    values: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def schema() -> list[dict[str, Any]]:
        return [f.to_json() for f in FIELDS]

    @staticmethod
    def groups() -> dict[str, str]:
        return dict(GROUPS)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "Settings":
        values = {f.key: f.default for f in FIELDS}
        for row in conn.execute("SELECT key, value FROM settings"):
            f = FIELD_MAP.get(row["key"])
            if f is None:
                continue  # unknown key: ignore, never crash
            try:
                values[f.key] = f.coerce(db.loads(row["value"]))
            except ValueError:
                values[f.key] = f.default
        return cls(values)

    def get(self, key: str) -> Any:
        return self.values[key]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def set(self, conn: sqlite3.Connection, key: str, value: Any) -> Any:
        f = FIELD_MAP.get(key)
        if f is None:
            raise KeyError(key)
        coerced = f.coerce(value)
        conn.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, db.dumps(coerced), db.now_iso()),
        )
        self.values[key] = coerced
        return coerced

    def update(self, conn: sqlite3.Connection, patch: dict[str, Any]) -> list[str]:
        """Validate everything first, then write; returns the keys changed."""
        coerced = {}
        for key, value in patch.items():
            f = FIELD_MAP.get(key)
            if f is None:
                raise KeyError(key)
            coerced[key] = f.coerce(value)
        with db.transaction(conn):
            for key, value in coerced.items():
                self.set(conn, key, value)
        return list(coerced)

    def to_json(self) -> dict[str, Any]:
        return dict(self.values)
