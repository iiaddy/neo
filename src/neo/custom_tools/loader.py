"""Loader for user-defined tools from ``*.py`` files."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from ..tools.base import Tool


def _user_tools_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "neo" / "tools"


def _load_module(path: Path):
    """Import a module from an arbitrary file location."""
    # Make `from neo.tools.base import Tool` work inside the module even if
    # neo is only importable via the src layout (tests insert src on sys.path).
    module_name = f"neo_custom_tool_{path.stem}_{abs(hash(str(path))) % 10**8}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _is_tool_class(obj) -> bool:
    return (
        isinstance(obj, type)
        and issubclass(obj, Tool)
        and obj is not Tool
        and isinstance(getattr(obj, "name", None), str)
        and isinstance(getattr(obj, "description", None), str)
        and isinstance(getattr(obj, "parameters", None), dict)
    )


def discover_tools(workdir: str | Path) -> tuple[list[type[Tool]], list[str]]:
    """Discover custom tool classes.

    Scans ``.neo/tools/*.py`` (project) then ``~/.config/neo/tools/*.py``
    (user); a tool name defined in the project shadows the user's.

    Returns ``(classes, warnings)``: the tool *classes* (not instances —
    callers instantiate like the builtin registry does), plus one warning
    string per skipped module/class so callers can surface them.
    """
    warnings: list[str] = []
    classes: dict[str, type[Tool]] = {}
    for base in (Path(workdir) / ".neo" / "tools", _user_tools_dir()):
        if not base.is_dir():
            continue
        for py in sorted(base.glob("*.py")):
            if py.name.startswith("_"):
                continue
            try:
                module = _load_module(py)
            except Exception as exc:  # noqa: BLE001 - must never break startup
                warnings.append(f"{py.name}: import failed: {exc}")
                continue
            found = [v for v in vars(module).values() if _is_tool_class(v)]
            if not found:
                warnings.append(f"{py.name}: no Tool subclass defined")
                continue
            for cls in found:
                try:
                    cls()  # validate it instantiates
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{py.name}: {cls.__name__}() failed: {exc}")
                    continue
                if cls.name in classes:
                    warnings.append(
                        f"{py.name}: tool name '{cls.name}' already defined; skipped"
                    )
                    continue
                classes[cls.name] = cls
    return list(classes.values()), warnings
