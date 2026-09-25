"""neo agent roster — builtin agent definitions and config merging.

An agent def is a plain dict::

    {"description": str, "tools": [tool names], "system": str,
     "mode": "primary" | "subagent" | "all", "hidden": bool}

:func:`load_agents` merges user config over the builtins; :func:`toolset_for`
filters an instantiated tool dict down to what an agent may use.
"""

from .roster import AGENT_DEFS, load_agents, toolset_for

__all__ = ["AGENT_DEFS", "load_agents", "toolset_for"]
