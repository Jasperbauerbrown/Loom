"""Secrets: both backends, the fallback, and the templating both ways (03 §4.5)."""

from __future__ import annotations

import json

import pytest

from loom import secrets as secrets_mod
from loom.secrets import SecretMissing, Store


class FakeKeyring:
    """Dict-backed keyring double."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        self.data[(service, name)] = value

    def delete_password(self, service, name):
        self.data.pop((service, name), None)


class NoKeyring:
    """A keyring with no backend: every call raises, as on headless Linux."""

    class NoKeyringError(Exception):
        pass

    def get_password(self, service, name):
        raise self.NoKeyringError("no recommended backend")

    def set_password(self, service, name, value):
        raise self.NoKeyringError("no recommended backend")

    def delete_password(self, service, name):
        raise self.NoKeyringError("no recommended backend")


@pytest.fixture
def keyring_store(app, monkeypatch):
    monkeypatch.setattr(secrets_mod, "_keyring_module", FakeKeyring())
    return Store(app)


@pytest.fixture
def file_store(app, monkeypatch):
    monkeypatch.setattr(secrets_mod, "_keyring_module", NoKeyring())
    return Store(app)


def test_install_attaches_a_store(app):
    assert isinstance(app.secrets, Store)


def test_keyring_backend_round_trip(keyring_store):
    store = keyring_store
    assert store.backend == "keyring"
    assert store.warning is None
    assert store.list_names() == []

    store.set("GITHUB_TOKEN", "ghp_secret_value")
    assert store.get("GITHUB_TOKEN") == "ghp_secret_value"
    assert store.list_names() == ["GITHUB_TOKEN"]
    # keyring cannot enumerate; the names live in the index file
    assert json.loads(store.index_path.read_text()) == ["GITHUB_TOKEN"]
    assert not store.file_path.exists()

    store.set("GITHUB_TOKEN", "replaced")
    assert store.get("GITHUB_TOKEN") == "replaced"
    assert store.list_names() == ["GITHUB_TOKEN"]

    assert store.delete("GITHUB_TOKEN") is True
    assert store.get("GITHUB_TOKEN") is None
    assert store.list_names() == []
    assert store.delete("GITHUB_TOKEN") is False


def test_file_fallback_when_no_keyring_backend(file_store):
    store = file_store
    assert store.backend == "file"
    assert "unencrypted" in (store.warning or "")
    assert str(store.file_path) in (store.warning or "")

    store.set("API_KEY", "abcd1234")
    assert store.get("API_KEY") == "abcd1234"
    assert store.list_names() == ["API_KEY"]
    assert json.loads(store.file_path.read_text()) == {"API_KEY": "abcd1234"}
    assert store.to_json()["backend"] == "file"

    store.delete("API_KEY")
    assert store.list_names() == []


def test_backend_failure_after_selection_degrades_rather_than_raising(app, monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(secrets_mod, "_keyring_module", kr)
    store = Store(app)
    assert store.backend == "keyring"

    def boom(*a, **k):
        raise RuntimeError("credential manager went away")

    monkeypatch.setattr(kr, "set_password", boom)
    store.set("TOKEN", "value1234")  # must not raise
    assert store.backend == "file"
    assert store.get("TOKEN") == "value1234"


def test_missing_keyring_module_falls_back(app, monkeypatch):
    monkeypatch.setattr(secrets_mod, "_keyring_module", None)
    store = Store(app)
    assert store.backend == "file"
    store.set("X", "yyyy")
    assert store.get("X") == "yyyy"


def test_invalid_name_is_rejected(keyring_store):
    with pytest.raises(ValueError):
        keyring_store.set("bad name!", "value")


def test_substitute_replaces_in_strings_lists_and_dicts(keyring_store):
    store = keyring_store
    store.set("TOKEN", "s3cr3t-value")
    args = {
        "url": "https://api.example.com",
        "headers": {"Authorization": "Bearer {{secret:TOKEN}}"},
        "argv": ["--token", "{{ secret:TOKEN }}"],
        "n": 3,
        "flag": True,
    }
    out = store.substitute(args)
    assert out["headers"]["Authorization"] == "Bearer s3cr3t-value"
    assert out["argv"] == ["--token", "s3cr3t-value"]
    assert out["n"] == 3 and out["flag"] is True
    # the original is untouched: the transcript keeps the placeholder
    assert args["headers"]["Authorization"] == "Bearer {{secret:TOKEN}}"


def test_substitute_unknown_name_raises_secret_missing(keyring_store):
    with pytest.raises(SecretMissing) as excinfo:
        keyring_store.substitute({"h": "{{secret:NOPE}}"})
    assert excinfo.value.name == "NOPE"


def test_has_placeholders(keyring_store):
    assert keyring_store.has_placeholders({"a": ["{{secret:X}}"]}) is True
    assert keyring_store.has_placeholders({"a": ["plain"]}) is False


def test_redact_puts_placeholders_back_longest_first(keyring_store):
    store = keyring_store
    store.set("TOKEN", "abcd1234")
    store.set("LONGER", "abcd1234efgh")
    store.set("TINY", "ab")  # shorter than 4 chars: never redacted
    text = "used abcd1234efgh and abcd1234 with ab"
    out = store.redact(text)
    assert out == "used {{secret:LONGER}} and {{secret:TOKEN}} with ab"


def test_redact_is_safe_on_empty_and_clean_text(keyring_store):
    keyring_store.set("TOKEN", "abcd1234")
    assert keyring_store.redact("") == ""
    assert keyring_store.redact("nothing here") == "nothing here"
