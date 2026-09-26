"""Regression tests for the tools/VCS/patch/sandbox audit (2026-09-26).

Each test pins one of the audit fixes so the broken behavior cannot
silently come back:

- snapshots: records file excluded from snapshot trees; selective restore
  deletes post-snapshot files; records capped at the newest 50
- worktrees: create() resolves against workdir; boot() has a timeout
- fork: only the session's own id is remapped (parent chains preserved)
- edit: a single lock spans read-modify-write; CRLF files stay CRLF
- apply_patch: lock keys match edit/write; pure additions append at EOF
- parser: "*** End of File" anchors at EOF; zero-hunk updates rejected
- webfetch: 5MB streaming download cap
- read: 2000-char per-line and 50KB total caps
- bash: negative timeouts rejected; post-kill drain bounded (~3s)
- sandbox: allow_write symlinks resolving outside the workdir are skipped
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

# NOTE: neo.tools must be imported before neo.patch: tools/__init__ imports
# ApplyPatchTool from patch/applier, which imports tools.base — importing
# neo.patch first hits a pre-existing circular import.
from neo.tools import files as files_mod  # noqa: E402
from neo.agent.session import SessionStore, _new_id  # noqa: E402
from neo.patch import ApplyPatchTool, apply_ops, parse_patch  # noqa: E402
from neo.patch.applier import _lock_path  # noqa: E402
from neo.sandbox.config import SandboxConfig  # noqa: E402
from neo.sandbox.linux import build_bwrap_argv  # noqa: E402
from neo.tools.base import path_lock
from neo.tools.shell import BashTool
from neo.tools.web import WebFetchTool
from neo.vcs import VCSError, boot, create, fork_session, list_snapshots, restore, snapshot

git = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")


def _ctx(workdir: Path, **overrides) -> SimpleNamespace:
    base = dict(
        workdir=workdir,
        config=SimpleNamespace(verify_commands=[], disabled_tools=[]),
        permissions=None,
        gate=None,
        emit=lambda e: None,
        todos=[],
        ui=None,
        locks={},
        depth=0,
        background={},
        skills={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if not shutil.which("git"):
        pytest.skip("git not installed")
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True,
                   capture_output=True)
    (r / "a.txt").write_text("v1\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True,
                   capture_output=True)
    return r


# --- snapshots ---------------------------------------------------------------

@git
def test_snapshot_tree_excludes_records_file(repo: Path):
    snapshot(repo, "first")  # creates .neo/snapshots.json in the worktree
    assert (repo / ".neo" / "snapshots.json").is_file()
    h2 = snapshot(repo, "second")
    out = subprocess.run(["git", "ls-tree", "--name-only", h2], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    assert ".neo/snapshots.json" not in out.splitlines()


@git
def test_restore_does_not_destroy_later_snapshot_handles(repo: Path):
    h1 = snapshot(repo, "one")
    (repo / "a.txt").write_text("v2\n")
    h2 = snapshot(repo, "two")
    restore(repo, h1)
    handles = {r["handle"] for r in
               json.loads((repo / ".neo" / "snapshots.json").read_text())}
    assert handles == {h1, h2}
    assert (repo / "a.txt").read_text() == "v1\n"


@git
def test_restore_paths_deletes_post_snapshot_file(repo: Path):
    (repo / "keep.txt").write_text("keep\n")
    h = snapshot(repo, "base")
    (repo / "new.txt").write_text("created after snapshot\n")
    (repo / "keep.txt").write_text("changed\n")
    restore(repo, h, paths=["new.txt", "keep.txt"])
    assert not (repo / "new.txt").exists()
    assert (repo / "keep.txt").read_text() == "keep\n"


@git
def test_snapshot_records_capped_at_newest_50(repo: Path, capsys):
    for i in range(55):
        (repo / "f.txt").write_text(f"{i}\n")
        snapshot(repo, f"snap {i}")
    recs = list_snapshots(repo)
    assert len(recs) == 50
    assert recs[0]["message"] == "snap 5"
    assert recs[-1]["message"] == "snap 54"
    assert "pruned" in capsys.readouterr().err


# --- worktrees ----------------------------------------------------------------

@git
def test_worktree_create_resolves_against_workdir(repo: Path, tmp_path: Path,
                                                  monkeypatch):
    monkeypatch.chdir(tmp_path)  # process cwd != workdir
    p = create(repo, "wt1")
    assert p == str((repo / "wt1").resolve())
    assert Path(p).is_dir()


@git
def test_worktree_boot_timeout(repo: Path):
    wt = create(repo, "wt1")
    with pytest.raises(VCSError, match="timed out"):
        boot(repo, wt, ["sleep 5"], timeout=0.3)


# --- fork ---------------------------------------------------------------------

def test_fork_of_fork_preserves_parent_chain(tmp_path: Path):
    store = SessionStore(root=tmp_path / "sessions")
    s1 = store.new("one")
    other = _new_id("ses")  # an unrelated session id referenced in payloads
    store.append(s1, {"t": "sub", "session_id": s1})   # self-reference
    store.append(s1, {"t": "ref", "other": other})     # foreign reference
    f1 = fork_session(store, s1)
    f2 = fork_session(store, f1)

    m2 = next(r for r in store.load(f2) if r.get("t") == "meta")
    assert m2["forked_from"] == f1  # parent link intact, not remapped away
    m1 = next(r for r in store.load(f1) if r.get("t") == "meta")
    assert m1["forked_from"] == s1

    sub = next(r for r in store.load(f2) if r.get("t") == "sub")
    assert sub["session_id"] == f2  # self-reference followed the fork
    ref = next(r for r in store.load(f2) if r.get("t") == "ref")
    assert ref["other"] == other  # foreign ids are never remapped


# --- edit: single lock + CRLF --------------------------------------------------

def test_edit_holds_single_lock_across_read_modify_write(tmp_path: Path,
                                                         monkeypatch):
    f = tmp_path / "f.txt"
    f.write_text("hello world\n")
    ctx = _ctx(tmp_path)
    real = files_mod.path_lock
    acquisitions = 0

    @contextlib.asynccontextmanager
    async def counting(ctx_, path):
        nonlocal acquisitions
        acquisitions += 1
        async with real(ctx_, path):
            yield

    monkeypatch.setattr(files_mod, "path_lock", counting)
    res = run(files_mod.EditTool().run(
        {"path": "f.txt", "old": "world", "new": "there"}, ctx))
    assert not res.is_error, res.output
    # Two acquisitions (read, then write) would let a concurrent writer slip
    # in between and get clobbered; exactly one spans the whole operation.
    assert acquisitions == 1
    assert f.read_text() == "hello there\n"


def test_edit_preserves_crlf_line_endings(tmp_path: Path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    ctx = _ctx(tmp_path)
    res = run(files_mod.EditTool().run(
        {"path": "f.txt", "old": "two", "new": "TWO"}, ctx))
    assert not res.is_error, res.output
    assert f.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


# --- apply_patch: lock keys + pure-addition placement --------------------------

def test_patch_lock_key_matches_edit_write(tmp_path: Path):
    ctx = _ctx(tmp_path)
    # A relative patch path must resolve to the same key edit/write use,
    # otherwise the per-file lock is silently bypassed.
    assert _lock_path(ctx.workdir, "sub/a.txt") == (tmp_path / "sub" / "a.txt").resolve()
    assert _lock_path(ctx.workdir, str(tmp_path / "sub" / "a.txt")) == (
        tmp_path / "sub" / "a.txt").resolve()


def test_apply_patch_blocks_on_edit_lock(tmp_path: Path):
    async def main():
        ctx = _ctx(tmp_path)
        f = tmp_path / "x.txt"
        f.write_text("aaa\n")
        lock = path_lock(ctx, (tmp_path / "x.txt").resolve())
        await lock.acquire()
        patch = ("*** Begin Patch\n*** Update File: x.txt\n@@\n-aaa\n+bbb\n"
                 "*** End Patch\n")
        task = asyncio.create_task(
            ApplyPatchTool().run({"patch": patch}, ctx))
        await asyncio.sleep(0.3)
        blocked = not task.done()
        lock.release()
        res = await asyncio.wait_for(task, timeout=10)
        return blocked, res

    blocked, res = run(main())
    assert blocked, "relative patch path did not share the edit lock key"
    assert not res.is_error, res.output
    assert (tmp_path / "x.txt").read_text() == "bbb\n"


def test_pure_addition_hunk_appends_at_end(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("line1\nline2\n")
    ops = parse_patch("*** Begin Patch\n*** Update File: a.txt\n@@\n+line3\n*** End Patch\n")
    apply_ops(ops, tmp_path)
    assert f.read_text() == "line1\nline2\nline3\n"


# --- parser: EOF anchor + zero-hunk rejection -----------------------------------

def test_eof_anchored_hunk_matches_end_of_file(tmp_path: Path):
    # No trailing newline, so the old block aligns exactly at the end:
    # the anchor must pick the LAST "dup", not the first.
    f = tmp_path / "a.txt"
    f.write_text("dup\nmiddle\ndup")
    patch = ("*** Begin Patch\n*** Update File: a.txt\n@@\n-dup\n+DUP\n"
             "*** End of File\n*** End Patch\n")
    ops = parse_patch(patch)
    assert ops[0].hunks[0].end_of_file is True
    apply_ops(ops, tmp_path)
    assert f.read_text() == "dup\nmiddle\nDUP"


def test_hunk_without_anchor_matches_first(tmp_path: Path):
    # Control: without the anchor the forward search wins (first "dup").
    f = tmp_path / "a.txt"
    f.write_text("dup\nmiddle\ndup")
    patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n-dup\n+DUP\n*** End Patch\n"
    apply_ops(parse_patch(patch), tmp_path)
    assert f.read_text() == "DUP\nmiddle\ndup"


def test_eof_anchor_falls_back_to_forward_search(tmp_path: Path):
    # Mirrors opencode's own EOF test: the end-first attempt misses (the
    # trailing-newline sentinel), the forward search finds the real match.
    f = tmp_path / "a.txt"
    f.write_text("start\nmarker\nmiddle\nmarker\nend\n")
    patch = ("*** Begin Patch\n*** Update File: a.txt\n@@\n-marker\n-end\n"
             "+marker-changed\n+end\n*** End of File\n*** End Patch\n")
    apply_ops(parse_patch(patch), tmp_path)
    assert f.read_text() == "start\nmarker\nmiddle\nmarker-changed\nend\n"


def test_parse_eof_without_open_hunk_errors():
    with pytest.raises(ValueError, match="needs an open '@@' hunk"):
        parse_patch("*** Begin Patch\n*** Update File: a.txt\n*** End of File\n*** End Patch\n")


def test_parse_update_without_hunks_rejected():
    with pytest.raises(ValueError, match="expected at least one '@@' hunk"):
        parse_patch("*** Begin Patch\n*** Update File: a.txt\n*** End Patch\n")


# --- webfetch: 5MB streaming cap -------------------------------------------------

class _BigHandler(BaseHTTPRequestHandler):
    body = b"x" * (6 * 1024 * 1024)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def test_webfetch_caps_download_at_5mb(tmp_path: Path, monkeypatch):
    # Scrub proxy env so the local server is hit directly.
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BigHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ctx = _ctx(tmp_path)
        res = run(WebFetchTool().run(
            {"url": f"http://127.0.0.1:{port}/big", "format": "text"}, ctx))
    finally:
        server.shutdown()
        thread.join()
    assert not res.is_error, res.output
    assert "download capped at 5MB" in res.output
    assert "page capped at 30000 chars" in res.output
    assert len(res.output) < 100_000


# --- read: per-line + total caps --------------------------------------------------

def test_read_truncates_long_lines(tmp_path: Path):
    f = tmp_path / "long.txt"
    f.write_text("x" * 5000 + "\nshort\n")
    res = run(files_mod.ReadTool().run({"path": "long.txt"}, _ctx(tmp_path)))
    assert not res.is_error
    first = res.output.splitlines()[0]
    assert first.startswith("1| " + "x" * 2000)
    assert "(line truncated, 3000 more chars)" in first
    assert "2| short" in res.output.splitlines()[1]


def test_read_caps_total_output(tmp_path: Path):
    f = tmp_path / "big.txt"
    f.write_text(("z" * 100 + "\n") * 2000)
    res = run(files_mod.ReadTool().run({"path": "big.txt"}, _ctx(tmp_path)))
    assert not res.is_error
    assert "output capped at 50KB" in res.output
    assert len(res.output) <= 60_000


# --- bash: timeout validation + bounded post-kill drain ----------------------------

def test_bash_negative_timeout_rejected(tmp_path: Path):
    res = run(BashTool().run({"command": "echo hi", "timeout": -5}, _ctx(tmp_path)))
    assert res.is_error
    assert "invalid timeout" in res.output


def test_bash_non_integer_timeout_rejected(tmp_path: Path):
    res = run(BashTool().run({"command": "echo hi", "timeout": "soon"}, _ctx(tmp_path)))
    assert res.is_error
    assert "invalid timeout" in res.output


def test_bash_timeout_reports_and_kills(tmp_path: Path):
    res = run(BashTool().run({"command": "sleep 5", "timeout": 200}, _ctx(tmp_path)))
    assert res.is_error
    assert "timed out after 0.2s" in res.output


def test_bash_post_kill_drain_is_bounded(tmp_path: Path, monkeypatch):
    # Simulate a wedged child: the post-SIGKILL communicate() never returns.
    real_wait_for = asyncio.wait_for
    calls = 0

    async def flaky_wait_for(coro, timeout=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            coro.close()
            raise asyncio.TimeoutError()
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", flaky_wait_for)
    res = run(BashTool().run({"command": "sleep 5", "timeout": 100}, _ctx(tmp_path)))
    assert calls == 2
    assert res.is_error
    assert "timed out after 0.1s" in res.output
    assert "may still be running" in res.output


# --- sandbox: allow_write symlink escape -------------------------------------------

def test_allow_write_symlink_outside_workdir_skipped(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "evil").symlink_to(outside)  # planted symlink -> outside workdir

    cfg = SandboxConfig(mode="auto", allow_write=["evil", "."])
    argv, _env = build_bwrap_argv(cfg, "bwrap", wd, "true")
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    assert not any("outside" in b or b.endswith("/evil") for b in binds), binds
    # The workdir itself is still writable.
    assert str(wd.resolve()) in binds


def test_allow_write_inside_symlink_resolves(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    wd = tmp_path / "wd"
    (wd / "real").mkdir(parents=True)
    (wd / "link").symlink_to(wd / "real")
    cfg = SandboxConfig(mode="auto", allow_write=["link"])
    argv, _env = build_bwrap_argv(cfg, "bwrap", wd, "true")
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    # Bound once, at the resolved target inside the workdir — never the
    # literal symlink spelling (which the kernel would follow on bind).
    assert binds.count(str((wd / "real").resolve())) == 1
    assert not any(b.endswith("/link") for b in binds)
