"""neo MCP — stdio transport.

Spawns the server as a subprocess and speaks newline-delimited JSON-RPC
over its stdin/stdout (no Content-Length framing). stderr is drained in
the background so a chatty server can never block on a full pipe.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections import deque
from pathlib import Path
from typing import Any

from .protocol import MCPError, decode_message


class StdioTransport:
    """Transport over a child process's stdio pipes."""

    def __init__(
        self,
        command: str | list[str],
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        if isinstance(command, str):
            parts = shlex.split(command)
        else:
            parts = list(command)
        if not parts:
            raise MCPError("MCP stdio server needs a non-empty command")
        self.command = parts[0]
        self.args = parts[1:] + list(args or [])
        self.env = dict(env or {})
        self.cwd = str(cwd) if cwd is not None else None

        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int | str, asyncio.Future[dict]] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._closed = False

    @property
    def stderr_tail(self) -> str:
        """Last lines of server stderr — useful when a server dies early."""
        return "".join(self._stderr_tail)

    async def connect(self) -> None:
        if self._proc is not None:
            return
        merged = dict(os.environ)
        merged.update(self.env)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged,
                cwd=self.cwd,
            )
        except FileNotFoundError as exc:
            raise MCPError(
                f"could not spawn MCP server {self.command!r}: binary not found"
            ) from exc
        except OSError as exc:
            raise MCPError(f"could not spawn MCP server {self.command!r}: {exc}") from exc
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="mcp-stdio-reader"
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="mcp-stdio-stderr"
        )

    async def send_request(self, message: dict, timeout: float) -> dict:
        """Write a request and wait for the matching response.

        Raises MCPError on timeout, transport death, or write failure.
        """
        if self._proc is None or self._closed:
            raise MCPError("MCP stdio transport is not connected")
        rid = message.get("id")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[rid] = fut
        try:
            await self._write(message)
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            raise MCPError(
                f"MCP request {message.get('method')!r} timed out after {timeout:g}s"
            ) from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(rid, None)
            raise MCPError(
                f"MCP server {self.command!r} died while sending "
                f"{message.get('method')!r}: {exc}. stderr: {self.stderr_tail.strip()[-500:]}"
            ) from exc
        finally:
            self._pending.pop(rid, None)

    async def notify(self, message: dict) -> None:
        """Fire-and-forget notification. Never raises on a dead pipe."""
        if self._proc is None or self._closed:
            return
        try:
            await self._write(message)
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            pass

    async def _write(self, message: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break  # EOF: server exited
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # ignore non-JSON stdout chatter
                try:
                    msg = decode_message(obj)
                except MCPError:
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        finally:
            self._fail_pending("MCP server process exited")

    def _dispatch(self, msg: Any) -> None:
        from .protocol import ErrorResponse, Response

        if isinstance(msg, (Response, ErrorResponse)):
            fut = self._pending.get(msg.id)
            if fut is not None and not fut.done():
                # Deliver the raw dict; the client decodes it.
                rid = msg.id
                if isinstance(msg, Response):
                    fut.set_result({"jsonrpc": "2.0", "id": rid, "result": msg.result})
                else:
                    fut.set_result(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {
                                "code": msg.code,
                                "message": msg.message,
                                "data": msg.data,
                            },
                        }
                    )
        # Server notifications/requests are currently ignored; neo never
        # issues server->client requests (roots, sampling).

    def _fail_pending(self, reason: str) -> None:
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(MCPError(f"{reason} (request id {rid})"))
        self._pending.clear()

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                self._stderr_tail.append(line.decode("utf-8", "replace"))
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        """Terminate the server and release everything. Idempotent."""
        self._closed = True
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), 5)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:  # noqa: BLE001 - best-effort teardown
                            pass
            except ProcessLookupError:
                pass
        self._fail_pending("MCP transport closed")
