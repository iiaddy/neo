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

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("question", classes="modal-title")
            q = self._questions[self._qi]
            header = q.get("header") or f"Q{self._qi + 1}"
            yield Static(Text(f"{header}: {q.get('question', '')}"))
            for i, opt in enumerate(q.get("options", [])):
                label = opt.get("label", "")
                desc = opt.get("description", "")
                text = f"{label}" + (f" — {desc}" if desc else "")
                yield Button(text, id=f"opt-{i}")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if not bid.startswith("opt-"):
            return
        try:
            idx = int(bid[4:])
        except ValueError:
            return
        q = self._questions[self._qi]
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

    @on(Input.Changed)
    def _filter_changed(self, event: Input.Changed) -> None:
        from .palette import fuzzy_filter
        self._filter = event.value
        labels = [label for label, _ in self._choices]
        keep = set(fuzzy_filter(self._filter, labels, limit=50))
        for i, item in enumerate(self.query(ListItem)):
            item.display = self._choices[i][0] in keep

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
