"""Tests for custom provider setup (MiMo-Code-style) and /model picker fixes."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from neo.config import NeoConfig, save_global_provider
from neo.providers import ProviderConfigError, list_providers, resolve_provider
from neo.tui.app import (_CUSTOM_PROVIDER_ID_RE, _model_entries,
                         _valid_base_url)


def _cfg_with(**providers) -> NeoConfig:
    cfg = NeoConfig()
    cfg.providers.update(providers)
    return cfg


# ---------------------------------------------------------------------------
# list_providers(cfg) overlays
# ---------------------------------------------------------------------------

def test_list_providers_no_cfg_is_catalog():
    from neo.providers import PROVIDERS
    assert list_providers() == PROVIDERS
    assert list_providers(NeoConfig()) == PROVIDERS


def test_list_providers_catalog_override():
    cfg = _cfg_with(groq={"title": "Groq!", "models": ["m1", "m2"],
                          "default_model": "m1",
                          "model_names": {"m1": "Model One"}})
    spec = next(s for s in list_providers(cfg) if s.id == "groq")
    assert spec.title == "Groq!"
    assert spec.models == ("m1", "m2")
    assert spec.default_model == "m1"
    assert spec.model_names == {"m1": "Model One"}


def test_list_providers_synthesizes_unknown_id():
    cfg = _cfg_with(myproxy={"base_url": "https://x.test/v1",
                             "title": "My Proxy",
                             "models": ["a"], "default_model": "a"})
    spec = next(s for s in list_providers(cfg) if s.id == "myproxy")
    assert spec.title == "My Proxy"
    assert spec.protocol == "openai"  # default protocol
    assert spec.base_url == "https://x.test/v1"
    assert spec.models == ("a",)


def test_list_providers_skips_unusable_overrides():
    cfg = _cfg_with(nourl={"models": ["a"]},
                    badproto={"base_url": "https://x/v1", "protocol": "weird"})
    ids = {s.id for s in list_providers(cfg)}
    assert "nourl" not in ids
    assert "badproto" not in ids


# ---------------------------------------------------------------------------
# resolve_provider merges
# ---------------------------------------------------------------------------

def test_resolve_provider_unknown_merges_models_and_title():
    cfg = _cfg_with(myproxy={"base_url": "https://x.test/v1",
                             "title": "My Proxy",
                             "models": ["a", "b"],
                             "model_names": {"a": "Ay"},
                             "default_model": "a"})
    provider = resolve_provider("myproxy", cfg)
    assert provider.spec.title == "My Proxy"
    assert provider.spec.models == ("a", "b")
    assert provider.spec.model_names == {"a": "Ay"}
    assert provider.spec.default_model == "a"
    assert provider.base_url == "https://x.test/v1"


def test_resolve_provider_unknown_blank_base_url_raises():
    with pytest.raises(ProviderConfigError):
        resolve_provider("nosuch", _cfg_with(nosuch={"models": ["a"]}))


# ---------------------------------------------------------------------------
# save_global_provider
# ---------------------------------------------------------------------------

def test_save_global_provider_merges(monkeypatch, tmp_path):
    import neo.config as config_mod
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_DIR", tmp_path)
    existing = tmp_path / "neo.json"
    existing.write_text(json.dumps({"theme": "x", "providers": {
        "myproxy": {"base_url": "https://old/v1", "models": ["old"]}}}))

    save_global_provider("myproxy", {"base_url": "https://new/v1",
                                     "models": ["new"]})
    data = json.loads(existing.read_text())
    assert data["theme"] == "x"  # unrelated settings survive
    assert data["providers"]["myproxy"]["base_url"] == "https://new/v1"
    assert data["providers"]["myproxy"]["models"] == ["new"]

    save_global_provider("other", {"base_url": "https://o/v1"})
    data = json.loads(existing.read_text())
    assert data["providers"]["myproxy"]["base_url"] == "https://new/v1"
    assert data["providers"]["other"]["base_url"] == "https://o/v1"


def test_save_global_provider_creates_0600(monkeypatch, tmp_path):
    import os
    import stat
    import neo.config as config_mod
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_DIR", tmp_path)
    path = save_global_provider("p", {"base_url": "https://x/v1"})
    assert path.is_file()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ---------------------------------------------------------------------------
# validators
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,ok", [
    ("https://proxy.test/v1", True),
    ("http://localhost:8080", True),
    ("https://x", True),
    ("ftp://x/v1", False),
    ("proxy.test/v1", False),
    ("", False),
    ("https://", False),
])
def test_valid_base_url(value, ok):
    assert _valid_base_url(value) is ok


@pytest.mark.parametrize("value,ok", [
    ("custom", True), ("my-proxy", True), ("p_1", True), ("a", True),
    ("MyProxy", False), ("my proxy", False), ("-x", False), ("", False),
])
def test_custom_provider_id_re(value, ok):
    assert bool(_CUSTOM_PROVIDER_ID_RE.match(value)) is ok


# ---------------------------------------------------------------------------
# _model_entries
# ---------------------------------------------------------------------------

def test_model_entries_labels_and_index():
    cfg = _cfg_with(myproxy={"base_url": "https://x/v1", "models": ["a", "b"],
                             "model_names": {"a": "Ay"}})
    spec = next(s for s in list_providers(cfg) if s.id == "myproxy")
    labels, index = _model_entries([spec])
    assert labels == ["myproxy/a — Ay", "myproxy/b"]
    assert index == {"myproxy/a — Ay": "myproxy/a", "myproxy/b": "myproxy/b"}


def test_model_entries_falls_back_to_default_model():
    from neo.providers.base import ProviderSpec
    spec = ProviderSpec(id="p", title="P", protocol="openai", base_url="u",
                        env_vars=(), default_model="dflt")
    labels, index = _model_entries([spec])
    assert labels == ["p/dflt"]
    assert index["p/dflt"] == "p/dflt"


# ---------------------------------------------------------------------------
# api_key_env honored
# ---------------------------------------------------------------------------

def test_resolve_api_key_env_beats_store(monkeypatch, tmp_path):
    import neo.auth as auth_mod
    store = auth_mod.AuthStore(path=tmp_path / "auth.json")
    store.set("myproxy", "sk-stored")
    monkeypatch.setattr(auth_mod, "AuthStore", lambda *a, **k: store)
    monkeypatch.setenv("MYPROXY_KEY", "sk-env")
    cfg = _cfg_with(myproxy={"api_key_env": "MYPROXY_KEY"})
    assert auth_mod.resolve_api_key("myproxy", cfg) == "sk-env"


def test_resolve_api_key_env_missing_falls_through(monkeypatch, tmp_path):
    import neo.auth as auth_mod
    store = auth_mod.AuthStore(path=tmp_path / "auth.json")
    store.set("myproxy", "sk-stored")
    monkeypatch.setattr(auth_mod, "AuthStore", lambda *a, **k: store)
    monkeypatch.delenv("MYPROXY_KEY", raising=False)
    cfg = _cfg_with(myproxy={"api_key_env": "MYPROXY_KEY"})
    assert auth_mod.resolve_api_key("myproxy", cfg) == "sk-stored"


# ---------------------------------------------------------------------------
# TextModal (pilot)
# ---------------------------------------------------------------------------

async def _text_modal_result(default="", keys=()):
    from textual.app import App
    from textual.widgets import Input
    from neo.tui.dialogs import TextModal

    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    class _A(App):
        pass

    app = _A()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(TextModal("Title", "ph", default, fut))
        await pilot.pause(0.3)
        for key in keys:
            await pilot.press(key)
        if not keys:
            # type then submit via Enter
            await pilot.press(*"hello")
            await pilot.press("enter")
        result = await asyncio.wait_for(fut, timeout=5)
        return result


@pytest.mark.asyncio
async def test_text_modal_typing_resolves():
    assert await _text_modal_result() == "hello"


@pytest.mark.asyncio
async def test_text_modal_default_used_when_empty():
    assert await _text_modal_result(default="dflt", keys=["enter"]) == "dflt"


@pytest.mark.asyncio
async def test_text_modal_escape_resolves_none():
    assert await _text_modal_result(keys=["escape"]) is None


# ---------------------------------------------------------------------------
# _setup_custom_provider wizard (scripted prompts)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_custom_provider_wizard(monkeypatch, tmp_path):
    import neo.auth
    import neo.config as config_mod
    from neo.tui.app import NeoApp

    saved: dict = {}
    monkeypatch.setattr(config_mod, "save_global_provider",
                        lambda pid, definition: saved.setdefault(
                            pid, definition) or tmp_path / "neo.json")
    store = neo.auth.AuthStore(path=tmp_path / "auth.json")
    monkeypatch.setattr(neo.auth, "AuthStore", lambda *a, **k: store)

    secrets = iter(["{env:MYPROXY_KEY}"])

    async def fake_prompt_secret(title, placeholder):
        return next(secrets)

    calls = [0]

    async def fake_text(title, placeholder="", default=""):
        calls[0] += 1
        if calls[0] == 1:
            return "myproxy"
        if calls[0] == 2:
            return "My Proxy"
        if calls[0] == 3:
            return "https://proxy.test/v1"
        if calls[0] == 4:
            return "m1"          # model id
        if calls[0] == 5:
            return "Model One"   # display name
        return "n"               # no more models

    async def fake_rebuild(keep_session=False):
        calls.append("rebuilt")

    notices: list = []
    stub = SimpleNamespace(
        config=NeoConfig(),
        _prompt_text=fake_text,
        _prompt_secret=fake_prompt_secret,
        _rebuild_runtime=fake_rebuild,
        _notice=lambda *a, **k: notices.append(a),
    )
    await NeoApp._setup_custom_provider(stub)

    definition = saved["myproxy"]
    assert definition["title"] == "My Proxy"
    assert definition["base_url"] == "https://proxy.test/v1"
    assert definition["protocol"] == "openai"
    assert definition["models"] == ["m1"]
    assert definition["model_names"] == {"m1": "Model One"}
    assert definition["default_model"] == "m1"
    assert definition["api_key_env"] == "MYPROXY_KEY"
    assert store.get("myproxy") is None  # env ref: nothing stored
    assert stub.config.providers["myproxy"] == definition
    assert stub.config.model == "myproxy/m1"
    assert "rebuilt" in calls


@pytest.mark.asyncio
async def test_setup_custom_provider_literal_key_stored(monkeypatch, tmp_path):
    import neo.auth
    import neo.config as config_mod
    from neo.tui.app import NeoApp

    monkeypatch.setattr(config_mod, "save_global_provider",
                        lambda pid, definition: tmp_path / "neo.json")
    store = neo.auth.AuthStore(path=tmp_path / "auth.json")
    monkeypatch.setattr(neo.auth, "AuthStore", lambda *a, **k: store)

    async def fake_text(title, placeholder="", default=""):
        t = title.lower()
        if "base url" in t:
            return "https://x.test/v1"
        if "model id" in t:
            return "gemini"
        if "model display name" in t:
            return "Gemini"
        if t.startswith("custom provider — id"):
            return "custom"
        if "display name" in t:
            return "Custom"
        return "n"

    stub = SimpleNamespace(
        config=NeoConfig(),
        _prompt_text=fake_text,
        _prompt_secret=lambda *a, **k: asyncio.sleep(0, result="sk-live"),
        _rebuild_runtime=lambda keep_session=False: asyncio.sleep(0),
        _notice=lambda *a, **k: None,
    )
    await NeoApp._setup_custom_provider(stub)
    assert store.get("custom") == "sk-live"
    assert stub.config.model == "custom/gemini"
