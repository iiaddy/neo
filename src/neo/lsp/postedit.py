"""neo post-edit hook: format the file, then surface new LSP diagnostics.

``after_edit`` is called by the write/edit tools after a successful edit.
It never raises: every failure degrades to a short note or silence.

Dedupe contract: the caller captures diagnostics *before* the edit (see
``snapshot_diagnostics``) and passes them as ``old_diagnostics``; only
diagnostics whose (line, message) pair was not already present are
reported. This keeps the agent from re-reading errors it already knew
about.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..format.registry import format_enabled, format_file
from .client import get_diagnostics
from .servers import lsp_enabled


def dedupe(new_diags: list[dict], old_diags: list[dict]) -> list[dict]:
    """Keep only diagnostics whose (line, message) is not in old_diags."""
    seen = {(d.get("line"), d.get("message")) for d in old_diags}
    return [d for d in new_diags
            if (d.get("line"), d.get("message")) not in seen]


async def snapshot_diagnostics(path: str | Path, workdir: str | Path,
                               config: Any = None) -> list[dict]:
    """Capture current diagnostics (for the caller to pass as old_diagnostics)."""
    diags, _note = await get_diagnostics(path, workdir, config)
    return diags


async def after_edit(path: str | Path,
                     original_text: str | None,
                     workdir: str | Path,
                     config: Any = None,
                     old_diagnostics: list[dict] | None = None) -> str:
    """Format the file and report new LSP diagnostics. Returns a note string.

    - ``path``: edited file (resolved against cwd if relative).
    - ``original_text``: file content before the edit; when it equals the
      current content the edit was a no-op and "" is returned immediately.
    - ``workdir``: project root used to key the LSP client.
    - ``config``: full neo config (reads its ``lsp`` / ``format`` sections).
    - ``old_diagnostics``: diagnostics captured before the edit for dedupe.

    Returns "" when there is nothing new to say.
    """
    p = Path(path)
    try:
        current = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        current = None
    if original_text is not None and current == original_text:
        return ""

    notes: list[str] = []

    # 1. format -----------------------------------------------------------
    if format_enabled(config):
        try:
            changed, fmt_out = format_file(p, config)
            if changed:
                notes.append(f"[format] {fmt_out}")
        except Exception as exc:  # noqa: BLE001 - hook must never raise
            notes.append(f"[format] error: {type(exc).__name__}: {exc}")

    # 2. diagnostics -------------------------------------------------------
    if lsp_enabled(config):
        try:
            new_diags, lsp_note = await get_diagnostics(str(p), workdir, config)
            if lsp_note:
                notes.append(lsp_note)
            fresh = dedupe(new_diags, old_diagnostics or [])
            if fresh:
                lines = ["", "[LSP] new diagnostics after edit:"]
                for d in fresh:
                    src = f" [{d['source']}]" if d.get("source") else ""
                    lines.append(
                        f"  {d['path']}:{d['line']}:{d['col']} "
                        f"{d['severity']}: {d['message']}{src}"
                    )
                notes.append("\n".join(lines))
        except Exception as exc:  # noqa: BLE001 - hook must never raise
            notes.append(f"[LSP] error: {type(exc).__name__}: {exc}")

    return "\n".join(n for n in notes if n)
