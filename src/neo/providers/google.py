"""Google provider: POST {base}/models/{model}:streamGenerateContent?key=.

The response is a stream of JSON values (arrays and/or objects) separated by
newlines and commas — parsing is defensive: buffer text, split into lines,
strip leading/trailing ``[,]`` and commas, json.loads each non-empty line,
silently skipping anything that fails to decode.
"""

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
    ToolCallReady,
    UsageTick,
)
from .retry import is_retryable

_FINISH_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "stop",
    "RECITATION": "stop",
}


def _safe_json_values(buffer: str) -> list[dict[str, Any]]:
    """Extract defensively-decoded JSON objects from a raw text buffer."""
    values: list[dict[str, Any]] = []
    for line in buffer.splitlines():
        chunk = line.strip()
        while chunk and chunk[0] in "[,":
            chunk = chunk[1:].strip()
        while chunk and chunk[-1] in "],":
            chunk = chunk[:-1].strip()
        if not chunk:
            continue
        try:
            value = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
        elif isinstance(value, list):
            values.extend(v for v in value if isinstance(v, dict))
    return values


def convert_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutral messages -> Gemini contents format (user/model roles)."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            parts: list[dict[str, Any]] = []
            content = msg.get("content")
            if content:
                parts.append({"text": str(content)})
            for c in msg.get("tool_calls") or []:
                parts.append(
                    {
                        "functionCall": {
                            "name": c["name"],
                            "args": c.get("arguments", {}),
                        }
                    }
                )
            out.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif role == "tool":
            out.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": msg.get("name", ""),
                                "response": {"output": str(msg.get("content", ""))},
                            }
                        }
                    ],
                }
            )
        else:  # user / system mapped into contents
            out.append({"role": "user", "parts": [{"text": str(msg.get("content", ""))}]})
    return out


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutral tool schemas -> Gemini functionDeclarations."""
    return [
        {
            "function_declarations": [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object"}),
                }
                for t in tools
            ]
        }
    ]


class GoogleProvider(Provider):
    """POST {base}/models/{model}:streamGenerateContent with ?key={api_key}."""

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
            "system_instruction": {"parts": [{"text": system}]},
            "contents": convert_contents(messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if tools:
            body["tools"] = convert_tools(tools)

        headers = self._headers({"Content-Type": "application/json"})
        url = f"{self.base_url}/models/{model}:streamGenerateContent"
        params = {"key": key, "alt": "sse"}

        try:
            response = await self._client.post(
                url, json=body, headers=headers, params=params, timeout=self.timeout
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
                    f"HTTP {response.status_code}: invalid or missing Google API "
                    "key -- check the GEMINI_API_KEY (or GOOGLE_API_KEY) env var "
                    "or providers.google.api_key in neo.json"
                )
            yield StreamError(
                message=message or f"HTTP {response.status_code}",
                retryable=is_retryable(response.status_code, text),
            )
            return

        async for event in self._iter_stream(response, signal):
            yield event

    async def _iter_stream(
        self, response: httpx.Response, signal: asyncio.Event | None
    ) -> AsyncIterator[ProviderEvent]:
        buffer = ""
        finish = "stop"
        call_seq = 0
        errored = False
        # Gemini repeats usageMetadata cumulatively on later chunks; only the
        # latest snapshot is meaningful, emitted once at stream end.
        latest_usage: dict[str, Any] | None = None

        async def handle_chunk(text: str) -> AsyncIterator[ProviderEvent]:
            nonlocal finish, call_seq, errored, latest_usage, buffer
            buffer += text
            lines = buffer.split("\n")
            buffer = lines.pop()  # keep possibly-incomplete trailing fragment
            for value in _safe_json_values("\n".join(lines)):
                if value.get("error"):
                    errored = True
                    err = value["error"] or {}
                    code = err.get("code")
                    try:
                        status = int(code) if code is not None else None
                    except (TypeError, ValueError):
                        status = None
                    yield StreamError(
                        message=str(err.get("message") or err or value)[:500],
                        retryable=is_retryable(status, str(err.get("message") or "")),
                    )
                    continue
                async for event in self._handle_value(value):
                    if isinstance(event, ToolCallReady):
                        call_seq += 1
                        event = ToolCallReady(
                            call_id=f"gcall_{call_seq}",
                            name=event.name,
                            arguments=event.arguments,
                        )
                    elif isinstance(event, UsageTick):
                        latest_usage = {
                            "input_tokens": event.input_tokens,
                            "output_tokens": event.output_tokens,
                        }
                        continue
                    yield event
                for candidate in value.get("candidates") or []:
                    fr = candidate.get("finishReason")
                    if fr:
                        finish = _FINISH_MAP.get(fr, "stop")

        async for text in response.aiter_text():
            if signal is not None and signal.is_set():
                return
            async for event in handle_chunk(text):
                yield event
        # Flush any trailing fragment at end of stream.
        async for event in handle_chunk("\n"):
            yield event
        # A stream that closes without an error always terminates with
        # StreamEnd -- even when it carried no data -- so the agent loop
        # never hangs waiting for a terminal event.
        if not errored:
            if latest_usage is not None:
                yield UsageTick(**latest_usage)
            yield StreamEnd(finish=finish)

    async def _handle_value(
        self, value: dict[str, Any]
    ) -> AsyncIterator[ProviderEvent]:
        for candidate in value.get("candidates") or []:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                if "functionCall" in part:
                    fc = part["functionCall"] or {}
                    yield ToolCallReady(
                        call_id="",
                        name=fc.get("name", ""),
                        arguments=fc.get("args") or {},
                    )
                elif part.get("thought") is True and part.get("text"):
                    yield ReasonDelta(text=str(part["text"]))
                elif part.get("text"):
                    yield TextDelta(text=str(part["text"]))
        usage = value.get("usageMetadata") or {}
        if usage:
            yield UsageTick(
                input_tokens=int(usage.get("promptTokenCount", 0)),
                output_tokens=int(usage.get("candidatesTokenCount", 0)),
            )
