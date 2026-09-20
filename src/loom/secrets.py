"""Secret storage and `{{secret:NAME}}` templating (03 §4.5).

One store, two backends, one behaviour per failure class:

- `keyring` (Windows Credential Manager, macOS Keychain, Secret Service) is preferred.
  Keyring cannot enumerate, so the *names* live in `APP_HOME/secrets.index.json`.
- If no keyring backend exists (headless Linux) or any keyring call fails, the store
  falls back — permanently, for this process — to `APP_HOME/secrets.json` with 0600
  permissions and a non-empty `warning` the Settings screen shows. Documented deviation
  from the charter: the fallback is not encrypted (no stdlib AEAD; `cryptography` is
  refused under the dependency rule).

A secrets failure never crashes a caller: `get` returns None, `list_names` returns what
it can, and a failing backend degrades to the file.

Values never reach the model. Tool arguments and MCP env values may carry
`{{secret:NAME}}`; the dispatcher calls `substitute` immediately before execution and
`redact` on the output, so the persisted transcript keeps the placeholder.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SERVICE = "loom"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*secret:([A-Za-z0-9_.-]+)\s*\}\}")
MIN_REDACT_LEN = 4

try:  # pragma: no cover - import guard
    import keyring as _keyring_module
except Exception:  # pragma: no cover - keyring is a declared dependency; be safe anyway
    _keyring_module = None  # type: ignore[assignment]


class SecretMissing(KeyError):
    """A `{{secret:NAME}}` placeholder named a secret that is not stored."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"no secret named {self.name!r}"


class Store:
    """`app.secrets`. Backend chosen lazily at first use, then fixed for the process."""

    def __init__(self, app: Any):
        self.app = app
        self.home: Path = Path(app.home)
        self._backend: str | None = None
        self._warning: str | None = None

    # -- paths ------------------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.home / "secrets.index.json"

    @property
    def file_path(self) -> Path:
        return self.home / "secrets.json"

    # -- backend ----------------------------------------------------------------
    @property
    def backend(self) -> str:
        self._ensure_backend()
        return self._backend or "file"

    @property
    def warning(self) -> str | None:
        self._ensure_backend()
        return self._warning

    def _ensure_backend(self) -> None:
        if self._backend is not None:
            return
        kr = _keyring_module
        if kr is None:
            self._fallback("no keyring module")
            return
        try:
            kr.get_password(SERVICE, "__loom_probe__")
        except Exception as exc:
            self._fallback(f"{type(exc).__name__}: {exc}")
            return
        self._backend = "keyring"
        self._warning = None

    def _fallback(self, reason: str) -> None:
        if self._backend != "file":
            log.warning("secrets: using the unencrypted file backend (%s)", reason)
        self._backend = "file"
        self._warning = f"secrets are stored unencrypted in {self.file_path}"

    # -- file backend -----------------------------------------------------------
    def _read_file(self) -> dict[str, str]:
        return _read_json(self.file_path, {})

    def _write_file(self, data: dict[str, str]) -> None:
        _write_json(self.file_path, data, private=True)

    # -- name index (keyring backend) -------------------------------------------
    def _read_index(self) -> list[str]:
        names = _read_json(self.index_path, [])
        return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []

    def _write_index(self, names: list[str]) -> None:
        _write_json(self.index_path, sorted(set(names)), private=False)

    # -- API --------------------------------------------------------------------
    def get(self, name: str) -> str | None:
        self._ensure_backend()
        if self._backend == "keyring":
            try:
                return _keyring_module.get_password(SERVICE, name)  # type: ignore[union-attr]
            except Exception as exc:
                self._fallback(f"get failed: {exc}")
        return self._read_file().get(name)

    def set(self, name: str, value: str) -> None:
        if not NAME_RE.match(name or ""):
            raise ValueError(f"invalid secret name {name!r}: use letters, digits, _ . -")
        if not isinstance(value, str):
            raise ValueError("secret value must be a string")
        self._ensure_backend()
        if self._backend == "keyring":
            try:
                _keyring_module.set_password(SERVICE, name, value)  # type: ignore[union-attr]
                self._write_index(self._read_index() + [name])
                return
            except Exception as exc:
                self._fallback(f"set failed: {exc}")
        data = self._read_file()
        data[name] = value
        self._write_file(data)

    def delete(self, name: str) -> bool:
        """True if the secret existed. Missing is not an error."""
        self._ensure_backend()
        existed = False
        if self._backend == "keyring":
            try:
                existed = _keyring_module.get_password(SERVICE, name) is not None  # type: ignore[union-attr]
                if existed:
                    _keyring_module.delete_password(SERVICE, name)  # type: ignore[union-attr]
                names = [n for n in self._read_index() if n != name]
                self._write_index(names)
                return existed
            except Exception as exc:
                self._fallback(f"delete failed: {exc}")
        data = self._read_file()
        existed = name in data
        if existed:
            data.pop(name, None)
            self._write_file(data)
        return existed

    def list_names(self) -> list[str]:
        self._ensure_backend()
        if self._backend == "keyring":
            return sorted(self._read_index())
        return sorted(self._read_file())

    def to_json(self) -> dict[str, Any]:
        return {"names": self.list_names(), "backend": self.backend, "warning": self.warning}

    # -- templating -------------------------------------------------------------
    def has_placeholders(self, obj: Any) -> bool:
        if isinstance(obj, str):
            return PLACEHOLDER_RE.search(obj) is not None
        if isinstance(obj, dict):
            return any(self.has_placeholders(v) for v in obj.values())
        if isinstance(obj, (list, tuple)):
            return any(self.has_placeholders(v) for v in obj)
        return False

    def substitute(self, obj: Any) -> Any:
        """Deep copy with every `{{secret:NAME}}` replaced. Unknown name → SecretMissing."""
        if isinstance(obj, str):
            def repl(m: re.Match[str]) -> str:
                name = m.group(1)
                value = self.get(name)
                if value is None:
                    raise SecretMissing(name)
                return value

            return PLACEHOLDER_RE.sub(repl, obj)
        if isinstance(obj, dict):
            return {k: self.substitute(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.substitute(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.substitute(v) for v in obj)
        return copy.deepcopy(obj)

    def redact(self, text: str) -> str:
        """Put the placeholders back: every stored value of length >= 4 found in `text`
        becomes `{{secret:NAME}}`. Longest values first so overlaps redact fully."""
        if not text:
            return text
        pairs = []
        for name in self.list_names():
            value = self.get(name)
            if value and len(value) >= MIN_REDACT_LEN:
                pairs.append((value, name))
        for value, name in sorted(pairs, key=lambda p: -len(p[0])):
            if value in text:
                text = text.replace(value, "{{secret:%s}}" % name)
        return text


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        log.warning("secrets: unreadable %s (%s); treating as empty", path, exc)
        return default


def _write_json(path: Path, data: Any, private: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    if private:
        _restrict(tmp)
    os.replace(tmp, path)
    if private:
        _restrict(path)


def _restrict(path: Path) -> None:
    """0600 where it means something. On Windows `os.chmod` only toggles the read-only
    bit, so this is best effort and the Settings warning says the file is unencrypted."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # pragma: no cover - platform dependent
        log.debug("secrets: chmod %s failed (%s)", path, exc)


def install(app: Any) -> None:
    app.secrets = Store(app)
