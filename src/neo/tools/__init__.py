"""neo tools — registry and toolset builder."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult
from .delegate import TaskTool
from .files import EditTool, GlobTool, GrepTool, ListDirTool, ReadTool, WriteTool
from .inquire import InquiryTool
from .shell import BashTool
from .skill import SkillTool
from .todos import TodoReadTool, TodoWriteTool
from .web import WebFetchTool, WebSearchTool

__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "TOOL_CLASSES",
    "build_toolset",
]

TOOL_CLASSES: list[type[Tool]] = [
    ReadTool,
    ListDirTool,
    GlobTool,
    GrepTool,
    WriteTool,
    EditTool,
    BashTool,
    WebFetchTool,
    WebSearchTool,
    TodoWriteTool,
    TodoReadTool,
    TaskTool,
    InquiryTool,  # LLM name: "question"
    SkillTool,
]


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
    return tools
