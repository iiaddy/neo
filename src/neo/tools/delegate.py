"""neo tools — task (subagent delegation)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

from .base import Tool, ToolContext, ToolResult

_CHILD_TOOLSETS = {
    # agent kind -> tool names the child may call
    "general": None,  # resolved at runtime: every tool except task/todo_write
    "explore": {"read", "list_dir", "glob", "grep", "webfetch"},
}

_SUBAGENT_SUFFIX = (
    "\nYou are a subagent (role: {agent}). Solve the task you were given and "
    "only return results, do not ask questions. Keep your final answer "
    "concise and self-contained."
)

_BASE_SYSTEM = "You are neo, a terminal coding agent. Be direct and precise."


class TaskTool(Tool):
    name = "task"
    description = (
        "Delegate work to a subagent. The child runs with a restricted toolset "
        "and returns a text summary. Pass resume=<id> to poll a background task."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short label for the subtask."},
            "prompt": {"type": "string", "description": "Full instructions for the subagent."},
            "agent": {
                "type": "string",
                "description": 'Subagent kind: "general" or "explore".',
                "default": "general",
            },
            "background": {
                "type": "boolean",
                "description": "Run in the background; poll with resume.",
                "default": False,
            },
            "resume": {
                "type": "string",
                "description": "Background task id to poll for a result.",
            },
        },
        "required": ["description", "prompt"],
        "additionalProperties": False,
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        resume = args.get("resume")
        if resume:
            return self._poll(resume, ctx)

        agent = str(args.get("agent", "general") or "general")
        if agent not in _CHILD_TOOLSETS:
            return ToolResult(
                is_error=True,
                output=f"unknown agent {agent!r}; expected one of {', '.join(sorted(_CHILD_TOOLSETS))}",
                title=self.name,
            )
        if ctx.depth >= 2:
            return ToolResult(is_error=True, output="subagent depth limit reached", title=self.name)
        prompt = str(args.get("prompt", "")).strip()
        description = str(args.get("description", "")).strip()
        if not prompt:
            return ToolResult(is_error=True, output="prompt is empty", title=self.name)

        # Lazy imports: the agent loop (and providers) are built after tools.
        try:
            from neo.agent.loop import AgentHarness
        except Exception as exc:  # noqa: BLE001
            return ToolResult(is_error=True, output=f"agent loop unavailable: {exc}", title=self.name)

        from . import build_toolset  # lazy to avoid import cycles at module load

        child_ctx = ToolContext(
            workdir=ctx.workdir,
            config=ctx.config,
            permissions=ctx.permissions,
            gate=ctx.gate,
            emit=ctx.emit,
            todos=[],
            ui=None,
            locks=ctx.locks,
            depth=ctx.depth + 1,
            background=ctx.background,
            skills=ctx.skills,
        )
        include = _CHILD_TOOLSETS[agent]
        if include is None:
            exclude = {"task", "todo_write"}
            tools = build_toolset(child_ctx)
            tools = {k: v for k, v in tools.items() if k not in exclude}
        else:
            tools = build_toolset(child_ctx, include=include)

        provider, model = self._resolve_provider(ctx)

        async def _child() -> str:
            harness = AgentHarness(
                provider=provider,
                model=model,
                tools=tools,
                system=_BASE_SYSTEM + _SUBAGENT_SUFFIX.format(agent=agent),
                config=ctx.config,
                ctx=child_ctx,
            )
            text_parts: list[str] = []
            async for event in harness.run([{"role": "user", "content": prompt}]):
                kind = getattr(event, "kind", "")
                if kind == "text_delta":
                    text_parts.append(getattr(event, "text", ""))
                elif kind == "text_end":
                    full = getattr(event, "text", "")
                    if full:
                        text_parts = [full]
            answer = "".join(text_parts).strip() or "(subagent returned no text)"
            if len(answer) > 8000:
                answer = answer[:8000] + "\n... (truncated)"
            return f"Subagent '{agent}' result:\n{answer}"

        if args.get("background"):
            task_id = "bg_" + uuid.uuid4().hex[:12]
            task = asyncio.create_task(_child(), name=f"neo-task-{task_id}")
            ctx.background[task_id] = task
            return ToolResult(
                output=f"started background task {task_id}; use resume={task_id} to poll",
                title=f"task: {description or agent}",
                details={"task_id": task_id},
            )
        try:
            return ToolResult(output=await _child(), title=f"task: {description or agent}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(is_error=True, output=f"subagent failed: {type(exc).__name__}: {exc}", title=self.name)

    def _poll(self, task_id: str, ctx: ToolContext) -> ToolResult:
        task = ctx.background.get(task_id)
        if task is None:
            return ToolResult(is_error=True, output=f"unknown background task {task_id!r}", title=self.name)
        if isinstance(task, asyncio.Task):
            if not task.done():
                return ToolResult(output="still running", title=f"task {task_id}")
            try:
                result = task.result()
            except Exception as exc:  # noqa: BLE001
                result = f"Subagent failed: {type(exc).__name__}: {exc}"
            del ctx.background[task_id]
            return ToolResult(output=result, title=f"task {task_id}")
        # Already-completed value stored by someone else.
        del ctx.background[task_id]
        return ToolResult(output=str(task), title=f"task {task_id}")

    def _resolve_provider(self, ctx: ToolContext) -> tuple:
        try:
            from neo.providers import resolve_provider
            from neo.config import split_model

            provider_id, model = split_model(str(getattr(ctx.config, "model", "openai/gpt-4o")))
            return resolve_provider(provider_id, ctx.config), model
        except Exception:  # noqa: BLE001 - providers/config modules may not exist yet
            return None, str(getattr(ctx.config, "model", "unknown"))
