"""neo LSP wire protocol: stdio framing, ids, and async reader/writer.

Every LSP message on stdio is framed as::

    Content-Length: <byte-count>\\r\\n
    \\r\\n
    <json body>

This module implements exactly that framing plus a tiny id generator for
JSON-RPC request ids. The dispatch loop lives in ``client.py``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any


class ProtocolError(Exception):
    """The bytes on the wire did not look like LSP framing."""


def encode_message(payload: dict[str, Any]) -> bytes:
    """Serialize one JSON-RPC message with its Content-Length header."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one framed message. Returns None on clean EOF.

    Raises ProtocolError on malformed headers or a truncated body.
    """
    headers: dict[bytes, bytes] = {}
    while True:
        try:
            line = await reader.readline()
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        if not line:
            # EOF: clean only if we have not started a message.
            return None if not headers else _eof_error()
        stripped = line.strip()
        if not stripped:
            break  # blank line: end of headers
        name, sep, value = stripped.partition(b":")
        if not sep:
            raise ProtocolError(f"malformed LSP header line: {stripped!r}")
        headers[name.strip().lower()] = value.strip()
    raw_length = headers.get(b"content-length")
    if raw_length is None:
        raise ProtocolError("LSP message missing Content-Length header")
    try:
        length = int(raw_length)
    except ValueError:
        raise ProtocolError(f"bad Content-Length: {raw_length!r}")
    if length < 0:
        raise ProtocolError(f"negative Content-Length: {length}")
    try:
        body = await reader.readexactly(length)
    except (asyncio.IncompleteReadError, ConnectionError) as exc:
        raise ProtocolError(f"truncated LSP body: {exc}") from exc
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"LSP body is not UTF-8: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"LSP body is not JSON: {exc}") from exc


def _eof_error() -> None:
    raise ProtocolError("EOF in the middle of LSP headers")


async def write_message(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    """Write one framed message and drain the pipe."""
    writer.write(encode_message(payload))
    await writer.drain()


class IdGenerator:
    """Monotonic JSON-RPC request ids (per client instance)."""

    def __init__(self, start: int = 1) -> None:
        self._counter = itertools.count(start)

    def next(self) -> int:
        return next(self._counter)
