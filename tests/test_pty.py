"""Real-PTY tests for neo.pty_runner (no mocks, no controlling terminal)."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from neo.pty_runner import run_pty


def _clean(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def test_echo():
    out, code = asyncio.run(run_pty("echo hello", timeout_s=10))
    assert _clean(out) == "hello"
    assert code == 0


def test_exit_codes():
    _, code = asyncio.run(run_pty("exit 3", timeout_s=10))
    assert code == 3
    _, code = asyncio.run(run_pty("false", timeout_s=10))
    assert code != 0


def test_interactive_read_with_input():
    out, code = asyncio.run(
        run_pty("read -p 'name? ' n; echo got:$n", timeout_s=10,
                input_data=b"ada\n"))
    assert "got:ada" in _clean(out)


def test_timeout_kills_sleep():
    start = time.monotonic()
    out, code = asyncio.run(run_pty("sleep 30", timeout_s=2))
    elapsed = time.monotonic() - start
    assert elapsed < 15
    assert code is not None and code != 0  # killed by signal
    assert isinstance(out, str)


def test_cwd_and_env():
    out, code = asyncio.run(
        run_pty("pwd", cwd="/tmp", timeout_s=10))
    assert _clean(out) == "/tmp"
    assert code == 0


def test_multiline_output_preserved():
    out, _ = asyncio.run(run_pty("printf 'a\\nb\\nc\\n'", timeout_s=10))
    assert _clean(out).split("\n") == ["a", "b", "c"]
