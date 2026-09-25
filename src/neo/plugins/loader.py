"""Plugin discovery and loading.

Scan order:
  1. ``<workdir>/.neo/plugins/*.py``   (project scope)
  2. ``~/.neo/plugins/*.py``            (global scope)
  3. any extra dirs (config ``plugins.dirs``; scope "extra")

Each module is loaded in isolation under the name
``neo_plugin_<stem>`` (scope-qualified on collision) and may expose::

    name  = "my-plugin"                    # optional; defaults to the file stem
    hooks = {"tool.execute.before": fn}    # fn sync or async; or a list of fns
    tools = [MyTool]                       # Tool subclasses

Deterministic order: project plugins first (sorted by stem), then
global (sorted by stem), then extras. A broken plugin is skipped with
a warning Notice -- it never takes the loader down with it.
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import events as E
from .api import PluginAPI
from .hooks import HOOK_POINTS

MODULE_PREFIX = "neo_plugin_"


@dataclass
class PluginRecord:
    """One successfully loaded plugin module."""

    name: str
    module_name: str
    path: Path
    scope: str  # "project" | "global" | "extra"
    hooks: dict  # hook name -> list of handlers
    tools: list  # validated Tool subclasses
    module: Any = None


class PluginLoader:
    """Discovers ``*.py`` plugin files and loads them in isolation."""

    def __init__(self, workdir: str | Path,
                 emit: Callable[[object], None] | None = None,
                 extra_dirs: Sequence[str | Path] | None = None,
                 api: PluginAPI | None = None) -> None:
        self.workdir = Path(workdir).resolve()
        self.emit = emit or (lambda e: None)
        self.extra_dirs = [Path(d) for d in (extra_dirs or [])]
        self.api = api or PluginAPI(workdir=self.workdir, config=None,
                                    emit=self.emit)
        self.records: list[PluginRecord] = []

    # -- discovery ------------------------------------------------------

    def plugin_dirs(self) -> list[tuple[str, Path]]:
        dirs = [("project", self.workdir / ".neo" / "plugins"),
                ("global", Path.home() / ".neo" / "plugins")]
        dirs.extend(("extra", d) for d in self.extra_dirs)
        return dirs

    def discover(self) -> list[tuple[str, Path]]:
        """(scope, path) for every plugin file, in deterministic load order."""
        found: list[tuple[str, Path]] = []
        for scope, d in self.plugin_dirs():
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.py"), key=lambda p: p.stem):
                if p.stem.startswith("_"):
                    continue
                found.append((scope, p))
        return found

    # -- loading --------------------------------------------------------

    def load(self) -> list[PluginRecord]:
        """Load every discovered plugin; skip failures with a warning."""
        self.records = []
        for scope, path in self.discover():
            record = self._load_one(scope, path)
            if record is not None:
                self.records.append(record)
        return self.records

    def reload(self) -> list[PluginRecord]:
        """Drop all loaded plugin modules from sys.modules and re-scan."""
        for name in [m for m in sys.modules if m.startswith(MODULE_PREFIX)]:
            sys.modules.pop(name, None)
        return self.load()

    def _load_one(self, scope: str, path: Path) -> PluginRecord | None:
        stem = path.stem
        module_name = f"{MODULE_PREFIX}{stem}"
        existing = sys.modules.get(module_name)
        if (existing is not None
                and getattr(existing, "__file__", None) != str(path)):
            # same stem in two scopes: keep them isolated
            module_name = f"{MODULE_PREFIX}{scope}_{stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot build module spec for {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            self._warn(f"plugin {path.name}: failed to load, skipping\n"
                       f"{traceback.format_exc(limit=3)}")
            return None

        name = getattr(module, "name", None) or stem
        hooks = self._read_hooks(module, path)
        tools = self._read_tools(module, path)
        return PluginRecord(name=name, module_name=module_name, path=path,
                            scope=scope, hooks=hooks, tools=tools,
                            module=module)

    def _read_hooks(self, module: Any, path: Path) -> dict[str, list]:
        raw = getattr(module, "hooks", None) or {}
        if not isinstance(raw, dict):
            self._warn(f"plugin {path.name}: 'hooks' must be a dict, ignoring it")
            return {}
        hooks: dict[str, list] = {}
        for hook, fns in raw.items():
            if hook not in HOOK_POINTS:
                self._warn(f"plugin {path.name}: unknown hook {hook!r}, ignoring it")
                continue
            fns = list(fns) if isinstance(fns, (list, tuple)) else [fns]
            good = [fn for fn in fns if callable(fn)]
            if len(good) != len(fns):
                self._warn(f"plugin {path.name}: hook {hook!r} has "
                           "non-callable entries, skipping them")
            if good:
                hooks[hook] = good
        return hooks

    def _read_tools(self, module: Any, path: Path) -> list:
        raw = getattr(module, "tools", None) or []
        if not isinstance(raw, (list, tuple)):
            self._warn(f"plugin {path.name}: 'tools' must be a list, ignoring it")
            return []
        tools = []
        for cls in raw:
            try:
                tools.append(self.api.register_tool(cls))
            except ValueError as exc:
                self._warn(f"plugin {path.name}: ignoring bad tool: {exc}")
        return tools

    def _warn(self, msg: str) -> None:
        self.emit(E.Notice(text=msg, level="warn"))
