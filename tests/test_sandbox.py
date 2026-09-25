"""Tests for the neo sandbox: policy, config, argv builder, proxy, planning."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from neo.sandbox.config import SandboxConfig, SandboxError, default_sandbox_config
from neo.sandbox.detect import detect_sandbox
from neo.sandbox.linux import build_bwrap_argv, build_inner_command, proxy_env
from neo.sandbox.policy import domain_allowed, expand_path, sanitize_env
from neo.sandbox.proxy import FilteringProxy
from neo.sandbox.runner import plan_sandbox


# ---------------------------------------------------------------- policy ---


@pytest.mark.parametrize(
    "host,allowed,denied,expected",
    [
        ("github.com", ["github.com"], [], True),
        ("api.github.com", ["*.github.com"], [], True),
        ("github.com", ["*.github.com"], [], False),  # wildcard != bare domain
        ("evil.com", ["github.com"], [], False),
        ("evil.com", [], [], True),  # empty allowlist = allow all
        ("evil.com", [], ["evil.com"], False),  # deny wins
        ("sub.evil.com", [], ["*.evil.com"], False),
        ("evil.com", ["evil.com"], ["evil.com"], False),  # deny wins over allow
        ("GitHub.COM", ["github.com"], [], True),  # case-insensitive
        ("github.com.", ["github.com"], [], True),  # trailing dot
        ("example.com:443", ["example.com"], [], True),  # port stripped
        ("1.2.3.4", ["1.2.3.4"], [], True),
    ],
)
def test_domain_allowed(host, allowed, denied, expected):
    assert domain_allowed(host, allowed, denied) is expected


def test_expand_path(tmp_path):
    assert expand_path("sub/dir", tmp_path) == tmp_path / "sub" / "dir"
    assert expand_path("/abs/x", tmp_path) == Path("/abs/x")
    assert str(expand_path("~/.ssh", tmp_path)).endswith(".ssh")


def test_sanitize_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MY_API_KEY", "shh")
    monkeypatch.setenv("GITHUB_TOKEN", "shh")
    env = sanitize_env(["PATH", "MY_API_KEY", "GITHUB_TOKEN"], [])
    assert env["PATH"] == "/usr/bin"
    assert "MY_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    # explicit opt-in restores
    env2 = sanitize_env(["PATH", "GITHUB_TOKEN"], ["GITHUB_TOKEN"])
    assert env2["GITHUB_TOKEN"] == "shh"


# ---------------------------------------------------------------- config ---


def test_config_from_dict_camel_and_snake():
    cfg = SandboxConfig.from_dict({"allowedDomains": ["x.com"], "deny_read": ["~/.ssh"]})
    assert cfg.allowed_domains == ["x.com"]
    assert cfg.deny_read == ["~/.ssh"]


def test_config_rejects_unknown_mode():
    with pytest.raises(SandboxError):
        SandboxConfig.from_dict({"mode": "yolo"})


def test_config_rejects_unknown_key():
    with pytest.raises(SandboxError):
        SandboxConfig.from_dict({"nope": 1})


def test_default_config_validates():
    SandboxConfig.from_dict(default_sandbox_config())


# ----------------------------------------------------------------- linux ---


def _cfg(**over):
    data = default_sandbox_config()
    data.update(over)
    return SandboxConfig.from_dict(data)


def test_argv_baseline_flags(tmp_path):
    argv, env = build_bwrap_argv(_cfg(network="none"), "bwrap", tmp_path, "echo hi")
    for flag in ("--new-session", "--die-with-parent", "--unshare-user",
                 "--unshare-ipc", "--unshare-pid", "--unshare-net",
                 "--cap-drop", "--ro-bind", "--dev", "--proc",
                 "--chdir", "--clearenv"):
        assert flag in argv, flag
    assert "--setenv" in argv
    # workdir bound read-write *after* the read-only root bind
    ro_root = argv.index("--ro-bind")
    bind_wd = argv.index("--bind")
    assert bind_wd > ro_root
    assert str(tmp_path.resolve()) in argv


def test_argv_full_network_shares_net(tmp_path):
    argv, _ = build_bwrap_argv(_cfg(network="full"), "bwrap", tmp_path, "echo hi")
    assert "--unshare-net" not in argv


def test_argv_deny_write_is_ro_bind(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("K=1")
    argv, _ = build_bwrap_argv(_cfg(deny_write=[".env"]), "bwrap", tmp_path, "echo hi")
    idx = argv.index(str(secret.resolve()))
    assert argv[idx - 1] == "--ro-bind"  # re-mounted read-only


def test_argv_deny_read_dir_is_tmpfs(tmp_path):
    ssh = Path.home() / ".ssh_test_neo"
    ssh.mkdir(exist_ok=True)
    try:
        argv, _ = build_bwrap_argv(
            _cfg(deny_read=[str(ssh)]), "bwrap", tmp_path, "echo hi")
        idx = argv.index(str(ssh.resolve()))
        assert argv[idx - 1] == "--tmpfs"
    finally:
        ssh.rmdir()


def test_argv_never_hides_workdir(tmp_path):
    argv, _ = build_bwrap_argv(
        _cfg(deny_read=["."]), "bwrap", tmp_path, "echo hi")
    # workdir must stay visible even if user denies "." — no tmpfs ON it
    wd = str(tmp_path.resolve())
    for i, arg in enumerate(argv):
        if arg == "--tmpfs":
            assert argv[i + 1] != wd


def test_inner_command_plain():
    inner = build_inner_command("echo hi", None)
    assert inner == "exec /bin/sh -c 'echo hi'"
    assert "socat" not in inner


def test_inner_command_filtered():
    inner = build_inner_command("echo hi", "/tmp/sockdir")
    assert "TCP-LISTEN:3128" in inner
    assert "TCP-LISTEN:1080" in inner
    assert "lo up" in inner


def test_proxy_env_vars():
    env = proxy_env()
    assert env["http_proxy"] == "http://127.0.0.1:3128"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:3128"


# ----------------------------------------------------------------- proxy ---


@pytest.mark.asyncio
async def test_proxy_allows_and_blocks():
    proxy = FilteringProxy(["example.com", "*.example.com"], ["bad.example.com"])
    assert proxy.check("example.com")
    assert proxy.check("sub.example.com")
    assert not proxy.check("bad.example.com")
    assert not proxy.check("other.com")

    port = await proxy.start()
    try:
        # blocked CONNECT -> 403
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com\r\n\r\n")
        await w.drain()
        resp = await asyncio.wait_for(r.read(200), 5)
        assert b"403" in resp
        w.close()
        assert proxy.blocked == 1
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_forwards_allowed():
    # upstream echo server pretending to be an allowed host
    async def _echo(r, w):
        data = await r.read(1024)
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await w.drain()
        w.close()

    upstream = await asyncio.start_server(_echo, "127.0.0.1", 0)
    uport = upstream.sockets[0].getsockname()[1]
    proxy = FilteringProxy(["127.0.0.1"], [])
    port = await proxy.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(
            f"GET http://127.0.0.1:{uport}/x HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{uport}\r\n\r\n".encode()
        )
        await w.drain()
        resp = await asyncio.wait_for(r.read(200), 5)
        assert b"200 OK" in resp and resp.endswith(b"ok")
        w.close()
    finally:
        await proxy.stop()
        upstream.close()


# ---------------------------------------------------------------- runner ---


def test_plan_off_returns_none(tmp_path):
    assert plan_sandbox(_cfg(mode="off"), tmp_path, "echo hi") is None


def test_plan_strict_fails_without_bwrap(tmp_path, monkeypatch):
    import neo.sandbox.runner as runner

    status = detect_sandbox()
    if status.available:
        pytest.skip("bwrap available here; strict-failure path not testable")
    monkeypatch.setattr(runner, "detect_sandbox", lambda: status)
    with pytest.raises(SandboxError):
        plan_sandbox(_cfg(mode="strict"), tmp_path, "echo hi")


def test_plan_auto_degrades_without_bwrap(tmp_path, monkeypatch):
    import neo.sandbox.runner as runner

    status = detect_sandbox()
    if status.available:
        pytest.skip("bwrap available here; degrade path not testable")
    monkeypatch.setattr(runner, "detect_sandbox", lambda: status)
    plan = plan_sandbox(_cfg(mode="auto"), tmp_path, "echo hi")
    assert plan is not None and not plan.sandboxed
    assert plan.warnings


def test_plan_sandboxed_when_available(tmp_path, monkeypatch):
    import neo.sandbox.runner as runner

    real = detect_sandbox()
    if not real.available:
        pytest.skip("bwrap not installed; cannot test live planning")
    plan = plan_sandbox(_cfg(mode="auto", network="none"), tmp_path, "echo hi")
    assert plan is not None and plan.sandboxed
    assert plan.argv_prefix[0].endswith("bwrap")


# ------------------------------------------------------- live integration ---

LIVE = shutil.which("bwrap") is not None


@pytest.mark.skipif(not LIVE, reason="bwrap not installed")
def test_live_echo(tmp_path):
    import subprocess

    cfg = _cfg(mode="strict", network="none")
    plan = plan_sandbox(cfg, tmp_path, "echo hello-sandbox")
    assert plan is not None and plan.sandboxed
    inner = build_inner_command("echo hello-sandbox", plan.bridge_sock_dir)
    argv = [*plan.argv_prefix, "--", "/bin/sh", "-c", inner]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    assert "hello-sandbox" in proc.stdout


@pytest.mark.skipif(not LIVE, reason="bwrap not installed")
def test_live_deny_write_enforced(tmp_path):
    import subprocess

    secret = tmp_path / ".env"
    secret.write_text("K=1")
    cfg = _cfg(mode="strict", network="none", deny_write=[".env"])
    plan = plan_sandbox(cfg, tmp_path, "echo hacked >> .env")
    inner = build_inner_command("echo hacked >> .env", plan.bridge_sock_dir)
    argv = [*plan.argv_prefix, "--", "/bin/sh", "-c", inner]
    subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert secret.read_text() == "K=1"  # write was blocked


@pytest.mark.skipif(not LIVE, reason="bwrap not installed")
def test_live_deny_read_enforced(tmp_path):
    import subprocess

    cfg = _cfg(mode="strict", network="none",
               deny_read=["~/.ssh"], allow_write=["."])
    home_ssh = Path(os.path.expanduser("~/.ssh"))
    if not home_ssh.exists():
        pytest.skip("no ~/.ssh on this machine")
    plan = plan_sandbox(cfg, tmp_path, "ls ~/.ssh")
    inner = build_inner_command("ls ~/.ssh", plan.bridge_sock_dir)
    argv = [*plan.argv_prefix, "--", "/bin/sh", "-c", inner]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    # hidden: empty dir listing or an error, but never real entries
    assert "id_rsa" not in proc.stdout and "id_ed25519" not in proc.stdout


# --- pidns degradation ------------------------------------------------------

def test_argv_pidns_degraded(tmp_path):
    argv, _ = build_bwrap_argv(_cfg(), "bwrap", tmp_path, "true", pidns=False)
    assert "--unshare-pid" not in argv
    assert "--proc" not in argv
    # everything else still isolated
    assert "--unshare-user" in argv and "--unshare-ipc" in argv
    assert "--ro-bind" in argv and "--cap-drop" in argv


def test_plan_warns_on_pidns_degraded(tmp_path):
    from neo.sandbox.detect import SandboxStatus
    status = SandboxStatus(available=True, bwrap="bwrap", socat="socat",
                           userns_ok=True, pidns_ok=False, filtered_net_ok=True)
    plan = plan_sandbox(_cfg(mode="strict", network="none"), tmp_path, "true",
                        status=status)
    assert plan.sandboxed and not plan.pidns
    assert any("pid" in w for w in plan.warnings)
    argv, _ = build_bwrap_argv(_cfg(), "bwrap", tmp_path, "true", pidns=plan.pidns)
    assert "--unshare-pid" not in argv


# --- inner command validity --------------------------------------------------

def test_inner_command_no_ampersand_semicolon():
    inner = build_inner_command("echo hi", "/tmp/neo-sandbox-x")
    assert "& ;" not in inner  # `& ;` is a shell syntax error
    # every backgrounded socat is still terminated by bare `&`
    assert inner.count("&") >= 2


def test_inner_command_valid_shell(tmp_path):
    import subprocess
    inner = build_inner_command("echo inner-ok", "/tmp/neo-sandbox-x")
    proc = subprocess.run(["/bin/sh", "-c", inner], capture_output=True,
                          text=True, timeout=10)
    # socat listeners fail (no sockets) but must not be a syntax error;
    # the final exec runs.
    assert "Syntax error" not in proc.stderr
    assert "inner-ok" in proc.stdout


def test_inner_command_no_bridge():
    inner = build_inner_command("echo hi", None)
    assert inner == "exec /bin/sh -c 'echo hi'"


# --- filtered proxy env stays on the fixed in-sandbox port --------------------

def test_plan_filtered_proxy_env_fixed_port(tmp_path):
    from neo.sandbox.detect import SandboxStatus
    status = SandboxStatus(available=True, bwrap="bwrap", socat="socat",
                           userns_ok=True, pidns_ok=True, filtered_net_ok=True)
    plan = plan_sandbox(_cfg(mode="strict", network="filtered"), tmp_path,
                        "true", status=status)
    assert plan.sandboxed and plan.network == "filtered"
    assert plan.bridge_sock_dir is not None
    assert plan.extra_env["http_proxy"] == "http://127.0.0.1:3128"
    assert plan.extra_env["HTTPS_PROXY"] == "http://127.0.0.1:3128"
    import shutil
    shutil.rmtree(plan.bridge_sock_dir, ignore_errors=True)


# --- invalid config fails closed ----------------------------------------------

def test_invalid_config_raises():
    with pytest.raises(SandboxError):
        SandboxConfig.from_dict({"mode": "bogus"})
    with pytest.raises(SandboxError):
        SandboxConfig.from_dict({"network": "sometimes"})
