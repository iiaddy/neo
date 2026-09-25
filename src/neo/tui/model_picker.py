"""Model picker: favorites + recents storage plus fuzzy picking.

Backed by ~/.config/neo/models.json:
    {"favorites": [...], "recents": [...]}   (recents: max 10, deduped, most-recent-first)

Also reused as a generic searchable picker (providers, …) via the
``title`` / ``record_recent`` parameters.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView

_DEFAULT_PATH = Path.home() / ".config" / "neo" / "models.json"
_MAX_RECENTS = 10
_MAX_ROWS = 200  # never render more rows than this per filter pass


def _path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else _DEFAULT_PATH


def _read(path: Path | str | None = None) -> dict:
    p = _path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict, path: Path | str | None = None) -> None:
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def load_favorites(path: Path | str | None = None) -> list[str]:
    favs = _read(path).get("favorites", [])
    return [f for f in favs if isinstance(f, str)]


def save_favorites(favorites: list[str], path: Path | str | None = None) -> None:
    data = _read(path)
    data["favorites"] = list(favorites)
    _write(data, path)


def load_recents(path: Path | str | None = None) -> list[str]:
    recents = _read(path).get("recents", [])
    return [r for r in recents if isinstance(r, str)]


def push_recent(model: str, path: Path | str | None = None) -> list[str]:
    """Add model to recents (deduped, most-recent-first, capped at 10)."""
    recents = [r for r in load_recents(path) if r != model]
    recents.insert(0, model)
    recents = recents[:_MAX_RECENTS]
    data = _read(path)
    data["recents"] = recents
    _write(data, path)
    return recents


# ---------------------------------------------------------------------------
# picking
# ---------------------------------------------------------------------------

def _subseq_score(query: str, target: str) -> float | None:
    q, t = query.lower(), target.lower()
    if not q:
        return 1.0
    if t.startswith(q):
        return 3.0 + min(1.0, len(q) / max(1, len(t)))
    idx = t.find(q)
    if idx != -1:
        return 2.0 + min(1.0, len(q) / max(1, len(t)))
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


def filter_models(models: list[str], query: str) -> list[str]:
    """Fuzzy-filter model names; best score first. Empty query -> all."""
    if not query.strip():
        return list(models)
    scored = []
    for m in models:
        s = _subseq_score(query.strip(), m)
        if s is not None:
            scored.append((s, m))
    scored.sort(key=lambda p: (-p[0], p[1]))
    return [m for _, m in scored]


def pick_model(models: list[str], query: str,
               favorites: list[str] | None = None,
               recents: list[str] | None = None) -> list[str]:
    """Sort models: favorites first, then recents, then the rest.

    Each subgroup is fuzzy-filtered by query (empty query keeps group order).
    """
    favorites = favorites or []
    recents = recents or []
    fav_set = set(favorites)
    rec_set = set(recents)

    def _in_order(group: list[str], seen: set[str]) -> list[str]:
        return [m for m in group if m in seen]

    fav_group = filter_models(_in_order(models, fav_set), query)
    rec_group = filter_models(
        [m for m in recents if m in set(models) and m not in fav_set], query)
    rest = filter_models(
        [m for m in models if m not in fav_set and m not in rec_set], query)

    out: list[str] = []
    for m in fav_group + rec_group + rest:
        if m not in out:
            out.append(m)
    return out


def format_model_row(model: str, *, favorite: bool, recent: bool) -> str:
    tags = []
    if favorite:
        tags.append("★")
    if recent:
        tags.append("recent")
    return f"{model}  {' '.join(tags)}".rstrip()


# ---------------------------------------------------------------------------
# widget
# ---------------------------------------------------------------------------

class ModelPicker(ModalScreen):
    """Searchable list picker. Resolves future with the chosen item,
    or None when dismissed.

    Crash-safe rebuild: ``ListView.clear()`` is async — the old items are
    only gone after awaiting it. Rebuilding synchronously with reused ids
    raised DuplicateIds and killed the app on the first keystroke.
    """

    def __init__(self, models: list[str],
                 favorites: list[str] | None = None,
                 recents: list[str] | None = None,
                 future=None,
                 title: str = "model",
                 record_recent: bool = True) -> None:
        super().__init__()
        self._models = list(models)
        self._favorites = list(favorites or [])
        self._recents = list(recents or [])
        self._future = future
        self._title = title
        self._record_recent = record_recent
        self._visible: list[str] = []
        self._gen = 0  # rebuild generation; stale passes abort
        self._count_label: Label | None = None

    def _rows(self, visible: list[str], gen: int) -> list[ListItem]:
        return [
            ListItem(
                Label(Text(format_model_row(
                    m, favorite=m in self._favorites,
                    recent=m in self._recents))),
                id=f"m-{gen}-{i}")
            for i, m in enumerate(visible[:_MAX_ROWS])
        ]

    def _count_text(self, total: int) -> str:
        shown = min(total, _MAX_ROWS)
        return f"{shown} of {total}" if total > shown else f"{total}"

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(self._title, classes="modal-title")
            yield Input(placeholder="type to filter…", id="model-filter")
            self._visible = pick_model(self._models, "",
                                       self._favorites, self._recents)
            self._gen = 1
            yield ListView(*self._rows(self._visible, self._gen),
                           classes="modal-list")
            self._count_label = Label(self._count_text(len(self._visible)),
                                      classes="modal-count")
            yield self._count_label

    def on_mount(self) -> None:
        self.query_one("#model-filter", Input).focus()

    async def _rebuild(self, query: str) -> None:
        self._gen += 1
        gen = self._gen
        # Await a beat so rapid keystrokes coalesce; only the latest
        # generation is allowed to touch the DOM.
        await asyncio.sleep(0)
        if gen != self._gen:
            return
        lst = self.query_one(ListView)
        await lst.clear()
        if gen != self._gen:
            return
        self._visible = pick_model(self._models, query,
                                   self._favorites, self._recents)
        await lst.mount(*self._rows(self._visible, gen))
        if self._count_label is not None:
            self._count_label.update(self._count_text(len(self._visible)))

    @on(Input.Changed)
    async def _filter_changed(self, event: Input.Changed) -> None:
        if event.input.id == "model-filter":
            await self._rebuild(event.value)

    @on(ListView.Selected)
    def _selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._visible):
            chosen = self._visible[idx]
            if self._record_recent:
                push_recent(chosen)
            if self._future is not None and not self._future.done():
                self._future.set_result(chosen)
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape":
            if self._future is not None and not self._future.done():
                self._future.set_result(None)
            self.dismiss()
