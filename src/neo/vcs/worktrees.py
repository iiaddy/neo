"""Git worktrees: create/remove/list/boot, via the git CLI.

Clean ``VCSError`` (a ValueError) when the workdir is not a git repo or git
is missing. No force-push, no reset — this module only manages worktree
lifecycles.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .snapshots import VCSError, _git_bin, _run


# Upper bound for a single boot command; boot scripts must be finite.
_BOOT_TIMEOUT = 120.0


def _ensure_repo(workdir: Path) -> None:
    try:
        r = _run(["rev-parse", "--git-dir"], workdir)
    except VCSError:
        raise
    except OSError as exc:
        raise VCSError(f"cannot run git: {exc}") from exc
    if r.returncode != 0:
        raise VCSError(f"{workdir} is not a git repository")


def create(workdir: Path, path: str | Path, branch: str | None = None) -> str:
    """Add a worktree at ``path`` (optionally on a new ``branch``).

    Returns the resolved path as a string.
    """
    _ensure_repo(workdir)
    args = ["worktree", "add"]
    if branch:
        args += ["-b", branch]
    args.append(str(path))
    r = _run(args, workdir)
    if r.returncode != 0:
        raise VCSError(f"git worktree add failed: {r.stderr.strip()}")
    # Resolve relative to the workdir, not the process cwd (Path.resolve
    # alone would use os.getcwd()).
    p = Path(path)
    if not p.is_absolute():
        p = workdir / p
    return str(p.resolve())


def remove(workdir: Path, path: str | Path, force: bool = False) -> None:
    """Remove the worktree at ``path`` (``--force`` if requested)."""
    _ensure_repo(workdir)
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    r = _run(args, workdir)
    if r.returncode != 0:
        raise VCSError(f"git worktree remove failed: {r.stderr.strip()}")


def list_worktrees(workdir: Path) -> list[dict]:
    """Parse ``git worktree list --porcelain`` into dicts.

    Each dict: {"path", "head", "branch" (short name or None), "detached",
    "bare", "locked" (bool)}.
    """
    _ensure_repo(workdir)
    r = _run(["worktree", "list", "--porcelain"], workdir)
    if r.returncode != 0:
        raise VCSError(f"git worktree list failed: {r.stderr.strip()}")
    items: list[dict] = []
    cur: dict = {}
    for line in r.stdout.splitlines():
        if not line.strip():
            if cur:
                items.append(cur)
                cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur["path"] = val
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.removeprefix("refs/heads/")
            cur["detached"] = False
        elif key == "detached":
            cur["branch"] = None
            cur["detached"] = True
        elif key == "bare":
            cur["bare"] = True
        elif key == "locked":
            cur["locked"] = True
    if cur:
        items.append(cur)
    for it in items:
        it.setdefault("branch", None)
        it.setdefault("detached", False)
        it.setdefault("bare", False)
        it.setdefault("locked", False)
        it.setdefault("head", None)
    return items


def boot(workdir: Path, path: str | Path, commands: list[str],
         timeout: float = _BOOT_TIMEOUT) -> str:
    """Run ``commands`` sequentially inside the worktree, capturing output.

    ``timeout`` bounds each command in seconds. Returns the combined
    stdout/stderr of all commands. Raises VCSError on the first non-zero
    exit or timeout (output included in the message).
    """
    _ensure_repo(workdir)
    wt = Path(path)
    if not wt.is_absolute():
        wt = workdir / wt
    wt = wt.resolve()
    out: list[str] = []
    for cmd in commands:
        try:
            r = subprocess.run(cmd, shell=True, cwd=str(wt),
                               capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            out.append(f"$ {cmd}\n{exc.stdout or ''}{exc.stderr or ''}")
            raise VCSError(
                f"boot command timed out after {timeout:g}s: {cmd!r}\n"
                f"{exc.stdout or ''}{exc.stderr or ''}"
            ) from exc
        except OSError as exc:
            raise VCSError(f"boot command failed to start ({cmd!r}): {exc}") from exc
        out.append(f"$ {cmd}\n{r.stdout}{r.stderr}")
        if r.returncode != 0:
            raise VCSError(
                f"boot command failed (exit {r.returncode}): {cmd!r}\n"
                f"{r.stdout}{r.stderr}"
            )
    return "".join(out)
