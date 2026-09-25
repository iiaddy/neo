"""PluginAPI -- the object handed to plugin hook handlers.

Deliberately small and stable: plugins get read access to the session's
workdir and config, an event emitter, a notice logger, and a validated
``register_tool`` helper. They do not get the agent harness, the
provider, or other plugins.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

from .. import events as E
from ..tools.base import Tool


class PluginAPI:
    """Limited, stable surface plugins interact with."""

    def __init__(self, *, workdir: Path, config: Any,
                 emit: Callable[[object], None]) -> None:
        self.workdir = workdir
        self.config = config
        self.emit = emit

    def log(self, msg: str, level: str = "info") -> None:
        """Emit a Notice event (visible in the UI / print frontend)."""
        self.emit(E.Notice(text=str(msg), level=level))

    def register_tool(self, cls: type) -> type:
        """Validate ``cls`` as a Tool subclass and return it.

        Raises ValueError if ``cls`` is not a Tool subclass or lacks the
        required class attributes (name, description, parameters).
        """
        if not (inspect.isclass(cls) and issubclass(cls, Tool)):
            raise ValueError(
                f"register_tool expects a Tool subclass, got {cls!r}")
        for attr in ("name", "description", "parameters"):
            if not getattr(cls, attr, None):
                raise ValueError(
                    f"tool {cls.__name__!r} must define class attr {attr!r}")
        if not getattr(cls, "name", "").replace("_", "").replace("-", "").isalnum():
            raise ValueError(
                f"tool {cls.__name__!r} has an invalid name {cls.name!r}")
        return cls
