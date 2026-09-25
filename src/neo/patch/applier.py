"""Apply parsed patch ops to the working tree, plus the apply_patch tool."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..tools.base import Tool, ToolContext, ToolResult, path_lock
from .parser import PatchHunk, PatchOp, parse_patch


class PatchApplyError(Exception):
    """A patch op could not be applied (missing file, hunk mismatch, ...)."""


def _safe_resolve(workdir: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = workdir / p
    resolved = p.resolve()
    base = workdir.resolve()
    if resolved != base and base not in resolved.parents:
        raise PatchApplyError(f"path escapes workdir: {raw}")
    return resolved


def _match_hunk(text_lines: list[str], hunk: PatchHunk) -> int | None:
    """Find the file line index where the hunk's old block starts.

    Old block = context + remove lines. Exact match first, then
    whitespace-insensitive (line-trimmed) match. Returns None if no match.
    """
    old = [t for kind, t in hunk.lines if kind in ("context", "remove")]
    if not old:
        return 0  # pure-addition hunk applies at the top
    exact = _find_block(text_lines, old, lambda a, b: a == b)
    if exact is not None:
        return exact
    return _find_block(text_lines, old, lambda a, b: a.strip() == b.strip())


def _find_block(lines: list[str], block: list[str], eq) -> int | None:
    n = len(block)
    for i in range(len(lines) - n + 1):
        if all(eq(lines[i + k], block[k]) for k in range(n)):
            return i
    return None


def _apply_hunk(text_lines: list[str], hunk: PatchHunk, hunk_no: int,
                path: str) -> list[str]:
    start = _match_hunk(text_lines, hunk)
    if start is None:
        raise PatchApplyError(
            f"hunk {hunk_no} of {path} does not match "
            f"(context changed; {len(hunk.lines)} lines)"
        )
    old_len = sum(1 for kind, _ in hunk.lines if kind in ("context", "remove"))
    new_block = [t for kind, t in hunk.lines if kind in ("context", "add")]
    return text_lines[:start] + new_block + text_lines[start + old_len:]


def _apply_update(op: PatchOp, workdir: Path) -> str:
    path = _safe_resolve(workdir, op.path)
    if not path.is_file():
        raise PatchApplyError(f"update target not found: {op.path}")
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    for n, hunk in enumerate(op.hunks, 1):
        lines = _apply_hunk(lines, hunk, n, op.path)
    path.write_text("\n".join(lines), encoding="utf-8")
    note = f"updated {op.path} ({len(op.hunks)} hunk(s))"
    if op.move_to:
        dest = _safe_resolve(workdir, op.move_to)
        dest.parent.mkdir(parents=True, exist_ok=True)
        path.rename(dest)
        note += f" -> moved to {op.move_to}"
    return note


def _apply_add(op: PatchOp, workdir: Path) -> str:
    path = _safe_resolve(workdir, op.path)
    if path.exists():
        raise PatchApplyError(f"add target already exists: {op.path}")
    content_lines = [t for hunk in op.hunks for kind, t in hunk.lines
                     if kind == "add"]
    content = "\n".join(content_lines)
    if content_lines:
        content += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"added {op.path} ({len(content_lines)} line(s))"


def _apply_delete(op: PatchOp, workdir: Path) -> str:
    path = _safe_resolve(workdir, op.path)
    if not path.exists():
        raise PatchApplyError(f"delete target not found: {op.path}")
    if path.is_dir():
        raise PatchApplyError(f"delete target is a directory: {op.path}")
    path.unlink()
    return f"deleted {op.path}"


def apply_ops(ops: Sequence[PatchOp], workdir: Path) -> list[str]:
    """Apply ops to files under *workdir*.

    Returns one note string per op. Raises :class:`PatchApplyError`
    naming the failing op/hunk; ops applied before the failure are kept
    (no rollback), matching Codex semantics.
    """
    notes: list[str] = []
    workdir = Path(workdir)
    for op in ops:
        if op.op == "update":
            notes.append(_apply_update(op, workdir))
        elif op.op == "add":
            notes.append(_apply_add(op, workdir))
        elif op.op == "delete":
            notes.append(_apply_delete(op, workdir))
        else:
            raise PatchApplyError(f"unknown op: {op.op}")
    return notes


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "Apply a Codex-style patch envelope ('*** Begin Patch' ... '*** End Patch') "
        "with Update/Add/Delete File blocks and @@ hunks. Context lines start with "
        "a space, '-' removes, '+' adds."
    )
    parameters = {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "Full patch text in the *** Begin Patch envelope.",
            },
        },
        "required": ["patch"],
        "additionalProperties": False,
    }
    needs_approval = True

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        patch_text = str(args.get("patch", ""))
        if not patch_text.strip():
            return ToolResult(is_error=True, output="empty patch", title=self.name)
        try:
            ops = parse_patch(patch_text)
        except ValueError as exc:
            return ToolResult(is_error=True, output=f"parse error: {exc}", title=self.name)
        if not ops:
            return ToolResult(is_error=True, output="patch has no operations", title=self.name)
        try:
            notes: list[str] = []
            for op in ops:
                lock_path = Path(op.move_to or op.path)
                async with path_lock(ctx, lock_path):
                    notes.extend(apply_ops([op], ctx.workdir))
        except PatchApplyError as exc:
            detail = f"patch failed: {exc}"
            if notes:
                detail += "\nApplied before failure:\n" + "\n".join(f"- {n}" for n in notes)
            return ToolResult(is_error=True, output=detail, title=self.name)
        return ToolResult(output="\n".join(notes), title=self.name)
