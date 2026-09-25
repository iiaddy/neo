"""Tests for neo's formatter registry: marker detection and the runner.

Real subprocesses run fake formatter executables written to tmp_path, so no
real black/ruff/prettier installation is needed.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from neo.format import (  # noqa: E402
    FORMATTERS,
    detect_formatter,
    format_enabled,
    format_file,
)
import neo.format.registry as registry  # noqa: E402


@pytest.fixture()
def bindir(tmp_path, monkeypatch):
    """A bin dir as the entire PATH, holding fake formatter executables.

    PATH is replaced (not prepended) so detection is deterministic even on
    machines with real formatters installed.
    """
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    return d


def make_exe(bindir: Path, name: str, body: str) -> Path:
    exe = bindir / name
    exe.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def fake_rewriter(name: str) -> str:
    # Appends a marker comment to the file given as the last argument.
    return f'f=""; for a in "$@"; do f="$a"; done; printf "# fmt:{name}\\n" >> "$f"'


# ---------------------------------------------------------------- detection


def test_detect_python_prefers_ruff_with_marker(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    target = tmp_path / "a.py"
    target.write_text("x=1\n", encoding="utf-8")
    assert detect_formatter(target).name == "ruff"


def test_detect_python_setup_py_marker(tmp_path):
    (tmp_path / "setup.py").write_text("x=1\n", encoding="utf-8")
    assert detect_formatter(tmp_path / "a.py").name == "ruff"


def test_detect_python_no_marker_falls_back_to_extension(tmp_path):
    assert detect_formatter(tmp_path / "a.py").name == "ruff"


def test_detect_python_prefers_installed_black(bindir, tmp_path):
    # Only black is on PATH -> black wins over the ruff default.
    make_exe(bindir, "black", fake_rewriter("black"))
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert detect_formatter(tmp_path / "a.py").name == "black"


def test_detect_python_prefers_installed_ruff(bindir, tmp_path):
    make_exe(bindir, "ruff", fake_rewriter("ruff"))
    make_exe(bindir, "black", fake_rewriter("black"))
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert detect_formatter(tmp_path / "a.py").name == "ruff"


def test_detect_prettier_with_package_json(tmp_path):
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    assert detect_formatter(tmp_path / "a.ts").name == "prettier"
    assert detect_formatter(tmp_path / "a.js").name == "prettier"


def test_detect_gofmt_with_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert detect_formatter(tmp_path / "a.go").name == "gofmt"


def test_detect_rustfmt_with_cargo_toml(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    assert detect_formatter(tmp_path / "a.rs").name == "rustfmt"


def test_detect_marker_found_upward(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)
    assert detect_formatter(nested / "a.py").name == "ruff"


def test_detect_unknown_extension(tmp_path):
    assert detect_formatter(tmp_path / "a.unknownext") is None
    assert detect_formatter(tmp_path / "Makefile") is None


def test_detect_formatter_disabled(tmp_path):
    cfg = {"format": {"enabled": False}}
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert detect_formatter(tmp_path / "a.py", cfg) is None
    assert not format_enabled(cfg)
    assert format_enabled(None)


def test_detect_per_formatter_disable(tmp_path):
    cfg = {"format": {"formatters": {"ruff": {"enabled": False}}}}
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert detect_formatter(tmp_path / "a.py", cfg).name == "black"


def test_registry_lists_expected_formatters():
    names = {f.name for f in FORMATTERS}
    assert {"ruff", "black", "prettier", "gofmt", "rustfmt"} <= names


# ---------------------------------------------------------------- runner


def test_format_file_with_fake_black(bindir, tmp_path):
    make_exe(bindir, "black", fake_rewriter("black"))
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    target = tmp_path / "a.py"
    target.write_text("x=1\n", encoding="utf-8")
    changed, output = format_file(target)
    assert changed is True
    assert target.read_text(encoding="utf-8").endswith("# fmt:black\n")
    assert output  # some human-readable output


def test_format_file_idempotent_second_run(bindir, tmp_path):
    make_exe(bindir, "black", fake_rewriter("black"))
    target = tmp_path / "a.py"
    target.write_text("x=1\n# fmt:black\n", encoding="utf-8")
    # A formatter that only rewrites when needed: our fake always appends,
    # so emulate idempotency with a smarter fake.
    (bindir / "black").write_text(
        "#!/bin/sh\n"
        'f=""; for a in "$@"; do f="$a"; done\n'
        '/bin/grep -q "fmt:black" "$f" || printf "# fmt:black\\n" >> "$f"\n',
        encoding="utf-8",
    )
    changed, output = format_file(target)
    assert changed is False
    assert "no changes" in output


def test_format_file_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()
    target = tmp_path / "a.py"
    target.write_text("x=1\n", encoding="utf-8")
    changed, output = format_file(target)
    assert changed is False
    assert output.startswith("formatter not installed:")


def test_format_file_no_formatter_for_extension(tmp_path):
    target = tmp_path / "a.unknownext"
    target.write_text("x\n", encoding="utf-8")
    changed, output = format_file(target)
    assert changed is False
    assert "no formatter configured" in output


def test_format_file_missing_file(tmp_path):
    changed, output = format_file(tmp_path / "nope.py")
    assert changed is False
    assert "file not found" in output


def test_format_file_nonzero_exit(bindir, tmp_path):
    make_exe(bindir, "black", "exit 3")
    target = tmp_path / "a.py"
    target.write_text("x=1\n", encoding="utf-8")
    changed, output = format_file(target)
    assert changed is False
    assert "black failed" in output


def test_format_file_timeout(bindir, tmp_path, monkeypatch):
    make_exe(bindir, "black", "/bin/sleep 30")
    monkeypatch.setattr(registry, "_RUN_TIMEOUT", 0.2)
    target = tmp_path / "a.py"
    target.write_text("x=1\n", encoding="utf-8")
    changed, output = format_file(target)
    assert changed is False
    assert "timed out" in output


def test_format_file_respects_disabled_config(bindir, tmp_path):
    make_exe(bindir, "black", fake_rewriter("black"))
    target = tmp_path / "a.py"
    target.write_text("x=1\n", encoding="utf-8")
    changed, output = format_file(target, {"format": {"enabled": False}})
    assert changed is False
    assert "no formatter configured" in output
    assert target.read_text(encoding="utf-8") == "x=1\n"
