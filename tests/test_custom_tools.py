"""Tests for neo.custom_tools loader."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from neo.custom_tools import discover_tools  # noqa: E402

GOOD_TOOL = '''from neo.tools.base import Tool, ToolResult


class EchoTool(Tool):
    name = "echo2"
    description = "Echoes back."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    needs_approval = False

    async def run(self, args, ctx) -> ToolResult:
        return ToolResult(output=f"echo:{args.get('text', '')}", title=self.name)
'''

BROKEN_TOOL = "this is not valid python (((\n"

NO_TOOL = "X = 42\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_discover_and_run(tmp_path, monkeypatch):
    _write(tmp_path / ".neo" / "tools" / "echo2.py", GOOD_TOOL)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    classes, warnings = discover_tools(tmp_path)
    assert warnings == []
    assert len(classes) == 1
    cls = classes[0]
    assert cls.name == "echo2"
    tool = cls()
    assert issubclass(cls, __import__("neo.tools.base", fromlist=["Tool"]).Tool)
    res = asyncio.run(tool.run({"text": "hi"}, None))
    assert res.output == "echo:hi"


def test_broken_module_skipped(tmp_path, monkeypatch):
    _write(tmp_path / ".neo" / "tools" / "broken.py", BROKEN_TOOL)
    _write(tmp_path / ".neo" / "tools" / "notool.py", NO_TOOL)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    classes, warnings = discover_tools(tmp_path)
    assert classes == []
    assert len(warnings) == 2
    assert any("broken.py" in w and "import failed" in w for w in warnings)
    assert any("notool.py" in w and "no Tool subclass" in w for w in warnings)


def test_duplicate_name_project_shadows_user(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write(home / ".config" / "neo" / "tools" / "user_echo.py", GOOD_TOOL)
    _write(tmp_path / ".neo" / "tools" / "proj_echo.py", GOOD_TOOL)
    monkeypatch.setenv("HOME", str(home))
    classes, warnings = discover_tools(tmp_path)
    assert len(classes) == 1  # same name "echo2" defined twice
    assert any("already defined" in w for w in warnings)


def test_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    classes, warnings = discover_tools(tmp_path)
    assert classes == [] and warnings == []


def test_bad_constructor_skipped(tmp_path, monkeypatch):
    bad = GOOD_TOOL.replace(
        "needs_approval = False",
        "def __init__(self):\n        raise RuntimeError('boom')\n    needs_approval = False",
    )
    _write(tmp_path / ".neo" / "tools" / "badinit.py", bad)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    classes, warnings = discover_tools(tmp_path)
    assert classes == []
    assert any("failed" in w and "boom" in w for w in warnings)
