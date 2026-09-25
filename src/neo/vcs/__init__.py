"""neo VCS layer: git snapshots (undo), session forks, and worktrees.

All git access goes through the CLI via subprocess (stdlib only). The two
hard safety rules, enforced in snapshots.py:

* never run ``git reset --hard``;
* snapshot capture never touches the user's real index — ``git add`` runs
  with ``GIT_INDEX_FILE`` pointed at a throwaway temp file.
"""
from .fork import fork_session
from .snapshots import (
    VCSError,
    auto_snapshot,
    list_snapshots,
    preview_restore,
    restore,
    snapshot,
)
from .worktrees import boot, create, list_worktrees, remove

__all__ = [
    "VCSError",
    "auto_snapshot",
    "boot",
    "create",
    "fork_session",
    "list_snapshots",
    "preview_restore",
    "remove",
    "list_worktrees",
    "restore",
    "snapshot",
]
