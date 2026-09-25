"""Tests for neo.vcs — real git repos in tmp_path, no mocks of git."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from neo.agent.session import SessionStore
from neo.vcs import (
    VCSError,
    auto_snapshot,
    boot,
    create,
    fork_session,
    list_snapshots,
    list_worktrees,
    preview_restore,
    remove,
    restore,
    snapshot,
)

git = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("hello\n")
    (r / "b.txt").write_text("bee\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(root=tmp_path / "sessions")


# --- snapshots ---

@git
def test_snapshot_captures_dirty_file(repo: Path):
    (repo / "a.txt").write_text("dirty\n")
    handle = snapshot(repo, "test")
    assert handle and len(handle) == 40
    records = list_snapshots(repo)
    assert len(records) == 1
    rec = records[0]
    assert rec["handle"] == handle
    assert rec["message"] == "test"
    assert rec["head"] and len(rec["head"]) == 40
    assert isinstance(rec["ts"], float)


@git
def test_snapshot_recorded_in_snapshots_json(repo: Path):
    h = snapshot(repo, "first")
    p = repo / ".neo" / "snapshots.json"
    assert p.is_file()
    data = json.loads(p.read_text())
    assert len(data) == 1 and data[0]["handle"] == h
    snapshot(repo, "second")
    assert len(json.loads(p.read_text())) == 2


@git
def test_restore_brings_back_dirty_file(repo: Path):
    (repo / "a.txt").write_text("dirty\n")
    handle = snapshot(repo, "pre")
    (repo / "a.txt").write_text("dirtier\n")
    summary = restore(repo, handle)
    assert (repo / "a.txt").read_text() == "dirty\n"
    assert handle[:12] in summary


@git
def test_selective_restore_only_touches_chosen_file(repo: Path):
    handle = snapshot(repo, "pre")
    (repo / "a.txt").write_text("a-changed\n")
    (repo / "b.txt").write_text("b-changed\n")
    restore(repo, handle, paths=["a.txt"])
    assert (repo / "a.txt").read_text() == "hello\n"
    assert (repo / "b.txt").read_text() == "b-changed\n"  # untouched


@git
def test_preview_shows_diff_without_changing_files(repo: Path):
    (repo / "a.txt").write_text("v2\n")
    handle = snapshot(repo, "pre")
    (repo / "a.txt").write_text("v3\n")
    diff = preview_restore(repo, handle)
    assert "a.txt" in diff
    assert "-v2" in diff and "+v3" in diff
    assert (repo / "a.txt").read_text() == "v3\n"  # unchanged by preview


@git
def test_preview_selective_paths(repo: Path):
    handle = snapshot(repo, "pre")
    (repo / "a.txt").write_text("a2\n")
    (repo / "b.txt").write_text("b2\n")
    diff = preview_restore(repo, handle, paths=["a.txt"])
    assert "a.txt" in diff
    assert "b.txt" not in diff


@git
def test_restore_unknown_handle_raises(repo: Path):
    snapshot(repo)
    with pytest.raises(VCSError):
        restore(repo, "0" * 40)
    with pytest.raises(VCSError):
        preview_restore(repo, "0" * 40)


@git
def test_auto_snapshot_never_raises(repo: Path):
    handle = auto_snapshot(repo, "test-reason")
    assert handle and len(handle) == 40
    # outside a repo it must return None, not raise
    assert auto_snapshot(repo / "nonexistent-dir-xyz", "x") is None


# --- non-repo behavior ---

def test_snapshot_nonrepo_returns_none(tmp_path: Path):
    if (tmp_path / ".git").exists():
        pytest.skip("tmp_path is inside a git repo")
    assert snapshot(tmp_path, "x") is None


def test_worktree_nonrepo_raises(tmp_path: Path):
    if (tmp_path / ".git").exists():
        pytest.skip("tmp_path is inside a git repo")
    with pytest.raises(VCSError):
        list_worktrees(tmp_path)
    with pytest.raises(VCSError):
        create(tmp_path, tmp_path / "wt")


# --- fork ---

def test_fork_session_copies_records(store: SessionStore):
    sid = store.new(title="main", model="m")
    store.append(sid, {"t": "user", "text": "hi"})
    store.append(sid, {"t": "note", "self": sid, "other": "ses_20200101000000_zzzz"})
    new_sid = fork_session(store, sid)
    assert new_sid != sid
    new_recs = store.load(new_sid)
    meta = new_recs[0]
    assert meta["t"] == "meta" and meta["forked_from"] == sid
    assert "fork" in meta["title"]
    bodies = [r for r in new_recs if r.get("t") != "meta"]
    assert [r["t"] for r in bodies] == ["user", "note"]
    assert bodies[0]["text"] == "hi"
    # ses_ ids in payloads remapped away from the originals
    note = bodies[1]
    assert note["self"] == new_sid
    assert note["other"] != "ses_20200101000000_zzzz"
    # original untouched
    assert store.load(sid)[0]["t"] == "meta"
    assert "forked_from" not in store.load(sid)[0]


def test_fork_unknown_session_raises(store: SessionStore):
    with pytest.raises(ValueError):
        fork_session(store, "ses_20200101000000_nope")


# --- worktrees ---

@git
def test_worktree_roundtrip(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt1"
    path = create(repo, wt)
    assert Path(path).is_dir()
    names = [w["path"] for w in list_worktrees(repo)]
    assert str(Path(path)) in names
    listed = [w for w in list_worktrees(repo) if w["path"] == str(Path(path))][0]
    assert listed["branch"] is not None  # on a real branch
    out = boot(repo, wt, ["echo hello-wt"])
    assert "hello-wt" in out
    remove(repo, wt)
    assert str(Path(path)) not in [w["path"] for w in list_worktrees(repo)]


@git
def test_worktree_with_new_branch(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt2"
    create(repo, wt, branch="feature-x")
    listed = {w["path"]: w for w in list_worktrees(repo)}
    assert listed[str(wt.resolve())]["branch"] == "feature-x"
    remove(repo, wt, force=True)


@git
def test_worktree_remove_unknown_raises(repo: Path, tmp_path: Path):
    with pytest.raises(VCSError):
        remove(repo, tmp_path / "nope")
