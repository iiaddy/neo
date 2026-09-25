"""neo tools — shared base types: result, context, and the Tool contract."""

from __future__ import annotations

import abc
import asyncio
import dataclasses
from pathlib import Path
from typing import Any, Awaitable, Callable, ClassVar


@dataclasses.dataclass
class ToolResult:
    """Outcome of a single tool invocation."""

    output: str
    is_error: bool = False
    title: str = ""  # short human label, e.g. "src/neo/cli.py:12-40"
    details: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ToolContext:
    """Everything a tool may need while it runs."""

    workdir: Path
    config: Any  # NeoConfig; typed as Any to avoid a circular import
    permissions: Any  # PermissionPolicy; typed as Any likewise
    gate: Callable[[str, str, str], Awaitable[str]]
    # async (tool_name, target, detail) -> "once" | "always" | "reject"
    emit: Callable[[object], None]  # push an agent-level event (sync callback)
    todos: list[dict]  # shared session todo list (mutated by todos.py)
    ui: Any = None  # optional; must provide `async ask(questions) -> dict` for inquiry
    locks: dict = dataclasses.field(default_factory=dict)  # path -> asyncio.Lock
    depth: int = 0  # subagent nesting depth
    background: dict = dataclasses.field(default_factory=dict)  # id -> asyncio.Task/result
    skills: dict = dataclasses.field(default_factory=dict)  # name -> skill content


class Tool(abc.ABC):
    """Base class for every tool neo can call."""

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict]  # JSON Schema for the LLM
    needs_approval: ClassVar[bool] = True  # False for pure readers

    @abc.abstractmethod
    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        """Execute the tool. Any exception is the subclass's own business
        only if it wants finer errors; otherwise the shared wrapper below
        converts it into an error ToolResult."""
        raise NotImplementedError

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            return await self.run(args, ctx)
        except Exception as exc:  # noqa: BLE001 - tools must never raise
            return ToolResult(
                is_error=True,
                output=f"{type(exc).__name__}: {exc}",
                title=self.name,
            )

    def schema(self) -> dict:
        """JSON-schema style declaration handed to the provider."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def path_lock(ctx: ToolContext, path: Path) -> asyncio.Lock:
    """Return (creating if needed) a per-path lock from ctx.locks."""
    key = str(Path(path).resolve()) if path.is_absolute() else str(path)
    lock = ctx.locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        ctx.locks[key] = lock
    return lock
