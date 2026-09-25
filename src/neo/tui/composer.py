"""neo TUI composer: input with `/` command palette and `@` file completion."""
from __future__ import annotations

from textual import on
from textual.containers import Vertical
from textual.widgets import Input, Label, ListItem, ListView

from .palette import fuzzy_filter


class Composer(Vertical):
    """Text input with an inline suggestion overlay."""

    def __init__(self, *, on_submit, get_commands, complete_files) -> None:
        super().__init__(classes="composer")
        self._on_submit = on_submit
        self._get_commands = get_commands
        self._complete_files = complete_files
        self._input: Input | None = None
        self._overlay: ListView | None = None
        self._overlay_items: list[str] = []
        self._overlay_kind: str = ""  # "cmd" | "file" | ""
        self._sel = 0
        self._sug_seq = 0

    def compose(self):
        self._overlay = ListView(classes="suggest")
        self._overlay.display = False
        yield self._overlay
        self._input = Input(placeholder="Ask neo…  ( / for commands, @ for files )",
                            classes="prompt-input")
        yield self._input

    def focus_input(self) -> None:
        if self._input:
            self._input.focus()

    @property
    def value(self) -> str:
        return self._input.value if self._input else ""

    def clear(self) -> None:
        if self._input:
            self._input.value = ""
        self._hide_overlay()

    # -- suggestion logic -------------------------------------------------

    @on(Input.Changed)
    def _changed(self, event: Input.Changed) -> None:
        value = event.value
        if value.startswith("/") and " " not in value.split("\n")[0]:
            query = value[1:]
            cmds = self._get_commands()
            names = [n for n, _ in cmds]
            matches = fuzzy_filter(query, names)
            descs = {n: d for n, d in cmds}
            self._show_overlay("cmd", matches,
                               [f"/{m}  — {descs.get(m, '')}" for m in matches])
        elif "@" in value:
            prefix = value.rsplit("@", 1)[1].split()[0] if value.rsplit("@", 1)[1] else ""
            if " " not in value.rsplit("@", 1)[1]:
                files = self._complete_files(prefix)
                self._show_overlay("file", files, files)
            else:
                self._hide_overlay()
        else:
            self._hide_overlay()

    def _show_overlay(self, kind: str, values: list[str], labels: list[str]) -> None:
        if not values or self._overlay is None:
            self._hide_overlay()
            return
        self._overlay_kind = kind
        self._overlay_items = values
        self._sel = 0
        self._overlay.clear()
        for v, lab in zip(values, labels):
            self._sug_seq += 1
            self._overlay.append(ListItem(Label(lab), id=f"sug-{self._sug_seq}"))
        self._overlay.display = True
        self._highlight()

    def _hide_overlay(self) -> None:
        self._overlay_kind = ""
        self._overlay_items = []
        if self._overlay is not None:
            self._overlay.display = False

    def _highlight(self) -> None:
        if self._overlay is None:
            return
        self._overlay.index = self._sel

    def _apply_completion(self) -> bool:
        """Apply the highlighted suggestion. Returns True if applied."""
        if not self._overlay_items or self._input is None:
            return False
        chosen = self._overlay_items[self._sel]
        if self._overlay_kind == "cmd":
            self._input.value = f"/{chosen} "
        else:  # file
            cur = self._input.value
            head = cur.rsplit("@", 1)[0]
            self._input.value = f"{head}@{chosen} "
        self._input.cursor_position = len(self._input.value)
        self._hide_overlay()
        return True

    # -- keys -------------------------------------------------------------

    async def on_key(self, event) -> None:
        if self._overlay_items:
            if event.key in ("up", "down"):
                d = -1 if event.key == "up" else 1
                self._sel = (self._sel + d) % len(self._overlay_items)
                self._highlight()
                event.prevent_default()
                event.stop()
                return
            if event.key == "tab":
                self._apply_completion()
                event.prevent_default()
                event.stop()
                return
            if event.key == "escape":
                self._hide_overlay()
                event.prevent_default()
                event.stop()
                return
        if event.key == "enter":
            if self._overlay_items and self._sel is not None:
                # Enter picks the highlighted suggestion when overlay is open
                # and the input is still a bare "/query" or "@prefix".
                v = self.value
                if v.startswith("/") and " " not in v.strip():
                    self._apply_completion()
                    event.prevent_default()
                    event.stop()
                    return
            text = self.value.strip()
            if text:
                self.clear()
                await self._on_submit(text)
            event.prevent_default()
            event.stop()
