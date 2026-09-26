"""Git snapshots: capture/restore/preview worktree state for undo.

Mechanism (adapted from opencode's shadow-git snapshot idea, simplified):
instead of a shadow repo, we stage into a **throwaway index** (``GIT_INDEX_FILE``
pointing at a temp file) so the user's real index is never touched, then
``git write-tree`` captures the staged worktree as a tree object. The tree
hash doubles as the snapshot handle.

Records live in ``<workdir>/.neo/snapshots.json`` as a plain JSON list::

    {"handle": "<tree>", "tree": "<tree>", "head": "<commit>" | None,
     "message": "...", "ts": 1234.5}

Restore is deliberately conservative: only ``git checkout <tree> -- ...``,
never ``git reset --hard``. Whole-tree restore rewinds tracked files to the
snapshot; untracked files added *after* the snapshot are left in place.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class VCSError(ValueError):
    """Clean, catchable error for VCS problems (not-a-repo, unknown handle...)."""


_SNAP_FILE = ".neo/snapshots.json"
_HANDLE_LEN = 40

# Cap on stored snapshot records; older ones are pruned on each new snapshot.
_MAX_RECORDS = 50


def _git_bin() -> str:
    exe = shutil.which("git")
    if not exe:
        raise VCSError("git is not installed or not on PATH")
    return exe


def _run(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_git_bin(), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def _is_repo(workdir: Path) -> bool:
    r = _run(["rev-parse", "--git-dir"], workdir)
    return r.returncode == 0


def _snap_path(workdir: Path) -> Path:
    return workdir / _SNAP_FILE


def _read_records(workdir: Path) -> list[dict]:
    p = _snap_path(workdir)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_records(workdir: Path, records: list[dict]) -> None:
    p = _snap_path(workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(p)


def _find(workdir: Path, handle: str) -> dict:
    for rec in _read_records(workdir):
        if rec.get("handle") == handle or rec.get("tree") == handle:
            return rec
    raise VCSError(f"unknown snapshot handle: {handle}")


def snapshot(workdir: Path, message: str = "") -> str | None:
    """Capture the current worktree (tracked + untracked, all dirs) as a tree.

    Never touches the user's real index: staging happens in a throwaway
    index file. Returns the tree hash handle, or None if not a git repo /
    git is missing.
    """
    try:
        if not _is_repo(workdir):
            return None
        base_env = dict(os.environ)
        with tempfile.TemporaryDirectory(prefix="neo-snap-") as tmpd:
            env = dict(base_env)
            env["GIT_INDEX_FILE"] = os.path.join(tmpd, "index")
            r = _run(["add", "-A"], workdir, env=env)
            if r.returncode != 0:
                return None
            # Never capture the snapshot records file itself: restore()
            # checks out the whole tree, which would otherwise rewind the
            # records and destroy the handles of every later snapshot.
            _run(["rm", "--cached", "--ignore-unmatch", "-q", "--", _SNAP_FILE],
                 workdir, env=env)
            r = _run(["write-tree"], workdir, env=env)
            if r.returncode != 0:
                return None
            tree = r.stdout.strip()
        # HEAD may not exist yet (unborn branch); that is fine.
        r = _run(["rev-parse", "HEAD"], workdir)
        head = r.stdout.strip() if r.returncode == 0 else None
        rec = {"handle": tree, "tree": tree, "head": head,
               "message": message, "ts": time.time()}
        records = _read_records(workdir)
        records.append(rec)
        pruned = 0
        if len(records) > _MAX_RECORDS:
            # Keep the newest records; pruned trees still exist as git
            # objects, but their handles are no longer restorable.
            pruned = len(records) - _MAX_RECORDS
            records = records[-_MAX_RECORDS:]
        _write_records(workdir, records)
        if pruned:
            print(f"[neo] pruned {pruned} old snapshot(s); "
                  f"keeping newest {_MAX_RECORDS}", file=sys.stderr)
        return tree
    except VCSError:
        return None
    except OSError:
        return None


def list_snapshots(workdir: Path) -> list[dict]:
    """All snapshot records, oldest first."""
    return _read_records(workdir)


def preview_restore(workdir: Path, handle: str,
                    paths: list[str] | None = None) -> str:
    """Dry-run diff: unified diff of worktree vs the snapshot. No changes made.

    Note: ``git diff <tree>`` misbehaves on raw tree hashes (shows every
    file as deleted), so the snapshot tree is extracted with ``git archive``
    and compared with ``diff -ru`` instead. Raises VCSError on unknown
    handle or git failure.
    """
    rec = _find(workdir, handle)  # raises on unknown handle
    tree = rec.get("tree") or handle
    arch = subprocess.run([_git_bin(), "archive", tree], cwd=str(workdir),
                          capture_output=True)
    if arch.returncode != 0:
        raise VCSError(f"git archive failed: {arch.stderr.decode().strip()}")
    with tempfile.TemporaryDirectory(prefix="neo-preview-") as tmpd:
        tmp = Path(tmpd)
        tar = subprocess.run(["tar", "-x", "-C", str(tmp)], input=arch.stdout,
                             capture_output=True)
        if tar.returncode != 0:
            raise VCSError(
                f"snapshot extract failed: {tar.stderr.decode().strip()}")
        if paths:
            chunks: list[str] = []
            for p in paths:
                d = subprocess.run(
                    ["diff", "-ru", str(tmp / p), str(workdir / p)],
                    capture_output=True, text=True)
                chunks.append(d.stdout)
            return "".join(chunks)
        d = subprocess.run(
            ["diff", "-ru", "--exclude=.git", "--exclude=snapshots.json",
             str(tmp), str(workdir)],
            capture_output=True, text=True)
        return d.stdout


def restore(workdir: Path, handle: str,
            paths: list[str] | None = None) -> str:
    """Restore the worktree to the snapshot (whole tree or selected paths).

    Uses ``git checkout <tree> -- ...`` only — no reset, no index surgery.
    A selected path that does not exist in the snapshot tree (created after
    the snapshot) is deleted instead of erroring. Returns a short summary
    ending in ``git status --porcelain`` output. Refuses unknown handles.
    """
    _find(workdir, handle)  # raises on unknown handle
    if paths:
        # checkout errors on paths absent from the snapshot tree; a path
        # that was created after the snapshot must be deleted instead.
        present, missing = [], []
        for p in paths:
            r = _run(["ls-tree", "--name-only", handle, "--", p], workdir)
            if r.returncode == 0 and r.stdout.strip():
                present.append(p)
            else:
                missing.append(p)
        for p in missing:
            target = workdir / p
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        args = ["checkout", handle, "--", *present] if present else None
    else:
        args = ["checkout", handle, "--", "."]
    if args is not None:
        r = _run(args, workdir)
        if r.returncode != 0:
            raise VCSError(f"git checkout failed: {r.stderr.strip()}")
    r = _run(["status", "--porcelain"], workdir)
    if r.returncode != 0:
        raise VCSError(f"git status failed: {r.stderr.strip()}")
    status = r.stdout.strip()
    short = handle[:12]
    if status:
        return f"restored {short}; status vs HEAD:\n{status}"
    return f"restored {short}; worktree clean vs HEAD"


def auto_snapshot(workdir: Path, reason: str) -> str | None:
    """Snapshot wrapper for the agent loop: never raises.

    On success emits a one-line note to stderr and returns the handle;
    on any failure returns None.
    """
    try:
        handle = snapshot(workdir, message=f"auto: {reason}")
        if handle:
            print(f"[neo] snapshot {handle[:12]} ({reason})", file=sys.stderr)
        return handle
    except Exception:  # noqa: BLE001 - must never raise
        return None
