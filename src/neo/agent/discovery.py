"""Discovery of markdown-defined skills, commands and agents."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, text[m.end():]


def _search_dirs(workdir: Path, sub: str) -> list[Path]:
    dirs = [workdir / ".neo" / sub, Path.home() / ".config" / "neo" / sub]
    return [d for d in dirs if d.is_dir()]


@dataclass
class SkillInfo:
    name: str
    path: Path
    description: str = ""
    content: str = ""


@dataclass
class CommandInfo:
    name: str
    path: Path
    description: str = ""
    template: str = ""


@dataclass
class AgentInfo:
    name: str
    path: Path
    description: str = ""
    prompt: str = ""
    model: str = ""
    mode: str = "subagent"  # subagent|primary|all
    permission: dict = field(default_factory=dict)


def find_skills(workdir: str | Path) -> dict[str, SkillInfo]:
    out: dict[str, SkillInfo] = {}
    for d in _search_dirs(Path(workdir), "skills"):
        for skill_md in sorted(d.glob("*/SKILL.md")):
            meta, body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            name = meta.get("name", skill_md.parent.name)
            if name not in out:
                out[name] = SkillInfo(name=name, path=skill_md,
                                      description=meta.get("description", ""),
                                      content=body.strip())
    return out


def find_commands(workdir: str | Path) -> dict[str, CommandInfo]:
    out: dict[str, CommandInfo] = {}
    for d in _search_dirs(Path(workdir), "commands"):
        for md in sorted(d.glob("*.md")):
            meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
            name = md.stem
            if name not in out:
                out[name] = CommandInfo(name=name, path=md,
                                        description=meta.get("description", ""),
                                        template=body.strip())
    return out


def find_agents(workdir: str | Path) -> dict[str, AgentInfo]:
    out: dict[str, AgentInfo] = {}
    for d in _search_dirs(Path(workdir), "agents"):
        for md in sorted(d.glob("*.md")):
            meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
            name = md.stem
            if name not in out:
                out[name] = AgentInfo(
                    name=name, path=md,
                    description=meta.get("description", ""),
                    prompt=body.strip(),
                    model=meta.get("model", ""),
                    mode=meta.get("mode", "subagent"),
                    permission={},
                )
    return out


def expand_command_template(template: str, args: list[str]) -> str:
    """Substitute $1..$n and $ARGUMENTS in a command template."""
    out = template
    for i, a in enumerate(args, start=1):
        out = out.replace(f"${i}", a)
    out = out.replace("$ARGUMENTS", " ".join(args))
    if args and "$1" not in template and "$ARGUMENTS" not in template:
        out = out.rstrip() + "\n\n" + " ".join(args)
    return out


def builtin_agents() -> dict[str, AgentInfo]:
    return {
        "explore": AgentInfo(
            name="explore", path=Path("<builtin>"),
            description="Read-only codebase exploration",
            prompt=("You are an exploration subagent. Read files, search the codebase, "
                    "and report findings concisely. Do not modify anything. "
                    "Return a structured summary with file paths and line numbers."),
            mode="subagent"),
        "general": AgentInfo(
            name="general", path=Path("<builtin>"),
            description="General-purpose subagent for multi-step work",
            prompt=("You are a general subagent. Complete the task thoroughly and "
                    "return a concise summary of what you did and the key results."),
            mode="subagent"),
        "plan": AgentInfo(
            name="plan", path=Path("<builtin>"),
            description="Plan mode: research and design, no code changes",
            prompt=("You are in plan mode. Research the codebase and produce a detailed "
                    "implementation plan. Do NOT write or edit code. End with the plan."),
            mode="primary"),
    }
