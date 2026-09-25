"""Sandbox capability detection: bwrap / socat presence and namespace probing."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess


@dataclasses.dataclass
class SandboxStatus:
    available: bool  # bwrap present and user namespaces work
    bwrap: str | None
    socat: str | None
    userns_ok: bool
    pidns_ok: bool  # new pid namespace + fresh /proc mount works
    filtered_net_ok: bool  # bwrap + socat + userns: domain-filtered net possible
    problems: list[str] = dataclasses.field(default_factory=list)


def detect_sandbox(bwrap_path: str | None = None, socat_path: str | None = None) -> SandboxStatus:
    problems: list[str] = []
    bwrap = bwrap_path or shutil.which("bwrap")
    if bwrap is None:
        problems.append("bwrap not found (install the 'bubblewrap' package)")
    socat = socat_path or shutil.which("socat")
    if socat is None:
        problems.append("socat not found (needed for filtered network mode)")

    userns_ok = False
    pidns_ok = False
    if bwrap is not None:
        userns_ok = _probe(bwrap, ["--unshare-user", "--dev-bind", "/", "/", "true"])
        if not userns_ok:
            problems.append("user namespaces unavailable (bwrap probe failed)")
        else:
            # Some hardened kernels/containers forbid mounting a fresh
            # /proc inside a new pid namespace — degrade, don't die.
            pidns_ok = _probe(bwrap, [
                "--unshare-user", "--unshare-pid",
                "--ro-bind", "/", "/", "--proc", "/proc",
                "--clearenv", "--", "/bin/sh", "-c", "true",
            ])
            if not pidns_ok:
                problems.append("pid namespace + /proc mount blocked; sandbox runs without pid isolation")

    available = bwrap is not None and userns_ok
    return SandboxStatus(
        available=available,
        bwrap=bwrap,
        socat=socat,
        userns_ok=userns_ok,
        pidns_ok=pidns_ok,
        filtered_net_ok=available and socat is not None,
        problems=problems,
    )


def _probe(bwrap: str, args: list[str]) -> bool:
    try:
        proc = subprocess.run(
            [bwrap, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def install_hint() -> str:
    return (
        "sandbox needs bubblewrap: "
        "`sudo apt install bubblewrap` (Debian/Ubuntu), "
        "`sudo dnf install bubblewrap` (Fedora), "
        "`brew install bubblewrap` (macOS is unsupported — Linux only). "
        "Filtered network mode also needs `socat`."
    )
