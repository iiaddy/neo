"""Provider base types for neo: specs, neutral message shapes, events, Provider ABC.

Neutral message shapes (plain dicts):

- {"role": "user", "content": str | [{"type": "text", "text": ...}]}
- {"role": "assistant", "content": str | None,
   "tool_calls": [{"id": str, "name": str, "arguments": dict}]}
- {"role": "tool", "tool_call_id": str, "name": str,
   "content": str, "is_error": bool}

Tool schemas passed to providers:
- [{"name": str, "description": str, "parameters": <jsonschema dict>}]

ProviderEvent union (frozen dataclasses, each with a ``kind`` ClassVar):
- TextDelta(text) / ReasonDelta(text)          — streaming output fragments
- ToolArgDelta(call_id, name, args_text)      — streaming args fragment
- ToolCallReady(call_id, name, arguments)     — full tool call is ready
- UsageTick(input_tokens, output_tokens)       — cumulative for the response
- StreamEnd(finish)                           — stop | length | tool_calls | error
- StreamError(message, retryable)             — transport/API failure
"""

from __future__ import annotations

import abc
import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

PROTOCOL_OPENAI = "openai"  # chat/completions SSE
PROTOCOL_ANTHROPIC = "anthropic"  # /v1/messages SSE
PROTOCOL_GOOGLE = "google"  # :streamGenerateContent SSE


@dataclass(frozen=True)
class ProviderSpec:
    id: str  # e.g. "anthropic"
    title: str  # e.g. "Anthropic"
    protocol: str  # one of the PROTOCOL_* constants
    base_url: str
    env_vars: tuple[str, ...]  # tried in order
    default_model: str
    models: tuple[str, ...] = ()  # full model id list for /model pickers
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    context_window: int = 200_000
    max_output: int = 8192
    cost_in: float = 0.0  # USD per 1M tokens
    cost_out: float = 0.0


# ---------------------------------------------------------------------------
# ProviderEvent union
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextDelta:
    kind: ClassVar[str] = "text_delta"
    text: str = ""


@dataclass(frozen=True)
class ReasonDelta:
    kind: ClassVar[str] = "reason_delta"
    text: str = ""


@dataclass(frozen=True)
class ToolArgDelta:
    kind: ClassVar[str] = "tool_arg_delta"
    call_id: str = ""
    name: str = ""
    args_text: str = ""


@dataclass(frozen=True)
class ToolCallReady:
    kind: ClassVar[str] = "tool_call_ready"
    call_id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageTick:
    kind: ClassVar[str] = "usage_tick"
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class StreamEnd:
    kind: ClassVar[str] = "stream_end"
    finish: str = "stop"  # stop | length | tool_calls | error


@dataclass(frozen=True)
class StreamError:
    kind: ClassVar[str] = "stream_error"
    message: str = ""
    retryable: bool = False


ProviderEvent = (
    TextDelta
    | ReasonDelta
    | ToolArgDelta
    | ToolCallReady
    | UsageTick
    | StreamEnd
    | StreamError
)


class ProviderAuthError(Exception):
    """Raised when no usable API key is available for a provider."""


class ProviderConfigError(Exception):
    """Raised when a provider's configuration (e.g. base_url) is missing."""


# ---------------------------------------------------------------------------
# Proxy environment handling
# ---------------------------------------------------------------------------


@contextmanager
def _fixed_proxy_env() -> Iterator[None]:
    """Temporarily sanitize NO_PROXY for httpx.

    httpx 0.28 cannot parse bracketed IPv6 entries in NO_PROXY (e.g. "[::1]")
    and raises InvalidURL while building the client; curl and most other
    tools accept them. Stripping the brackets keeps the bypass entries
    working (unbracketed IPv6 is parsed fine), so local providers such as
    ollama still bypass the proxy.
    """
    saved: dict[str, str] = {}
    for key in ("no_proxy", "NO_PROXY"):
        val = os.environ.get(key)
        if val and "[" in val:
            fixed = ",".join(
                part[1:-1] if part.startswith("[") and part.endswith("]") else part
                for part in val.split(",")
            )
            if fixed != val:
                saved[key] = val
                os.environ[key] = fixed
    try:
        yield
    finally:
        for key, val in saved.items():
            os.environ[key] = val


def _build_client(timeout: float) -> httpx.AsyncClient:
    try:
        with _fixed_proxy_env():
            return httpx.AsyncClient(timeout=timeout)
    except httpx.InvalidURL:
        # Proxy env still unparseable: run without it rather than dying.
        # (Direct egress may be unavailable in such sandboxes.)
        return httpx.AsyncClient(timeout=timeout, trust_env=False)


class Provider(abc.ABC):
    """Base class for streaming LLM providers."""

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None,
        base_url: str | None = None,
        timeout: float = 120.0,
        extra_headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.spec = spec
        self.api_key = api_key
        self.base_url = (base_url or spec.base_url).rstrip("/")
        self.timeout = timeout
        self.extra_headers = dict(spec.extra_headers)
        self.extra_headers.update(extra_headers or {})
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            self._client = _build_client(timeout)

    @abc.abstractmethod
    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        signal: asyncio.Event | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream a chat completion as ProviderEvents."""
        raise NotImplementedError
        yield  # pragma: no cover - keeps this an async generator

    def _require_key(self) -> str:
        if not self.api_key:
            raise ProviderAuthError(
                f"no API key for provider '{self.spec.id}': "
                f"set {' or '.join(self.spec.env_vars) or 'an api key'} "
                f"or providers.{self.spec.id}.api_key in neo.json"
            )
        return self.api_key

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = dict(self.extra_headers)
        if extra:
            headers.update(extra)
        return headers

    async def aclose(self) -> None:
        """Close the owned httpx client (no-op if one was injected)."""
        if self._owns_client:
            await self._client.aclose()
