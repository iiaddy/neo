"""Tests for neo.scan.bashscan."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from neo.scan import first_word, scan  # noqa: E402


def test_git_checkout_main():
    assert scan("git checkout main") == ["git checkout main", "git checkout *", "git *"]


def test_rm_rf_x():
    assert scan("rm -rf x") == ["rm -rf x", "rm -rf *", "rm *"]


def test_single_word():
    assert scan("ls") == ["ls"]


def test_semicolon_chain():
    sigs = scan("cd /tmp; ls -la")
    assert sigs == ["cd /tmp", "cd *", "ls -la", "ls *"]


def test_and_chain():
    assert scan("make && make install") == ["make", "make install", "make *"]


def test_or_chain_and_newlines():
    sigs = scan("false || true\nwhoami")
    assert sigs == ["false", "true", "whoami"]


def test_pipes():
    sigs = scan("cat f.txt | grep foo | wc -l")
    assert sigs == [
        "cat f.txt",
        "cat *",
        "grep foo",
        "grep *",
        "wc -l",
        "wc *",
    ]


def test_quoted_operators_not_split():
    sigs = scan('echo "a; b" \'c|d\'')
    assert sigs == ['echo a; b c|d', "echo a; b *", "echo *"]


def test_shlex_error_fallback():
    # Unbalanced quote -> shlex raises -> naive split fallback still scans.
    sigs = scan("echo 'oops")
    assert sigs[0].startswith("echo")
    assert "echo *" in sigs


def test_empty():
    assert scan("") == []
    assert scan("   ") == []


def test_dedup_preserves_order():
    sigs = scan("ls; ls")
    assert sigs == ["ls"]


def test_first_word():
    assert first_word("git checkout main") == "git"
    assert first_word("  sudo rm -rf /") == "sudo"
    assert first_word("cd /tmp && ls") == "cd"
    assert first_word("") == ""
    assert first_word("echo 'oops") == "echo"
