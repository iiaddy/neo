"""Tests for neo.commands (discovery, frontmatter, template rendering)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from neo.commands import Command, discover, render  # noqa: E402
from neo.commands.engine import _split_frontmatter  # noqa: E402


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_frontmatter_parsing():
    meta, body = _split_frontmatter(
        "---\ndescription: Review diff\nagent: review\nmodel: x\nsubtask: true\n---\nHello $1\n"
    )
    assert meta == {
        "description": "Review diff",
        "agent": "review",
        "model": "x",
        "subtask": "true",
    }
    assert body == "Hello $1"


def test_frontmatter_absent():
    meta, body = _split_frontmatter("just a body\n")
    assert meta == {} and body == "just a body\n"


def test_discover_project_and_user(tmp_path, monkeypatch):
    proj = tmp_path / ".neo" / "commands"
    _write(proj / "review.md", "---\ndescription: R\n---\nreview $1\n")
    _write(proj / "note.md", "plain\n")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _write(home / ".config" / "neo" / "commands" / "usercmd.md", "hi\n")
    _write(home / ".config" / "neo" / "commands" / "review.md", "shadowed\n")
    cmds = discover(tmp_path)
    by_name = {c.name: c for c in cmds}
    assert set(by_name) == {"review", "note", "usercmd"}
    # project shadows user dir for the same name
    assert by_name["review"].description == "R"
    assert by_name["review"].body == "review $1"
    assert by_name["note"].description == ""
    assert isinstance(by_name["usercmd"], Command)


def test_discover_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    assert discover(tmp_path) == []


def test_render_positional_and_arguments():
    out = render("one=$1 two=$2 nine=$9 all=[$ARGUMENTS]", ["a", "b"], "/tmp")
    assert out == "one=a two=b nine= all=[a b]"


def test_render_dollar_escape():
    assert render("cost $$5 and $1", ["x"], "/tmp") == "cost $5 and x"


def test_render_shell_directive(tmp_path):
    out = render("!echo hi $1\nsecond", ["there"], tmp_path)
    assert out == "hi there\nsecond"


def test_render_shell_directive_failure(tmp_path):
    out = render("!exit 3", [], tmp_path)
    assert "shell exited 3" in out


def test_render_shell_timeout(monkeypatch):
    import neo.commands.engine as eng

    monkeypatch.setattr(eng, "_SHELL_TIMEOUT", 0.05)
    out = render("!sleep 5", [], "/tmp")
    assert "timed out" in out


def test_render_at_file(tmp_path):
    _write(tmp_path / "snippet.txt", "FILE-CONTENT\n")
    out = render("before\n@snippet.txt\nafter", [], tmp_path)
    assert out == "before\nFILE-CONTENT\n\nafter"


def test_render_at_file_missing(tmp_path):
    out = render("@nope.txt", [], tmp_path)
    assert "file not found: nope.txt" in out


def test_render_directives_only_at_line_start(tmp_path):
    out = render("say !echo hi and @snippet.txt", [], tmp_path)
    assert out == "say !echo hi and @snippet.txt"


def test_render_indented_directive(tmp_path):
    _write(tmp_path / "f.txt", "C\n")
    out = render("  @f.txt", [], tmp_path)
    assert out == "C\n"
