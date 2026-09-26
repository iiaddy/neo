"""neo custom_tools — user-defined tools loaded from Python files.

Tool files live in ``.neo/tools/*.py`` (project) and
``~/.config/neo/tools/*.py`` (user). Each module must define one or more
:class:`neo.tools.base.Tool` subclasses::

    from neo.tools.base import Tool, ToolContext, ToolResult

    class MyTool(Tool):
        name = "my_tool"
        description = "Does something useful."
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}

        async def run(self, args, ctx) -> ToolResult:
            return ToolResult(output="done", title=self.name)

Modules are imported from their file location (no package install needed).
A module that fails to import, defines no valid tools, or whose tool class
fails to instantiate is skipped with a warning — it never breaks startup.
"""

from __future__ import annotations

from .loader import discover_tools

__all__ = ["discover_tools"]
