"""neo formatter registry: marker-file detection + binary runners.

Detection order for a file:
  1. formatters whose marker files are found walking up from the file's
     directory (e.g. ``pyproject.toml`` -> ruff/black, ``package.json`` ->
     prettier, ``go.mod`` -> gofmt, ``Cargo.toml`` -> rustfmt);
  2. otherwise the per-extension default map.

For Python, ruff is preferred when its binary exists, else black.
``format_file`` runs the resolved binary on the file with a 30s timeout and
reports whether the content changed. A missing binary is a clean
``(False, "formatter not installed: ...")``, never a crash.

Config (``neo.json`` ``"format"`` section)::

    "format": {
        "enabled": true,
        "formatters": {"black": {"enabled": false}},
    }
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

_RUN_TIMEOUT = 30.0


@dataclasses.dataclass
class Formatter:
    name: str
    command: list[str]          # "$FILE" is replaced with the file path
    markers: tuple[str, ...]    # marker files searched upward from the file
    extensions: tuple[str, ...] # fallback extensions when no marker matches
    install_hint: str


FORMATTERS: list[Formatter] = [
    Formatter(
        name="ruff",
        command=["ruff", "format", "$FILE"],
        markers=("pyproject.toml", "setup.py", "setup.cfg",
                 "ruff.toml", ".ruff.toml"),
        extensions=(".py", ".pyi"),
        install_hint="pip install ruff  (or: uv tool install ruff)",
    ),
    Formatter(
        name="black",
        command=["black", "-q", "$FILE"],
        markers=("pyproject.toml", "setup.py", "setup.cfg"),
        extensions=(".py", ".pyi"),
        install_hint="pip install black",
    ),
    Formatter(
        name="prettier",
        command=["prettier", "--write", "$FILE"],
        markers=("package.json",),
        extensions=(".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
                    ".json", ".css", ".scss", ".html",
                    ".yaml", ".yml", ".md"),
        install_hint="npm i -D prettier  (or: npm i -g prettier)",
    ),
    Formatter(
        name="gofmt",
        command=["gofmt", "-w", "$FILE"],
        markers=("go.mod",),
        extensions=(".go",),
        install_hint="install Go from https://go.dev/dl",
    ),
    Formatter(
        name="rustfmt",
        command=["rustfmt", "--edition", "2021", "$FILE"],
        markers=("Cargo.toml",),
        extensions=(".rs",),
        install_hint="rustup component add rustfmt",
    ),
]


def _format_config(config: Any) -> dict:
    if config is None:
        return {}
    if isinstance(config, dict):
        section = config.get("format")
    else:
        section = getattr(config, "format", None)
    return section if isinstance(section, dict) else {}


def format_enabled(config: Any = None) -> bool:
    """Global kill switch; defaults to on."""
    return bool(_format_config(config).get("enabled", True))


def _formatter_enabled(name: str, config: Any) -> bool:
    if not format_enabled(config):
        return False
    per = _format_config(config).get("formatters", {}) or {}
    spec = per.get(name)
    if isinstance(spec, dict):
        return bool(spec.get("enabled", True))
    return True


def _binary_available(argv0: str) -> bool:
    if os.path.isabs(argv0):
        return os.path.isfile(argv0) and os.access(argv0, os.X_OK)
    return shutil.which(argv0) is not None


def _has_marker(directory: Path, markers: tuple[str, ...]) -> bool:
    """True when any marker file exists in directory or an ancestor."""
    node = directory.resolve()
    while True:
        for marker in markers:
            if (node / marker).is_file():
                return True
        parent = node.parent
        if parent == node:
            return False
        node = parent


def _prefer_existing(candidates: list[Formatter]) -> Formatter:
    """Pick the first candidate whose binary exists; else the first."""
    for fmt in candidates:
        if _binary_available(fmt.command[0]):
            return fmt
    return candidates[0]


def detect_formatter(path: str | Path, config: Any = None) -> Formatter | None:
    """Resolve the formatter for a file, or None when none applies.

    Marker matches beat the plain extension map; among several candidates
    the one with an installed binary wins (this is what prefers ruff over
    black for Python). Returns a Formatter even when its binary is missing
    so callers can report a clean "not installed" message.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if not ext:
        return None
    enabled = [f for f in FORMATTERS if _formatter_enabled(f.name, config)]
    by_ext = [f for f in enabled if ext in f.extensions]
    if not by_ext:
        return None
    directory = p.parent if p.parent.is_dir() else Path.cwd()
    marked = [f for f in by_ext if _has_marker(directory, f.markers)]
    candidates = marked or by_ext
    # Keep marker/extension priority stable, but prefer an installed binary
    # among candidates (ruff before black when both match).
    return _prefer_existing(candidates)


def format_file(path: str | Path,
                config: Any = None) -> tuple[bool, str]:
    """Format a file in place. Returns (changed, output).

    output is the formatter's own output when it produced any, otherwise a
    short summary. Missing binaries, missing files, timeouts, and failures
    all return (False, <clean message>) — never raise.
    """
    p = Path(path)
    if not p.is_file():
        return False, f"file not found: {p}"
    fmt = detect_formatter(p, config)
    if fmt is None:
        return False, f"no formatter configured for *{p.suffix or '?'} files"
    argv = [str(p) if arg == "$FILE" else arg for arg in fmt.command]
    if not _binary_available(argv[0]):
        return False, f"formatter not installed: {fmt.name} ({fmt.install_hint})"
    try:
        before = p.read_bytes()
    except OSError as exc:
        return False, f"cannot read {p}: {exc}"
    try:
        proc = subprocess.run(
            argv,
            cwd=str(p.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"formatter timed out after {_RUN_TIMEOUT:.0f}s: {fmt.name}"
    except OSError as exc:
        return False, f"formatter failed to start: {exc}"
    try:
        after = p.read_bytes()
    except OSError as exc:
        return False, f"cannot re-read {p}: {exc}"
    changed = before != after
    out = (proc.stdout.decode("utf-8", errors="replace").strip() + "\n"
           + proc.stderr.decode("utf-8", errors="replace").strip()).strip()
    if proc.returncode != 0 and not changed:
        detail = out[:500] or f"exit {proc.returncode}"
        return False, f"{fmt.name} failed: {detail}"
    summary = f"{fmt.name}: formatted {p.name}" if changed else f"{fmt.name}: no changes"
    return changed, out or summary
