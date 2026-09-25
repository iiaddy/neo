"""shlex-based scanner that splits compound shell commands and emits
progressively generalized permission signatures.

Example::

    scan("git checkout main && rm -rf /tmp/x | tee log")
    # ["git checkout main", "git checkout *", "git *",
    #  "rm -rf /tmp/x", "rm -rf *", "rm *",
    #  "tee log", "tee *"]
"""

from __future__ import annotations

import re
import shlex

# Operators that separate independent commands (only when unquoted).
_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")


def _split_segments(cmd: str) -> list[str]:
    """Split on ;, &&, ||, | and newlines, respecting quotes/escapes."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 2
            continue
        else:
            m = _SPLIT_RE.match(cmd, i)
            if m:
                segments.append("".join(buf))
                buf = []
                i = m.end()
                continue
            buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s for s in segments if s.strip()]


def _tokenize(segment: str) -> list[str]:
    """shlex tokenize; on shlex errors fall back to naive whitespace split."""
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _signatures(argv: list[str]) -> list[str]:
    """Progressively generalize: full args, then trailing args -> `*`.

    ``["git", "checkout", "main"]`` -> ``["git checkout main",
    "git checkout *", "git *"]``. Flags stay only in the most-specific form.
    """
    prog, rest = argv[0], argv[1:]
    sigs = [" ".join(argv)]
    for i in range(len(rest) - 1, -1, -1):
        sigs.append(" ".join([prog, *rest[:i], "*"]))
    return sigs


def scan(cmd: str) -> list[str]:
    """Return permission signatures for every command in *cmd*.

    Compound commands are split on ``;``, ``&&``, ``||``, ``|`` and newlines
    (quote-aware); each segment is shlex-tokenized (naive split on shlex
    errors) and expanded into progressively generalized signatures.
    Duplicates are dropped, order preserved.
    """
    out: list[str] = []
    seen: set[str] = set()
    for segment in _split_segments(cmd or ""):
        argv = [t for t in _tokenize(segment) if t]
        if not argv:
            continue
        for sig in _signatures(argv):
            if sig not in seen:
                seen.add(sig)
                out.append(sig)
    return out


def first_word(cmd: str) -> str:
    """The program name of the first command segment (shlex-aware)."""
    segments = _split_segments(cmd or "")
    if not segments:
        return ""
    argv = [t for t in _tokenize(segments[0]) if t]
    return argv[0] if argv else ""
