"""Custom command discovery + template engine.

Template syntax (opencode-style):
- ``$1`` .. ``$9`` — positional arguments (missing -> empty string).
- ``$ARGUMENTS`` — all arguments joined with spaces.
- ``$$`` — literal ``$`` escape.
- ``!shell command`` at line start — executed pre-prompt via subprocess
  (timeout 15 s, cwd=workdir); stdout spliced in place of the line,
  stderr merged on failure, and a nonzero exit becomes an inline error note.
- ``@relative/path`` at line start — replaced with the file's contents;
  a missing file becomes an inline error note.

``!`` and ``@`` are only special at line start (first non-whitespace char).
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path
from typing import Sequence

_SHELL_TIMEOUT = 15.0
_SENTINEL = "\x00DOLLAR\x00"
_META_KEYS = ("description", "agent", "model", "subtask")


@dataclasses.dataclass
class Command:
    """A discovered custom slash command."""

    name: str  # file stem, e.g. "review"
    description: str
    path: Path
    meta: dict = dataclasses.field(default_factory=dict)  # agent/model/subtask/...
    body: str = ""  # template body after frontmatter


def _user_commands_dir() -> Path:
    home = os.path.expanduser("~")
    return Path(home) / ".config" / "neo" / "commands"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split leading `---` frontmatter into (meta, body). YAML-lite only."""
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].strip() == "---":
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is not None:
            meta: dict[str, str] = {}
            for raw in lines[1:end]:
                if ":" not in raw:
                    continue
                key, _, val = raw.partition(":")
                key = key.strip()
                if key in _META_KEYS or key.isidentifier():
                    meta[key] = val.strip()
            return meta, "\n".join(lines[end + 1 :])
    return {}, text


def _load_file(path: Path) -> Command | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _split_frontmatter(text)
    return Command(
        name=path.stem,
        description=meta.get("description", ""),
        path=path,
        meta=meta,
        body=body,
    )


def discover(workdir: str | Path) -> list[Command]:
    """Find custom commands: `.neo/commands/*.md` then `~/.config/neo/commands/*.md`.

    Project commands shadow user commands of the same name.
    """
    found: dict[str, Command] = {}
    for base in (Path(workdir) / ".neo" / "commands", _user_commands_dir()):
        if not base.is_dir():
            continue
        for md in sorted(base.glob("*.md")):
            cmd = _load_file(md)
            if cmd is not None and cmd.name not in found:
                found[cmd.name] = cmd
    return [found[name] for name in sorted(found)]


# ---------------------------------------------------------------- rendering


def _substitute_vars(text: str, args: list[str]) -> str:
    out = text.replace("$$", _SENTINEL)
    for i in range(1, 10):
        out = out.replace(f"${i}", args[i - 1] if i - 1 < len(args) else "")
    out = out.replace("$ARGUMENTS", " ".join(args))
    return out.replace(_SENTINEL, "$")


def _resolve_at(path_raw: str, workdir: Path) -> Path:
    p = Path(path_raw).expanduser()
    if not p.is_absolute():
        p = workdir / p
    return p


def _run_directive(shell_cmd: str, workdir: Path) -> str:
    try:
        proc = subprocess.run(
            shell_cmd,
            shell=True,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=_SHELL_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"<shell timed out after {_SHELL_TIMEOUT:.0f}s: {shell_cmd}>"
    except OSError as exc:
        return f"<shell failed to start: {exc}>"
    out = proc.stdout
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        note = f"<shell exited {proc.returncode}: {shell_cmd}"
        if err:
            note += f"\n{err}"
        note += ">"
        return f"{out.rstrip()}\n{note}" if out.strip() else note
    return out.rstrip("\n")


def render(body: str, args: Sequence[str] | str, workdir: str | Path) -> str:
    """Expand a command template.

    *args* may be a list of positional arguments or a single raw string
    (split on whitespace).
    """
    if isinstance(args, str):
        arg_list = args.split()
    else:
        arg_list = [str(a) for a in args]
    workdir = Path(workdir)
    rendered: list[str] = []
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("!") and not stripped.startswith("!!"):
            cmd = _substitute_vars(stripped[1:], arg_list)
            rendered.append(_run_directive(cmd, workdir))
        elif stripped.startswith("@") and not stripped.startswith("@@"):
            target = _substitute_vars(stripped[1:].strip(), arg_list)
            fp = _resolve_at(target, workdir)
            try:
                rendered.append(fp.read_text(encoding="utf-8"))
            except OSError:
                rendered.append(f"<file not found: {target}>")
        else:
            rendered.append(_substitute_vars(line, arg_list))
    return "\n".join(rendered)
