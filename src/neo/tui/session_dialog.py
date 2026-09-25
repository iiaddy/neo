"""Session list dialog: data logic + a Textual ModalScreen listing sessions.

Data functions (filter_sessions / format_session / rename_session) are plain
and unit-testable; SessionDialog follows the dialogs.py modal pattern
(Vertical modal + modal-title + modal-list + escape-to-dismiss).
"""
from __future__ import annotations

import asyncio
import datetime

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static


# ---------------------------------------------------------------------------
# fuzzy subsequence matching: prefix > substring > subsequence
# ---------------------------------------------------------------------------

def _subseq_score(query: str, target: str) -> float | None:
    """Return a score, or None if query is not a subsequence of target.

    Scoring tiers (checked in order):
    - prefix match      -> 3.0 + length bonus
    - substring match   -> 2.0 + length bonus
    - subsequence match -> 1.0 + compactness bonus
    """
    q, t = query.lower(), target.lower()
    if not q:
        return 1.0
    if t.startswith(q):
        return 3.0 + min(1.0, len(q) / max(1, len(t)))
    idx = t.find(q)
    if idx != -1:
        return 2.0 + min(1.0, len(q) / max(1, len(t)))
    # subsequence
    ti = 0
    first = last = -1
    for ch in q:
        pos = t.find(ch, ti)
        if pos == -1:
            return None
        if first == -1:
            first = pos
        last = pos
        ti = pos + 1
    span = last - first + 1
    return 1.0 + max(0.0, (len(t) - span) / max(1, len(t)))


def _item_score(query: str, item: dict) -> float | None:
    """Best score across title + id."""
    q = query.strip()
    if not q:
        return 1.0
    scores = [
        s for s in (
            _subseq_score(q, item.get("title", "") or ""),
            _subseq_score(q, item.get("id", "") or ""),
        )
        if s is not None
    ]
    return max(scores) if scores else None


def filter_sessions(sessions: list[dict], query: str) -> list[dict]:
    """Filter sessions by fuzzy match on title+id, best score first.

    Empty query returns all sessions (caller order preserved).
    """
    if not query.strip():
        return list(sessions)
    scored = []
    for item in sessions:
        s = _item_score(query, item)
        if s is not None:
            scored.append((s, item))
    scored.sort(key=lambda p: (-p[0], p[1].get("title", "") or ""))
    return [item for _, item in scored]


def format_session(item: dict) -> str:
    """One-line row text: 'title — model · 2026-09-25 22:10 · ses_…'."""
    title = (item.get("title") or "").strip() or "(untitled)"
    model = (item.get("model") or "").strip()
    ts = item.get("updated_at")
    when = ""
    if isinstance(ts, (int, float)) and ts > 0:
        when = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    bits = [title]
    if model:
        bits.append(model)
    if when:
        bits.append(when)
    row = " · ".join(bits)
    sid = item.get("id", "")
    if sid and sid not in row:
        row += f"  {sid}"
    return row


def rename_session(store, sid: str, new_title: str) -> str:
    """Rename a session via store.set_title. Returns the cleaned title."""
    title = new_title.strip()
    store.set_title(sid, title)
    return title


# ---------------------------------------------------------------------------
# widget
# ---------------------------------------------------------------------------

class SessionDialog(ModalScreen):
    """List sessions, filter-as-you-type. Resolves future with the chosen
    session id (or None when dismissed)."""

    def __init__(self, sessions: list[dict],
                 future: asyncio.Future | None = None) -> None:
        super().__init__()
        self._sessions = list(sessions)
        self._future = future
        self._visible: list[dict] = list(sessions)
        self._list: ListView | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("sessions", classes="modal-title")
            yield Input(placeholder="type to filter…", id="session-filter")
            self._list = ListView(
                *[ListItem(Label(Text(format_session(s))), id=f"ses-{i}")
                  for i, s in enumerate(self._visible)],
                classes="modal-list")
            yield self._list

    def on_mount(self) -> None:
        self.query_one("#session-filter", Input).focus()

    def _rebuild(self, query: str) -> None:
        self._visible = filter_sessions(self._sessions, query)
        lst = self.query_one(ListView)
        lst.clear()
        for i, s in enumerate(self._visible):
            lst.append(ListItem(Label(Text(format_session(s))), id=f"ses-{i}"))

    @on(Input.Changed)
    def _filter_changed(self, event: Input.Changed) -> None:
        if event.input.id == "session-filter":
            self._rebuild(event.value)

    @on(ListView.Selected)
    def _selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._visible):
            sid = self._visible[idx].get("id")
            if self._future is not None and not self._future.done():
                self._future.set_result(sid)
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape":
            if self._future is not None and not self._future.done():
                self._future.set_result(None)
            self.dismiss()
