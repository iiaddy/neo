"""Hook dispatch for neo plugins.

A hook point is a named moment in the agent loop where plugins can
observe and optionally mutate a payload dict. Handlers run in plugin
load order; each may return a dict of updates (merged into the payload)
or ``None`` (no change). Handlers may also mutate the payload in place.
One bad handler never stops the others -- its traceback is collected
into ``payload["_hook_errors"]``.
"""
from __future__ import annotations

import inspect
import traceback
from typing import Any, Callable

HOOK_POINTS: tuple[str, ...] = (
    "tool.execute.before",   # {"tool", "args"}            -- may modify args
    "tool.execute.after",    # {"tool", "args", "result"}   -- may modify result.output
    "permission.ask",        # {"tool", "target", "detail", "decision"}
    "chat.params",           # {"system", "tools", "model"} -- may modify
    "command.execute.before",  # {"name", "args"}
    "session.end",           # {"session_id", "reason"}
)

_ERRORS_KEY = "_hook_errors"

Handler = Callable[..., Any]


def _takes_api(fn: Callable) -> bool:
    """True if the handler accepts a second positional argument (the PluginAPI)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    positional = [
        p for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2 or any(
        p.kind == p.VAR_POSITIONAL for p in sig.parameters.values()
    )


async def _call_handler(fn: Handler, payload: dict, api: Any) -> Any:
    result = fn(payload, api) if _takes_api(fn) else fn(payload)
    if inspect.isawaitable(result):
        result = await result
    return result


class HookManager:
    """Registers and dispatches hook handlers in load order."""

    def __init__(self, api: Any = None) -> None:
        self._handlers: dict[str, list[Handler]] = {h: [] for h in HOOK_POINTS}
        self.api = api

    def register(self, hook: str, fn: Handler) -> None:
        """Register a handler. Raises ValueError/TypeError on misuse."""
        if hook not in self._handlers:
            raise ValueError(f"unknown hook point: {hook!r}")
        if not callable(fn):
            raise TypeError(f"hook handler for {hook!r} is not callable")
        self._handlers[hook].append(fn)

    def handler_count(self, hook: str) -> int:
        return len(self._handlers.get(hook, []))

    def has_handlers(self, hook: str) -> bool:
        return self.handler_count(hook) > 0

    async def emit(self, hook: str, payload: dict) -> dict:
        """Run the hook's handlers in registration order.

        Returns the (mutated) payload. A handler returning a dict has it
        merged into the payload; exceptions are collected into
        ``payload["_hook_errors"]`` and do not stop later handlers.
        """
        if hook not in self._handlers:
            raise ValueError(f"unknown hook point: {hook!r}")
        payload.setdefault(_ERRORS_KEY, [])
        for fn in list(self._handlers[hook]):
            try:
                update = await _call_handler(fn, payload, self.api)
            except Exception:
                payload[_ERRORS_KEY].append(traceback.format_exc(limit=3))
                continue
            if isinstance(update, dict):
                payload.update(update)
        return payload
