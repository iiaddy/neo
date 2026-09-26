"""Prompts-as-markdown: every agent prompt must load from src/neo/prompts/*.md."""
import pytest

from neo.prompts import load_prompt, render_prompt

BASE_PROMPTS = ["system", "memory", "summary", "max_steps", "plan_enter"]
AGENT_PROMPTS = {
    "explore": "agents/explore",
    "title": "agents/title",
    "summary": "agents/summary",
    "build": "agents/build",
    "plan": "agents/plan",
}


def test_all_prompt_files_exist_and_nonempty():
    for name in BASE_PROMPTS + list(AGENT_PROMPTS.values()):
        text = load_prompt(name)
        assert text.strip(), f"{name}.md is empty"


def test_constants_match_files():
    """Module constants must be the .md content (no drift, no duplication)."""
    from neo.agent import prompts as P
    assert P.NEO_SYSTEM_BASE == load_prompt("system")
    assert P.MEMORY_SYSTEM == load_prompt("memory")
    assert P.SUMMARY_PROMPT == load_prompt("summary")
    assert P.MAX_STEPS_PROMPT == load_prompt("max_steps")


def test_roster_systems_match_files():
    from neo.agents import roster
    got = {
        "explore": roster._EXPLORE_SYSTEM,
        "title": roster._TITLE_SYSTEM,
        "summary": roster._SUMMARY_SYSTEM,
        "build": roster._BUILD_SYSTEM,
        "plan": roster._PLAN_SYSTEM,
    }
    for agent, expected in AGENT_PROMPTS.items():
        assert got[agent] == load_prompt(expected), agent


def test_render_plan_enter_substitutes_placeholders():
    out = render_prompt(
        "plan_enter",
        goal="add rate limiting",
        plan_file="/tmp/x/.neo/plans/add-rate-limiting.md",
        plans_dirname=".neo/plans",
    )
    assert "Goal: add rate limiting" in out
    assert "/tmp/x/.neo/plans/add-rate-limiting.md" in out
    assert ".neo/plans/" in out
    assert "{" not in out and "}" not in out  # no leftover placeholders


def test_render_unknown_placeholder_raises():
    with pytest.raises(KeyError):
        render_prompt("plan_enter", goal="x")  # missing plan_file etc.


def test_system_prompt_assembly_unchanged():
    from neo.agent.prompts import build_system_prompt
    p = build_system_prompt(tools={}, project_notes=[], model="m/x",
                            workdir="/tmp")
    for section in ("# Tone and style", "# Proactiveness",
                    "# Following conventions", "# Code style",
                    "# Doing tasks", "# Tool usage", "# Code references",
                    "## Memory", "## Environment"):
        assert section in p, section
    assert "m/x" in p
    assert "NEVER commit" in p


def test_plan_enter_tool_uses_rendered_template():
    """PlanEnterTool output must equal the rendered plan_enter.md."""
    import asyncio
    from pathlib import Path
    from neo.plan.tools import PlanEnterTool

    class Ctx:
        workdir = "/tmp/neo-plan-md-test"
        plan_mode = None
        ui = None

    async def go():
        tool = PlanEnterTool()
        res = await tool.run({"goal": "add rate limiting"}, Ctx())
        return res

    res = asyncio.run(go())
    assert not res.is_error
    expected = render_prompt(
        "plan_enter",
        goal="add rate limiting",
        plan_file=str(Path("/tmp/neo-plan-md-test").resolve()
                       / ".neo/plans/add-rate-limiting.md"),
        plans_dirname=".neo/plans",
    )
    assert res.output == expected
