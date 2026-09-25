"""Domain-filtering HTTP proxy for the sandbox's ``filtered`` network mode.

The sandbox runs with ``--unshare-net`` (no network at all). Host-side
socat bridges forward the in-sandbox listeners on 127.0.0.1:3128/1080 to
this proxy, which is the only way out. Every request's target host is
checked against ``allowed_domains`` / ``denied_domains``; anything else
gets a 403 and is logged.

Handles:
- ``CONNECT host:port`` (HTTPS, git+ssh-style tunnels, websockets)
- plain HTTP requests (absolute-URI form, as sent to a proxy)
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

log = logging.getLogger("neo.sandbox.proxy")

_BUFFER = 65536
_CONNECT_TIMEOUT = 20


class FilteringProxy:
    def __init__(self, allowed_domains: list, denied_domains: list):
        from .policy import domain_allowed  # local import: no cycles

        self._allowed = list(allowed_domains)
        self._denied = list(denied_domains)
        self._domain_allowed = domain_allowed
        self._server: asyncio.AbstractServer | None = None
        self.port: int = 0
        self.blocked: int = 0
        self.allowed_count: int = 0
        # Chain through the host's own egress proxy when one is configured
        # (corporate networks, sandboxed dev VMs). The domain check still
        # happens here first, so the policy is never weakened.
        self._upstream = _host_egress_proxy()
        self._upstream_no_proxy = _no_proxy_list()

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def check(self, host: str) -> bool:
        return self._domain_allowed(host, self._allowed, self._denied)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, OSError):
            writer.close()
            return
        try:
            request_line = head.split(b"\r\n", 1)[0].decode("latin1")
            method, target, _ = request_line.split(" ", 2)
        except ValueError:
            writer.close()
            return

        method = method.upper()
        if method == "CONNECT":
            host = target.split(":", 1)[0] if ":" in target else target
            port = int(target.rsplit(":", 1)[1]) if ":" in target else 443
            # swallow remaining headers already in `head`; body none for CONNECT
            await self._handle_connect(reader, writer, host, port)
        else:
            await self._handle_http(reader, writer, method, target, head)

    async def _open_upstream(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        """Connect to host:port directly, or tunnel via the host egress proxy."""
        if self._upstream is None or _bypassed(host, self._upstream_no_proxy):
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(host, port), _CONNECT_TIMEOUT
                )
            except OSError:
                return None
        # Chain: CONNECT through the host's egress proxy.
        try:
            proxy_host, proxy_port = self._upstream
            pr, pw = await asyncio.wait_for(
                asyncio.open_connection(proxy_host, proxy_port), _CONNECT_TIMEOUT
            )
        except OSError:
            return None
        try:
            pw.write(
                f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
            )
            await pw.drain()
            head = await asyncio.wait_for(pr.readuntil(b"\r\n\r\n"), _CONNECT_TIMEOUT)
        except (OSError, asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            pw.close()
            return None
        if not head.startswith(b"HTTP/1.1 200") and not head.startswith(b"HTTP/1.0 200"):
            pw.close()
            return None
        return pr, pw

    async def _handle_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
    ) -> None:
        if not self.check(host):
            await self._deny(writer, f"CONNECT {host}:{port}")
            return
        upstream = await self._open_upstream(host, port)
        if upstream is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        upstream_r, upstream_w = upstream
        self.allowed_count += 1
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await _relay(reader, writer, upstream_r, upstream_w)

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        head: bytes,
    ) -> None:
        # Proxy-form requests carry an absolute URI; origin-form needs Host:.
        parts = urlsplit(target)
        host = parts.hostname or ""
        port = parts.port or 80
        if not host:
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"host:"):
                    host = line.split(b":", 1)[1].decode("latin1").strip()
                    break
        host_only = host.split(":", 1)[0]
        if not host_only or not self.check(host_only):
            await self._deny(writer, f"{method} {target}")
            return
        if self._upstream is not None and not _bypassed(host_only, self._upstream_no_proxy):
            # Forward the absolute-URI request to the host egress proxy.
            proxy_host, proxy_port = self._upstream
            try:
                upstream_r, upstream_w = await asyncio.wait_for(
                    asyncio.open_connection(proxy_host, proxy_port), _CONNECT_TIMEOUT
                )
            except OSError:
                upstream_r = upstream_w = None
            if upstream_w is None:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            self.allowed_count += 1
            upstream_w.write(head)  # absolute URI form is what proxies expect
            await upstream_w.drain()
            await _relay(reader, writer, upstream_r, upstream_w)
            return
        try:
            opened = await self._open_upstream(host_only, port)
        except OSError:
            opened = None
        if opened is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        upstream_r, upstream_w = opened
        self.allowed_count += 1
        # Re-emit origin-form so the upstream server is happy.
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        lines = head.split(b"\r\n")
        lines[0] = f"{method} {path} HTTP/1.1".encode("latin1")
        # `head` already ends with \r\n\r\n, so the join reconstructs the
        # headers byte-for-byte (no extra terminator).
        upstream_w.write(b"\r\n".join(lines))
        await upstream_w.drain()
        await _relay(reader, writer, upstream_r, upstream_w)

    async def _deny(self, writer: asyncio.StreamWriter, what: str) -> None:
        self.blocked += 1
        log.warning("sandbox proxy blocked: %s", what)
        body = b"blocked by sandbox network policy"
        writer.write(
            b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()


async def _relay(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    upstream_r: asyncio.StreamReader,
    upstream_w: asyncio.StreamWriter,
) -> None:
    async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(_BUFFER)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                dst.close()
            except OSError:
                pass

    await asyncio.gather(
        _pump(client_r, upstream_w),
        _pump(upstream_r, client_w),
    )


def _host_egress_proxy() -> tuple[str, int] | None:
    """The host's own egress proxy, if configured (and not pointing at us)."""
    import os

    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        parts = urlsplit(raw if "://" in raw else "http://" + raw)
        host = parts.hostname or ""
        port = parts.port or 8080
        # Never chain into ourselves / the sandbox listeners.
        if host in ("127.0.0.1", "localhost", "::1"):
            continue
        return host, port
    return None


def _no_proxy_list() -> list[str]:
    import os

    raw = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
    return [h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip()]


def _bypassed(host: str, no_proxy: list[str]) -> bool:
    host = host.lower()
    return any(host == entry or host.endswith("." + entry) for entry in no_proxy)
