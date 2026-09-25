"""Tests for neo's agent roster: builtin defs, config merging,
and per-agent tool filtering."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from neo.agents import AGENT_DEFS, load_agents, toolset_for  # noqa: E402


ALL_TOOLS = {name: object() for name in (
    "read", "list_dir", "glob", "grep",
    "write", "edit", "apply_patch", "bash",
    "webfetch", "websearch",
    "todo_write", "todo_read",
    "task", "question", "skill",
    "plan_enter", "plan_exit", "undo",
)}


# --- builtin defs ------------------------------------------------------------


def test_builtin_agents_present():
    for name in ("explore", "title", "summary", "build", "plan"):
        assert name in AGENT_DEFS
        assert AGENT_DEFS[name]["description"]
        assert AGENT_DEFS[name]["system"]


def test_hidden_agents_flagged():
    assert AGENT_DEFS["title"]["hidden"] is True
    assert AGENT_DEFS["summary"]["hidden"] is True
    assert AGENT_DEFS["build"]["hidden"] is False


def test_explore_toolset_is_read_only():
    tools = toolset_for("explore", ALL_TOOLS)
    assert set(tools) == {"read", "list_dir", "glob", "grep",
                          "webfetch", "websearch"}
    for banned in ("write", "edit", "bash", "task", "question"):
        assert banned not in tools


def test_toolset_for_unknown_agent_raises():
    with pytest.raises(KeyError):
        toolset_for("nope", ALL_TOOLS)


def test_toolset_for_filters_correctly():
    build_tools = toolset_for("build", ALL_TOOLS)
    assert "write" in build_tools and "bash" in build_tools
    assert "plan_enter" in build_tools
    # build exits plan mode too: plan_enter without plan_exit traps the
    # harness in plan mode (plan-exit handoff becomes a build turn).
    assert "plan_exit" in build_tools
    assert "apply_patch" in build_tools and "undo" in build_tools

    plan_tools = toolset_for("plan", ALL_TOOLS)
    assert "plan_exit" in plan_tools
    assert "bash" not in plan_tools

    assert toolset_for("title", ALL_TOOLS) == {}
    assert toolset_for("summary", ALL_TOOLS) == {}


# --- load_agents -------------------------------------------------------------


def test_load_agents_disables_builtin():
    agents = load_agents({"explore": {"disable": True}})
    assert "explore" not in agents
    assert "build" in agents  # others untouched


def test_load_agents_overrides_fields():
    agents = load_agents({"build": {"description": "Custom build agent."}})
    assert agents["build"]["description"] == "Custom build agent."
    assert agents["build"]["tools"] == AGENT_DEFS["build"]["tools"]
    assert agents["build"]["system"] == AGENT_DEFS["build"]["system"]


def test_load_agents_adds_new_agent():
    agents = load_agents({
        "docs": {"description": "Docs writer.", "tools": ["read", "write"],
                 "system": "Write docs."},
    })
    assert agents["docs"]["description"] == "Docs writer."
    assert agents["docs"]["tools"] == ["read", "write"]
    filtered = {n: t for n, t in ALL_TOOLS.items()
                if n in agents["docs"]["tools"]}
    assert set(filtered) == {"read", "write"}


def test_load_agents_defaults_for_sparse_new_agent():
    agents = load_agents({"x": {"system": "hi"}})
    assert agents["x"]["tools"] == []
    assert agents["x"]["mode"] == "all"
    assert agents["x"]["hidden"] is False


def test_load_agents_does_not_mutate_builtins():
    before = dict(AGENT_DEFS)
    load_agents({"build": {"disable": True},
                 "build2": {"description": "d", "tools": [], "system": "s"}})
    assert set(AGENT_DEFS) == set(before)
    assert AGENT_DEFS["build"]["description"] == before["build"]["description"]
