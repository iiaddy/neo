"""neo tools — registry and toolset builder."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult
from .delegate import TaskTool
from .files import EditTool, GlobTool, GrepTool, ListDirTool, ReadTool, WriteTool
from .inquire import InquiryTool
from ..patch.applier import ApplyPatchTool
from ..plan.tools import PlanEnterTool, PlanExitTool
from .shell import BashTool
from .skill import SkillTool
from .todos import TodoReadTool, TodoWriteTool
from .undo import UndoTool
from .web import WebFetchTool, WebSearchTool

__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "TOOL_CLASSES",
    "build_toolset",
    "attach_mcp_tools",
]

TOOL_CLASSES: list[type[Tool]] = [
    ReadTool,
    ListDirTool,
    GlobTool,
    GrepTool,
    WriteTool,
    EditTool,
    ApplyPatchTool,
    BashTool,
    WebFetchTool,
    WebSearchTool,
    TodoWriteTool,
    TodoReadTool,
    TaskTool,
    InquiryTool,  # LLM name: "question"
    SkillTool,
    PlanEnterTool,  # LLM name: "plan_enter"
    PlanExitTool,  # LLM name: "plan_exit"
    UndoTool,
]


def attach_mcp_tools(tools: dict[str, Tool], manager) -> dict[str, Tool]:
    """Merge a started MCPManager's tools into an existing toolset.

    The manager must already be started (await manager.start()).
    Names colliding with existing tools are skipped (builtins win).
    """
    for name, tool in manager.tools().items():
        tools.setdefault(name, tool)
    return tools


def build_toolset(ctx: ToolContext, include: set[str] | None = None) -> dict[str, Tool]:
    """Instantiate tools for a session.

    `include` restricts to the given tool names (e.g. subagent toolsets);
    `ctx.config.disabled_tools` is always honored.
    """
    disabled = set(getattr(ctx.config, "disabled_tools", None) or [])
    tools: dict[str, Tool] = {}
    for cls in TOOL_CLASSES:
        tool = cls()
        if tool.name in disabled:
            continue
        if include is not None and tool.name not in include:
            continue
        tools[tool.name] = tool
    # Plugin-registered tools (ctx.plugins attached by cli.build_runtime).
    plugin_mgr = getattr(ctx, "plugins", None)
    if plugin_mgr is not None:
        for cls in plugin_mgr.tools():
            if cls.name in disabled:
                continue
            if include is not None and cls.name not in include:
                continue
            tools[cls.name] = cls()  # plugin tools may override builtins
    # Custom Python tools from .neo/tools/*.py (project shadows user config).
    try:
        from ..custom_tools import discover_tools
        custom, warnings = discover_tools(ctx.workdir)
    except Exception:
        custom, warnings = [], []
    import sys as _sys
    for w in warnings:
        print(f"[neo] custom tool skipped: {w}", file=_sys.stderr)
    for cls in custom:
        if cls.name in tools:  # builtin (or plugin) wins
            print(f"[neo] custom tool '{cls.name}' shadows an existing tool; skipped",
                  file=_sys.stderr)
            continue
        if cls.name in disabled:
            continue
        if include is not None and cls.name not in include:
            continue
        tools[cls.name] = cls()
    return tools
