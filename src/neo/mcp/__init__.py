"""neo MCP (Model Context Protocol) subsystem.

Connects neo to external MCP servers over stdio or StreamableHTTP,
exposing their tools as neo Tools and their prompts as slash commands.

Typical wiring (see ``neo.cli.boot_mcp``)::

    manager = await boot_mcp(config, workdir, harness.tools, harness)
    ...
    await manager.stop()
"""

from __future__ import annotations

from .client import MCPClient, MCPToolCall, client_from_config
from .http import StreamableHTTPTransport, parse_sse
from .manager import MCPManager, MCP_TOOL_NAMES, sanitize_name
from .protocol import (
    MCPError,
    ErrorResponse,
    IdGenerator,
    Notification,
    Request,
    Response,
    decode_message,
    encode_error,
    encode_notification,
    encode_request,
    encode_response,
)
from .stdio import StdioTransport

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPManager",
    "MCP_TOOL_NAMES",
    "MCPToolCall",
    "ErrorResponse",
    "IdGenerator",
    "Notification",
    "Request",
    "Response",
    "StdioTransport",
    "StreamableHTTPTransport",
    "client_from_config",
    "decode_message",
    "encode_error",
    "encode_notification",
    "encode_request",
    "encode_response",
    "parse_sse",
    "sanitize_name",
]
