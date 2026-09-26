"""Tests for neo's MCP subsystem (stdio + StreamableHTTP + manager).

Uses a REAL subprocess running a small fake MCP server written to tmp_path
— no mocks. HTTP tests use a real local HTTP server in a thread.

NOTE: every test performs all of its async work inside ONE asyncio.run()
call, because asyncio subprocess transports are bound to the loop that
created them.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from neo.mcp import (  # noqa: E402
    MCPClient,
    MCPError,
    MCPManager,
    StdioTransport,
    StreamableHTTPTransport,
    decode_message,
    encode_notification,
    encode_request,
    encode_response,
    parse_sse,
    sanitize_name,
)
from neo.mcp.client import client_from_config  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _gate(tool_name: str, target: str, detail: str) -> str:
    return "once"


def make_ctx(workdir: Path, **overrides) -> SimpleNamespace:
    base = dict(
        workdir=workdir,
        config=SimpleNamespace(verify_commands=[], disabled_tools=[]),
        permissions=None,
        gate=_gate,
        emit=lambda e: None,
        todos=[],
        ui=None,
        locks={},
        depth=0,
        background={},
        skills={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


FAKE_SERVER = textwrap.dedent(
    """\
    import json, os, sys

    TOOL_NAME = os.environ.get("FAKE_TOOL_NAME", "echo")

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    def result(rid, payload):
        send({"jsonrpc": "2.0", "id": rid, "result": payload})

    def main():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            method = msg.get("method")
            rid = msg.get("id")
            if method == "initialize":
                result(rid, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}, "prompts": {}},
                    "serverInfo": {"name": "fake-mcp", "version": "1.0.0"},
                })
            elif method == "tools/list":
                result(rid, {"tools": [
                    {"name": TOOL_NAME,
                     "description": "Echo back the arguments",
                     "inputSchema": {"type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"]}},
                    {"name": "fail",
                     "description": "Always fails",
                     "inputSchema": {"type": "object", "properties": {}}},
                ]})
            elif method == "tools/call":
                params = msg.get("params", {})
                if params.get("name") == TOOL_NAME:
                    args = params.get("arguments", {})
                    result(rid, {"content": [
                        {"type": "text",
                         "text": "echo:" + json.dumps(args, sort_keys=True)}]})
                elif params.get("name") == "fail":
                    result(rid, {"isError": True, "content": [
                        {"type": "text", "text": "boom went the tool"}]})
                else:
                    send({"jsonrpc": "2.0", "id": rid, "error":
                          {"code": -32602, "message": "unknown tool"}})
            elif method == "prompts/list":
                result(rid, {"prompts": [
                    {"name": "greet", "description": "Greet someone",
                     "arguments": [{"name": "name",
                                    "description": "Who to greet",
                                    "required": True}]}]})
            elif method == "prompts/get":
                who = msg.get("params", {}).get("arguments", {}).get("name", "?")
                result(rid, {"messages": [
                    {"role": "user",
                     "content": {"type": "text", "text": "Hello, %s!" % who}}]})
            # notifications/initialized and anything else: no reply

    main()
    """
)

HANG_SERVER = "import sys\nsys.stdin.read()\n"


def write_server(tmp_path: Path, name: str = "fake_mcp.py", body: str = FAKE_SERVER) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def stdio_client(tmp_path: Path, tool_name: str = "echo", timeout: float = 10.0,
                 body: str = FAKE_SERVER, name: str = "fake") -> MCPClient:
    srv = write_server(tmp_path, body=body)
    env = dict(os.environ)
    env["FAKE_TOOL_NAME"] = tool_name
    transport = StdioTransport([sys.executable, str(srv)], env=env)
    return MCPClient(transport, timeout=timeout, name=name)


# --- protocol ------------------------------------------------------------


def test_protocol_roundtrip():
    req = encode_request(1, "initialize", {"a": 1})
    assert decode_message(req).method == "initialize"
    notif = encode_notification("notifications/initialized")
    assert decode_message(notif).method == "notifications/initialized"
    resp = encode_response(1, {"ok": True})
    m = decode_message(resp)
    assert m.result == {"ok": True}
    err = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "nope"}}
    m = decode_message(err)
    assert m.code == -32601 and m.message == "nope"


def test_protocol_rejects_garbage():
    with pytest.raises(MCPError):
        decode_message({"hello": "world"})
    with pytest.raises(MCPError):
        decode_message({"jsonrpc": "1.0", "id": 1, "result": {}})


def test_parse_sse():
    body = 'event: message\ndata: {"a": 1}\n\ndata: [DONE]\n\ngarbage\n\ndata: not json\n'
    assert parse_sse(body) == [{"a": 1}]


def test_sanitize_name():
    assert sanitize_name("my-server_Weird.Tool!") == "my_server_weird_tool"
    assert sanitize_name("!!!") == "tool"
    assert sanitize_name("already_fine9") == "already_fine9"


# --- stdio client ----------------------------------------------------------


def test_handshake(tmp_path):
    async def main():
        client = stdio_client(tmp_path)
        try:
            await client.connect()
            assert client.connected
            assert client.protocol_version == "2024-11-05"
            assert client.server_info["name"] == "fake-mcp"
        finally:
            await client.close()

    run(main())


def test_list_tools(tmp_path):
    async def main():
        client = stdio_client(tmp_path)
        try:
            await client.connect()
            tools = await client.list_tools()
            by_name = {t["name"]: t for t in tools}
            assert "echo" in by_name and "fail" in by_name
            schema = by_name["echo"]["inputSchema"]
            assert schema["properties"]["text"]["type"] == "string"
            assert "Echo back" in by_name["echo"]["description"]
        finally:
            await client.close()

    run(main())


def test_call_tool_roundtrip(tmp_path):
    async def main():
        client = stdio_client(tmp_path)
        try:
            await client.connect()
            call = await client.call_tool("echo", {"text": "hello"})
            assert not call.is_error
            assert call.text == 'echo:{"text": "hello"}'
        finally:
            await client.close()

    run(main())


def test_call_tool_error_surfaces(tmp_path):
    async def main():
        client = stdio_client(tmp_path)
        try:
            await client.connect()
            call = await client.call_tool("fail", {})
            assert call.is_error
            assert "boom" in call.text
            with pytest.raises(MCPError):
                await client.call_tool("nope", {})
        finally:
            await client.close()

    run(main())


def test_prompts(tmp_path):
    async def main():
        client = stdio_client(tmp_path)
        try:
            await client.connect()
            prompts = await client.list_prompts()
            assert [p["name"] for p in prompts] == ["greet"]
            assert prompts[0]["arguments"][0]["name"] == "name"
            result = await client.get_prompt("greet", {"name": "Ada"})
            assert "Hello, Ada!" in client.prompt_text(result)
        finally:
            await client.close()

    run(main())


def test_bad_binary_gives_clean_error():
    async def main():
        transport = StdioTransport(["/nonexistent-neo-mcp-binary-xyz"])
        with pytest.raises(MCPError, match="not found|spawn"):
            await transport.connect()

    run(main())


def test_timeout_on_hung_server(tmp_path):
    async def main():
        srv = write_server(tmp_path, name="hang.py", body=HANG_SERVER)
        transport = StdioTransport([sys.executable, str(srv)])
        client = MCPClient(transport, timeout=2.0, name="hang")
        try:
            with pytest.raises(MCPError, match="timed out"):
                await client.connect()
        finally:
            await client.close()

    run(main())


def test_server_dying_mid_session(tmp_path):
    async def main():
        # Server that exits immediately: handshake must fail cleanly, not hang.
        srv = write_server(tmp_path, name="die.py", body="import sys\nsys.exit(3)\n")
        transport = StdioTransport([sys.executable, str(srv)])
        client = MCPClient(transport, timeout=5.0, name="die")
        try:
            with pytest.raises(MCPError):
                await client.connect()
        finally:
            await client.close()

    run(main())


# --- manager ---------------------------------------------------------------


def _manager(tmp_path, extra_servers=None, tool_name="echo", server_name="fake"):
    srv = write_server(tmp_path)
    env = dict(os.environ)
    env["FAKE_TOOL_NAME"] = tool_name
    servers = {
        server_name: {"command": [sys.executable, str(srv)], "env": env},
    }
    servers.update(extra_servers or {})
    return MCPManager({"servers": servers}, workdir=tmp_path)


def test_manager_start_stop_two_servers_one_bad(tmp_path):
    async def main():
        mgr = _manager(tmp_path, extra_servers={
            "bad": {"command": ["/nonexistent-neo-mcp-binary-xyz"]},
        })
        try:
            await mgr.start()
            assert "fake" in mgr.clients
            assert "bad" in mgr.errors  # isolated failure, recorded
            assert "fake" not in mgr.errors
            tools = mgr.tools()
            assert "fake_echo" in tools
            assert "fake_fail" in tools
            assert tools["fake_echo"].parameters["properties"]["text"]["type"] == "string"
        finally:
            await mgr.stop()
        assert mgr.clients == {}

    run(main())


def test_manager_tool_runs_end_to_end(tmp_path):
    ctx = make_ctx(tmp_path)

    async def main():
        mgr = _manager(tmp_path)
        try:
            await mgr.start()
            tool = mgr.tools()["fake_echo"]
            res = await tool({"text": "ping"}, ctx)
            assert not res.is_error
            assert res.output == 'echo:{"text": "ping"}'
            assert res.title == "echo"
        finally:
            await mgr.stop()

    run(main())


def test_tool_name_sanitization(tmp_path):
    async def main():
        mgr = _manager(tmp_path, tool_name="weird.tool-v2", server_name="my-server!")
        try:
            await mgr.start()
            # "my-server!" + "_" + "weird.tool-v2": "!" -> "_" plus the
            # "_" separator gives a double underscore — valid and stable.
            assert "my_server__weird_tool_v2" in mgr.tools()
        finally:
            await mgr.stop()

    run(main())


def test_prompt_commands_and_resolve(tmp_path):
    async def main():
        mgr = _manager(tmp_path)
        try:
            await mgr.start()
            cmds = mgr.prompt_commands()
            assert len(cmds) == 1
            cmd = cmds[0]
            assert cmd["name"] == "fake_greet"
            assert cmd["template"] == "<name>"
            text = await mgr.resolve_prompt("fake", "greet", {"name": "Ada"})
            assert text == "Hello, Ada!"
        finally:
            await mgr.stop()

    run(main())


def test_system_instructions(tmp_path):
    async def main():
        mgr = _manager(tmp_path, extra_servers={
            "bad": {"command": ["/nonexistent-neo-mcp-binary-xyz"]},
        })
        try:
            await mgr.start()
            text = mgr.system_instructions()
            assert text.startswith("## MCP instructions")
            assert "fake_echo" in text
            assert "bad" in text  # failed servers are reported, not hidden
        finally:
            await mgr.stop()

    run(main())


def test_manager_no_servers():
    async def main():
        mgr = MCPManager({})
        await mgr.start()
        assert mgr.tools() == {}
        assert "No MCP servers" in mgr.system_instructions()
        await mgr.stop()

    run(main())


def test_client_from_config_shapes(tmp_path):
    srv = write_server(tmp_path)
    c = client_from_config("s", {"command": [sys.executable, str(srv)]})
    assert isinstance(c.transport, StdioTransport)
    c = client_from_config("s", {"command": f"{sys.executable} {srv}"})
    assert isinstance(c.transport, StdioTransport)
    c = client_from_config("h", {"url": "http://127.0.0.1:9/mcp"})
    assert isinstance(c.transport, StreamableHTTPTransport)
    with pytest.raises(MCPError):
        client_from_config("x", {"type": "http"})
    with pytest.raises(MCPError):
        client_from_config("x", {"command": []})


# --- StreamableHTTP --------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    seen_session_headers: list = []

    def log_message(self, *a):
        pass

    def _send(self, status, body: bytes, ctype, extra=()):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        msg = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen_session_headers.append(self.headers.get("mcp-session-id"))
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            payload = {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-fake", "version": "1.0.0"}}}
            self._send(200, json.dumps(payload).encode(), "application/json",
                       [("mcp-session-id", "sess-abc")])
        elif method == "tools/list":
            payload = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "ping", "description": "pong",
                 "inputSchema": {"type": "object", "properties": {}}}]}}
            sse = ("data: " + json.dumps(payload) + "\n\n").encode()
            self._send(200, sse, "text/event-stream",
                       [("mcp-session-id", "sess-abc")])
        elif method == "tools/call":
            payload = {"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": "pong"}]}}
            self._send(200, json.dumps(payload).encode(), "application/json")
        else:
            # notifications etc: accept, no body needed
            self._send(202, b"{}", "application/json")


def _http_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_http_handshake_tools_and_session_id():
    async def main():
        server = _http_server()
        try:
            url = f"http://127.0.0.1:{server.server_port}/mcp"
            transport = StreamableHTTPTransport(url)
            client = MCPClient(transport, timeout=10.0, name="http-fake")
            await client.connect()
            try:
                assert client.server_info["name"] == "http-fake"
                assert transport.session_id == "sess-abc"
                tools = await client.list_tools()
                assert [t["name"] for t in tools] == ["ping"]
                call = await client.call_tool("ping", {})
                assert call.text == "pong" and not call.is_error
            finally:
                await client.close()
            # the tools/* requests (after initialize) echoed the session id back
            assert "sess-abc" in _Handler.seen_session_headers
        finally:
            server.shutdown()

    run(main())


def test_http_error_status_is_clean():
    class _Err(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = b"nope"
            self.send_response(500)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    async def main():
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Err)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/mcp"
            transport = StreamableHTTPTransport(url)
            client = MCPClient(transport, timeout=5.0, name="err")
            with pytest.raises(MCPError, match="500"):
                await client.connect()
        finally:
            server.shutdown()

    run(main())
