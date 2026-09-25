"""Tests for neo's plan mode: plan_enter / plan_exit tools,
plan_mode_allows enforcement, and build_handoff."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from neo.plan import PlanEnterTool, PlanExitTool, build_handoff, plan_mode_allows  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make_ctx(workdir: Path, **overrides) -> SimpleNamespace:
    base = dict(
        workdir=workdir,
        config=SimpleNamespace(verify_commands=[], disabled_tools=[]),
        permissions=None,
        gate=lambda *a: asyncio.sleep(0, result="once"),
        emit=lambda e: None,
        todos=[],
        ui=None,
        locks={},
        depth=0,
        background={},
        skills={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --- plan_enter --------------------------------------------------------------


def test_plan_enter_sets_plan_mode(tmp_path):
    ctx = make_ctx(tmp_path)
    res = run(PlanEnterTool()({"goal": "Add caching to the router"}, ctx))
    assert not res.is_error
    assert ctx.plan_mode["active"] is True
    assert ctx.plan_mode["goal"] == "Add caching to the router"
    assert ctx.plan_mode["plan_path"].endswith(".neo/plans/add-caching-to-the-router.md")
    assert (tmp_path / ".neo" / "plans").is_dir()
    assert "plan_exit" in res.output


def test_plan_enter_requires_goal(tmp_path):
    ctx = make_ctx(tmp_path)
    res = run(PlanEnterTool()({"goal": "  "}, ctx))
    assert res.is_error
    assert not hasattr(ctx, "plan_mode")


def test_plan_enter_rejects_when_already_active(tmp_path):
    ctx = make_ctx(tmp_path)
    run(PlanEnterTool()({"goal": "first goal"}, ctx))
    res = run(PlanEnterTool()({"goal": "second goal"}, ctx))
    assert res.is_error
    assert "plan_exit" in res.output


# --- plan_exit ---------------------------------------------------------------


def _active_ctx(tmp_path) -> SimpleNamespace:
    ctx = make_ctx(tmp_path)
    run(PlanEnterTool()({"goal": "write a plan"}, ctx))
    return ctx


def test_plan_exit_rejects_path_outside_plans(tmp_path):
    ctx = _active_ctx(tmp_path)
    outside = tmp_path / "src" / "x.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("x = 1")
    res = run(PlanExitTool()({"plan_path": str(outside), "summary": "s"}, ctx))
    assert res.is_error
    assert ".neo/plans" in res.output
    assert ctx.plan_mode["active"] is True  # still in plan mode


def test_plan_exit_rejects_traversal(tmp_path):
    ctx = _active_ctx(tmp_path)
    res = run(PlanExitTool()(
        {"plan_path": ".neo/plans/../../evil.md", "summary": "s"}, ctx))
    assert res.is_error


def test_plan_exit_rejects_missing_file(tmp_path):
    ctx = _active_ctx(tmp_path)
    res = run(PlanExitTool()(
        {"plan_path": ".neo/plans/nope.md", "summary": "a plan"}, ctx))
    assert res.is_error
    assert "not found" in res.output.lower()


def test_plan_exit_accepts_valid_plan(tmp_path):
    ctx = _active_ctx(tmp_path)
    plan_file = Path(ctx.plan_mode["plan_path"])
    plan_file.write_text("# Plan\n\nDo the thing.\n")
    res = run(PlanExitTool()(
        {"plan_path": str(plan_file), "summary": "We do the thing."}, ctx))
    assert not res.is_error
    assert ctx.plan_mode["active"] is False
    assert "[Approved plan]" in res.output
    assert "Do the thing." in res.output
    assert "We do the thing." in res.output


# --- plan_mode_allows --------------------------------------------------------


def test_plan_mode_allows_reads_anywhere(tmp_path):
    for tool, target in [("read", "src/x.py"), ("list_dir", "."),
                         ("glob", "**/*.py"), ("grep", "TODO"),
                         ("webfetch", "https://example.com"),
                         ("websearch", "neo agent"),
                         ("question", ""), ("plan_exit", "")]:
        ok, _ = plan_mode_allows(tool, target, tmp_path)
        assert ok, tool


def test_plan_mode_allows_explore_task_only(tmp_path):
    ok, _ = plan_mode_allows("task", "explore", tmp_path)
    assert ok
    ok, reason = plan_mode_allows("task", "general", tmp_path)
    assert not ok
    assert "explore" in reason


def test_plan_mode_denies_edit_on_code_but_allows_plan_file(tmp_path):
    ok, reason = plan_mode_allows("edit", "src/x.py", tmp_path)
    assert not ok
    assert "plan mode" in reason.lower()

    ok, _ = plan_mode_allows("edit", ".neo/plans/p.md", tmp_path)
    assert ok
    ok, _ = plan_mode_allows("write", str(tmp_path / ".neo" / "plans" / "p.md"),
                             tmp_path)
    assert ok


def test_plan_mode_denies_bash_and_unknown_tools(tmp_path):
    ok, reason = plan_mode_allows("bash", "pytest -q", tmp_path)
    assert not ok
    assert "bash" in reason.lower()

    ok, _ = plan_mode_allows("todo_write", "", tmp_path)
    assert not ok

    ok, _ = plan_mode_allows("plan_enter", "", tmp_path)
    assert not ok


# --- build_handoff -----------------------------------------------------------


def test_build_handoff_contains_plan_text(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# Goal\n\nShip it.\n")
    out = build_handoff(plan, "Ship the thing.")
    assert out.startswith("[Approved plan]")
    assert "Ship it." in out
    assert "Ship the thing." in out
    assert "implement it now" in out.lower()
