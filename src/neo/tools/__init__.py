"""neo tools — registry and toolset builder."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult
from .delegate import TaskTool
from .files import EditTool, GlobTool, GrepTool, ListDirTool, ReadTool, WriteTool
from .inquire import InquiryTool
from .shell import BashTool
from .skill import SkillTool
from .todos import TodoReadTool, TodoWriteTool
from .undo import UndoTool
from .web import WebFetchTool, WebSearchTool


def __getattr__(name: str):
    # Lazy imports: neo.patch.applier and neo.plan.tools both import
    # neo.tools.base, so importing them eagerly here would create a circular
    # import when they are the first neo module loaded.
    if name == "ApplyPatchTool":
        from ..patch.applier import ApplyPatchTool

        return ApplyPatchTool
    if name in ("PlanEnterTool", "PlanExitTool"):
        from ..plan import tools as _plan_tools

        return getattr(_plan_tools, name)
    if name == "TOOL_CLASSES":
        return _tool_classes()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _tool_classes() -> list[type[Tool]]:
    from ..patch.applier import ApplyPatchTool
    from ..plan.tools import PlanEnterTool, PlanExitTool

    return [
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

__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "TOOL_CLASSES",
    "build_toolset",
    "attach_mcp_tools",
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
    for cls in _tool_classes():
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
