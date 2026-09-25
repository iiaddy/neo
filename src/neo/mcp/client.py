"""neo MCP — client: initialize handshake, tools, prompts.

One MCPClient owns one transport and one logical session. All public
methods raise MCPError with a human-readable message on failure; nothing
here touches the tool registry (see manager.py for that).
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from .http import StreamableHTTPTransport
from .protocol import (
    MCPError,
    ErrorResponse,
    IdGenerator,
    Response,
    decode_message,
    encode_notification,
    encode_request,
)
from .stdio import StdioTransport

PROTOCOL_VERSION = "2024-11-05"


def _client_info() -> dict[str, str]:
    try:
        from neo import __version__
    except Exception:  # noqa: BLE001 - version is best-effort
        __version__ = "0.0.0"
    return {"name": "neo", "version": __version__}


@dataclasses.dataclass
class MCPToolCall:
    """Outcome of a single tools/call."""

    text: str
    is_error: bool = False
    raw: dict = dataclasses.field(default_factory=dict)


class MCPClient:
    def __init__(
        self,
        transport: StdioTransport | StreamableHTTPTransport,
        timeout: float = 30.0,
        name: str = "",
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.name = name or "mcp"
        self._ids = IdGenerator()
        self.connected = False
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.protocol_version: str = ""

    # -- lifecycle ------------------------------------------------------

    async def connect(self) -> None:
        """Run the MCP initialize handshake.

        Raises MCPError with a clear message when the server cannot be
        spawned, speaks garbage, or rejects the handshake.
        """
        try:
            await self.transport.connect()
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize transport errors
            raise MCPError(
                f"MCP server {self.name!r}: transport connect failed: {exc}"
            ) from exc

        rid = self._ids.next()
        request = encode_request(
            rid,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "prompts": {}},
                "clientInfo": _client_info(),
            },
        )
        raw = await self.transport.send_request(request, self.timeout)
        msg = decode_message(raw)
        if isinstance(msg, ErrorResponse):
            raise MCPError(
                f"MCP server {self.name!r} rejected initialize: "
                f"[{msg.code}] {msg.message}"
            )
        if not isinstance(msg, Response):
            raise MCPError(
                f"MCP server {self.name!r} sent an unexpected reply to "
                f"initialize: {raw!r:.160}"
            )
        result = msg.result if isinstance(msg.result, dict) else {}
        self.protocol_version = str(result.get("protocolVersion", ""))
        if not self.protocol_version:
            raise MCPError(
                f"MCP server {self.name!r} omitted protocolVersion in its "
                f"initialize result"
            )
        self.capabilities = (
            result.get("capabilities") if isinstance(result.get("capabilities"), dict) else {}
        )
        self.server_info = (
            result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
        )
        # Handshake is only complete after this notification.
        await self.transport.notify(encode_notification("notifications/initialized"))
        self.connected = True

    async def close(self) -> None:
        self.connected = False
        await self.transport.close()

    # -- helpers --------------------------------------------------------

    async def _request(self, method: str, params: dict | None = None) -> Any:
        if not self.connected:
            raise MCPError(f"MCP server {self.name!r} is not connected")
        rid = self._ids.next()
        raw = await self.transport.send_request(
            encode_request(rid, method, params), self.timeout
        )
        msg = decode_message(raw)
        if isinstance(msg, ErrorResponse):
            raise MCPError(
                f"MCP server {self.name!r} error on {method!r}: "
                f"[{msg.code}] {msg.message}"
            )
        if not isinstance(msg, Response):
            raise MCPError(
                f"MCP server {self.name!r} sent an unexpected reply to {method!r}"
            )
        return msg.result

    # -- tools ----------------------------------------------------------

    async def list_tools(self) -> list[dict]:
        """Return [{name, description, inputSchema}] for every server tool."""
        result = await self._request("tools/list")
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        out = []
        for t in tools:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            out.append(
                {
                    "name": str(t["name"]),
                    "description": str(t.get("description") or ""),
                    "inputSchema": t.get("inputSchema")
                    if isinstance(t.get("inputSchema"), dict)
                    else {"type": "object", "properties": {}},
                }
            )
        return out

    async def call_tool(self, name: str, args: dict | None) -> MCPToolCall:
        """Call a server tool; concatenate text content parts.

        Image/audio/resource parts are summarized in brackets rather than
        dropped silently. Server `isError` results surface as is_error.
        """
        result = await self._request(
            "tools/call", {"name": name, "arguments": args or {}}
        )
        result = result if isinstance(result, dict) else {}
        is_error = bool(result.get("isError"))
        parts = result.get("content") or []
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                texts.append(str(part.get("text", "")))
            elif ptype == "image":
                texts.append(
                    f"[image content omitted: {part.get('mimeType', 'unknown type')}]"
                )
            elif ptype == "audio":
                texts.append(
                    f"[audio content omitted: {part.get('mimeType', 'unknown type')}]"
                )
            elif ptype == "resource":
                res = part.get("resource") or {}
                texts.append(f"[embedded resource omitted: {res.get('uri', '?')}]")
            else:
                texts.append(f"[{ptype or 'unknown'} content omitted]")
        text = "\n".join(t for t in texts if t)
        if not text and is_error:
            text = f"MCP tool {name!r} reported an error with no message"
        return MCPToolCall(text=text, is_error=is_error, raw=result)

    # -- prompts --------------------------------------------------------

    async def list_prompts(self) -> list[dict]:
        """Return [{name, description, arguments}] for every server prompt.

        Servers without prompt support return [] instead of failing.
        """
        try:
            result = await self._request("prompts/list")
        except MCPError as exc:
            if "Method not found" in str(exc) or "-32601" in str(exc):
                return []
            raise
        prompts = (result or {}).get("prompts", []) if isinstance(result, dict) else []
        out = []
        for p in prompts:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            out.append(
                {
                    "name": str(p["name"]),
                    "description": str(p.get("description") or ""),
                    "arguments": p.get("arguments") or [],
                }
            )
        return out

    async def get_prompt(self, name: str, args: dict | None) -> dict:
        """Resolve a prompt template; returns the raw result dict."""
        result = await self._request(
            "prompts/get", {"name": name, "arguments": args or {}}
        )
        return result if isinstance(result, dict) else {}

    def prompt_text(self, prompt_result: dict) -> str:
        """Flatten a prompts/get result to plain text for slash commands."""
        texts: list[str] = []
        for msg in prompt_result.get("messages", []) or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, dict) and content.get("type") == "text":
                texts.append(str(content.get("text", "")))
            elif isinstance(content, str):
                texts.append(content)
        return "\n".join(texts)


def client_from_config(
    name: str, cfg: dict, workdir: Any = None
) -> MCPClient:
    """Build an MCPClient from one server entry of the neo config.

    stdio shape:  {"command": [...], "env": {}, "cwd": "...", "timeout": 30}
    http shape:   {"url": "https://...", "headers": {}, "timeout": 30}
    An explicit "type": "stdio" | "http" overrides the sniffing.
    """
    import shlex
    from pathlib import Path

    cfg = cfg or {}
    timeout = float(cfg.get("timeout", 30) or 30)
    stype = cfg.get("type")
    if stype is None:
        stype = "http" if cfg.get("url") else "stdio"

    if stype == "http":
        url = cfg.get("url")
        if not url:
            raise MCPError(
                f"MCP server {name!r}: http type needs a \"url\" field"
            )
        transport: StdioTransport | StreamableHTTPTransport = StreamableHTTPTransport(
            url, headers=cfg.get("headers"), timeout=timeout
        )
    elif stype == "stdio":
        command = cfg.get("command")
        if not command:
            raise MCPError(
                f"MCP server {name!r}: stdio type needs a \"command\" field"
            )
        cwd = cfg.get("cwd")
        if cwd and workdir is not None and not Path(cwd).is_absolute():
            cwd = str(Path(workdir) / cwd)
        transport = StdioTransport(
            command if isinstance(command, list) else shlex.split(str(command)),
            args=cfg.get("args"),
            env=cfg.get("env"),
            cwd=cwd,
        )
    else:
        raise MCPError(f"MCP server {name!r}: unknown type {stype!r}")
    return MCPClient(transport, timeout=timeout, name=name)
