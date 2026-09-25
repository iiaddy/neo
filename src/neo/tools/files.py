"""neo tools — file system tools: read, list_dir, glob, grep, write, edit."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

from .base import Tool, ToolContext, ToolResult, path_lock

_READ_LIMIT = 2000
_GLOB_CAP = 200
_GREP_CAP = 100
_OUTPUT_CAP = 60_000


def _resolve(ctx: ToolContext, raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = ctx.workdir / p
    return p


def _rel(ctx: ToolContext, p: Path) -> str:
    try:
        return str(p.relative_to(ctx.workdir))
    except ValueError:
        return str(p)


async def _snapshot_diags(ctx: ToolContext, path: Path) -> list[dict]:
    """Capture LSP diagnostics *before* an edit (best effort; [] on failure)."""
    try:
        from ..lsp.postedit import snapshot_diagnostics
        return await snapshot_diagnostics(str(path), ctx.workdir, ctx.config)
    except Exception:
        return []


async def _post_edit_note(ctx: ToolContext, path: Path,
                          original_text: str | None,
                          old_diags: list[dict] | None = None) -> str:
    """Format the file and report new LSP diagnostics. Never raises."""
    try:
        from ..lsp.postedit import after_edit
        return await after_edit(str(path), original_text, ctx.workdir,
                                ctx.config, old_diagnostics=old_diags)
    except Exception:
        return ""


class ReadTool(Tool):
    name = "read"
    description = (
        "Read a text file and return its contents with line numbers. "
        "Use list_dir for directories."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file."},
            "offset": {"type": "integer", "description": "First line to read (1-based).", "default": 1},
            "limit": {"type": "integer", "description": "Max lines to return.", "default": 2000},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    needs_approval = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx, args["path"])
        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, min(int(args.get("limit", _READ_LIMIT)), _READ_LIMIT))
        async with path_lock(ctx, path):
            if not path.exists():
                return ToolResult(is_error=True, output=f"File not found: {path}", title=self.name)
            if path.is_dir():
                return ToolResult(
                    is_error=True,
                    output=f"{path} is a directory; use list_dir instead.",
                    title=self.name,
                )
            raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            return ToolResult(
                is_error=True,
                output=f"{path}: binary file omitted.",
                title=self.name,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                is_error=True,
                output=f"{path}: not valid UTF-8; binary omitted.",
                title=self.name,
            )
        lines = text.splitlines()
        total = len(lines)
        if offset > total:
            return ToolResult(output="", title=f"{_rel(ctx, path)}:{offset}")
        picked = lines[offset - 1 : offset - 1 + limit]
        out = "\n".join(f"{offset + i}| {line}" for i, line in enumerate(picked))
        if offset - 1 + limit < total:
            out += f"\n... ({total - (offset - 1 + limit)} more lines; re-read with offset)"
        return ToolResult(output=out, title=f"{_rel(ctx, path)}:{offset}-{offset + len(picked) - 1}")


class ListDirTool(Tool):
    name = "list_dir"
    description = "List entries of a directory, sorted; directories get a trailing '/'."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path.", "default": "."},
        },
        "additionalProperties": False,
    }
    needs_approval = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx, args.get("path", "."))
        if not path.exists():
            return ToolResult(is_error=True, output=f"Path not found: {path}", title=self.name)
        if not path.is_dir():
            return ToolResult(is_error=True, output=f"Not a directory: {path}", title=self.name)
        entries = []
        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            entries.append(child.name + "/" if child.is_dir() else child.name)
        return ToolResult(output="\n".join(entries), title=_rel(ctx, path) or ".")


class GlobTool(Tool):
    name = "glob"
    description = "Find files matching a glob pattern (recursive). Pattern examples: '**/*.py', 'src/*.ts'."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern."},
            "path": {"type": "string", "description": "Base directory.", "default": "."},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    needs_approval = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        base = _resolve(ctx, args.get("path", "."))
        pattern = args["pattern"]
        if not base.is_dir():
            return ToolResult(is_error=True, output=f"Not a directory: {base}", title=self.name)
        matches = []
        for hit in sorted(base.rglob(pattern)):
            if hit.is_file():
                matches.append(_rel(ctx, hit))
                if len(matches) >= _GLOB_CAP:
                    break
        out = "\n".join(matches)
        if len(matches) >= _GLOB_CAP:
            out += f"\n... (capped at {_GLOB_CAP} results; narrow the pattern)"
        return ToolResult(output=out or "(no matches)", title=f"glob {pattern}")


class GrepTool(Tool):
    name = "grep"
    description = "Search file contents with a regex pattern. Uses ripgrep when available."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern."},
            "path": {"type": "string", "description": "File or directory to search.", "default": "."},
            "include": {"type": "string", "description": "Glob filter for file names.", "default": "*"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    needs_approval = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        target = _resolve(ctx, args.get("path", "."))
        pattern = args["pattern"]
        include = args.get("include", "*") or "*"
        if not target.exists():
            return ToolResult(is_error=True, output=f"Path not found: {target}", title=self.name)
        if shutil.which("rg"):
            return await self._via_rg(ctx, target, pattern, include)
        return await self._via_python(ctx, target, pattern, include)

    async def _via_rg(self, ctx: ToolContext, target: Path, pattern: str, include: str) -> ToolResult:
        cmd = [
            "rg", "--no-heading", "--line-number",
            f"--max-count={_GREP_CAP}", "--glob", include,
            "-e", pattern,
        ]
        if target.is_file():
            cmd.append(str(target))
        try:
            proc = await _run_subprocess(cmd, cwd=target if target.is_dir() else target.parent)
        except Exception as exc:
            return ToolResult(is_error=True, output=f"rg failed: {exc}", title=self.name)
        out = proc.stdout.strip()
        if proc.returncode not in (0, 1):
            return ToolResult(is_error=True, output=f"rg error: {out[:2000]}", title=self.name)
        lines = out.splitlines()[:_GREP_CAP]
        note = ""
        if len(out.splitlines()) > _GREP_CAP:
            note = f"\n... (capped at {_GREP_CAP} matches)"
        return ToolResult(output=("\n".join(lines) or "(no matches)") + note, title=f"grep {pattern}")

    async def _via_python(self, ctx: ToolContext, target: Path, pattern: str, include: str) -> ToolResult:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return ToolResult(is_error=True, output=f"Invalid regex: {exc}", title=self.name)
        files: list[Path] = []
        if target.is_file():
            files = [target]
        else:
            for root, dirs, names in os.walk(target):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
                for n in names:
                    if n.startswith("."):
                        continue
                    fp = Path(root) / n
                    if fnmatch.fnmatch(n, include):
                        files.append(fp)
        hits: list[str] = []
        for fp in sorted(files):
            try:
                data = fp.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                continue
            for ln, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{_rel(ctx, fp)}:{ln}: {line.strip()}")
                    if len(hits) >= _GREP_CAP:
                        break
            if len(hits) >= _GREP_CAP:
                break
        out = "\n".join(hits) or "(no matches)"
        if len(hits) >= _GREP_CAP:
            out += f"\n... (capped at {_GREP_CAP} matches)"
        return ToolResult(output=out, title=f"grep {pattern}")


class WriteTool(Tool):
    name = "write"
    description = "Write (or overwrite) a file, creating parent directories as needed."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file."},
            "content": {"type": "string", "description": "Full file content (UTF-8)."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx, args["path"])
        content = args["content"]
        async with path_lock(ctx, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        output = f"Wrote {len(content)} chars to {_rel(ctx, path)}"
        output += await _post_edit_note(ctx, path, None)
        return ToolResult(output=output, title=_rel(ctx, path))


# ---------------------------------------------------------------- edit tool


def _all_spans(text: str, old: str) -> list[tuple[int, int]]:
    """All non-overlapping (start, end) spans of `old` in `text`."""
    if not old:
        return []
    spans = []
    i = 0
    while True:
        j = text.find(old, i)
        if j < 0:
            return spans
        spans.append((j, j + len(old)))
        i = j + max(len(old), 1)


def _line_trimmed_spans(text: str, old: str) -> list[tuple[int, int]]:
    tlines = text.splitlines(keepends=True)
    olines = old.splitlines()
    tstripped = [ln.strip() for ln in tlines]
    ostripped = [ln.strip() for ln in olines]
    n = len(olines)
    spans = []
    i = 0
    while i <= len(tlines) - n:
        if tstripped[i : i + n] == ostripped:
            start = sum(len(tlines[k]) for k in range(i))
            end = sum(len(tlines[k]) for k in range(i + n))
            spans.append((start, end))
            i += n
        else:
            i += 1
    return spans


def _whitespace_normalized_spans(text: str, old: str) -> list[tuple[int, int]]:
    def normalize(s: str) -> tuple[str, list[int]]:
        out: list[str] = []
        idx: list[int] = []
        i = 0
        while i < len(s):
            ch = s[i]
            if ch.isspace():
                out.append(" ")
                idx.append(i)
                while i < len(s) and s[i].isspace():
                    i += 1
            else:
                out.append(ch)
                idx.append(i)
                i += 1
        return "".join(out), idx

    tnorm, tmap = normalize(text)
    onorm, _ = normalize(old)
    spans = []
    for s, e in _all_spans(tnorm, onorm):
        spans.append((tmap[s], tmap[e - 1] + 1))
    return spans


def _dedent_lines(lines: list[str]) -> tuple[list[str], list[int]]:
    """Strip common leading whitespace (textwrap.dedent semantics on a slice).
    Returns (dedented lines, per-line strip widths)."""
    margin: str | None = None
    for ln in lines:
        if not ln.strip():
            continue
        indent = ln[: len(ln) - len(ln.lstrip())]
        if margin is None or len(indent) < len(margin):
            margin = indent
    margin = margin or ""
    stripped = []
    widths = []
    for ln in lines:
        if ln.startswith(margin):
            stripped.append(ln[len(margin) :])
            widths.append(len(margin))
        else:
            stripped.append(ln)
            widths.append(0)
    return stripped, widths


def _indent_flexible_spans(text: str, old: str) -> list[tuple[int, int]]:
    tlines = text.splitlines(keepends=True)
    olines = old.splitlines()
    tstrip, _ = _dedent_lines(tlines)
    ostr, _ = _dedent_lines(olines)
    tkeys = [ln.strip("\n").rstrip() for ln in tstrip]
    okeys = [ln.strip("\n").rstrip() for ln in ostr]
    n = len(okeys)
    line_starts = []
    acc = 0
    for ln in tlines:
        line_starts.append(acc)
        acc += len(ln)
    spans = []
    i = 0
    while i <= len(tkeys) - n:
        if tkeys[i : i + n] == okeys:
            spans.append((line_starts[i], line_starts[i] + sum(len(tlines[k]) for k in range(i, i + n))))
            i += n
        else:
            i += 1
    return spans


def _unescape(s: str) -> str:
    sentinel = "\x00"
    s = s.replace("\\\\", sentinel)
    s = s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    return s.replace(sentinel, "\\")


class EditTool(Tool):
    name = "edit"
    description = (
        "Replace text in a file. Strategies tried in order: exact match, "
        "line-trimmed, whitespace-normalized, indentation-flexible, "
        "escape-normalized. Requires exactly one match unless replace_all."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file."},
            "old": {"type": "string", "description": "Text to replace."},
            "new": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "description": "Replace every match.", "default": False},
        },
        "required": ["path", "old", "new"],
        "additionalProperties": False,
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx, args["path"])
        old = args["old"]
        new = args["new"]
        replace_all = bool(args.get("replace_all", False))
        if old == new:
            return ToolResult(is_error=True, output="old and new are identical; nothing to do.", title=self.name)
        if not old:
            return ToolResult(is_error=True, output="old is empty; use write to create content.", title=self.name)
        if not path.exists():
            return ToolResult(is_error=True, output=f"File not found: {path}", title=self.name)
        if path.is_dir():
            return ToolResult(is_error=True, output=f"{path} is a directory.", title=self.name)

        old_diags = await _snapshot_diags(ctx, path)
        async with path_lock(ctx, path):
            text = path.read_text(encoding="utf-8")

        newline = "\r\n" if "\r\n" in text else "\n"

        strategies = [
            ("exact", lambda: _all_spans(text, old), old, new),
            ("line-trimmed", lambda: _line_trimmed_spans(text, old), old, new),
            ("whitespace-normalized", lambda: _whitespace_normalized_spans(text, old), old, new),
            ("indentation-flexible", lambda: _indent_flexible_spans(text, old), old, new),
            ("escape-normalized", lambda: _all_spans(text, _unescape(old)), _unescape(old), _unescape(new)),
        ]
        spans: list[tuple[int, int]] = []
        chosen = ""
        rep_new = new
        for sname, fn, _o, _n in strategies:
            found = fn()
            if found:
                spans, chosen, rep_new = found, sname, _n
                break
        if not spans:
            return ToolResult(is_error=True, output="old text not found in file.", title=self.name)
        if not replace_all:
            if len(spans) > 1:
                return ToolResult(
                    is_error=True,
                    output=f"old text matched {len(spans)} times; not unique. Pass replace_all=true or narrow the text.",
                    title=self.name,
                )
            if chosen != "exact" and (spans[0][1] - spans[0][0]) > 3 * len(old):
                return ToolResult(
                    is_error=True,
                    output="match too fuzzy, re-read the file",
                    title=self.name,
                )
        # Preserve the file's newline style in the replacement text.
        if newline == "\r\n":
            rep_new = rep_new.replace("\n", "\r\n")
        updated = text
        for s, e in sorted(spans, reverse=True):
            updated = updated[:s] + rep_new + updated[e:]
        async with path_lock(ctx, path):
            path.write_text(updated, encoding="utf-8")
        n = len(spans)
        output = (f"Replaced {n} occurrence{'s' if n != 1 else ''} in {_rel(ctx, path)} "
                  f"(strategy: {chosen}).")
        output += await _post_edit_note(ctx, path, text, old_diags)
        return ToolResult(
            output=output,
            title=_rel(ctx, path),
        )


async def _run_subprocess(cmd: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    """Shared async subprocess runner used by grep's rg path."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    out, err = await proc.communicate()
    return subprocess.CompletedProcess(cmd, proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
