"""neo MCP — manager: config -> connected servers -> neo Tools.

Reads the `mcp` section of the neo config, connects to every enabled
server (one bad server never kills the others), and exposes the servers'
tools as neo Tool classes named `<server>_<tool>` plus their prompts as
slash-command definitions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from ..tools.base import Tool, ToolContext, ToolResult
from .client import MCPClient, client_from_config
from .protocol import MCPError

_SANITIZE = re.compile(r"[^a-z0-9_]+")

#: Sanitized names of live MCP tools, populated by MCPManager._register_tools.
#: Used by the permission layer (``agent/permissions.py``) to route MCP tools
#: to the "mcp" permission key.
MCP_TOOL_NAMES: set[str] = set()


def sanitize_name(raw: str) -> str:
    """Make a tool/command name safe for the LLM: [a-z0-9_], never empty."""
    name = _SANITIZE.sub("_", raw.lower()).strip("_")
    return name or "tool"


class _MCPTool(Tool):
    """Base for generated per-server tools. Subclasses carry ClassVars."""

    needs_approval: ClassVar[bool] = True

    def __init__(self, client: MCPClient, tool_name: str) -> None:
        self._client = client
        self._tool_name = tool_name

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        # Tool.__call__ already converts raises into error results, but we
        # keep the message MCP-specific for clarity.
        try:
            call = await self._client.call_tool(self._tool_name, args or {})
        except MCPError as exc:
            return ToolResult(is_error=True, output=str(exc), title=self.name)
        return ToolResult(
            output=call.text,
            is_error=call.is_error,
            title=self._tool_name,
        )


class MCPManager:
    """Owns MCP server lifecycles for one neo session."""

    def __init__(
        self, mcp_config: dict | None = None, workdir: str | Path | None = None
    ) -> None:
        cfg = mcp_config or {}
        self.servers: dict[str, dict] = dict(cfg.get("servers") or {})
        self.workdir = Path(workdir) if workdir is not None else None
        self.clients: dict[str, MCPClient] = {}
        self.errors: dict[str, str] = {}  # server -> human-readable failure
        self.server_info: dict[str, dict] = {}
        self._tools: dict[str, Tool] = {}  # sanitized tool name -> instance
        self._prompts: list[dict] = []  # slash-command defs
        self._started = False

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Connect to every enabled server. Failures are isolated."""
        if self._started:
            return
        self._started = True
        for name, scfg in self.servers.items():
            if not isinstance(scfg, dict):
                self.errors[name] = f"server config must be a dict, got {type(scfg).__name__}"
                continue
            if not scfg.get("enabled", True):
                continue
            try:
                client = client_from_config(name, scfg, self.workdir)
                await client.connect()
            except MCPError as exc:
                self.errors[name] = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - one bad server, never a crash
                self.errors[name] = f"unexpected error: {exc}"
                try:
                    await client.close()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass
                continue
            try:
                tool_defs = await client.list_tools()
                prompt_defs = await client.list_prompts()
            except MCPError as exc:
                self.errors[name] = str(exc)
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            except Exception as exc:  # noqa: BLE001
                self.errors[name] = f"unexpected error: {exc}"
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            self.clients[name] = client
            self.server_info[name] = client.server_info
            self._register_tools(name, client, tool_defs)
            self._register_prompts(name, client, prompt_defs)

    async def stop(self) -> None:
        """Close every connection. Idempotent; never raises."""
        for name, client in list(self.clients.items()):
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        self.clients.clear()
        self._started = False

    # -- tools ------------------------------------------------------------

    def _register_tools(
        self, server: str, client: MCPClient, tool_defs: list[dict]
    ) -> None:
        for t in tool_defs:
            raw = f"{server}_{t['name']}"
            name = sanitize_name(raw)
            base, i = name, 2
            while name in self._tools:  # de-dupe sanitization collisions
                name = f"{base}_{i}"
                i += 1
            params = t.get("inputSchema") or {}
            if not isinstance(params, dict) or params.get("type") != "object":
                params = {"type": "object", "properties": {}}
            MCP_TOOL_NAMES.add(name)
            description = t.get("description") or f"MCP tool {t['name']!r}"
            cls = type(
                f"MCPTool_{name}",
                (_MCPTool,),
                {
                    "name": name,
                    "description": f"[mcp:{server}] {description}",
                    "parameters": params,
                    "needs_approval": True,
                    "__doc__": f"MCP tool {t['name']!r} from server {server!r}.",
                },
            )
            self._tools[name] = cls(client, t["name"])

    def tools(self) -> dict[str, Tool]:
        """Instantiated neo tools, keyed by sanitized `<server>_<tool>` name."""
        return dict(self._tools)

    def tool_classes(self) -> list[type[Tool]]:
        """Tool classes (for registry-style consumers)."""
        return [type(t) for t in self._tools.values()]

    # -- prompts -> slash commands ----------------------------------------

    def _register_prompts(
        self, server: str, client: MCPClient, prompt_defs: list[dict]
    ) -> None:
        for p in prompt_defs:
            args = p.get("arguments") or []
            arg_names = [a.get("name") for a in args if a.get("name")]
            template = " ".join(f"<{n}>" for n in arg_names)
            name = sanitize_name(f"{server}_{p['name']}")
            self._prompts.append(
                {
                    "name": name,
                    "description": p.get("description")
                    or f"MCP prompt {p['name']!r} from {server!r}",
                    "server": server,
                    "prompt": p["name"],
                    "arguments": args,
                    "template": template,  # e.g. "<name> <style>"
                }
            )

    def prompt_commands(self) -> list[dict]:
        """Slash-command defs derived from MCP prompts."""
        return list(self._prompts)

    async def resolve_prompt(self, server: str, prompt: str, args: dict) -> str:
        """Resolve an MCP prompt to plain text for a slash-command run."""
        client = self.clients.get(server)
        if client is None:
            raise MCPError(f"MCP server {server!r} is not connected")
        result = await client.get_prompt(prompt, args)
        return client.prompt_text(result)

    # -- system prompt injection ------------------------------------------

    def system_instructions(self) -> str:
        """Text for injection into the agent's system prompt."""
        lines = ["## MCP instructions"]
        if not self.clients:
            lines.append("No MCP servers are connected.")
        else:
            lines.append(
                "The following MCP servers are connected. Their tools are "
                "available by name; prefer them when they fit the task."
            )
            for server in sorted(self.clients):
                info = self.server_info.get(server) or {}
                label = info.get("name") or server
                lines.append(f"\n### {label} (server `{server}`)")
                names = sorted(
                    n for n, t in self._tools.items()
                    if n.startswith(sanitize_name(server) + "_")
                )
                lines.append(
                    "Tools: " + (", ".join(f"`{n}`" for n in names) or "(none)")
                )
        if self.errors:
            lines.append("\nFailed MCP servers (skipped):")
            for server in sorted(self.errors):
                lines.append(f"- `{server}`: {self.errors[server]}")
        return "\n".join(lines)
