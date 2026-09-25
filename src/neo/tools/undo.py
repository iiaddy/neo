"""neo tools — undo: restore the worktree to a git snapshot."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult


class UndoTool(Tool):
    name = "undo"
    description = (
        "Restore the worktree to a git snapshot previously captured by the "
        "agent (a snapshot is taken automatically before every mutating tool "
        "batch). Params: handle (snapshot tree-hash, required), paths "
        "(optional list for selective restore), dry_run (show the diff that "
        "would be applied without changing anything)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "handle": {"type": "string",
                       "description": "Snapshot handle (tree hash)."},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Restore only these paths."},
            "dry_run": {"type": "boolean",
                        "description": "Preview the diff, change nothing.",
                        "default": False},
        },
        "required": ["handle"],
        "additionalProperties": False,
    }
    needs_approval = True  # destructive: always ask

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from pathlib import Path
        from ..vcs import preview_restore, restore
        handle = str(args.get("handle", "")).strip()
        if not handle:
            return ToolResult(is_error=True, output="undo: 'handle' is required.",
                              title="undo")
        paths = args.get("paths")
        if paths is not None:
            paths = [str(p) for p in paths]
        try:
            if args.get("dry_run"):
                out = preview_restore(Path(ctx.workdir), handle, paths)
                return ToolResult(output=out or "(no differences)",
                                  title="undo (dry-run)")
            out = restore(Path(ctx.workdir), handle, paths)
            return ToolResult(output=out, title="undo")
        except Exception as exc:
            return ToolResult(is_error=True,
                              output=f"undo failed: {exc}", title="undo")
