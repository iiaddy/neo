"""Sandbox orchestration: decide, set up bridges, and build the final argv.

``plan_sandbox`` is pure: given config + workdir + command it returns a
``SandboxPlan`` describing exactly what will happen (or ``None`` when the
command should run directly). ``SandboxSession`` is an async context
manager that brings up the filtering proxy + socat bridges for the
``filtered`` network mode and tears them down afterwards.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import tempfile
from pathlib import Path

from .config import SandboxConfig, SandboxError
from .detect import SandboxStatus, detect_sandbox, install_hint
from .linux import build_bwrap_argv, build_inner_command, proxy_env


@dataclasses.dataclass
class SandboxPlan:
    sandboxed: bool
    network: str  # effective network mode
    workdir: str  # resolved working directory
    bwrap: str  # bwrap binary used
    pidns: bool  # new pid namespace + fresh /proc (False = degraded)
    argv_prefix: list[str]  # bwrap flags, ends before ["--", shell, "-c", inner]
    extra_env: dict[str, str]
    bridge_sock_dir: str | None
    warnings: list[str] = dataclasses.field(default_factory=list)


def plan_sandbox(
    cfg: SandboxConfig,
    workdir: Path,
    command: str,
    *,
    status: SandboxStatus | None = None,
) -> SandboxPlan | None:
    """Return a plan, or ``None`` when the command should run unsandboxed.

    Raises ``SandboxError`` in ``strict`` mode when sandboxing is impossible.
    """
    status = status or detect_sandbox()
    if cfg.mode == "off":
        return None
    if not status.available:
        msg = "sandbox unavailable: " + "; ".join(status.problems)
        if cfg.mode == "strict":
            raise SandboxError(msg + ". " + install_hint())
        return SandboxPlan(
            sandboxed=False,
            network="full",
            workdir=str(workdir.resolve()),
            bwrap=status.bwrap or "bwrap",
            pidns=status.pidns_ok,
            argv_prefix=[],
            extra_env={},
            bridge_sock_dir=None,
            warnings=[msg + " — running without sandbox"],
        )

    network = cfg.network
    warnings: list[str] = []
    bridge_sock_dir: str | None = None
    extra_env: dict[str, str] = {}

    if not status.pidns_ok:
        warnings.append("pid namespace unavailable — sandbox runs without pid isolation")

    if network == "filtered" and not status.filtered_net_ok:
        # socat missing: domain filtering impossible — fail closed, not open.
        msg = "socat missing: cannot do filtered network, falling back to 'none'"
        if cfg.mode == "strict":
            raise SandboxError(msg + ". Install socat or set sandbox.network.")
        warnings.append(msg)
        network = "none"

    if network == "filtered":
        bridge_sock_dir = tempfile.mkdtemp(prefix="neo-sandbox-")
        extra_env.update(proxy_env())

    argv_prefix, _env = build_bwrap_argv(
        dataclasses.replace(cfg, network=network),
        status.bwrap or "bwrap",
        workdir,
        command,
        bridge_sock_dir=bridge_sock_dir,
        extra_env=extra_env,
        pidns=status.pidns_ok,
    )
    return SandboxPlan(
        sandboxed=True,
        network=network,
        workdir=str(workdir.resolve()),
        bwrap=status.bwrap or "bwrap",
        pidns=status.pidns_ok,
        argv_prefix=argv_prefix,
        extra_env=extra_env,
        bridge_sock_dir=bridge_sock_dir,
        warnings=warnings,
    )


class SandboxSession:
    """Bring up proxy + socat bridges for a filtered-network plan.

    Usage::

        plan = plan_sandbox(cfg, workdir, command)
        async with SandboxSession(plan, cfg) as full_argv:
            proc = await asyncio.create_subprocess_exec(*full_argv, ...)

    ``full_argv`` is the complete command line including bwrap flags and
    the wrapped shell invocation.
    """

    def __init__(self, plan: SandboxPlan, cfg: SandboxConfig, command: str):
        self._plan = plan
        self._cfg = cfg
        self._command = command
        self._proxy = None
        self._bridges: list[asyncio.subprocess.Process] = []

    async def __aenter__(self) -> list[str]:
        plan = self._plan
        if not plan.sandboxed:
            raise SandboxError("SandboxSession entered for a non-sandboxed plan")
        try:
            if plan.network == "filtered":
                from .proxy import FilteringProxy

                proxy = FilteringProxy(self._cfg.allowed_domains, self._cfg.denied_domains)
                port = await proxy.start()
                self._proxy = proxy
                # Host side of each bridge: unix socket -> host proxy port.
                # (The sandbox side is a socat TCP-LISTEN on the fixed
                # 127.0.0.1:3128/1080, started by the wrapped shell command;
                # see linux.build_inner_command. The in-sandbox proxy env
                # therefore stays at the fixed port — only the bridge needs
                # the real host port, which it gets above.)
                assert plan.bridge_sock_dir is not None
                for name in ("http", "socks"):
                    sock = os.path.join(plan.bridge_sock_dir, f"{name}.sock")
                    bridge = await asyncio.create_subprocess_exec(
                        "socat",
                        f"UNIX-LISTEN:{sock},fork",
                        f"TCP:127.0.0.1:{port}",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    self._bridges.append(bridge)
            inner = build_inner_command(self._command, plan.bridge_sock_dir)
            return [*plan.argv_prefix, "--", "/bin/sh", "-c", inner]
        except BaseException:
            # Partial setup must not leak proxy/bridges/tempdirs.
            await self.__aexit__()
            raise

    async def __aexit__(self, *exc: object) -> None:
        for bridge in self._bridges:
            try:
                bridge.terminate()
            except ProcessLookupError:
                pass
        for bridge in self._bridges:
            try:
                await asyncio.wait_for(bridge.wait(), 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    bridge.kill()
                except ProcessLookupError:
                    pass
        self._bridges.clear()
        if self._proxy is not None:
            await self._proxy.stop()
            self._proxy = None
        if self._plan.bridge_sock_dir:
            shutil.rmtree(self._plan.bridge_sock_dir, ignore_errors=True)
            self._plan.bridge_sock_dir = None
