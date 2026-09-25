"""Tests for neo.auth (AuthStore + resolve_api_key)."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from neo.auth import AuthStore, resolve_api_key


@pytest.fixture
def store(tmp_path):
    return AuthStore(tmp_path / "auth.json")


def test_round_trip(store):
    store.set("openai", "sk-test-123")
    assert store.get("openai") == "sk-test-123"
    assert store.get("anthropic") is None


def test_file_mode_0600(store, tmp_path):
    store.set("openai", "k")
    mode = stat.S_IMODE(os.stat(tmp_path / "auth.json").st_mode)
    assert mode == 0o600


def test_rechmod_existing_file(store, tmp_path):
    p = tmp_path / "auth.json"
    p.write_text("{}", encoding="utf-8")
    os.chmod(p, 0o644)
    store.set("openai", "k2")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600
    assert store.get("openai") == "k2"


def test_delete(store):
    store.set("openai", "k")
    store.delete("openai")
    assert store.get("openai") is None
    store.delete("missing")  # no error


def test_providers(store):
    store.set("a", "1")
    store.set("b", "2")
    assert store.providers() == {"a": "1", "b": "2"}


def test_corrupt_file_is_empty(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text("not json{", encoding="utf-8")
    assert AuthStore(p).get("openai") is None


class _Cfg:
    def __init__(self, providers):
        self.providers = providers


def _clean_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                 "GOOGLE_API_KEY", "FOO_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_resolution_env_wins(store, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    store.set("openai", "file-key")
    cfg = _Cfg({"openai": {"api_key": "cfg-key"}})
    assert resolve_api_key("openai", cfg, store) == "env-key"


def test_resolution_file_over_config(store, monkeypatch):
    _clean_env(monkeypatch)
    store.set("openai", "file-key")
    cfg = _Cfg({"openai": {"api_key": "cfg-key"}})
    assert resolve_api_key("openai", cfg, store) == "file-key"


def test_resolution_config_fallback(store, monkeypatch):
    _clean_env(monkeypatch)
    cfg = _Cfg({"openai": {"api_key": "cfg-key"}})
    assert resolve_api_key("openai", cfg, store) == "cfg-key"


def test_resolution_nothing(store, monkeypatch):
    _clean_env(monkeypatch)
    assert resolve_api_key("openai", _Cfg({}), store) is None


def test_resolution_env_map_variants(store, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "go-key")
    monkeypatch.setenv("FOO_API_KEY", "foo-key")
    cfg = _Cfg({})
    assert resolve_api_key("anthropic", cfg, store) == "a-key"
    assert resolve_api_key("google", cfg, store) == "g-key"  # GEMINI first
    monkeypatch.delenv("GEMINI_API_KEY")
    assert resolve_api_key("google", cfg, store) == "go-key"
    assert resolve_api_key("foo", cfg, store) == "foo-key"  # generic fallback
