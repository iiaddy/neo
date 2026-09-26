"""System prompt assembly and project-notes discovery."""
from __future__ import annotations

import datetime
import os
from pathlib import Path

from ..prompts import load_prompt

# Base prompts live as markdown files under ``neo.prompts`` (opencode-style)
# so they can be read and tuned without touching Python.
NEO_SYSTEM_BASE = load_prompt("system")

SUMMARY_PROMPT = load_prompt("summary")


MAX_STEPS_PROMPT = load_prompt("max_steps")


MEMORY_SYSTEM = load_prompt("memory")


def _git_root(start: Path) -> Path | None:
    node = start.resolve()
    while True:
        if (node / ".git").exists():
            return node
        parent = node.parent
        if parent == node:
            return None
        node = parent


def load_project_notes(workdir: str | Path) -> list[tuple[str, str]]:
    """Collect AGENTS.md-style project instructions.

    Walks from the git root (or workdir) down to cwd, then adds the global
    notes file. First file found per directory wins; results are deduplicated.
    """
    workdir = Path(workdir).resolve()
    root = _git_root(workdir) or workdir
    notes: list[tuple[str, str]] = []
    seen: set[str] = set()

    def take(path: Path):
        key = str(path.resolve())
        if key in seen or not path.is_file():
            return
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return
        if content.strip():
            seen.add(key)
            notes.append((str(path), content))

    node = root
    ups = []
    n = workdir
    while True:
        ups.append(n)
        if n == root or n.parent == n:
            break
        n = n.parent
    for d in reversed(ups):
        for name in ("AGENTS.md", ".neo/AGENTS.md"):
            take(d / name)
    take(Path.home() / ".config" / "neo" / "AGENTS.md")
    return notes


def load_memory(workdir: str | Path) -> list[tuple[str, str]]:
    """Collect MEMORY.md long-term memory files.

    Same walk as load_project_notes: from the git root (or workdir) down to
    cwd, then the global file. These hold durable facts the agent recorded in
    earlier sessions — user preferences, project conventions, decisions.
    """
    workdir = Path(workdir).resolve()
    root = _git_root(workdir) or workdir
    notes: list[tuple[str, str]] = []
    seen: set[str] = set()

    def take(path: Path):
        key = str(path.resolve())
        if key in seen or not path.is_file():
            return
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return
        if content.strip():
            seen.add(key)
            notes.append((str(path), content))

    node = root
    ups = []
    n = workdir
    while True:
        ups.append(n)
        if n == root or n.parent == n:
            break
        n = n.parent
    for d in reversed(ups):
        for name in ("MEMORY.md", ".neo/MEMORY.md"):
            take(d / name)
    take(Path.home() / ".config" / "neo" / "MEMORY.md")
    return notes


def build_system_prompt(*, tools: dict, project_notes: list[tuple[str, str]],
                        memory_notes: list[tuple[str, str]] | None = None,
                        skills_index: str = "", extra: str = "",
                        mcp_instructions: str = "", model: str = "",
                        workdir: str | Path | None = None) -> str:
    parts = [NEO_SYSTEM_BASE, MEMORY_SYSTEM]
    env_lines = ["## Environment"]
    if model:
        env_lines.append(f"- You are powered by the model `{model}`.")
    wd = Path(workdir).resolve() if workdir else Path(os.getcwd())
    env_lines.append(f"- Working directory: {wd}")
    env_lines.append(f"- Is directory a git repo: "
                     f"{'yes' if _git_root(wd) else 'no'}")
    env_lines.append(f"- Date: {datetime.date.today().isoformat()}")
    env_lines.append(f"- Platform: {os.name}")
    parts.append("\n".join(env_lines))
    if tools:
        lines = ["## Tools"]
        for name in sorted(tools):
            t = tools[name]
            desc = (t.description or "").strip().split("\n")[0]
            lines.append(f"- `{name}`: {desc}")
        parts.append("\n".join(lines))
    for path, content in project_notes:
        parts.append(f"## Project notes ({path})\n{content.strip()}")
    for path, content in (memory_notes or []):
        parts.append(f"## Long-term memory ({path})\n{content.strip()}")
    if skills_index:
        parts.append(f"## Available skills\n{skills_index}\n"
                     "Load a skill with the `skill` tool when its expertise applies.")
    if mcp_instructions:
        parts.append(mcp_instructions)
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)
