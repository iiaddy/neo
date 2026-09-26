"""neo tools — skill: load a discovered skill's content into context."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult


class SkillTool(Tool):
    name = "skill"
    description = (
        "Load a skill by name. Skills are discovered from .neo/skills/ and "
        "~/.config/neo/skills/; the name and description are listed in the "
        "system prompt, this tool loads the full content."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name."},
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        name = str(args.get("name", "")).strip()
        skills = ctx.skills or {}
        if not name or name not in skills:
            available = ", ".join(sorted(skills)) or "(none)"
            return ToolResult(
                is_error=True,
                output=f"unknown skill {name!r}; available: {available}",
                title=self.name,
            )
        content = skills[name]
        return ToolResult(
            output=f'<skill name="{name}">\n{content}\n</skill>',
            title=f"skill: {name}",
        )
