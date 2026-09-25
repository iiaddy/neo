"""neo LSP client: lazy per-project server processes over stdio.

Servers are spawned on first use, keyed by (project root, server name), and
reused across calls. A server that fails to start, crashes, or stops
answering is remembered as broken and is not respawned for 5 minutes.

Diagnostics are fetched with the pull API (``textDocument/diagnostic``);
servers that do not implement it fall back to pushed
``textDocument/publishDiagnostics`` notifications collected for up to 2s.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from .protocol import ProtocolError, read_message, write_message
from .servers import ServerEntry, detect_server, resolve_command

_BROKEN_TTL = 300.0        # do not respawn a broken server for 5 minutes
_REQUEST_TIMEOUT = 10.0    # per JSON-RPC request
_PUSH_WAIT = 2.0           # how long to wait for pushed diagnostics

_SEVERITY = {1: "error", 2: "warning", 3: "information", 4: "hint"}


class LSPError(Exception):
    """The server errored, timed out, or exited."""


class LSPNotInstalled(Exception):
    """No server binary found. str(exc) is the user-facing message."""


_clients: dict[tuple[str, str], "LSPClient"] = {}
_broken: dict[tuple[str, str], float] = {}


def _client_key(root: Path, name: str) -> tuple[str, str]:
    return (str(root), name)


def reset_state() -> None:
    """Drop cached clients and broken-server memory (tests / shutdown)."""
    _clients.clear()
    _broken.clear()


async def shutdown_all() -> None:
    """Terminate every spawned server (best effort)."""
    clients = list(_clients.values())
    reset_state()
    for client in clients:
        try:
            await client.shutdown()
        except Exception:
            pass


class LSPClient:
    """One long-lived connection to one language server in one project."""

    def __init__(
        self,
        name: str,
        entry: ServerEntry,
        root: Path,
        command: list[str],
        config: Any = None,
    ) -> None:
        self.name = name
        self.entry = entry
        self.root = root
        self.command = command
        self.config = config
        self._key = _client_key(root, name)
        self.proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._opened: dict[str, tuple[int, str]] = {}  # uri -> (version, text)
        self._pushes: dict[str, list[dict]] = {}       # uri -> last pushed diags
        self._push_events: dict[str, asyncio.Event] = {}
        self._spawn_lock = asyncio.Lock()
        self._closing = False
        self.initialized = False

    # -- lifecycle ------------------------------------------------------

    @classmethod
    def for_path(cls, path: str | Path, workdir: str | Path,
                 config: Any = None) -> "LSPClient | None":
        """Return (creating if needed) the client for a file, or None when
        no server handles it, LSP is disabled, or the server is broken.

        Raises LSPNotInstalled when the server binary is missing.
        """
        entry = detect_server(path, config)
        if entry is None:
            return None
        root = Path(workdir).resolve()
        key = _client_key(root, entry.name)
        broken_at = _broken.get(key)
        if broken_at is not None:
            if time.monotonic() - broken_at < _BROKEN_TTL:
                return None
            del _broken[key]
        client = _clients.get(key)
        if client is None:
            command = resolve_command(entry, config)
            if command is None:
                raise LSPNotInstalled(f"not installed: {entry.install_hint}")
            client = cls(entry.name, entry, root, command, config)
            _clients[key] = client
        return client

    def _mark_broken(self) -> None:
        _broken[self._key] = time.monotonic()
        _clients.pop(self._key, None)

    async def _ensure(self) -> None:
        """Spawn + initialize the server if it is not already running."""
        proc = self.proc
        if proc is not None and proc.returncode is None and self.initialized:
            return
        async with self._spawn_lock:
            proc = self.proc
            if proc is not None and proc.returncode is None and self.initialized:
                return
            await self._spawn()

    async def _spawn(self) -> None:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(self.root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, FileNotFoundError) as exc:
            self._mark_broken()
            raise LSPNotInstalled(
                f"not installed: {self.entry.install_hint}"
            ) from exc
        self._reader_task = asyncio.ensure_future(self._read_loop())
        self._closing = False  # a fresh spawn is a fresh lifecycle
        try:
            await self._initialize()
        except Exception:
            await self.shutdown()
            self._mark_broken()
            raise

    async def _initialize(self) -> None:
        root_uri = self.root.as_uri()
        result = await self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "capabilities": {
                    "textDocument": {
                        "publishDiagnostics": {"relatedInformation": True},
                        "diagnostic": {"relatedInformation": True},
                    },
                    "workspace": {"workspaceFolders": True},
                },
                "workspaceFolders": [{"uri": root_uri, "name": self.root.name}],
            },
            timeout=_REQUEST_TIMEOUT,
            _ensure=False,  # _spawn already holds the spawn lock
        )
        _ = result
        await self._notify("initialized", {}, _ensure=False)
        self.initialized = True

    async def shutdown(self) -> None:
        """Best-effort teardown: stop the reader, kill the process."""
        self._closing = True  # the reader's disconnect must not mark broken
        task, self._reader_task = self._reader_task, None
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:
                pass  # e.g. task belongs to an already-closed loop
        proc, self.proc = self.proc, None
        self.initialized = False
        for fut in self._pending.values():
            if not fut.done():
                try:
                    fut.cancel()
                except Exception:
                    pass
        self._pending.clear()
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass

    # -- wire ------------------------------------------------------------

    async def _request(self, method: str, params: dict,
                       timeout: float = _REQUEST_TIMEOUT,
                       _ensure: bool = True) -> Any:
        if _ensure:
            await self._ensure()
        assert self.proc is not None and self.proc.stdin is not None
        self._next_id += 1
        rid = self._next_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        try:
            await write_message(
                self.proc.stdin, {"jsonrpc": "2.0", "id": rid,
                                  "method": method, "params": params}
            )
        except (ConnectionError, BrokenPipeError, asyncio.IncompleteReadError) as exc:
            self._pending.pop(rid, None)
            raise LSPError(f"server connection lost during {method}") from exc
        try:
            response = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            raise LSPError(f"request {method} timed out after {timeout}s") from exc
        error = response.get("error")
        if error:
            raise LSPError(f"{method}: {error}")
        return response.get("result")

    async def _notify(self, method: str, params: dict,
                      _ensure: bool = True) -> None:
        if _ensure:
            await self._ensure()
        assert self.proc is not None and self.proc.stdin is not None
        try:
            await write_message(
                self.proc.stdin,
                {"jsonrpc": "2.0", "method": method, "params": params},
            )
        except (ConnectionError, BrokenPipeError) as exc:
            raise LSPError(f"server connection lost during {method}") from exc

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while True:
                try:
                    msg = await read_message(self.proc.stdout)
                except ProtocolError:
                    continue  # skip one malformed frame, keep listening
                if msg is None:
                    break  # clean EOF: server exited
                self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            self._on_disconnect()

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        method = msg.get("method")
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params", {}) or {}
            uri = params.get("uri", "")
            self._pushes[uri] = params.get("diagnostics", []) or []
            event = self._push_events.get(uri)
            if event is not None and not event.is_set():
                event.set()
        # Other notifications (window/logMessage, $/progress, ...) are ignored.

    def _on_disconnect(self) -> None:
        """The server went away: fail pending requests, remember breakage."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(LSPError("server exited"))
        self._pending.clear()
        self.initialized = False
        if not self._closing:
            self._mark_broken()
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass

    # -- documents ---------------------------------------------------------

    @staticmethod
    def _uri(path: str | Path) -> str:
        return Path(path).resolve().as_uri()

    async def did_open(self, path: str | Path) -> None:
        """Tell the server about a file's current text (open or full change)."""
        p = Path(path).resolve()
        uri = p.as_uri()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LSPError(f"cannot read {p}: {exc}") from exc
        prev = self._opened.get(uri)
        if prev is None:
            await self._notify(
                "textDocument/didOpen",
                {"textDocument": {
                    "uri": uri,
                    "languageId": self.entry.language_id or "plaintext",
                    "version": 0,
                    "text": text,
                }},
            )
            self._opened[uri] = (0, text)
        elif prev[1] != text:
            version = prev[0] + 1
            await self._notify(
                "textDocument/didChange",
                {"textDocument": {"uri": uri, "version": version},
                 "contentChanges": [{"text": text}]},
            )
            self._opened[uri] = (version, text)
        # unchanged: nothing to send

    # -- diagnostics ---------------------------------------------------------

    def _normalize(self, path: str | Path, item: dict) -> dict:
        start = (item.get("range") or {}).get("start") or {}
        return {
            "path": str(Path(path)),
            "line": int(start.get("line", 0)) + 1,
            "col": int(start.get("character", 0)) + 1,
            "severity": _SEVERITY.get(item.get("severity", 1), "error"),
            "message": str(item.get("message", "")),
            "source": str(item.get("source", "") or ""),
        }

    async def diagnostics(self, path: str | Path) -> list[dict]:
        """Diagnostics for a file: pull API first, push fallback for 2s."""
        uri = self._uri(path)
        self._pushes.pop(uri, None)
        event = asyncio.Event()
        self._push_events[uri] = event
        try:
            await self.did_open(path)  # the open itself triggers pushes
            result = None
            try:
                result = await self._request(
                    "textDocument/diagnostic",
                    {"textDocument": {"uri": uri}},
                )
            except LSPError:
                result = None  # server may not implement pull; use pushes
            items = (result or {}).get("items") if result else None
            if items is not None:
                return [self._normalize(path, it) for it in items]
            try:
                await asyncio.wait_for(event.wait(), timeout=_PUSH_WAIT)
            except asyncio.TimeoutError:
                return []
            return [self._normalize(path, it)
                    for it in self._pushes.get(uri, [])]
        finally:
            self._push_events.pop(uri, None)


async def get_diagnostics(path: str | Path, workdir: str | Path,
                          config: Any = None) -> tuple[list[dict], str]:
    """Diagnostics for a file, never raising.

    Returns (diagnostics, note); note is "" on success and a clean
    human-readable message ("not installed: ...", "[LSP] ...") otherwise.
    """
    try:
        client = LSPClient.for_path(path, workdir, config)
    except LSPNotInstalled as exc:
        return [], str(exc)
    if client is None:
        return [], ""  # no server for this language, disabled, or broken
    try:
        return await client.diagnostics(path), ""
    except LSPNotInstalled as exc:
        return [], str(exc)
    except LSPError as exc:
        return [], f"[LSP] diagnostics failed: {exc}"
    except Exception as exc:  # noqa: BLE001 - diagnostics must never crash callers
        return [], f"[LSP] diagnostics failed: {type(exc).__name__}: {exc}"
