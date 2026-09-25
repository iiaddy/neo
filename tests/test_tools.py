"""Tests for neo's tools subsystem."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from neo.tools import TOOL_CLASSES, build_toolset  # noqa: E402
from neo.tools.base import ToolContext  # noqa: E402


async def _gate(tool_name: str, target: str, detail: str) -> str:
    return "once"


def make_ctx(workdir: Path, **overrides) -> SimpleNamespace:
    base = dict(
        workdir=workdir,
        config=SimpleNamespace(verify_commands=[], disabled_tools=[]),
        permissions=None,
        gate=_gate,
        emit=lambda e: None,
        todos=[],
        ui=None,
        locks={},
        depth=0,
        background={},
        skills={"demo": "demo skill content"},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def run(coro):
    return asyncio.run(coro)


def tools_for(ctx):
    return build_toolset(ctx)


# --- read / write roundtrip -------------------------------------------------


def test_read_write_roundtrip(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    run(tools["write"]({"path": "a.txt", "content": "line one\nline two\nline three\n"}, ctx))
    res = run(tools["read"]({"path": "a.txt"}, ctx))
    assert not res.is_error
    assert res.output == "1| line one\n2| line two\n3| line three"
    assert res.title == "a.txt:1-3"


def test_read_offset_and_limit(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    run(tools["write"]({"path": "b.txt", "content": "\n".join(f"l{i}" for i in range(1, 11))}, ctx))
    res = run(tools["read"]({"path": "b.txt", "offset": 4, "limit": 3}, ctx))
    assert res.output.splitlines()[:3] == ["4| l4", "5| l5", "6| l6"]
    assert "4 more lines" in res.output


def test_read_binary_and_missing(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02hello")
    res = run(tools["read"]({"path": "bin.dat"}, ctx))
    assert res.is_error and "binary" in res.output
    res = run(tools["read"]({"path": "nope.txt"}, ctx))
    assert res.is_error and "not found" in res.output.lower()


def test_read_rejects_directory(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    res = run(tools["read"]({"path": "."}, ctx))
    assert res.is_error and "directory" in res.output.lower()


# --- edit -------------------------------------------------------------------


def test_edit_exact(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    run(tools["write"]({"path": "e.txt", "content": "foo = 1\nbar = 2\n"}, ctx))
    res = run(tools["edit"]({"path": "e.txt", "old": "bar = 2", "new": "bar = 3"}, ctx))
    assert not res.is_error, res.output
    assert (tmp_path / "e.txt").read_text() == "foo = 1\nbar = 3\n"


def test_edit_line_trimmed_fuzzy(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    run(tools["write"]({"path": "f.txt", "content": "def x():\n      return  1   \n"}, ctx))
    res = run(tools["edit"]({"path": "f.txt", "old": "return  1", "new": "return 2"}, ctx))
    assert not res.is_error, res.output
    assert "return 2" in (tmp_path / "f.txt").read_text()


def test_edit_whitespace_normalized(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    run(tools["write"]({"path": "w.txt", "content": "alpha   beta\ngamma\n"}, ctx))
    res = run(tools["edit"]({"path": "w.txt", "old": "alpha beta", "new": "alpha"}, ctx))
    assert not res.is_error, res.output
    assert (tmp_path / "w.txt").read_text() == "alpha\ngamma\n"


def test_edit_guards(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    run(tools["write"]({"path": "g.txt", "content": "dup\ndup\n"}, ctx))

    res = run(tools["edit"]({"path": "g.txt", "old": "same", "new": "same"}, ctx))
    assert res.is_error and "identical" in res.output

    res = run(tools["edit"]({"path": "g.txt", "old": "", "new": "x"}, ctx))
    assert res.is_error and "use write" in res.output

    res = run(tools["edit"]({"path": "g.txt", "old": "dup", "new": "x"}, ctx))
    assert res.is_error and "not unique" in res.output

    res = run(tools["edit"]({"path": "g.txt", "old": "missing", "new": "x"}, ctx))
    assert res.is_error and "not found" in res.output

    res = run(tools["edit"]({"path": "g.txt", "old": "dup", "new": "x", "replace_all": True}, ctx))
    assert not res.is_error, res.output
    assert (tmp_path / "g.txt").read_text() == "x\nx\n"


def test_edit_too_fuzzy_rejected(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    # line-trimmed matches, but the matched span (52 chars) dwarfs len(old)=3
    run(tools["write"]({"path": "h.txt", "content": "a" + " " * 50 + "\nb\n"}, ctx))
    res = run(tools["edit"]({"path": "h.txt", "old": "a\nb", "new": "replaced"}, ctx))
    assert res.is_error and "too fuzzy" in res.output


# --- glob / grep / list_dir -------------------------------------------------


def test_glob_finds_files(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "one.py").write_text("x")
    (tmp_path / "pkg" / "two.py").write_text("x")
    (tmp_path / "note.md").write_text("x")
    res = run(tools["glob"]({"pattern": "**/*.py"}, ctx))
    assert not res.is_error
    assert "pkg/one.py" in res.output and "pkg/two.py" in res.output
    assert "note.md" not in res.output


def test_grep_finds_matches(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    (tmp_path / "a.py").write_text("needle here\nnothing\n")
    (tmp_path / "b.py").write_text("needle there\n")
    res = run(tools["grep"]({"pattern": "needle", "include": "*.py"}, ctx))
    assert not res.is_error, res.output
    assert "a.py:1:" in res.output and "b.py:1:" in res.output


def test_list_dir(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    (tmp_path / "sub").mkdir()
    (tmp_path / "z.txt").write_text("x")
    res = run(tools["list_dir"]({"path": "."}, ctx))
    assert not res.is_error
    lines = res.output.splitlines()
    assert "sub/" in lines and "z.txt" in lines
    assert lines == sorted(lines, key=str.lower)


# --- bash -------------------------------------------------------------------


def test_bash_echo_and_exit_code(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    res = run(tools["bash"]({"command": "echo hello"}, ctx))
    assert not res.is_error
    assert res.output.strip() == "hello"
    assert res.details["exit_code"] == 0


def test_bash_failing_command(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    res = run(tools["bash"]({"command": "echo oops >&2; exit 3"}, ctx))
    assert res.is_error
    assert "oops" in res.output
    assert res.details["exit_code"] == 3


def test_bash_timeout(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    start = time.monotonic()
    res = run(tools["bash"]({"command": "sleep 2", "timeout": 500}, ctx))
    elapsed = time.monotonic() - start
    assert res.is_error
    assert "timed out after 0.5s" in res.output
    assert elapsed < 5


# --- todos ------------------------------------------------------------------


def test_todo_write_and_read(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    emitted = []
    ctx.emit = emitted.append
    res = run(tools["todo_write"]({
        "todos": [
            {"content": "first", "status": "in_progress", "priority": "high"},
            {"content": "second", "status": "pending"},
        ]
    }, ctx))
    assert not res.is_error, res.output
    assert res.output == "2 todos: 1 in_progress, 1 pending"
    assert emitted and hasattr(emitted[-1], "todos")
    res = run(tools["todo_read"]({}, ctx))
    assert "first" in res.output and "second" in res.output

    res = run(tools["todo_read"]({}, make_ctx(tmp_path)))
    assert res.output == "No todos"


def test_todo_write_bad_status(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    res = run(tools["todo_write"]({"todos": [{"content": "x", "status": "done-ish"}]}, ctx))
    assert res.is_error and "invalid status" in res.output


def test_todo_write_demotes_extra_in_progress(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    res = run(tools["todo_write"]({
        "todos": [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
            {"content": "c", "status": "in_progress"},
        ]
    }, ctx))
    assert not res.is_error, res.output
    statuses = [t["status"] for t in ctx.todos]
    assert statuses == ["in_progress", "pending", "pending"]
    assert "demoted" in res.output


# --- skill ------------------------------------------------------------------


def test_skill_found_and_missing(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    res = run(tools["skill"]({"name": "demo"}, ctx))
    assert not res.is_error
    assert res.output == '<skill name="demo">\ndemo skill content\n</skill>'
    res = run(tools["skill"]({"name": "nope"}, ctx))
    assert res.is_error and "demo" in res.output


# --- registry ---------------------------------------------------------------


def test_toolset_registration(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = build_toolset(ctx)
    expected = [
        "read", "list_dir", "glob", "grep", "write", "edit", "bash",
        "webfetch", "websearch", "todo_write", "todo_read", "task",
        "question", "skill",
    ]
    assert sorted(tools) == sorted(expected)
    assert [c().name for c in TOOL_CLASSES] == expected
    assert tools["read"].needs_approval is False
    assert tools["write"].needs_approval is True
    assert tools["question"].needs_approval is False


def test_build_toolset_include_and_disabled(tmp_path):
    ctx = make_ctx(tmp_path)
    child = build_toolset(ctx, include={"read", "glob"})
    assert sorted(child) == ["glob", "read"]
    ctx2 = make_ctx(tmp_path)
    ctx2.config.disabled_tools = ["bash"]
    assert "bash" not in build_toolset(ctx2)


def test_tool_never_raises(tmp_path):
    ctx = make_ctx(tmp_path)
    tools = tools_for(ctx)
    res = run(tools["bash"]({"command": ""}, ctx))
    assert isinstance(res.output, str)
