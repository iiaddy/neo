"""Linux bubblewrap argv builder — the heart of the sandbox.

Mount strategy (later mounts shadow earlier ones, so order matters):

1. ``--ro-bind / /``            whole filesystem read-only
2. ``--dev /dev --proc /proc``  minimal device/proc views
3. ``--tmpfs /tmp``             private scratch (or shared ``--bind``)
4. ``--bind <w> <w>``           every allow_write entry, read-write
5. denies: ``deny_read`` dirs → ``--tmpfs`` (content hidden);
   ``deny_read`` files → empty file bound read-only over them;
   ``deny_write`` paths → bound read-only over themselves
6. ``--chdir`` + ``--clearenv`` + ``--setenv`` for the whitelisted env
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from .config import SANDBOX_HTTP_PROXY_PORT, SANDBOX_SOCKS_PROXY_PORT, SandboxConfig
from .policy import resolved_paths, sanitize_env

_EMPTY_NAME = "empty"


def empty_file_path() -> Path:
    """Zero-byte file used to mask deny_read files (created on demand)."""
    path = Path.home() / ".neo" / "sandbox" / _EMPTY_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"")
    try:
        os.chmod(path, 0o444)
    except OSError:
        pass
    return path


def build_bwrap_argv(
    cfg: SandboxConfig,
    bwrap: str,
    workdir: Path,
    command: str,
    *,
    bridge_sock_dir: str | None = None,
    extra_env: dict | None = None,
    pidns: bool = True,
) -> tuple[list[str], dict[str, str]]:
    """Return ``(argv_prefix, env)``; caller appends ``["--", shell, "-c", inner]``.

    ``bridge_sock_dir`` enables filtered-network mode: the host-side socat
    sockets are bound into the sandbox and an in-sandbox socat listener is
    started by the wrapped shell command (see runner.py). ``pidns=False``
    degrades gracefully on kernels that forbid a fresh /proc mount.
    """
    workdir = workdir.resolve()
    argv: list[str] = [
        bwrap,
        "--new-session",
        "--die-with-parent",
        "--unshare-user",
        "--unshare-ipc",
    ]
    if pidns:
        argv.append("--unshare-pid")
    argv += [
        "--cap-drop",
        "ALL",
        # 1. whole tree read-only
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
    ]
    if pidns:
        argv += ["--proc", "/proc"]
    argv += [
        "--bind-try",
        "/dev/shm",
        "/dev/shm",
    ]
    if cfg.network in ("none", "filtered"):
        argv.append("--unshare-net")

    # 3. /tmp
    if cfg.private_tmp:
        argv += ["--tmpfs", "/tmp"]
    else:
        argv += ["--bind", "/tmp", "/tmp"]

    # 4. writable areas (mkdir -p first so --bind never fails)
    bound: set[str] = set()
    for entry in cfg.allow_write:
        for path in resolved_paths(entry, workdir):
            # Resolve symlinks like the deny lists do: binding the literal
            # path would let the kernel follow a planted symlink (e.g.
            # "data" -> /etc) and mount the target read-write — a sandbox
            # escape. Anything resolving outside the workdir is skipped.
            try:
                real = path.resolve()
            except OSError:
                continue
            if real != workdir and workdir not in real.parents:
                continue
            key = str(real)
            if key in bound:
                continue
            bound.add(key)
            try:
                real.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            if real.exists():
                argv += ["--bind", key, key]

    # 5a. deny_read — hide content
    empty = empty_file_path()
    for entry in cfg.deny_read:
        for path in resolved_paths(entry, workdir):
            if not path.exists():
                continue
            if path == workdir or workdir.is_relative_to(path):
                # never hide the workdir itself from the agent
                continue
            if path.is_dir() and not path.is_symlink():
                argv += ["--tmpfs", str(path)]
            else:
                argv += ["--ro-bind", str(empty), str(path)]

    # 5b. deny_write — re-mount read-only (reads still work, writes fail)
    for entry in cfg.deny_write:
        for path in resolved_paths(entry, workdir):
            if not path.exists():
                continue
            argv += ["--ro-bind", str(path), str(path)]

    # filtered-network bridge sockets
    if bridge_sock_dir is not None:
        argv += ["--bind", bridge_sock_dir, bridge_sock_dir]

    env = sanitize_env(cfg.pass_env, cfg.allow_secrets, extra_env)

    argv += ["--chdir", str(workdir), "--clearenv"]
    for key, value in env.items():
        argv += ["--setenv", key, value]
    return argv, env


def build_inner_command(command: str, bridge_sock_dir: str | None) -> str:
    """Shell snippet bwrap executes: optional socat listeners, then the command."""
    parts: list[str] = []
    if bridge_sock_dir is not None:
        http_sock = f"{bridge_sock_dir}/http.sock"
        socks_sock = f"{bridge_sock_dir}/socks.sock"
        parts.append(_LOOPBACK_UP)
        parts.append(
            "socat TCP-LISTEN:"
            f"{SANDBOX_HTTP_PROXY_PORT},fork,reuseaddr "
            f"UNIX-CONNECT:{shlex.quote(http_sock)} >/dev/null 2>&1 &"
        )
        parts.append(
            "socat TCP-LISTEN:"
            f"{SANDBOX_SOCKS_PROXY_PORT},fork,reuseaddr "
            f"UNIX-CONNECT:{shlex.quote(socks_sock)} >/dev/null 2>&1 &"
        )
    parts.append(f"exec /bin/sh -c {shlex.quote(command)}")
    # '&' already terminates a command — never emit '& ;' (syntax error).
    seq = " ".join(p if p.endswith("&") else f"{p};" for p in parts)
    return seq.removesuffix(";")


# A fresh --unshare-net namespace has lo DOWN; without it 127.0.0.1:3128
# is unreachable. Inside our own user+net namespace we hold CAP_NET_ADMIN,
# so SIOCSIFFLAGS works. `ip` when present, python ioctl fallback.
_LOOPBACK_UP = (
    "(ip link set lo up 2>/dev/null || "
    "python3 -c 'import socket,fcntl,struct;"
    "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
    "fcntl.ioctl(s,0x8914,struct.pack(\"16sh\",b\"lo\",65))' "
    "2>/dev/null || true)"
)


def proxy_env(port: int = SANDBOX_HTTP_PROXY_PORT) -> dict[str, str]:
    url = f"http://127.0.0.1:{port}"
    return {
        "http_proxy": url,
        "https_proxy": url,
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
    }
