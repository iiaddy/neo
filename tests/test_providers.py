"""Tests for the neo providers subsystem.

Uses httpx.MockTransport with canned SSE streams for all three protocols.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
import pytest

from neo.providers import (
    OpenAICompatProvider,
    ProviderAuthError,
    ProviderConfigError,
    list_providers,
    resolve_provider,
)
from neo.providers.anthropic import AnthropicProvider
from neo.providers.base import (
    ProviderSpec,
    ReasonDelta,
    StreamEnd,
    StreamError,
    TextDelta,
    ToolArgDelta,
    ToolCallReady,
    UsageTick,
)
from neo.providers.catalog import PROVIDERS
from neo.providers.google import GoogleProvider
from neo.providers.retry import compute_delay, is_retryable


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class FakeCfg:
    def __init__(self, providers: dict[str, dict[str, Any]] | None = None):
        self.providers = providers or {}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sse(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode(), headers={"Content-Type": "text/event-stream"}
    )


async def _collect(provider, **kwargs) -> list:
    return [e async for e in provider.stream(**kwargs)]


def _stream_kwargs() -> dict[str, Any]:
    return {
        "model": "test-model",
        "system": "sys",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [],
        "max_tokens": 64,
    }


# --------------------------------------------------------------------------
# retry helpers
# --------------------------------------------------------------------------


def test_is_retryable():
    assert is_retryable(None, "") is True
    assert is_retryable(429, "") is True
    assert is_retryable(500, "") is True
    assert is_retryable(503, "x") is True
    assert is_retryable(400, "") is False
    assert is_retryable(404, "") is False
    assert is_retryable(400, "rate limit exceeded") is True
    assert is_retryable(400, "overloaded, try again later") is True
    assert is_retryable(400, "invalid api key") is False


def test_compute_delay_bounds():
    for attempt in range(10):
        delay = compute_delay(attempt)
        lo = min(30.0, 2.0 * 2**attempt)
        hi = min(30.0, 2.0 * 2**attempt * 1.25)
        assert lo <= delay <= hi + 1e-9
    assert compute_delay(100) == 30.0
    assert compute_delay(0) < 3.0


# --------------------------------------------------------------------------
# OpenAI-compatible provider
# --------------------------------------------------------------------------

OPENAI_SSE = """data: {"choices": [{"delta": {"content": "Hel"}, "index": 0}]}

data: {"choices": [{"delta": {"content": "lo"}, "index": 0}]}

data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "bash", "arguments": "{\\"cmd\\":"}}]}}]}

data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\\"ls\\"}"}}]}}]}

data: {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}

data: {"usage": {"prompt_tokens": 10, "completion_tokens": 20}}

data: [DONE]
"""


def _openai_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path.endswith("/chat/completions")
    body = json.loads(request.content.decode())
    assert body["stream"] is True
    assert body["model"] == "test-model"
    assert request.headers["authorization"] == "Bearer k-openai"
    assert request.headers["content-type"] == "application/json"
    return _sse(OPENAI_SSE)


def _openai_spec(extra_headers=None) -> ProviderSpec:
    return ProviderSpec(
        id="openai-test",
        title="OpenAI Test",
        protocol="openai",
        base_url="https://example.com/v1",
        env_vars=(),
        default_model="m",
        extra_headers=extra_headers or {},
    )


def test_openai_full_sequence():
    provider = OpenAICompatProvider(
        _openai_spec(), "k-openai", client=_client(_openai_handler)
    )
    events = asyncio.run(_collect(provider, **_stream_kwargs()))

    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["Hel", "lo"]

    args = [e for e in events if isinstance(e, ToolArgDelta)]
    assert len(args) == 2
    assert args[0].call_id == "call_1" and args[0].name == "bash"
    assert args[0].args_text == '{"cmd":'
    assert args[1].args_text == '"ls"}'

    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(ready) == 1
    assert ready[0].call_id == "call_1"
    assert ready[0].name == "bash"
    assert ready[0].arguments == {"cmd": "ls"}

    usage = [e for e in events if isinstance(e, UsageTick)]
    assert len(usage) == 1
    assert usage[0].input_tokens == 10 and usage[0].output_tokens == 20

    end = events[-1]
    assert isinstance(end, StreamEnd) and end.finish == "tool_calls"


def test_openai_reasoning_and_tolerates_blanks():
    body = (
        'data: {"choices": [{"delta": {"reasoning_content": "think"}, "index": 0}]}\n'
        "\n"
        "\n"
        'data: {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}\n'
        "data: [DONE]\n"
    )
    provider = OpenAICompatProvider(
        _openai_spec(), "k", client=_client(lambda r: _sse(body))
    )
    events = asyncio.run(_collect(provider, **_stream_kwargs()))
    reasons = [e.text for e in events if isinstance(e, ReasonDelta)]
    assert reasons == ["think"]
    assert isinstance(events[-1], StreamEnd) and events[-1].finish == "stop"


def test_openai_bad_tool_args_falls_back_to_raw():
    body = (
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", '
        '"function": {"name": "bash", "arguments": "not-json"}}]}}]}\n'
        'data: {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}\n'
        "data: [DONE]\n"
    )
    provider = OpenAICompatProvider(
        _openai_spec(), "k", client=_client(lambda r: _sse(body))
    )
    events = asyncio.run(_collect(provider, **_stream_kwargs()))
    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert ready[0].arguments == {"_raw": "not-json"}


def test_openai_non_2xx_yields_error():
    def handler(request):
        return httpx.Response(429, content=b"rate limit exceeded")

    provider = OpenAICompatProvider(
        _openai_spec(), "k", client=_client(handler)
    )
    events = asyncio.run(_collect(provider, **_stream_kwargs()))
    assert len(events) == 1
    err = events[0]
    assert isinstance(err, StreamError)
    assert err.retryable is True
    assert "rate limit" in err.message


def test_openai_extra_headers_and_request_shape():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content.decode())
        return _sse("data: [DONE]\n")

    provider = OpenAICompatProvider(
        _openai_spec({"X-Title": "neo"}), "k", client=_client(handler)
    )
    kwargs = _stream_kwargs()
    kwargs["messages"] = [
        {
            "role": "assistant",
            "content": "using tool",
            "tool_calls": [{"id": "t1", "name": "read", "arguments": {"path": "f"}}],
        },
        {"role": "tool", "tool_call_id": "t1", "name": "read", "content": "data"},
    ]
    kwargs["tools"] = [
        {"name": "read", "description": "read file", "parameters": {"type": "object"}}
    ]
    asyncio.run(_collect(provider, **kwargs))

    assert seen["headers"]["x-title"] == "neo"
    msgs = seen["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "sys"}
    asst = msgs[1]
    assert asst["tool_calls"][0]["function"]["name"] == "read"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"path": "f"}
    assert msgs[2]["role"] == "tool" and msgs[2]["tool_call_id"] == "t1"
    tools = seen["body"]["tools"]
    assert tools[0]["function"]["parameters"] == {"type": "object"}


def test_openai_missing_key_raises():
    provider = OpenAICompatProvider(_openai_spec(), None, client=_client(_openai_handler))
    with pytest.raises(ProviderAuthError):
        asyncio.run(_collect(provider, **_stream_kwargs()))


# --------------------------------------------------------------------------
# Anthropic provider
# --------------------------------------------------------------------------

ANTHROPIC_SSE = """event: message_start
data: {"message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}

event: content_block_delta
data: {"index": 0, "delta": {"type": "text_delta", "text": "Answer"}}

event: content_block_start
data: {"index": 1, "content_block": {"type": "tool_use", "id": "tu_9", "name": "bash"}}

event: content_block_delta
data: {"index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\\"cmd\\":"}}

event: content_block_delta
data: {"index": 1, "delta": {"type": "input_json_delta", "partial_json": "\\"pwd\\"}"}}

event: content_block_stop
data: {"index": 1}

event: message_delta
data: {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}}

event: message_stop
data: {}
"""


def _anthropic_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "k-anthropic"
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content.decode())
    assert body["system"] == "sys"
    assert body["stream"] is True
    return _sse(ANTHROPIC_SSE)


def _anthropic_spec() -> ProviderSpec:
    return ProviderSpec(
        id="anthropic-test",
        title="Anthropic Test",
        protocol="anthropic",
        base_url="https://example.com",
        env_vars=(),
        default_model="m",
    )


def test_anthropic_full_sequence():
    provider = AnthropicProvider(
        _anthropic_spec(), "k-anthropic", client=_client(_anthropic_handler)
    )
    events = asyncio.run(_collect(provider, **_stream_kwargs()))

    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["Answer"]

    usage = [e for e in events if isinstance(e, UsageTick)]
    assert len(usage) == 2
    assert usage[0].input_tokens == 5 and usage[0].output_tokens == 0
    assert usage[1].output_tokens == 12

    args = [e for e in events if isinstance(e, ToolArgDelta)]
    assert len(args) == 2
    assert args[0].call_id == "tu_9" and args[0].name == "bash"

    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(ready) == 1
    assert ready[0].call_id == "tu_9"
    assert ready[0].arguments == {"cmd": "pwd"}

    end = events[-1]
    assert isinstance(end, StreamEnd) and end.finish == "tool_calls"


def test_anthropic_thinking_delta():
    body = (
        "event: content_block_delta\n"
        'data: {"index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}}\n'
        "\n"
        "event: message_stop\n"
        "data: {}\n"
    )
    provider = AnthropicProvider(
        _anthropic_spec(), "k", client=_client(lambda r: _sse(body))
    )
    events = asyncio.run(_collect(provider, **_stream_kwargs()))
    reasons = [e.text for e in events if isinstance(e, ReasonDelta)]
    assert reasons == ["hmm"]


def test_anthropic_message_shape():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return _sse("event: message_stop\ndata: {}\n")

    provider = AnthropicProvider(_anthropic_spec(), "k", client=_client(handler))
    kwargs = _stream_kwargs()
    kwargs["messages"] = [
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [{"id": "t1", "name": "bash", "arguments": {"cmd": "ls"}}],
        },
        {"role": "tool", "tool_call_id": "t1", "name": "bash", "content": "out"},
    ]
    kwargs["tools"] = [
        {"name": "bash", "description": "run", "parameters": {"type": "object"}}
    ]
    asyncio.run(_collect(provider, **kwargs))

    msgs = seen["body"]["messages"]
    assert msgs[0]["role"] == "assistant"
    blocks = msgs[0]["content"]
    assert blocks[0] == {"type": "text", "text": "ok"}
    assert blocks[1]["type"] == "tool_use" and blocks[1]["input"] == {"cmd": "ls"}
    tool_msg = msgs[1]
    assert tool_msg["role"] == "user"
    assert tool_msg["content"][0]["type"] == "tool_result"
    assert tool_msg["content"][0]["tool_use_id"] == "t1"
    tools = seen["body"]["tools"]
    assert tools[0]["input_schema"] == {"type": "object"}


def test_anthropic_error_event():
    body = "event: error\ndata: {\"error\": {\"message\": \"boom\"}}\n"
    provider = AnthropicProvider(
        _anthropic_spec(), "k", client=_client(lambda r: _sse(body))
    )
    events = asyncio.run(_collect(provider, **_stream_kwargs()))
    assert len(events) == 1
    assert isinstance(events[0], StreamError)
    assert events[0].message == "boom"
    assert events[0].retryable is False


# --------------------------------------------------------------------------
# Google provider
# --------------------------------------------------------------------------

GOOGLE_STREAM = (
    '[{"candidates": [{"content": {"parts": [{"text": "Hi"}]}, "finishReason": null}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}}]' + "\n"
    '[{"candidates": [{"content": {"parts": [{"functionCall": {"name": "bash", "args": {"cmd": "ls"}}}]}, "finishReason": "STOP"}]}]' + "\n"
)


def _google_handler(request: httpx.Request) -> httpx.Response:
    assert ":streamGenerateContent" in request.url.path
    assert request.url.params["key"] == "k-google"
    body = json.loads(request.content.decode())
    assert body["system_instruction"]["parts"][0]["text"] == "sys"
    assert body["contents"][0]["role"] == "user"
    return httpx.Response(200, content=GOOGLE_STREAM.encode())


def _google_spec() -> ProviderSpec:
    return ProviderSpec(
        id="google-test",
        title="Google Test",
        protocol="google",
        base_url="https://example.com/v1beta",
        env_vars=(),
        default_model="m",
    )


def test_google_full_sequence():
    provider = GoogleProvider(_google_spec(), "k-google", client=_client(_google_handler))
    events = asyncio.run(_collect(provider, **_stream_kwargs()))

    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["Hi"]

    usage = [e for e in events if isinstance(e, UsageTick)]
    assert len(usage) == 1
    assert usage[0].input_tokens == 3 and usage[0].output_tokens == 1

    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(ready) == 1
    assert ready[0].call_id.startswith("gcall_")
    assert ready[0].name == "bash"
    assert ready[0].arguments == {"cmd": "ls"}

    end = events[-1]
    assert isinstance(end, StreamEnd) and end.finish == "stop"


def test_google_thought_part():
    body = (
        '[{"candidates": [{"content": {"parts": [{"thought": true, '
        '"text": "pondering"}]}, "finishReason": "STOP"}]}]\n'
    )
    provider = GoogleProvider(
        _google_spec(), "k", client=_client(lambda r: httpx.Response(200, content=body.encode()))
    )
    events = asyncio.run(_collect(provider, **_stream_kwargs()))
    reasons = [e.text for e in events if isinstance(e, ReasonDelta)]
    assert reasons == ["pondering"]


def test_google_tool_message_shape():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, content=b"")

    provider = GoogleProvider(_google_spec(), "k", client=_client(handler))
    kwargs = _stream_kwargs()
    kwargs["messages"] = [
        {
            "role": "assistant",
            "content": "run",
            "tool_calls": [{"id": "t1", "name": "bash", "arguments": {"cmd": "ls"}}],
        },
        {"role": "tool", "tool_call_id": "t1", "name": "bash", "content": "o"},
    ]
    asyncio.run(_collect(provider, **kwargs))

    contents = seen["body"]["contents"]
    assert contents[0]["role"] == "model"
    fc = contents[0]["parts"][1]["functionCall"]
    assert fc["name"] == "bash" and fc["args"] == {"cmd": "ls"}
    fr = contents[1]["parts"][0]["functionResponse"]
    assert fr["name"] == "bash"
    assert fr["response"]["output"] == "o"


def test_google_non_2xx():
    def handler(request):
        return httpx.Response(400, content=b"bad request key")

    provider = GoogleProvider(_google_spec(), "k", client=_client(handler))
    events = asyncio.run(_collect(provider, **_stream_kwargs()))
    assert isinstance(events[0], StreamError)
    assert events[0].retryable is False


# --------------------------------------------------------------------------
# Catalog / resolution
# --------------------------------------------------------------------------


def test_catalog_has_no_opencode_and_expected_count():
    ids = [s.id for s in PROVIDERS]
    # Only the provider literally named "opencode" is excluded;
    # similarly-named providers (e.g. "opencode-go") stay.
    assert "opencode" not in ids
    assert "opencode-go" in ids
    # 227 catalog/curated providers + the "custom" OpenAI-compatible entry.
    assert len(PROVIDERS) == 228
    assert list_providers() == PROVIDERS


def test_resolve_provider_uses_cfg_override_and_env():
    os.environ["TEST_NEO_KEY"] = "env-key"
    spec = ProviderSpec(
        id="spec-test", title="T", protocol="openai",
        base_url="https://x/v1", env_vars=("TEST_NEO_KEY",), default_model="m",
    )
    from neo.providers.catalog import PROVIDERS as _p  # noqa
    from neo.providers import _BY_ID  # type: ignore

    _BY_ID["spec-test"] = spec
    try:
        p = resolve_provider("spec-test", FakeCfg())
        assert p.api_key == "env-key"
        assert p.base_url == "https://x/v1"

        # env var beats a stale api_key in config (documented env → auth.json → config order)
        p2 = resolve_provider(
            "spec-test", FakeCfg({"spec-test": {"api_key": "cfg-key", "base_url": "https://y/v1"}})
        )
        assert p2.api_key == "env-key"
        assert p2.base_url == "https://y/v1"
    finally:
        del _BY_ID["spec-test"]
        del os.environ["TEST_NEO_KEY"]


def test_resolve_unknown_id_synthesizes_openai_spec():
    p = resolve_provider(
        "mycustom", FakeCfg({"mycustom": {"base_url": "https://proxy.local/v1", "api_key": "k"}})
    )
    assert isinstance(p, OpenAICompatProvider)
    assert p.spec.protocol == "openai"
    assert p.base_url == "https://proxy.local/v1"


def test_resolve_unknown_id_without_base_url_raises():
    with pytest.raises(ProviderConfigError):
        resolve_provider("mycustom", FakeCfg())


def test_resolve_empty_base_url_raises():
    # azure-style entry: empty default base_url, no override
    p = None
    with pytest.raises(ProviderConfigError) as exc:
        resolve_provider("azure", FakeCfg())
    assert "providers.azure.base_url" in str(exc.value)
    assert "neo.json" in str(exc.value)


def test_resolve_protocol_mapping():
    from neo.providers import _BY_ID  # type: ignore

    p1 = resolve_provider("anthropic", FakeCfg({"anthropic": {"api_key": "k"}}))
    assert isinstance(p1, AnthropicProvider)
    p2 = resolve_provider("google", FakeCfg({"google": {"api_key": "k"}}))
    assert isinstance(p2, GoogleProvider)
    p3 = resolve_provider("xai", FakeCfg({"xai": {"api_key": "k"}}))
    assert isinstance(p3, OpenAICompatProvider)
    assert _BY_ID["openrouter"].extra_headers["X-Title"] == "neo"


def test_fixed_proxy_env_strips_bracketed_ipv6(monkeypatch):
    from neo.providers.base import _fixed_proxy_env

    monkeypatch.setenv(
        "no_proxy", "localhost,127.0.0.1,::1,[::1],[fd8b:4f84:7d32:99::1]"
    )
    with _fixed_proxy_env():
        assert (
            __import__("os").environ["no_proxy"]
            == "localhost,127.0.0.1,::1,::1,fd8b:4f84:7d32:99::1"
        )
    # original value restored afterwards
    assert (
        __import__("os").environ["no_proxy"]
        == "localhost,127.0.0.1,::1,[::1],[fd8b:4f84:7d32:99::1]"
    )


def test_build_client_survives_bracketed_ipv6_no_proxy(monkeypatch):
    # httpx 0.28 raises InvalidURL on "[::1]" in NO_PROXY at construction;
    # neo must still build a working client (regression: sandbox egress).
    import os

    from neo.providers.base import _build_client

    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "localhost,[::1]")
    client = _build_client(5.0)
    assert client.timeout.connect == 5.0
    # client was built with proxy env intact (not trust_env=False fallback)
    assert client._transport is not None
