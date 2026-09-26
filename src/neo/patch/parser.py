"""Parser for the Codex-style apply-patch envelope.

Format::

    *** Begin Patch
    *** Update File: path/to/file.py
    *** Move to: path/to/renamed.py      # optional, right after Update File
    @@  (optional hunk header)
     context line
    -removed line
    +added line
    *** Add File: path/to/new.py
    +full
    +content
    *** Delete File: path/to/old.py
    *** End Patch

Hunk lines: `` `` context, ``-`` removal, ``+`` addition. A completely empty
line inside a hunk is treated as an empty context line.

Malformed input raises :class:`ValueError` carrying the 1-based line number.
"""

from __future__ import annotations

import dataclasses


BEGIN = "*** Begin Patch"
END = "*** End Patch"
_EOF = "*** End of File"
_UPDATE = "*** Update File:"
_ADD = "*** Add File:"
_DELETE = "*** Delete File:"
_MOVE = "*** Move to:"


@dataclasses.dataclass
class PatchHunk:
    """One ``@@`` block: header text plus annotated lines.

    Each line is ``(kind, text)`` with kind in ``{"context", "remove", "add"}``.
    ``end_of_file`` (from a ``*** End of File`` marker) anchors matching at
    the end of the file.
    """

    header: str = ""
    lines: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    end_of_file: bool = False


@dataclasses.dataclass
class PatchOp:
    """A single file operation: update | add | delete (+ optional rename)."""

    op: str  # "update" | "add" | "delete"
    path: str
    hunks: list[PatchHunk] = dataclasses.field(default_factory=list)
    move_to: str | None = None


class PatchParseError(ValueError):
    """Raised for malformed envelopes; message always ends with the line no."""

    def __init__(self, lineno: int, message: str):
        super().__init__(f"patch line {lineno}: {message}")
        self.lineno = lineno


def _err(lineno: int, message: str) -> PatchParseError:
    return PatchParseError(lineno, message)


def parse_patch(text: str) -> list[PatchOp]:
    """Parse a patch envelope into a list of :class:`PatchOp`.

    Raises :class:`PatchParseError` (a :class:`ValueError`) with the
    offending 1-based line number on malformed input.
    """
    lines = text.splitlines()
    n = len(lines)
    i = 0
    # Skip leading blank lines before the envelope.
    while i < n and not lines[i].strip():
        i += 1
    if i >= n or lines[i].strip() != BEGIN:
        raise _err(i + 1, f"expected '{BEGIN}'")
    i += 1

    ops: list[PatchOp] = []
    current: PatchOp | None = None
    hunk: PatchHunk | None = None
    in_add = False

    def close_hunk() -> None:
        nonlocal hunk
        if hunk is not None and current is not None:
            current.hunks.append(hunk)
        hunk = None

    def close_op(lineno: int) -> None:
        nonlocal current, in_add
        close_hunk()
        if current is not None and current.op == "update" and not current.hunks:
            raise _err(
                lineno,
                f"Update File '{current.path}': expected at least one '@@' hunk",
            )
        current = None
        in_add = False

    while i < n:
        lineno = i + 1
        raw = lines[i]
        stripped = raw.strip()

        if stripped == END:
            close_op(lineno)
            # Trailing content after the envelope is not allowed.
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n:
                raise _err(j + 1, f"content after '{END}'")
            return ops

        if stripped.startswith(_UPDATE):
            close_op(lineno)
            path = stripped[len(_UPDATE):].strip()
            if not path:
                raise _err(lineno, "Update File needs a path")
            current = PatchOp(op="update", path=path)
            ops.append(current)
            i += 1
            continue

        if stripped.startswith(_MOVE):
            if current is None or current.op != "update":
                raise _err(lineno, "Move to: must follow an Update File block")
            if current.move_to is not None:
                raise _err(lineno, "duplicate Move to:")
            dest = stripped[len(_MOVE):].strip()
            if not dest:
                raise _err(lineno, "Move to: needs a path")
            current.move_to = dest
            i += 1
            continue

        if stripped.startswith(_ADD):
            close_op(lineno)
            path = stripped[len(_ADD):].strip()
            if not path:
                raise _err(lineno, "Add File needs a path")
            current = PatchOp(op="add", path=path)
            ops.append(current)
            in_add = True
            i += 1
            continue

        if stripped.startswith(_DELETE):
            close_op(lineno)
            path = stripped[len(_DELETE):].strip()
            if not path:
                raise _err(lineno, "Delete File needs a path")
            ops.append(PatchOp(op="delete", path=path))
            i += 1
            continue

        if current is None:
            raise _err(lineno, "expected Update/Add/Delete File")

        # Inside an op body.
        if in_add:
            if not current.hunks:
                current.hunks.append(PatchHunk(header=""))
            if raw == "":
                current.hunks[0].lines.append(("add", ""))
            elif raw.startswith("+"):
                current.hunks[0].lines.append(("add", raw[1:]))
            else:
                raise _err(lineno, "Add File content lines must start with '+'")
            i += 1
            continue

        if stripped.startswith("@@"):
            close_hunk()
            hunk = PatchHunk(header=stripped[2:].strip())
            i += 1
            continue

        if raw == _EOF:
            # EOF anchor: the current hunk is matched against the end of
            # the file first (falling back to a forward search), then closed.
            # Exact match only — a context line " *** End of File" stays data.
            if hunk is None:
                raise _err(lineno, f"'{_EOF}' needs an open '@@' hunk")
            hunk.end_of_file = True
            close_hunk()
            i += 1
            continue

        if hunk is None:
            raise _err(lineno, "expected '@@' to start a hunk")

        if raw == "":
            hunk.lines.append(("context", ""))
        elif raw.startswith(" "):
            hunk.lines.append(("context", raw[1:]))
        elif raw.startswith("-"):
            hunk.lines.append(("remove", raw[1:]))
        elif raw.startswith("+"):
            hunk.lines.append(("add", raw[1:]))
        else:
            raise _err(lineno, "hunk lines must start with ' ', '-', or '+'")
        i += 1

    close_op(n)
    raise _err(n, f"missing '{END}'")
