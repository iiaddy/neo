"""neo patch — Codex-style `*** Begin Patch` / `*** End Patch` envelope.

Submodules:
- :mod:`neo.patch.parser` — parse the envelope into ops.
- :mod:`neo.patch.applier` — apply ops to the working tree + the
  ``apply_patch`` tool.
"""

from __future__ import annotations

from .applier import ApplyPatchTool, PatchApplyError, apply_ops
from .parser import PatchHunk, PatchOp, parse_patch

__all__ = [
    "ApplyPatchTool",
    "PatchApplyError",
    "PatchHunk",
    "PatchOp",
    "apply_ops",
    "parse_patch",
]
