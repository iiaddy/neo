"""Anthropic provider: POST {base}/v1/messages with stream:true."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import (
    Provider,
    ProviderEvent,
    ReasonDelta,
    StreamEnd,
    StreamError,
    TextDelta,
    ToolArgDelta,
    ToolCallReady,
    UsageTick,
)
from .retry import _RETRY_PATTERNS, is_retryable

_STOP_MAP = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls"}
_API_VERSION = "2023-06-01"

# Mid-stream error types that are worth retrying even when the message
# text doesn't match the generic retry patterns.
_TRANSIENT_STREAM_ERROR_TYPES = ("overloaded_error", "api_error")


def convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutral messages -> Anthropic messages format.

    Tool results become user messages with tool_result content blocks.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            content = msg.get("content")
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for c in msg.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": c["id"],
                        "name": c["name"],
                        "input": c.get("arguments", {}),
                    }
                )
            out.append({"role": "assistant", "content": blocks or ""})
        elif role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id", ""),
                            "content": str(msg.get("content", "")),
                            "is_error": bool(msg.get("is_error", False)),
                        }
                    ],
                }
            )
        elif role == "system":
            continue  # handled via the system parameter
        else:
            out.append({"role": "user", "content": msg.get("content")})
    return out


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutral tool schemas -> Anthropic tools format."""
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("parameters", {"type": "object"}),
        }
        for t in tools
    ]


class AnthropicProvider(Provider):
    """POST {base}/v1/messages; named SSE event parsing."""

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
        key = self._require_key()
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": convert_messages(messages),
            "stream": True,
        }
        if tools:
            body["tools"] = convert_tools(tools)

        headers = self._headers(
            {
                "x-api-key": key,
                "anthropic-version": _API_VERSION,
                "content-type": "application/json",
            }
        )
        url = f"{self.base_url}/v1/messages"

        try:
            response = await self._client.post(
                url, json=body, headers=headers, timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            yield StreamError(message=f"request failed: {exc}", retryable=True)
            return

        if response.status_code != 200:
            raw = await response.aread()
            text = raw.decode("utf-8", "replace")
            message = text[:500]
            if not message and response.status_code in (401, 403):
                message = (
                    f"HTTP {response.status_code}: invalid or missing Anthropic "
                    "API key -- check the ANTHROPIC_API_KEY env var or "
                    "providers.anthropic.api_key in neo.json"
                )
            yield StreamError(
                message=message or f"HTTP {response.status_code}",
                retryable=is_retryable(response.status_code, text),
            )
            return

        async for event in self._iter_sse(response, signal):
            yield event

    async def _iter_sse(
        self, response: httpx.Response, signal: asyncio.Event | None
    ) -> AsyncIterator[ProviderEvent]:
        event_name = ""
        tool_blocks: dict[int, dict[str, Any]] = {}  # index -> {id,name,args}
        finish = "stop"

        async for line in response.aiter_lines():
            if signal is not None and signal.is_set():
                return
            line = line.strip()
            if not line:
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue

            if event_name == "error":
                err = data.get("error", {}) if isinstance(data, dict) else {}
                if not isinstance(err, dict):
                    err = {}
                err_type = str(err.get("type", ""))
                message = str(err.get("message", "") or payload)
                # Mid-stream errors carry no HTTP status (is_retryable(None,
                # ...) is always True), so classify by the error type and
                # message text: overload/server errors are transient,
                # auth/permission/request errors are not.
                text = f"{err_type} {message}"
                retryable = err_type in _TRANSIENT_STREAM_ERROR_TYPES or bool(
                    _RETRY_PATTERNS.search(text)
                )
                yield StreamError(
                    message=message[:500] or "stream error", retryable=retryable
                )
                return

            if event_name == "message_start":
                usage = (data.get("message") or {}).get("usage") or {}
                yield UsageTick(
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                )
            elif event_name == "content_block_start":
                block = data.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tool_blocks[data.get("index", 0)] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "args": "",
                    }
            elif event_name == "content_block_delta":
                delta = data.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    text = delta.get("text")
                    if text:
                        yield TextDelta(text=str(text))
                elif dtype in ("thinking_delta", "signature_delta"):
                    text = delta.get("thinking")
                    if text:
                        yield ReasonDelta(text=str(text))
                elif dtype == "input_json_delta":
                    idx = data.get("index", 0)
                    slot = tool_blocks.get(idx)
                    if slot is not None:
                        frag = delta.get("partial_json") or ""
                        slot["args"] += frag
                        yield ToolArgDelta(
                            call_id=slot["id"],
                            name=slot["name"],
                            args_text=str(frag),
                        )
            elif event_name == "content_block_stop":
                idx = data.get("index", 0)
                slot = tool_blocks.pop(idx, None)
                if slot is not None:
                    try:
                        arguments = json.loads(slot["args"]) if slot["args"] else {}
                    except json.JSONDecodeError:
                        arguments = {"_raw": slot["args"]}
                    yield ToolCallReady(
                        call_id=slot["id"], name=slot["name"], arguments=arguments
                    )
            elif event_name == "message_delta":
                usage = data.get("usage") or {}
                stop_reason = data.get("delta", {}).get("stop_reason")
                if stop_reason:
                    finish = _STOP_MAP.get(stop_reason, "stop")
                if usage:
                    yield UsageTick(
                        input_tokens=int(usage.get("input_tokens", 0)),
                        output_tokens=int(usage.get("output_tokens", 0)),
                    )
            elif event_name == "message_stop":
                pass

        yield StreamEnd(finish=finish)
