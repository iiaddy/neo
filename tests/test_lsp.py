"""Tests for neo's LSP subsystem (protocol, servers, client, post-edit hook).

A fake LSP server (real stdio framing, real subprocess) is written to
tmp_path per test; no real language server is required.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from neo.lsp import (  # noqa: E402
    LSPClient,
    ProtocolError,
    after_edit,
    dedupe,
    detect_server,
    encode_message,
    get_diagnostics,
    read_message,
    reset_state,
    resolve_command,
    shutdown_all,
    snapshot_diagnostics,
)
from neo.lsp.protocol import IdGenerator  # noqa: E402
from neo.lsp.servers import SERVERS, all_servers, server_enabled  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _lsp_isolation():
    reset_state()
    yield
    # Backstop: hard-kill anything a failed test left behind (sync kill is
    # safe even though the test's event loop is already closed).
    from neo.lsp import client as _client_mod

    for c in list(_client_mod._clients.values()):
        try:
            if c.proc is not None and c.proc.returncode is None:
                c.proc.kill()
        except Exception:
            pass
    reset_state()


# ---------------------------------------------------------------- fake server


FAKE_SERVER = r'''
import json, os, sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "pull"  # pull | push | die
DIAG = {
    "range": {"start": {"line": 0, "character": 5},
              "end": {"line": 0, "character": 8}},
    "severity": 1,
    "message": "fake undefined name 'foo'",
    "source": "fake-lsp",
}

stdin = sys.stdin.buffer
stdout = sys.stdout.buffer


def read_message():
    headers = {}
    while True:
        line = stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers[b"content-length"])
    body = stdin.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def send(payload):
    body = json.dumps(payload).encode("utf-8")
    stdout.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stdout.flush()


def main():
    if MODE == "die":
        sys.exit(3)
    while True:
        msg = read_message()
        if msg is None:
            break
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"capabilities": {}}})
        elif method == "textDocument/didOpen":
            if MODE == "push":
                uri = msg["params"]["textDocument"]["uri"]
                send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                      "params": {"uri": uri, "diagnostics": [DIAG]}})
        elif method == "textDocument/diagnostic":
            if MODE == "pull":
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"kind": "full", "items": [DIAG]}})
            else:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": "Method not found"}})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": mid, "result": None})
        elif method == "exit":
            break


main()
'''


@pytest.fixture()
def fake_server(tmp_path):
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return script


def fake_config(script, mode="pull", name="fakeserver", ext=".fk"):
    return {
        "lsp": {
            "servers": {
                name: {
                    "command": [sys.executable, str(script), mode],
                    "extensions": [ext],
                    "language_id": "fake",
                    "install_hint": "no hint needed",
                }
            }
        }
    }


async def _scenario(coro_fn):
    try:
        return await coro_fn()
    finally:
        await shutdown_all()


# ---------------------------------------------------------------- protocol


def test_framing_roundtrip():
    async def go():
        reader = asyncio.StreamReader()
        payloads = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "method": "initialized", "params": {}}]
        blob = b"".join(encode_message(p) for p in payloads)
        # headers use CRLF framing
        assert blob.startswith(b"Content-Length:")
        assert b"\r\n\r\n" in blob
        reader.feed_data(blob)
        reader.feed_eof()
        first = await read_message(reader)
        second = await read_message(reader)
        assert await read_message(reader) is None  # clean EOF
        return first, second

    first, second = run(go())
    assert first["method"] == "initialize"
    assert second["method"] == "initialized"


def test_framing_unicode_body():
    body = {"message": "héllo wörld — ✓"}
    raw = encode_message(body)
    length = int(raw.split(b"\r\n")[0].split(b":")[1])
    assert length == len(json.dumps(body, separators=(",", ":")).encode("utf-8"))

    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        return await read_message(reader)

    assert run(go())["message"] == "héllo wörld — ✓"


def test_framing_missing_content_length():
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Type: application/json\r\n\r\n{}")
        with pytest.raises(ProtocolError):
            await read_message(reader)

    run(go())


def test_id_generator_monotonic():
    gen = IdGenerator()
    ids = [gen.next() for _ in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5


# ---------------------------------------------------------------- servers


def test_detect_server_builtin():
    assert detect_server("a.py").name == "pyright"
    assert detect_server("a.pyi").name == "pyright"
    assert detect_server("a.ts").name == "typescript-language-server"
    assert detect_server("a.go").name == "gopls"
    assert detect_server("a.rs").name == "rust-analyzer"
    assert detect_server("a.xyz") is None
    assert detect_server("Makefile") is None


def test_detect_server_disabled():
    cfg = {"lsp": {"servers": {"pyright": {"enabled": False}}}}
    assert detect_server("a.py", cfg) is None
    assert detect_server("a.go", cfg).name == "gopls"


def test_detect_server_global_disable():
    assert detect_server("a.py", {"lsp": {"enabled": False}}) is None


def test_detect_server_custom():
    cfg = {"lsp": {"servers": {"mine": {"command": ["x"],
                                        "extensions": [".mine"]}}}}
    assert detect_server("a.mine", cfg).name == "mine"
    assert "mine" in all_servers(cfg)
    assert "mine" not in all_servers(None)


def test_server_enabled_flag():
    assert server_enabled("pyright", None)
    assert not server_enabled("pyright", {"lsp": {"enabled": False}})


def test_resolve_command_prefers_basedpyright(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "basedpyright-langserver").write_text("#!/bin/sh\n")
    os.chmod(bindir / "basedpyright-langserver", 0o755)
    monkeypatch.setenv("PATH", str(bindir))
    argv = resolve_command(SERVERS["pyright"])
    assert argv == ["basedpyright-langserver", "--stdio"]


def test_resolve_command_falls_back_to_pyright(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "pyright-langserver").write_text("#!/bin/sh\n")
    os.chmod(bindir / "pyright-langserver", 0o755)
    monkeypatch.setenv("PATH", str(bindir))
    argv = resolve_command(SERVERS["pyright"])
    assert argv == ["pyright-langserver", "--stdio"]


def test_resolve_command_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-dir-xyz")
    assert resolve_command(SERVERS["pyright"]) is None


def test_resolve_command_config_override():
    cfg = {"lsp": {"servers": {"gopls": {"command": ["my-gopls", "-x"]}}}}
    assert resolve_command(SERVERS["gopls"], cfg) == ["my-gopls", "-x"]


# ---------------------------------------------------------------- client: pull


def test_diagnostics_pull(fake_server, tmp_path):
    cfg = fake_config(fake_server, "pull")
    target = tmp_path / "main.fk"
    target.write_text("x = foo\n", encoding="utf-8")

    async def go():
        diags, note = await get_diagnostics(str(target), tmp_path, cfg)
        assert note == ""
        assert len(diags) == 1
        d = diags[0]
        assert d["path"] == str(target)
        assert (d["line"], d["col"]) == (1, 6)  # 0-based LSP -> 1-based
        assert d["severity"] == "error"
        assert d["message"] == "fake undefined name 'foo'"
        assert d["source"] == "fake-lsp"
        # client reuse: second call hits the same spawned server
        c1 = LSPClient.for_path(str(target), tmp_path, cfg)
        c2 = LSPClient.for_path(str(target), tmp_path, cfg)
        assert c1 is c2

    run(_scenario(go))


def test_did_open_sends_did_change(fake_server, tmp_path):
    cfg = fake_config(fake_server, "pull")
    target = tmp_path / "main.fk"
    target.write_text("x = 1\n", encoding="utf-8")

    async def go():
        client = LSPClient.for_path(str(target), tmp_path, cfg)
        await client.did_open(str(target))
        uri = target.resolve().as_uri()
        assert client._opened[uri][0] == 0
        target.write_text("x = 2\n", encoding="utf-8")
        await client.did_open(str(target))
        assert client._opened[uri][0] == 1  # version bumped via didChange
        await client.did_open(str(target))  # unchanged: no resend
        assert client._opened[uri][0] == 1

    run(_scenario(go))


# ---------------------------------------------------------------- client: push


def test_diagnostics_push_fallback(fake_server, tmp_path):
    cfg = fake_config(fake_server, "push")
    target = tmp_path / "main.fk"
    target.write_text("x = foo\n", encoding="utf-8")

    async def go():
        start = time.monotonic()
        diags, note = await get_diagnostics(str(target), tmp_path, cfg)
        elapsed = time.monotonic() - start
        assert note == ""
        assert len(diags) == 1
        assert diags[0]["severity"] == "error"
        assert diags[0]["message"] == "fake undefined name 'foo'"
        assert elapsed < 10  # 2s push wait, not a hang

    run(_scenario(go))


# ---------------------------------------------------------------- client: failure modes


def test_missing_binary_clean_message(tmp_path):
    cfg = {"lsp": {"servers": {"nosuch": {
        "command": ["definitely-not-a-real-lsp-binary-xyz"],
        "extensions": [".zz"],
        "install_hint": "install it somehow",
    }}}}
    target = tmp_path / "a.zz"
    target.write_text("x\n", encoding="utf-8")

    async def go():
        diags, note = await get_diagnostics(str(target), tmp_path, cfg)
        assert diags == []
        assert note == "not installed: install it somehow"

    run(_scenario(go))


def test_no_server_for_extension(tmp_path):
    target = tmp_path / "a.unknownext"
    target.write_text("x\n", encoding="utf-8")

    async def go():
        diags, note = await get_diagnostics(str(target), tmp_path, None)
        assert diags == [] and note == ""

    run(_scenario(go))


def test_broken_server_remembered(fake_server, tmp_path):
    cfg = fake_config(fake_server, "pull")
    target = tmp_path / "main.fk"
    target.write_text("x = 1\n", encoding="utf-8")

    async def go():
        client = LSPClient.for_path(str(target), tmp_path, cfg)
        assert client is not None
        diags, _ = await get_diagnostics(str(target), tmp_path, cfg)
        assert len(diags) == 1
        # Simulate a crash.
        client.proc.kill()
        # Wait until the disconnect is noticed and the server is remembered.
        deadline = time.monotonic() + 10
        while LSPClient.for_path(str(target), tmp_path, cfg) is not None:
            if time.monotonic() > deadline:
                raise AssertionError("broken server was respawned")
            await asyncio.sleep(0.05)
        # Second call goes through the broken-memory path: fast, no hang.
        start = time.monotonic()
        diags2, note2 = await asyncio.wait_for(
            get_diagnostics(str(target), tmp_path, cfg), timeout=15)
        assert time.monotonic() - start < 10
        assert diags2 == [] and note2 == ""

    run(_scenario(go))


def test_server_exits_on_initialize(fake_server, tmp_path):
    cfg = fake_config(fake_server, "die")
    target = tmp_path / "main.fk"
    target.write_text("x = 1\n", encoding="utf-8")

    async def go():
        diags, note = await get_diagnostics(str(target), tmp_path, cfg)
        assert diags == []
        assert "diagnostics failed" in note  # clean, no traceback leak
        # ...and it is now remembered as broken
        assert LSPClient.for_path(str(target), tmp_path, cfg) is None

    run(_scenario(go))


# ---------------------------------------------------------------- postedit


def test_dedupe():
    old = [{"line": 1, "message": "boom"}, {"line": 9, "message": "old"}]
    new = [{"line": 1, "message": "boom"},
           {"line": 2, "message": "fresh"}]
    assert dedupe(new, old) == [{"line": 2, "message": "fresh"}]
    assert dedupe(new, []) == new


def test_after_edit_noop_returns_empty(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    note = run(after_edit(str(target), "x = 1\n", tmp_path, None))
    assert note == ""


def test_after_edit_disabled_returns_empty(tmp_path):
    cfg = {"lsp": {"enabled": False}, "format": {"enabled": False}}
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    note = run(after_edit(str(target), None, tmp_path, cfg))
    assert note == ""


def test_after_edit_reports_new_diagnostics(fake_server, tmp_path):
    cfg = {**fake_config(fake_server, "pull"),
           "format": {"enabled": False}}
    target = tmp_path / "main.fk"
    target.write_text("x = foo\n", encoding="utf-8")

    async def go():
        old = await snapshot_diagnostics(str(target), tmp_path, cfg)
        assert len(old) == 1
        # same diagnostics -> deduped to silence
        note = await after_edit(str(target), "x = 1\n", tmp_path, cfg,
                                old_diagnostics=old)
        assert note == ""
        # no baseline -> the diagnostic is reported
        note2 = await after_edit(str(target), "x = 1\n", tmp_path, cfg,
                                 old_diagnostics=[])
        assert "[LSP] new diagnostics after edit:" in note2
        assert "fake undefined name 'foo'" in note2
        assert f"{target}:1:6" in note2
        assert "error" in note2

    run(_scenario(go))


def test_after_edit_missing_server_is_quiet(tmp_path):
    cfg = {"format": {"enabled": False}}  # no servers configured at all
    target = tmp_path / "a.unknownext"
    target.write_text("x\n", encoding="utf-8")
    note = run(after_edit(str(target), None, tmp_path, cfg))
    assert note == ""
