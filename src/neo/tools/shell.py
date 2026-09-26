"""neo tools — shell execution (bash), optionally inside the OS sandbox."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from ..sandbox.config import SandboxConfig, SandboxError
from ..sandbox.runner import SandboxPlan, SandboxSession, plan_sandbox
from .base import Tool, ToolContext, ToolResult

_OUTPUT_CAP = 60_000

# Upper bound for draining a process after SIGKILL (opencode: forceKillAfter
# 3s). A wedged child must not hang the tool forever.
_KILL_REAP_TIMEOUT = 3.0

# Degrade warnings ("bwrap missing — running without sandbox") are noisy if
# repeated on every call, so each distinct warning is shown once per process.
_WARNED: set[str] = set()


def _fresh_warnings(warnings: list[str]) -> list[str]:
    fresh = [w for w in warnings if w not in _WARNED]
    _WARNED.update(warnings)
    return fresh


def _sandbox_cfg(ctx: ToolContext) -> SandboxConfig:
    # Raises SandboxError on invalid config — the caller turns it into a
    # clean tool error. Never silently disable the sandbox on bad input.
    data = getattr(ctx.config, "sandbox", None) or {}
    return SandboxConfig.from_dict(data)


async def _direct_spawn(argv: list[str] | None, command: str, cwd: str):
    return await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # own process group so timeout kills children too
        env={**os.environ},
    )


async def _sandboxed_spawn(argv: list[str], command: str, cwd: str):
    assert argv is not None
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ},
    )


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command in a new process group. stdout and stderr are "
        "merged. On timeout the whole process group is killed. "
        "When the sandbox is enabled (neo.json), the command runs inside a "
        "bubblewrap sandbox: read-only filesystem except the workdir, "
        "sensitive paths hidden, network per policy. "
        "Usage: prefer the dedicated file tools (read/write/edit/glob/grep) "
        "over shell for file operations. Batch independent commands in one "
        "message so they run in parallel. Explain non-trivial commands "
        "before running them."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run (via /bin/sh)."},
            "timeout": {
                "type": "integer",
                "description": "Timeout in milliseconds.",
                "default": 120000,
            },
            "workdir": {
                "type": "string",
                "description": "Working directory (defaults to the session workdir).",
            },
            "pty": {
                "type": "boolean",
                "description": ("Run under a real PTY (for interactive commands "
                                "needing a terminal, e.g. prompts). Output keeps raw "
                                "ANSI. Cannot be combined with the sandbox."),
                "default": False,
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        command = args["command"]
        raw_timeout = args.get("timeout", 120000)
        try:
            timeout_ms = int(raw_timeout)
        except (TypeError, ValueError):
            return ToolResult(
                is_error=True,
                output=f"invalid timeout: {raw_timeout!r} (must be an integer number of milliseconds)",
                title="bash", details={"exit_code": -1})
        if timeout_ms < 0:
            # A negative timeout used to be coerced to 1ms — an instant,
            # surprising timeout. Reject it like opencode's PositiveInt.
            return ToolResult(
                is_error=True,
                output=f"invalid timeout: {timeout_ms}ms (must be >= 0)",
                title="bash", details={"exit_code": -1})
        timeout_ms = max(1, timeout_ms)
        raw_wd = args.get("workdir")
        cwd = str(ctx.workdir if raw_wd is None else (Path(raw_wd).expanduser()))
        if raw_wd is not None and not Path(cwd).is_absolute():
            cwd = str(ctx.workdir / cwd)

        # --- sandbox planning -------------------------------------------------
        # Invalid config or strict-mode impossibility -> clean tool error,
        # never a silent unsandboxed run.
        try:
            cfg = _sandbox_cfg(ctx)
            plan = plan_sandbox(cfg, Path(cwd), command) if cfg.mode != "off" else None
        except SandboxError as exc:
            return ToolResult(is_error=True, output=f"sandbox: {exc}",
                              title="bash", details={"exit_code": -1})

        sandbox_note = ""
        argv: list[str] | None = None
        session: SandboxSession | None = None
        if plan is not None:
            fresh = _fresh_warnings(plan.warnings)
            if fresh:
                sandbox_note = "".join(f"[sandbox] {w}\n" for w in fresh)
            if plan.sandboxed:
                sandbox_note += f"[sandbox] active (network={plan.network})\n"

        # --- spawn -------------------------------------------------------------
        # PTY mode bypasses the sandbox argv (a PTY and bubblewrap's fd
        # juggling don't compose); interactive commands are the exceptional
        # case, so this is a clean opt-in, never a silent downgrade.
        use_pty = bool(args.get("pty", False))
        if use_pty:
            if plan is not None and plan.sandboxed:
                return ToolResult(is_error=True,
                                  output="pty mode cannot be combined with the sandbox",
                                  title="bash", details={"exit_code": -1})
            from ..pty_runner import run_pty
            text, exit_code = await run_pty(command, cwd=cwd,
                                            timeout_s=timeout_ms / 1000)
            output = text.rstrip("\n") or "(no output)"
            if len(text) > _OUTPUT_CAP:
                output = (text[:_OUTPUT_CAP].rstrip("\n") +
                          f"\n... (output capped at {_OUTPUT_CAP} chars)")
            return ToolResult(
                output=output,
                is_error=(exit_code or 0) != 0,
                title=f"bash[pty]: {command[:60]}",
                details={"exit_code": exit_code if exit_code is not None else -1,
                         "pty": True},
            )
        # session.__aenter__ is self-cleaning on failure; __aexit__ is
        # idempotent, so the finally always runs it exactly when needed.
        try:
            if plan is not None and plan.sandboxed:
                session = SandboxSession(plan, cfg, command)
                argv = await session.__aenter__()
            if argv is not None:
                proc = await _sandboxed_spawn(argv, command, cwd)
            else:
                proc = await _direct_spawn(None, command, cwd)
            timed_out = False
            reap_failed = False
            try:
                raw = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                # Bound the post-kill drain: a wedged child (e.g. stuck in
                # uninterruptible I/O) must not hang the tool forever.
                try:
                    raw = await asyncio.wait_for(proc.communicate(),
                                                 timeout=_KILL_REAP_TIMEOUT)
                except asyncio.TimeoutError:
                    reap_failed = True
                    raw = (b"", b"")
        finally:
            if session is not None:
                await session.__aexit__()

        text = raw[0].decode("utf-8", errors="replace") if raw and raw[0] else ""
        exit_code = proc.returncode if proc.returncode is not None else -1

        if timed_out:
            secs = timeout_ms / 1000
            out = text.strip()
            if out:
                out += "\n"
            out += f"timed out after {secs:g}s"
            if reap_failed:
                out += ("; the process did not exit within "
                        f"{_KILL_REAP_TIMEOUT:g}s of SIGKILL and may still be running")
            return ToolResult(is_error=True, output=sandbox_note + out, title="bash",
                              details={"exit_code": exit_code})

        truncated = False
        if len(text) > _OUTPUT_CAP:
            text = text[:_OUTPUT_CAP]
            truncated = True
        output = text.rstrip("\n")
        if truncated:
            output += f"\n... (output capped at {_OUTPUT_CAP} chars)"
        if not output:
            output = "(no output)"
        details: dict = {"exit_code": exit_code}
        if plan is not None and plan.sandboxed:
            details["sandboxed"] = True
            details["sandbox_network"] = plan.network
        return ToolResult(
            output=sandbox_note + output,
            is_error=exit_code != 0,
            title=f"bash: {command[:60]}",
            details=details,
        )
