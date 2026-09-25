"""neo tools — shell execution (bash)."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from .base import Tool, ToolContext, ToolResult

_OUTPUT_CAP = 60_000


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command in a new process group. stdout and stderr are "
        "merged. On timeout the whole process group is killed."
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
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        command = args["command"]
        timeout_ms = max(1, int(args.get("timeout", 120000)))
        raw_wd = args.get("workdir")
        cwd = str(ctx.workdir if raw_wd is None else (Path(raw_wd).expanduser()))
        if raw_wd is not None and not Path(cwd).is_absolute():
            cwd = str(ctx.workdir / cwd)

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group so timeout kills children too
            env={**os.environ},
        )
        timed_out = False
        try:
            raw = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            raw = await proc.communicate()
        text = raw[0].decode("utf-8", errors="replace") if raw and raw[0] else ""
        exit_code = proc.returncode if proc.returncode is not None else -1

        if timed_out:
            secs = timeout_ms / 1000
            out = text.strip()
            if out:
                out += "\n"
            out += f"timed out after {secs:g}s"
            return ToolResult(is_error=True, output=out, title="bash", details={"exit_code": exit_code})

        truncated = False
        if len(text) > _OUTPUT_CAP:
            text = text[:_OUTPUT_CAP]
            truncated = True
        output = text.rstrip("\n")
        if truncated:
            output += f"\n... (output capped at {_OUTPUT_CAP} chars)"
        if not output:
            output = "(no output)"
        return ToolResult(
            output=output,
            is_error=exit_code != 0,
            title=f"bash: {command[:60]}",
            details={"exit_code": exit_code},
        )
