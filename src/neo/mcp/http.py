"""neo MCP — StreamableHTTP transport.

POSTs JSON-RPC messages to a single HTTP endpoint. Handles both response
shapes from the Streamable HTTP spec: a plain JSON-RPC response, or a
`text/event-stream` SSE body whose `data:` lines carry JSON-RPC messages.
The `mcp-session-id` response header is remembered and echoed back on
subsequent requests.

Server-initiated SSE streams (GET) and OAuth are out of scope for now.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .protocol import MCPError, decode_message


def parse_sse(text: str) -> list[dict]:
    """Extract JSON payloads from `data:` lines of an SSE body.

    Malformed lines are skipped; `[DONE]` sentinels are ignored.
    """
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


class StreamableHTTPTransport:
    """Transport over HTTP POST with optional SSE responses."""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url
        self.extra_headers = dict(headers or {})
        self.default_timeout = timeout
        self.session_id: str | None = None

    async def connect(self) -> None:
        # Nothing to do up front: the session starts on initialize.
        return None

    def _make_client(self, timeout: float) -> httpx.AsyncClient:
        """Build an httpx client that survives hostile proxy env vars.

        Reuses the provider layer's NO_PROXY bracket workaround (httpx 0.28
        raises InvalidURL on bracketed IPv6 entries); falls back to
        trust_env=False rather than dying when proxy env is unparseable.
        Local import: keeps neo.providers (heavy) out of module import time.
        """
        from ..providers.base import _fixed_proxy_env

        try:
            with _fixed_proxy_env():
                return httpx.AsyncClient(timeout=timeout)
        except httpx.InvalidURL:
            return httpx.AsyncClient(timeout=timeout, trust_env=False)

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        headers.update(self.extra_headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    async def send_request(self, message: dict, timeout: float) -> dict:
        rid = message.get("id")
        try:
            async with self._make_client(timeout) as client:
                resp = await client.post(
                    self.url, json=message, headers=self._headers()
                )
        except httpx.TimeoutException as exc:
            raise MCPError(
                f"MCP HTTP request {message.get('method')!r} to {self.url} "
                f"timed out after {timeout:g}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise MCPError(
                f"MCP HTTP request to {self.url} failed: {exc}"
            ) from exc

        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid

        if resp.status_code >= 400:
            raise MCPError(
                f"MCP HTTP server returned {resp.status_code} for "
                f"{message.get('method')!r}: {resp.text[:300].strip()}"
            )

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            messages = parse_sse(resp.text)
        else:
            try:
                body = resp.json()
            except ValueError as exc:
                raise MCPError(
                    f"MCP HTTP server returned non-JSON for "
                    f"{message.get('method')!r}: {resp.text[:200].strip()}"
                ) from exc
            messages = body if isinstance(body, list) else [body]

        return self._match_response(messages, rid, message.get("method"))

    async def notify(self, message: dict) -> None:
        """POST a notification; response is accepted but ignored."""
        try:
            async with self._make_client(self.default_timeout) as client:
                await client.post(self.url, json=message, headers=self._headers())
        except httpx.HTTPError:
            pass  # notifications are fire-and-forget

    def _match_response(
        self, messages: list[Any], rid: Any, method: Any
    ) -> dict:
        for obj in messages:
            if not isinstance(obj, dict):
                continue
            try:
                msg = decode_message(obj)
            except MCPError:
                continue
            if getattr(msg, "id", None) == rid:
                return obj
        raise MCPError(
            f"MCP HTTP server at {self.url} sent no response for request "
            f"{method!r} (id {rid})"
        )

    async def close(self) -> None:
        return None
