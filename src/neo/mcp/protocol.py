"""neo MCP — JSON-RPC 2.0 message framing.

Pure helpers for encoding/decoding the wire messages used by both the
stdio and StreamableHTTP transports. No I/O here.
"""

from __future__ import annotations

import dataclasses
import itertools
from typing import Any

JSONRPC_VERSION = "2.0"

# Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class MCPError(Exception):
    """Raised for any MCP-level failure: spawn, handshake, timeout, protocol."""


@dataclasses.dataclass(frozen=True)
class Request:
    """A JSON-RPC request: expects a Response or ErrorResponse."""

    id: int | str
    method: str
    params: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Notification:
    """A JSON-RPC notification: fire-and-forget, no id, no response."""

    method: str
    params: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Response:
    """A successful JSON-RPC response."""

    id: int | str
    result: Any


@dataclasses.dataclass(frozen=True)
class ErrorResponse:
    """A failed JSON-RPC response."""

    id: int | str | None
    code: int
    message: str
    data: Any = None


Message = Request | Notification | Response | ErrorResponse


def encode_request(id: int | str, method: str, params: dict | None = None) -> dict:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": id, "method": method}
    if params:
        msg["params"] = params
    return msg


def encode_notification(method: str, params: dict | None = None) -> dict:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params:
        msg["params"] = params
    return msg


def encode_response(id: int | str, result: Any) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "result": result}


def encode_error(
    id: int | str | None, code: int, message: str, data: Any = None
) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "error": err}


def decode_message(obj: dict) -> Message:
    """Classify a parsed JSON object into a typed JSON-RPC message.

    Raises MCPError if the object is not a well-formed message.
    """
    if not isinstance(obj, dict) or obj.get("jsonrpc") != JSONRPC_VERSION:
        raise MCPError(f"not a JSON-RPC 2.0 message: {obj!r:.120}")
    method = obj.get("method")
    rid = obj.get("id")
    if method is not None:
        if rid is None:
            return Notification(method=method, params=obj.get("params") or {})
        return Request(id=rid, method=method, params=obj.get("params") or {})
    if "error" in obj:
        err = obj.get("error") or {}
        return ErrorResponse(
            id=rid,
            code=err.get("code", INTERNAL_ERROR),
            message=err.get("message", "unknown error"),
            data=err.get("data"),
        )
    if "result" in obj:
        return Response(id=rid, result=obj.get("result"))
    raise MCPError(f"JSON-RPC message has neither method nor result/error: {obj!r:.120}")


class IdGenerator:
    """Monotonic request-id generator (one per client)."""

    def __init__(self, start: int = 1) -> None:
        self._counter = itertools.count(start)

    def next(self) -> int:
        return next(self._counter)
