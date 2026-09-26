"""Builtin agent definitions, config merging, and per-agent tool filtering."""

from __future__ import annotations

import copy

from ..prompts import load_prompt

_EXPLORE_SYSTEM = load_prompt("agents/explore")

_TITLE_SYSTEM = load_prompt("agents/title")

_SUMMARY_SYSTEM = load_prompt("agents/summary")

_BUILD_SYSTEM = load_prompt("agents/build")

_PLAN_SYSTEM = load_prompt("agents/plan")

AGENT_DEFS: dict[str, dict] = {
    "explore": {
        "description": "Read-only codebase search specialist; finds code and reports paths, never modifies anything.",
        "tools": ["read", "list_dir", "glob", "grep", "webfetch", "websearch"],
        "system": _EXPLORE_SYSTEM,
        "mode": "subagent",
        "hidden": False,
        "dynamic_tools": False,
    },
    "title": {
        "description": "Generates a <=50 char session title from the transcript.",
        "tools": [],
        "system": _TITLE_SYSTEM,
        "mode": "primary",
        "hidden": True,
        "dynamic_tools": False,
    },
    "summary": {
        "description": "Summarizes a session PR-style.",
        "tools": [],
        "system": _SUMMARY_SYSTEM,
        "mode": "primary",
        "hidden": True,
        "dynamic_tools": False,
    },
    "build": {
        "description": "Default agent: implements, verifies, and reports.",
        "tools": [
            "read", "list_dir", "glob", "grep",
            "write", "edit", "apply_patch", "bash",
            "webfetch", "websearch",
            "todo_write", "todo_read",
            "task", "question", "skill",
            "plan_enter", "plan_exit", "undo",
        ],
        "system": _BUILD_SYSTEM,
        "mode": "primary",
        "hidden": False,
    },
    "plan": {
        "description": "Plan mode: researches and designs; writes plan docs only, never code.",
        "tools": [
            "read", "list_dir", "glob", "grep",
            "webfetch", "websearch",
            "question", "plan_exit", "task",
            # write/edit are offered so the plan document can be saved;
            # plan_mode_allows restricts them to .neo/plans/ at runtime.
            "write", "edit",
        ],
        "system": _PLAN_SYSTEM,
        "mode": "primary",
        "hidden": False,
    },
}


def load_agents(config_agents: dict | None) -> dict[str, dict]:
    """Merge user config over the builtin agent defs.

    - ``{"name": {"disable": True}}`` removes a builtin.
    - Any other dict is merged field-by-field over the builtin of the
      same name (missing keys keep their builtin values).
    - A name with no builtin becomes a new agent; it should define
      ``description``, ``tools`` and ``system`` (missing fields get
      safe defaults).

    The builtin table is never mutated; a deep copy is returned.
    """
    agents = copy.deepcopy(AGENT_DEFS)
    for name, override in (config_agents or {}).items():
        if not isinstance(override, dict):
            continue
        if override.get("disable"):
            agents.pop(name, None)
            continue
        base = agents.get(name, {})
        merged = copy.deepcopy(base)
        for key, value in override.items():
            if key == "disable":
                continue
            merged[key] = copy.deepcopy(value)
        merged.setdefault("description", f"Custom agent '{name}'.")
        merged.setdefault("tools", [])
        merged.setdefault("system", "")
        merged.setdefault("mode", "all")
        merged.setdefault("hidden", False)
        agents[name] = merged
    return agents


def toolset_for(agent_name: str, all_tools: dict,
                agents: dict | None = None) -> dict:
    """Filter an instantiated tool dict down to one agent's toolset.

    Raises KeyError for an unknown agent name.

    Tools not listed in *any* builtin agent's tool list (dynamic tools:
    MCP `<server>_<tool>`, plugin tools, custom `.neo/tools/` tools) are
    included for agents whose def sets ``dynamic_tools`` (default True),
    so the build agent automatically picks up newly connected tools while
    read-only agents (explore/title/summary) stay locked down.
    """
    table = agents if agents is not None else AGENT_DEFS
    try:
        agent_def = table[agent_name]
    except KeyError:
        raise KeyError(f"unknown agent {agent_name!r}") from None
    wanted = agent_def["tools"]
    out = {name: tool for name, tool in all_tools.items() if name in wanted}
    if agent_def.get("dynamic_tools", True):
        known: set[str] = set()
        for d in table.values():
            known.update(d.get("tools", []))
        for name, tool in all_tools.items():
            if name not in known and name not in out:
                out[name] = tool
    return out
