"""Tests for neo.patch (parser + applier + ApplyPatchTool)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from neo.patch import ApplyPatchTool, PatchApplyError, apply_ops, parse_patch  # noqa: E402
from neo.tools.base import ToolContext  # noqa: E402

UPDATE_PATCH = """*** Begin Patch
*** Update File: a.txt
@@
 line1
-old
+new
 line3
*** End Patch
"""

MOVE_PATCH = """*** Begin Patch
*** Update File: a.txt
*** Move to: b.txt
@@
-old
+new
*** End Patch
"""

ADD_PATCH = """*** Begin Patch
*** Add File: sub/new.py
+def hello():
+    return "hi"
+
+print(hello())
*** End Patch
"""

DELETE_PATCH = """*** Begin Patch
*** Delete File: gone.txt
*** End Patch
"""


def test_parse_update():
    ops = parse_patch(UPDATE_PATCH)
    assert len(ops) == 1
    op = ops[0]
    assert op.op == "update" and op.path == "a.txt"
    assert len(op.hunks) == 1
    kinds = [k for k, _ in op.hunks[0].lines]
    assert kinds == ["context", "remove", "add", "context"]


def test_parse_move():
    ops = parse_patch(MOVE_PATCH)
    assert ops[0].op == "update"
    assert ops[0].move_to == "b.txt"


def test_parse_add():
    ops = parse_patch(ADD_PATCH)
    assert len(ops) == 1
    op = ops[0]
    assert op.op == "add" and op.path == "sub/new.py"
    content = [t for k, t in op.hunks[0].lines if k == "add"]
    assert content == ['def hello():', '    return "hi"', "", "print(hello())"]


def test_parse_delete():
    ops = parse_patch(DELETE_PATCH)
    assert ops[0].op == "delete" and ops[0].path == "gone.txt"


def test_parse_malformed_missing_end():
    with pytest.raises(ValueError, match="line 4"):
        parse_patch("*** Begin Patch\n*** Update File: a.txt\n@@\n x\n")


def test_parse_malformed_no_begin():
    with pytest.raises(ValueError, match="line 1"):
        parse_patch("*** Update File: a.txt\n*** End Patch\n")


def test_parse_malformed_bad_hunk_line():
    with pytest.raises(ValueError, match="line 4"):
        parse_patch("*** Begin Patch\n*** Update File: a.txt\n@@\nnope\n*** End Patch\n")


def test_parse_malformed_move_without_update():
    with pytest.raises(ValueError, match="line 2"):
        parse_patch("*** Begin Patch\n*** Move to: b.txt\n*** End Patch\n")


def test_parse_trailing_content_rejected():
    with pytest.raises(ValueError, match="line 3"):
        parse_patch("*** Begin Patch\n*** End Patch\njunk\n")


def _workdir(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("line1\nold\nline3\n")
    (tmp_path / "gone.txt").write_text("bye\n")
    return tmp_path


def test_apply_update(tmp_path):
    wd = _workdir(tmp_path)
    notes = apply_ops(parse_patch(UPDATE_PATCH), wd)
    assert notes == ["updated a.txt (1 hunk(s))"]
    assert (wd / "a.txt").read_text() == "line1\nnew\nline3\n"


def test_apply_update_fuzzy_whitespace(tmp_path):
    wd = tmp_path
    (wd / "a.txt").write_text("line1\n   old   \nline3\n")
    notes = apply_ops(parse_patch(UPDATE_PATCH), wd)
    assert "updated a.txt" in notes[0]
    assert (wd / "a.txt").read_text() == "line1\nnew\nline3\n"


def test_apply_update_hunk_mismatch(tmp_path):
    wd = tmp_path
    (wd / "a.txt").write_text("line1\ntotally different\nline3\n")
    with pytest.raises(PatchApplyError, match="hunk 1 of a.txt"):
        apply_ops(parse_patch(UPDATE_PATCH), wd)


def test_apply_add_and_delete(tmp_path):
    wd = _workdir(tmp_path)
    notes = apply_ops(parse_patch(ADD_PATCH), wd)
    assert (wd / "sub" / "new.py").read_text().startswith("def hello():")
    assert "added sub/new.py" in notes[0]
    notes = apply_ops(parse_patch(DELETE_PATCH), wd)
    assert not (wd / "gone.txt").exists()
    assert "deleted gone.txt" in notes[0]


def test_apply_move(tmp_path):
    wd = tmp_path
    (wd / "a.txt").write_text("old\n")
    notes = apply_ops(parse_patch(MOVE_PATCH), wd)
    assert "moved to b.txt" in notes[0]
    assert not (wd / "a.txt").exists()
    assert (wd / "b.txt").read_text() == "new\n"


def test_apply_add_existing_fails(tmp_path):
    wd = tmp_path
    (wd / "a.txt").write_text("x\n")
    with pytest.raises(PatchApplyError, match="already exists"):
        apply_ops(parse_patch(ADD_PATCH.replace("sub/new.py", "a.txt")), wd)


def test_apply_delete_missing_fails(tmp_path):
    with pytest.raises(PatchApplyError, match="not found"):
        apply_ops(parse_patch(DELETE_PATCH), tmp_path)


def test_apply_path_escape_rejected(tmp_path):
    patch = ("*** Begin Patch\n*** Add File: ../evil.txt\n+x\n*** End Patch\n")
    with pytest.raises(PatchApplyError, match="escapes workdir"):
        apply_ops(parse_patch(patch), tmp_path)


def _ctx(workdir: Path) -> ToolContext:
    return ToolContext(
        workdir=workdir,
        config=SimpleNamespace(disabled_tools=[]),
        permissions=SimpleNamespace(),
        gate=None,
        emit=lambda e: None,
        todos=[],
    )


def test_tool_run_end_to_end(tmp_path):
    wd = _workdir(tmp_path)
    ctx = _ctx(wd)
    tool = ApplyPatchTool()
    assert tool.name == "apply_patch" and tool.needs_approval is True
    res = asyncio.run(tool({"patch": UPDATE_PATCH}, ctx))
    assert not res.is_error
    assert "updated a.txt" in res.output
    assert (wd / "a.txt").read_text() == "line1\nnew\nline3\n"


def test_tool_run_parse_error(tmp_path):
    ctx = _ctx(tmp_path)
    res = asyncio.run(ApplyPatchTool()({"patch": "garbage"}, ctx))
    assert res.is_error and "parse error" in res.output


def test_tool_run_apply_error(tmp_path):
    (tmp_path / "a.txt").write_text("line1\ntotally different\nline3\n")
    ctx = _ctx(tmp_path)
    res = asyncio.run(ApplyPatchTool()({"patch": UPDATE_PATCH}, ctx))
    assert res.is_error and "hunk 1" in res.output


def test_tool_never_raises(tmp_path):
    ctx = _ctx(tmp_path)
    res = asyncio.run(ApplyPatchTool()({"patch": ""}, ctx))
    assert res.is_error
