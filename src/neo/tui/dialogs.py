"""neo TUI dialogs: permission gate, question bridge, list pickers."""
from __future__ import annotations

import asyncio

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static


class PermissionModal(ModalScreen):
    """Ask/allow/deny gate for a tool call. Resolves future with
    "once" | "always" | "reject"."""

    def __init__(self, tool: str, target: str, detail: str,
                 future: asyncio.Future) -> None:
        super().__init__()
        self._tool = tool
        self._target = target
        self._detail = detail
        self._future = future

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("permission required", classes="modal-title")
            yield Static(Text(f"{self._tool}  →  {self._target}"))
            if self._detail:
                yield Static(Text(self._detail[:1200], style="dim"),
                             classes="modal-detail")
            with Vertical(classes="modal-btns"):
                yield Button("Allow once", id="once", variant="primary")
                yield Button("Always allow", id="always")
                yield Button("Reject", id="reject", variant="error")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        choice = event.button.id or "reject"
        if not self._future.done():
            self._future.set_result(choice)
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape" and not self._future.done():
            self._future.set_result("reject")
            self.dismiss()


class QuestionModal(ModalScreen):
    """Structured question bridge for the `question` tool."""

    def __init__(self, questions: list[dict], future: asyncio.Future) -> None:
        super().__init__()
        self._questions = questions
        self._future = future
        self._answers: dict[str, str] = {}
        self._qi = 0

    def _current(self) -> dict | None:
        if 0 <= self._qi < len(self._questions):
            q = self._questions[self._qi]
            if isinstance(q, dict):
                return q
        return None

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("question", classes="modal-title")
            q = self._current()
            if q is None:
                yield Static(Text("no questions to show."))
                yield Button("Dismiss", id="q-dismiss")
                return
            header = q.get("header") or f"Q{self._qi + 1}"
            yield Static(Text(f"{header}: {q.get('question', '')}"))
            opts = q.get("options", [])
            if not opts:
                # Degenerate question: still offer a keyboard-dismissable
                # way out so the awaiting future always resolves.
                yield Button("Dismiss", id="q-dismiss")
                return
            for i, opt in enumerate(opts):
                label = opt.get("label", "")
                desc = opt.get("description", "")
                text = f"{label}" + (f" — {desc}" if desc else "")
                yield Button(text, id=f"opt-{i}")

    def _reject(self) -> None:
        if not self._future.done():
            self._future.set_result(None)
        self.dismiss()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "q-dismiss":
            self._reject()
            return
        if not bid.startswith("opt-"):
            return
        try:
            idx = int(bid[4:])
        except ValueError:
            return
        q = self._current()
        if q is None:
            self._reject()
            return
        opts = q.get("options", [])
        if not (0 <= idx < len(opts)):
            return
        label = opts[idx].get("label", "")
        header = q.get("header") or f"Q{self._qi + 1}"
        self._answers[str(self._qi)] = label
        self._answers[header] = label
        self._qi += 1
        if self._qi >= len(self._questions):
            if not self._future.done():
                self._future.set_result(self._answers)
            self.dismiss()
        else:
            self.dismiss()
            # re-open for the next question
            self.app.push_screen(QuestionModal(self._questions, self._future)
                                 ._with_state(self._qi, self._answers))

    def _with_state(self, qi: int, answers: dict) -> "QuestionModal":
        self._qi = qi
        self._answers = answers
        return self

    def on_key(self, event) -> None:
        # Escape rejects the question; the question tool turns a None
        # answer into a clean error result instead of hanging.
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self._reject()


class ChoiceModal(ModalScreen):
    """Generic fuzzy-filterable list picker. Resolves future with the chosen
    value, or None when dismissed."""

    def __init__(self, title: str, choices: list[tuple[str, object]],
                 future: asyncio.Future) -> None:
        super().__init__()
        self._title = title
        self._choices = choices
        self._future = future
        self._filter = ""
        self._list: ListView | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(self._title, classes="modal-title")
            yield Input(placeholder="type to filter…", id="choice-filter")
            self._list = ListView(
                *[ListItem(Label(label), id=f"ch-{i}")
                  for i, (label, _) in enumerate(self._choices)],
                classes="modal-list")
            yield self._list

    def on_mount(self) -> None:
        self.query_one("#choice-filter", Input).focus()
        lst = self.query_one(ListView)
        if self._choices:
            lst.index = 0

    def _shown_indexes(self) -> list[int]:
        lst = self.query_one(ListView)
        return [i for i, c in enumerate(lst.children) if c.display]

    def _move_highlight(self, delta: int) -> None:
        shown = self._shown_indexes()
        if not shown:
            return
        lst = self.query_one(ListView)
        cur = lst.index
        pos = shown.index(cur) if cur in shown else (-1 if delta > 0 else 0)
        lst.index = shown[max(0, min(len(shown) - 1, pos + delta))]

    def _choose_highlighted(self) -> None:
        lst = self.query_one(ListView)
        idx = lst.index
        if idx is None or not (0 <= idx < len(self._choices)):
            return
        if not lst.children[idx].display:
            return  # filtered out; keep the picker open
        _, value = self._choices[idx]
        if not self._future.done():
            self._future.set_result(value)
        self.dismiss()

    @on(Input.Changed)
    def _filter_changed(self, event: Input.Changed) -> None:
        from .palette import fuzzy_filter
        self._filter = event.value
        labels = [label for label, _ in self._choices]
        keep = set(fuzzy_filter(self._filter, labels, limit=50))
        for i, item in enumerate(self.query(ListItem)):
            item.display = self._choices[i][0] in keep
        shown = self._shown_indexes()
        self.query_one(ListView).index = shown[0] if shown else None

    @on(Input.Submitted)
    def _filter_submitted(self, event: Input.Submitted) -> None:
        # Enter while the filter has focus is consumed by the Input's
        # submit binding; choose the highlighted match here instead.
        if event.input.id == "choice-filter":
            event.stop()
            self._choose_highlighted()

    @on(ListView.Selected)
    def _selected(self, event: ListView.Selected) -> None:
        idx = int((event.item.id or "ch-0").split("-")[1])
        _, value = self._choices[idx]
        if not self._future.done():
            self._future.set_result(value)
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape" and not self._future.done():
            self._future.set_result(None)
            self.dismiss()
        elif event.key in ("up", "down"):
            # The filter Input has no up/down binding, so these bubble up
            # here. Drive the list highlight OpenCode-style.
            self._move_highlight(1 if event.key == "down" else -1)
            event.prevent_default()
            event.stop()


class SecretModal(ModalScreen):
    """Single masked text input. Resolves future with the entered string,
    or None when dismissed/escaped. The value is never echoed."""

    def __init__(self, title: str, placeholder: str,
                 future: asyncio.Future) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._future = future

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(self._title, classes="modal-title")
            yield Input(placeholder=self._placeholder, password=True,
                        id="secret-input")
            with Vertical(classes="modal-btns"):
                yield Button("Save", id="secret-ok", variant="primary")
                yield Button("Cancel", id="secret-cancel")

    def on_mount(self) -> None:
        self.query_one("#secret-input", Input).focus()

    def _done(self, value: str | None) -> None:
        if not self._future.done():
            self._future.set_result(value)
        self.dismiss()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "secret-ok":
            value = self.query_one("#secret-input", Input).value.strip()
            self._done(value or None)
        else:
            self._done(None)

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "secret-input":
            value = event.value.strip()
            self._done(value or None)

    def on_key(self, event) -> None:
        if event.key == "escape" and not self._future.done():
            self._done(None)
