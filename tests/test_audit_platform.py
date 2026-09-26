"""Regression tests for the OpenCode-parity platform/config/provider/MCP audit.

Covers: MCP lifecycle edge cases, deep config merging + field validation,
auth resolution order, provider resolution, $n template expansion, the
three streaming protocols, headless CLI behavior, LSP detection order,
and formatter failure handling.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from neo.agent.discovery import expand_command_template
from neo.auth import AuthStore, _env_names, resolve_api_key
from neo.config import NeoConfig, discover_config
from neo.format.registry import Formatter, format_file
from neo.lsp.servers import detect_server
from neo.mcp.manager import MCPManager
from neo.plugins.hooks import HOOK_POINTS
from neo.providers import (
    AnthropicProvider,
    GoogleProvider,
    OpenAICompatProvider,
    ProviderConfigError,
    resolve_provider,
)
from neo.providers.anthropic import AnthropicProvider as _AP
from neo.providers.base import (
    ProviderSpec,
    StreamEnd,
    StreamError,
    UsageTick,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class FakeCfg:
    def __init__(self, providers: dict[str, dict[str, Any]] | None = None):
        self.providers = providers or {}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _stream_kwargs(**kw) -> dict[str, Any]:
    d = {
        "model": "test-model",
        "system": "sys",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [],
        "max_tokens": 64,
    }
    d.update(kw)
    return d


def _spec(pid: str, protocol: str, base_url: str = "https://example.com",
          env_vars: tuple[str, ...] = ()) -> ProviderSpec:
    return ProviderSpec(
        id=pid, title=pid, protocol=protocol, base_url=base_url,
        env_vars=env_vars, default_model="m",
    )


def _run(coro):
    return asyncio.run(coro)


async def _collect_events(provider, **kwargs) -> list:
    return [e async for e in provider.stream(**kwargs)]


def _stream_events(provider, **kwargs) -> list:
    return _run(_collect_events(provider, **kwargs))


# --------------------------------------------------------------------------
# MCP manager lifecycle
# --------------------------------------------------------------------------


class _FakeMCPClient:
    """Stand-in for MCPClient: controllable connect/list/close behavior."""

    def __init__(self, connect_exc: Exception | None = None):
        self.connect_exc = connect_exc
        self.closed = 0

    async def connect(self):
        if self.connect_exc is not None:
            raise self.connect_exc

    async def close(self):
        self.closed += 1

    async def list_tools(self):
        return []

    async def list_prompts(self):
        return []


def test_mcp_construction_failure_records_error_without_nameerror(monkeypatch):
    """A non-MCPError from client_from_config must not raise NameError or
    close a stale client from a previous loop iteration."""
    import neo.mcp.manager as mgr_mod

    stale = _FakeMCPClient()

    def boom(name, scfg, workdir):
        raise ValueError("bad config shape")

    monkeypatch.setattr(mgr_mod, "client_from_config", boom)
    mgr = MCPManager({"servers": {"bad": {"command": ["x"]}}}, workdir="/tmp")
    mgr.clients["stale"] = stale  # pretend a previous iteration stored this
    _run(mgr.start())
    assert "bad" in mgr.errors
    assert "unexpected error" in mgr.errors["bad"]
    assert stale.closed == 0  # the stale client was never touched


def test_mcp_connect_failure_closes_transport(monkeypatch):
    """An MCPError from connect() still tears down the spawned transport."""
    import neo.mcp.manager as mgr_mod
    from neo.mcp.client import MCPError

    fake = _FakeMCPClient(connect_exc=MCPError("handshake blew up"))
    monkeypatch.setattr(mgr_mod, "client_from_config",
                        lambda name, scfg, workdir: fake)
    mgr = MCPManager({"servers": {"bad": {"command": ["x"]}}}, workdir="/tmp")
    _run(mgr.start())
    assert "bad" in mgr.errors
    assert "bad" not in mgr.clients
    assert fake.closed == 1


def test_mcp_prompt_name_collisions_get_suffixes():
    mgr = MCPManager({"servers": {}}, workdir="/tmp")
    mgr._register_prompts("srv", None, [
        {"name": "a-b", "description": "first"},
        {"name": "a_b", "description": "second"},
        {"name": "a b", "description": "third"},
    ])
    names = [p["name"] for p in mgr.prompt_commands()]
    assert names == ["srv_a_b", "srv_a_b_2", "srv_a_b_3"]


# --------------------------------------------------------------------------
# Config: deep merge + validation
# --------------------------------------------------------------------------


def test_discover_config_deep_merges_layers(tmp_path, monkeypatch):
    import neo.config as cfg_mod

    home = tmp_path / "home"
    monkeypatch.setattr(cfg_mod, "GLOBAL_CONFIG_DIR", home / ".config" / "neo")
    cfg_mod.GLOBAL_CONFIG_DIR.mkdir(parents=True)
    (cfg_mod.GLOBAL_CONFIG_DIR / "neo.json").write_text(json.dumps({
        "model": "openai/gpt-5",
        "permissions": {"bash": {"rm *": "ask", "git *": "allow"}},
    }))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "neo.json").write_text(json.dumps({
        "permissions": {"bash": {"git *": "deny"}},
    }))
    cfg, _ = discover_config(proj)
    # Later layer wins for its own keys...
    assert cfg.permissions["bash"]["git *"] == "deny"
    # ...but unrelated keys from the earlier layer survive.
    assert cfg.permissions["bash"]["rm *"] == "ask"
    # Scalars from the earlier layer survive too.
    assert cfg.model == "openai/gpt-5"


@pytest.mark.parametrize("field,value", [
    ("model", 5),
    ("small_model", ["x"]),
    ("theme", None),
    ("thinking", 3),
    ("agent", {"a": 1}),
    ("max_steps", "40"),
    ("max_steps", True),  # bools are ints in Python; must be rejected
    ("context_window", 1.5),
    ("permissions", ["read"]),
    ("providers", "x"),
    ("sandbox", []),
    ("keybindings", "x"),
    ("mcp", 7),
    ("plugins", "x"),
    ("lsp", []),
    ("format", "x"),
    ("agents", "x"),
    ("verify_commands", "make test"),
    ("disabled_tools", {"bash": True}),
])
def test_from_dict_rejects_mistyped_fields(field, value):
    with pytest.raises(TypeError, match=field):
        NeoConfig.from_dict({field: value})


def test_from_dict_accepts_valid_types():
    cfg = NeoConfig.from_dict({
        "model": "openai/gpt-5",
        "max_steps": 10,
        "permissions": {"bash": {"git *": "allow"}},
        "verify_commands": ["make test"],
    })
    assert cfg.model == "openai/gpt-5"
    assert cfg.max_steps == 10
    assert cfg.verify_commands == ["make test"]


# --------------------------------------------------------------------------
# Auth resolution order
# --------------------------------------------------------------------------


def test_catalog_env_var_beats_auth_store_and_config(tmp_path, monkeypatch):
    store = AuthStore(tmp_path / "auth.json")
    store.set("anthropic", "store-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    cfg = FakeCfg({"anthropic": {"api_key": "cfg-key"}})
    assert resolve_api_key("anthropic", cfg, store) == "env-key"


def test_hyphenated_provider_id_fallback_is_sanitized(tmp_path, monkeypatch):
    assert _env_names("my-cool-provider") == ("MY_COOL_PROVIDER_API_KEY",)
    store = AuthStore(tmp_path / "auth.json")
    monkeypatch.setenv("MY_COOL_PROVIDER_API_KEY", "k")
    assert resolve_api_key("my-cool-provider", FakeCfg(), store) == "k"


def test_catalog_env_vars_win_over_hardcoded_map(tmp_path, monkeypatch):
    # google's catalog entry declares GEMINI_API_KEY first
    assert _env_names("google")[0] == "GEMINI_API_KEY"
    store = AuthStore(tmp_path / "auth.json")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    assert resolve_api_key("google", FakeCfg(), store) == "gemini-key"


# --------------------------------------------------------------------------
# Provider resolution
# --------------------------------------------------------------------------


def test_unknown_provider_protocol_override():
    p = resolve_provider(
        "myx", FakeCfg({"myx": {"base_url": "https://proxy.local/v1",
                                "protocol": "anthropic",
                                "api_key": "k"}}))
    assert isinstance(p, AnthropicProvider)
    assert p.spec.protocol == "anthropic"


def test_unknown_provider_invalid_protocol_raises():
    with pytest.raises(ProviderConfigError, match="protocol"):
        resolve_provider(
            "myx", FakeCfg({"myx": {"base_url": "https://proxy.local/v1",
                                    "protocol": "graphql"}}))


def test_unknown_provider_missing_base_url_mentions_protocol():
    with pytest.raises(ProviderConfigError, match="Anthropic"):
        resolve_provider(
            "myx", FakeCfg({"myx": {"protocol": "anthropic"}}))


def test_blank_base_url_message_is_protocol_aware():
    with pytest.raises(ProviderConfigError) as exc:
        resolve_provider("azure", FakeCfg())
    assert "providers.azure.base_url" in str(exc.value)
    assert "OpenAI-compatible" in str(exc.value)

    from neo.providers import _BY_ID  # type: ignore

    spec = _spec("blank-anthropic-test", "anthropic", base_url="")
    _BY_ID["blank-anthropic-test"] = spec
    try:
        with pytest.raises(ProviderConfigError) as exc2:
            resolve_provider("blank-anthropic-test", FakeCfg())
    finally:
        del _BY_ID["blank-anthropic-test"]
    assert "Anthropic" in str(exc2.value)


def test_env_var_beats_stale_config_api_key(monkeypatch):
    from neo.providers import _BY_ID  # type: ignore

    spec = _spec("env-beats-cfg", "openai",
                 base_url="https://x/v1", env_vars=("TEST_AUDIT_ENV_KEY",))
    _BY_ID["env-beats-cfg"] = spec
    monkeypatch.setenv("TEST_AUDIT_ENV_KEY", "fresh-env-key")
    try:
        p = resolve_provider(
            "env-beats-cfg",
            FakeCfg({"env-beats-cfg": {"api_key": "stale-cfg-key"}}))
        assert p.api_key == "fresh-env-key"
    finally:
        del _BY_ID["env-beats-cfg"]


# --------------------------------------------------------------------------
# Command template expansion
# --------------------------------------------------------------------------


def test_expand_dollar_ten_is_positional_not_prefix():
    args = [f"a{i}" for i in range(1, 11)]
    assert expand_command_template("x=$10 y=$1", args) == "x=a10 y=a1"


def test_expand_does_not_rescan_inserted_text():
    assert expand_command_template("$1", ["$2"]) == "$2"
    assert expand_command_template("$1 $2", ["$2", "B"]) == "$2 B"


def test_expand_unmatched_position_left_as_is():
    assert expand_command_template("$1 $3", ["only-one"]) == "only-one $3"


# --------------------------------------------------------------------------
# Google provider: terminal StreamEnd, in-band errors, cumulative usage
# --------------------------------------------------------------------------


def _google_provider(handler) -> GoogleProvider:
    return GoogleProvider(_spec("google-test", "google",
                                base_url="https://example.com/v1beta"),
                          "k", client=_client(handler))


def test_google_empty_stream_still_ends():
    provider = _google_provider(lambda r: httpx.Response(200, content=b""))
    events = _stream_events(provider, **_stream_kwargs())
    assert len(events) == 1
    assert isinstance(events[0], StreamEnd)


def test_google_inband_error_surfaces_without_stream_end():
    body = '{"error": {"code": 429, "message": "slow down", "status": "RESOURCE_EXHAUSTED"}}\n'
    provider = _google_provider(
        lambda r: httpx.Response(200, content=body.encode()))
    events = _stream_events(provider, **_stream_kwargs())
    errors = [e for e in events if isinstance(e, StreamError)]
    assert len(errors) == 1
    assert "slow down" in errors[0].message
    assert errors[0].retryable is True
    assert not any(isinstance(e, StreamEnd) for e in events)


def test_google_cumulative_usage_emitted_once():
    body = (
        '{"candidates": [{"content": {"parts": [{"text": "a"}]}}], '
        '"usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}}\n'
        '{"candidates": [{"content": {"parts": [{"text": "b"}]}}], '
        '"usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5}}\n'
    )
    provider = _google_provider(
        lambda r: httpx.Response(200, content=body.encode()))
    events = _stream_events(provider, **_stream_kwargs())
    usage = [e for e in events if isinstance(e, UsageTick)]
    assert len(usage) == 1  # latest cumulative snapshot only
    assert (usage[0].input_tokens, usage[0].output_tokens) == (3, 5)
    assert isinstance(events[-1], StreamEnd)


def test_google_empty_401_names_api_key():
    provider = _google_provider(lambda r: httpx.Response(401, content=b""))
    events = _stream_events(provider, **_stream_kwargs())
    assert isinstance(events[0], StreamError)
    assert "GEMINI_API_KEY" in events[0].message
    assert events[0].retryable is False


# --------------------------------------------------------------------------
# Anthropic provider: 401/403 guidance + stream error classification
# --------------------------------------------------------------------------


def _anthropic_provider(handler) -> _AP:
    return _AP(_spec("anthropic-test", "anthropic"), "k", client=_client(handler))


def test_anthropic_empty_401_names_api_key():
    provider = _anthropic_provider(lambda r: httpx.Response(401, content=b""))
    events = _stream_events(provider, **_stream_kwargs())
    assert isinstance(events[0], StreamError)
    assert "ANTHROPIC_API_KEY" in events[0].message
    assert events[0].retryable is False


def test_anthropic_empty_403_names_api_key():
    provider = _anthropic_provider(lambda r: httpx.Response(403, content=b""))
    events = _stream_events(provider, **_stream_kwargs())
    assert isinstance(events[0], StreamError)
    assert "ANTHROPIC_API_KEY" in events[0].message


def _sse_error_stream(err_type: str, message: str) -> bytes:
    payload = json.dumps({"type": "error",
                          "error": {"type": err_type, "message": message}})
    return f"event: error\ndata: {payload}\n\n".encode()


def test_anthropic_stream_overload_error_is_retryable():
    provider = _anthropic_provider(
        lambda r: httpx.Response(
            200, content=_sse_error_stream("overloaded_error", "Overloaded"),
            headers={"Content-Type": "text/event-stream"}))
    events = _stream_events(provider, **_stream_kwargs())
    assert isinstance(events[0], StreamError)
    assert events[0].retryable is True


def test_anthropic_stream_auth_error_is_not_retryable():
    provider = _anthropic_provider(
        lambda r: httpx.Response(
            200, content=_sse_error_stream("authentication_error", "bad key"),
            headers={"Content-Type": "text/event-stream"}))
    events = _stream_events(provider, **_stream_kwargs())
    assert isinstance(events[0], StreamError)
    assert events[0].retryable is False


# --------------------------------------------------------------------------
# OpenAI-compatible provider: 401/403 guidance
# --------------------------------------------------------------------------


def test_openai_compat_empty_403_names_provider_key():
    spec = _spec("myproxy", "openai", base_url="https://proxy.local/v1",
                 env_vars=("MYPROXY_API_KEY",))
    provider = OpenAICompatProvider(spec, "k",
                                    client=_client(lambda r: httpx.Response(403, content=b"")))
    events = _stream_events(provider, **_stream_kwargs())
    assert isinstance(events[0], StreamError)
    assert "myproxy" in events[0].message
    assert "MYPROXY_API_KEY" in events[0].message
    assert events[0].retryable is False


# --------------------------------------------------------------------------
# Headless CLI: resume validation + broken pipe
# --------------------------------------------------------------------------


def test_run_print_unknown_resume_fails_without_creating_session(
        tmp_path, monkeypatch):
    from neo.cli import run_print

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    rc = _run(run_print("hello", str(tmp_path), NeoConfig(), False,
                        resume="no-such-session"))
    assert rc == 1
    sessions_dir = home / ".neo" / "sessions"
    # The failed resume must not have created a session file.
    assert not any(sessions_dir.glob("*.jsonl"))


def test_run_print_broken_pipe_exits_quietly(tmp_path, monkeypatch, capsys):
    import neo.cli as cli_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    class FakeHarness:
        def cancel(self):
            pass

        async def run(self, messages):
            return
            yield  # make this an async generator

    class FakeCtx:
        plugins = None

    monkeypatch.setattr(cli_mod, "build_runtime",
                        lambda *a, **k: (None, FakeHarness(), FakeCtx(), None))

    async def no_mcp(*a, **k):
        return None

    monkeypatch.setattr(cli_mod, "boot_mcp", no_mcp)

    def broken_print(*a, **k):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr("builtins.print", broken_print)
    rc = _run(cli_mod.run_print("hello", str(tmp_path), NeoConfig(), True,
                                resume=None))
    assert rc == 0


# --------------------------------------------------------------------------
# LSP detection order
# --------------------------------------------------------------------------


def test_eslint_is_reachable_for_js_ts():
    assert detect_server("app.js").name == "eslint"
    assert detect_server("app.ts").name == "eslint"
    assert detect_server("app.jsx").name == "eslint"
    # Extensions only the TS server claims still resolve to it.
    assert detect_server("lib.mjs").name == "typescript-language-server"
    assert detect_server("lib.cjs").name == "typescript-language-server"


# --------------------------------------------------------------------------
# Formatter failure restores original bytes
# --------------------------------------------------------------------------


def test_format_failure_restores_original_bytes(tmp_path, monkeypatch):
    import neo.format.registry as reg

    target = tmp_path / "code.py"
    target.write_bytes(b"ORIGINAL\n")
    fmt = Formatter(name="fakefmt", command=["fakefmt", "$FILE"],
                    markers=(), extensions=(".py",), install_hint="")

    def fake_run(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"MANGLED BY FAILED FORMATTER\n")

        class Proc:
            returncode = 1
            stdout = b""
            stderr = b"boom"

        return Proc()

    monkeypatch.setattr(reg, "detect_formatter", lambda p, config=None: fmt)
    monkeypatch.setattr(reg, "_binary_available", lambda argv0: True)
    monkeypatch.setattr(reg.subprocess, "run", fake_run)
    changed, msg = format_file(target)
    assert changed is False
    assert "failed" in msg
    assert target.read_bytes() == b"ORIGINAL\n"


# --------------------------------------------------------------------------
# Plugin hook registry sanity
# --------------------------------------------------------------------------


def test_dead_hook_point_removed():
    # "command.execute.before" was declared but never triggered anywhere;
    # registering it must now fail instead of silently doing nothing.
    assert "command.execute.before" not in HOOK_POINTS
    assert set(HOOK_POINTS) == {
        "tool.execute.before",
        "tool.execute.after",
        "permission.ask",
        "chat.params",
        "session.end",
    }
