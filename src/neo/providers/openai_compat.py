"""OpenAI-compatible provider: POST {base}/chat/completions with stream:true."""

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
from .retry import is_retryable

_FINISH_MAP = {"stop": "stop", "length": "length", "tool_calls": "tool_calls"}


def convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutral messages -> OpenAI chat message format."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": msg.get("content") or None,
            }
            calls = msg.get("tool_calls") or []
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c.get("arguments", {})),
                        },
                    }
                    for c in calls
                ]
            out.append(entry)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "content": str(msg.get("content", "")),
                }
            )
        else:  # user (and anything else) passes through
            out.append({"role": role, "content": msg.get("content")})
    return out


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutral tool schemas -> OpenAI function tool format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object"}),
            },
        }
        for t in tools
    ]


class OpenAICompatProvider(Provider):
    """POST {base}/chat/completions with stream:true; SSE delta parsing."""

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
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                *convert_messages(messages),
            ],
        }
        if tools:
            body["tools"] = convert_tools(tools)
            body["tool_choice"] = "auto"

        headers = self._headers(
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        )
        url = f"{self.base_url}/chat/completions"

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
            yield StreamError(
                message=text[:500] or f"HTTP {response.status_code}",
                retryable=is_retryable(response.status_code, text),
            )
            return

        async for event in self._iter_sse(response, signal):
            yield event

    async def _iter_sse(
        self, response: httpx.Response, signal: asyncio.Event | None
    ) -> AsyncIterator[ProviderEvent]:
        tool_args: dict[int, dict[str, Any]] = {}  # index -> {id, name, args}
        finish = "stop"

        async for line in response.aiter_lines():
            if signal is not None and signal.is_set():
                return
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            choices = chunk.get("choices") or []
            for choice in choices:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    yield TextDelta(text=str(content))
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    yield ReasonDelta(text=str(reasoning))
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_args.setdefault(
                        idx, {"id": "", "name": "", "args": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    frag = fn.get("arguments") or ""
                    if frag:
                        slot["args"] += frag
                        yield ToolArgDelta(
                            call_id=slot["id"],
                            name=slot["name"],
                            args_text=str(frag),
                        )
                if choice.get("finish_reason"):
                    finish = _FINISH_MAP.get(choice["finish_reason"], "stop")

            usage = chunk.get("usage")
            if usage:
                yield UsageTick(
                    input_tokens=int(usage.get("prompt_tokens", 0)),
                    output_tokens=int(usage.get("completion_tokens", 0)),
                )

        for idx in sorted(tool_args):
            slot = tool_args[idx]
            try:
                arguments = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                arguments = {"_raw": slot["args"]}
            yield ToolCallReady(call_id=slot["id"], name=slot["name"], arguments=arguments)
        yield StreamEnd(finish=finish)
