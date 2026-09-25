"""neo plugin system.

Plugins are plain ``*.py`` files dropped into ``<project>/.neo/plugins/``
or ``~/.neo/plugins/``. Each module may expose::

    name  = "my-plugin"                    # optional; defaults to the file stem
    hooks = {"tool.execute.before": fn}    # sync or async callables (or lists)
    tools = [MyTool]                       # Tool subclasses

A hook handler is called as ``fn(payload)`` or ``fn(payload, api)``
depending on its arity; ``api`` is a :class:`PluginAPI`. A handler may
return a dict of payload updates (merged in) or ``None``; it may also
mutate the payload in place.

``PluginManager`` is constructed once per session and handed to the
agent loop via ``ctx.plugins`` (see ``INTEGRATION.md``). It owns the
``HookManager``, the ``PluginAPI``, and the list of plugin ``Tool``
subclasses.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .api import PluginAPI
from .hooks import HOOK_POINTS, HookManager
from .loader import PluginLoader, PluginRecord

__all__ = [
    "HOOK_POINTS",
    "PluginAPI",
    "HookManager",
    "PluginLoader",
    "PluginRecord",
    "PluginManager",
]


def _plugins_cfg(config: Any) -> dict:
    return getattr(config, "plugins", None) or {}


class PluginManager:
    """Session-scoped owner of plugins: loads modules, owns hooks + tools."""

    def __init__(self, workdir: str | Path, config: Any,
                 emit: Callable[[object], None] | None = None) -> None:
        self.workdir = Path(workdir).resolve()
        self.config = config
        self._emit = emit or (lambda e: None)
        self.api = PluginAPI(workdir=self.workdir, config=config,
                             emit=self._emit)
        self.hooks = HookManager(api=self.api)
        self.plugins: list[PluginRecord] = []
        self._tool_classes: list[type] = []
        extra = _plugins_cfg(config).get("dirs") or []
        self.loader = PluginLoader(self.workdir, emit=self._emit,
                                   extra_dirs=extra, api=self.api)
        if self.enabled:
            self._register_all()

    @property
    def enabled(self) -> bool:
        return bool(_plugins_cfg(self.config).get("enabled", True))

    def _register_all(self) -> None:
        for record in self.loader.load():
            self.plugins.append(record)
            for hook, fns in record.hooks.items():
                for fn in fns:
                    self.hooks.register(hook, fn)
            self._tool_classes.extend(record.tools)
            self.api.log(f"plugin loaded: {record.name} ({record.scope})")

    def tools(self) -> list[type]:
        """Tool subclasses contributed by plugins (deduped by name)."""
        seen: set[str] = set()
        out: list[type] = []
        for cls in self._tool_classes:
            if cls.name not in seen:
                seen.add(cls.name)
                out.append(cls)
        return out

    async def trigger(self, hook: str, payload: dict) -> dict:
        """Emit a hook through all registered handlers.

        When plugins are disabled this is a no-op returning the payload.
        """
        if not self.enabled:
            return payload
        return await self.hooks.emit(hook, payload)

    def reload(self) -> None:
        """Re-scan the plugin dirs and re-register (use between sessions)."""
        self.plugins = []
        self._tool_classes = []
        self.hooks = HookManager(api=self.api)
        if self.enabled:
            self._register_all()
