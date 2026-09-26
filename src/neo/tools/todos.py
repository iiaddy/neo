"""neo tools — session todo list (todo_write / todo_read)."""

from __future__ import annotations

from types import SimpleNamespace

from .base import Tool, ToolContext, ToolResult

_VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")


def _todo_update_event(todos: list[dict]) -> object:
    try:
        from neo.events import TodoUpdate  # built later; import lazily

        return TodoUpdate(todos=[dict(t) for t in todos])
    except Exception:  # noqa: BLE001 - events module not available yet
        return SimpleNamespace(kind="todo_update", todos=[dict(t) for t in todos])


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "Replace the session todo list. Exactly one todo may be in_progress "
        "(extras are auto-demoted to pending). Emits a TodoUpdate event. "
        "When to use: 3+ step tasks, multi-part user requests, or new "
        "instructions arriving mid-task. Skip for single straightforward "
        "tasks. Rules: mark in_progress before starting work, completed only "
        "after the work (including verification) is actually done — never on "
        "intent. If blocked, keep it in_progress and add a follow-up todo "
        "for the blocker."
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Todo items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string"},
                        "priority": {"type": "string"},
                    },
                    "required": ["content", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["todos"],
        "additionalProperties": False,
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        raw = args.get("todos")
        if not isinstance(raw, list):
            return ToolResult(is_error=True, output="todos must be a list", title=self.name)
        todos: list[dict] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                return ToolResult(is_error=True, output=f"todo {i} is not an object", title=self.name)
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "")).strip()
            priority = str(item.get("priority", "") or "").strip() or None
            if not content:
                return ToolResult(is_error=True, output=f"todo {i} has empty content", title=self.name)
            if status not in _VALID_STATUSES:
                return ToolResult(
                    is_error=True,
                    output=f"todo {i}: invalid status {status!r}; must be one of {', '.join(_VALID_STATUSES)}",
                    title=self.name,
                )
            todo = {"content": content, "status": status}
            if priority:
                todo["priority"] = priority
            todos.append(todo)

        seen_in_progress = False
        demoted = 0
        for todo in todos:
            if todo["status"] == "in_progress":
                if seen_in_progress:
                    todo["status"] = "pending"
                    demoted += 1
                else:
                    seen_in_progress = True

        ctx.todos[:] = todos
        ctx.emit(_todo_update_event(ctx.todos))

        counts: dict[str, int] = {}
        for todo in todos:
            counts[todo["status"]] = counts.get(todo["status"], 0) + 1
        parts = [f"{c} {s}" for s, c in counts.items()]
        summary = f"{len(todos)} todo{'s' if len(todos) != 1 else ''}"
        if parts:
            summary += ": " + ", ".join(parts)
        if demoted:
            summary += f" ({demoted} demoted to pending)"
        return ToolResult(output=summary, title="todos")


class TodoReadTool(Tool):
    name = "todo_read"
    description = "Read the current session todo list."
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        if not ctx.todos:
            return ToolResult(output="No todos", title="todos")
        mark = {"pending": " ", "in_progress": ">", "completed": "x", "cancelled": "-"}
        lines = []
        for i, todo in enumerate(ctx.todos, 1):
            m = mark.get(todo.get("status", ""), "?")
            prio = todo.get("priority")
            tag = f" [{prio}]" if prio else ""
            lines.append(f"{i}. [{m}] {todo.get('content', '')}{tag} ({todo.get('status', '')})")
        return ToolResult(output="\n".join(lines), title="todos")
