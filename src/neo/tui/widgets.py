"""neo TUI widgets: transcript messages, tool rows, status bar, sidebar."""
from __future__ import annotations

import time

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Collapsible, Label, Markdown, Static

# ---------------------------------------------------------------------------
# Transcript messages
# ---------------------------------------------------------------------------

STREAM_FLUSH_MS = 120
STREAM_FLUSH_CHARS = 400


class UserMessage(Vertical):
    """A user prompt bubble."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self):
        yield Label("you", classes="msg-role")
        yield Static(Text(self._text), classes="msg-body")


class AssistantMessage(Vertical):
    """Assistant reply with throttled incremental markdown rendering."""

    def __init__(self) -> None:
        super().__init__()
        self._buf: list[str] = []
        self._len = 0
        self._last_flush = 0.0
        self._md: Markdown | None = None
        self._done = False

    def compose(self):
        yield Label("neo", classes="msg-role")
        self._md = Markdown("", classes="msg-body")
        yield self._md

    def append_text(self, text: str) -> None:
        if self._done or not text:
            return
        self._buf.append(text)
        self._len += len(text)
        now = time.monotonic()
        if (now - self._last_flush) * 1000 >= STREAM_FLUSH_MS or \
                self._len >= STREAM_FLUSH_CHARS:
            self._flush()

    def _flush(self) -> None:
        if self._md is None or not self._buf:
            return
        self._md.update("".join(self._buf))
        self._last_flush = time.monotonic()

    def finish(self, text: str) -> None:
        self._done = True
        if self._md is not None:
            self._md.update(text or "…")


class ReasoningBlock(Collapsible):
    """Collapsible thinking trace."""

    def __init__(self, text: str = "") -> None:
        super().__init__(title="thinking", classes="reasoning")
        self._text = text

    def compose(self):
        yield Static(Text(self._text, style="dim"), classes="reasoning-body")

    def set_text(self, text: str) -> None:
        self._text = text
        try:
            body = self.query_one(".reasoning-body", Static)
            body.update(Text(text, style="dim"))
        except Exception:
            pass


class NoticeLine(Static):
    LEVELS = {"info": "dim", "warn": "yellow", "error": "red"}

    def __init__(self, text: str, level: str = "info") -> None:
        super().__init__(Text(f"· {text}", style=self.LEVELS.get(level, "dim")),
                         classes="notice")


# ---------------------------------------------------------------------------
# Tool rows
# ---------------------------------------------------------------------------

class ToolRow(Vertical):
    """One-line tool call with spinner -> done state and expandable output."""

    status = reactive("running")

    def __init__(self, tool: str, title: str) -> None:
        super().__init__(classes="tool-row")
        self.tool = tool
        self.title = title
        self._icon: Label | None = None
        self._output: Static | None = None
        self._expanded = False
        self._frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._fi = 0
        self._timer = None

    def compose(self):
        with Horizontal(classes="tool-head"):
            self._icon = Label("⠋", classes="tool-icon")
            yield self._icon
            yield Label(f"{self.tool}", classes="tool-name")
            yield Label(self.title, classes="tool-title")

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.08, self._tick)

    async def on_unmount(self) -> None:
        self._stop_timer()

    def _stop_timer(self) -> None:
        try:
            self._timer.stop()
        except Exception:
            pass

    def _tick(self) -> None:
        if self.status != "running" or self._icon is None:
            return
        self._fi = (self._fi + 1) % len(self._frames)
        self._icon.update(self._frames[self._fi])

    def finish(self, ok: bool, output: str, ms: int) -> None:
        self.status = "ok" if ok else "error"
        self._stop_timer()
        if self._icon is not None:
            self._icon.update("✓" if ok else "✗")
            self._icon.add_class("tool-ok" if ok else "tool-err")
        if output:
            shown = output if len(output) <= 4000 else output[:4000] + "\n…[truncated]"
            self._output = Static(Text(shown), classes="tool-output")
            self._output.display = False
            self.mount(self._output)

    def toggle(self) -> None:
        if self._output is None:
            return
        self._expanded = not self._expanded
        self._output.display = self._expanded

    def on_click(self) -> None:
        self.toggle()


class VerifyRow(Static):
    def __init__(self, command: str) -> None:
        super().__init__(Text(f"⟳ verify: {command}", style="dim"))
        self.command = command

    def finish(self, ok: bool) -> None:
        self.update(Text(f"{'✓' if ok else '✗'} verify: {self.command}",
                         style="green" if ok else "red"))


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

class StatusBar(Horizontal):
    """Bottom status: spinner+phase | model | tokens | context gauge."""

    def __init__(self) -> None:
        super().__init__(classes="statusbar")
        self._left: Label | None = None
        self._right: Label | None = None
        self._frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._fi = 0
        self._busy = False
        self._phase = "ready"
        self._in = 0
        self._out = 0
        self._ctx_used = 0
        self._ctx_total = 200_000
        self._model = ""
        self._timer = None

    def compose(self):
        self._left = Label("ready", classes="status-left")
        yield self._left
        self._right = Label("", classes="status-right")
        yield self._right

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._tick)
        self._paint()

    async def on_unmount(self) -> None:
        try:
            self._timer.stop()
        except Exception:
            pass

    def _tick(self) -> None:
        if self._busy:
            self._fi = (self._fi + 1) % len(self._frames)
            self._paint()

    def set_busy(self, busy: bool, phase: str = "") -> None:
        self._busy = busy
        if phase:
            self._phase = phase
        elif not busy:
            self._phase = "ready"
        self._paint()

    def set_phase(self, phase: str) -> None:
        self._phase = phase
        self._paint()

    def set_usage(self, in_tok: int, out_tok: int, ctx_used: int = 0,
                  ctx_total: int = 0, model: str = "") -> None:
        self._in, self._out = in_tok, out_tok
        if ctx_used:
            self._ctx_used = ctx_used
        if ctx_total:
            self._ctx_total = ctx_total
        if model:
            self._model = model
        self._paint()

    def _paint(self) -> None:
        if self._left is None or self._right is None:
            return
        left = f"{self._frames[self._fi]} {self._phase}" if self._busy else self._phase
        self._left.update(left)
        pct = min(1.0, self._ctx_used / max(1, self._ctx_total))
        filled = int(pct * 10)
        gauge = "█" * filled + "░" * (10 - filled)
        right = (f"{self._model}  {self._in}↑ {self._out}↓  "
                 f"ctx {gauge} {pct:.0%}")
        self._right.update(right)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

class Sidebar(Vertical):
    """Right-side panel: session info + todos."""

    def __init__(self) -> None:
        super().__init__(classes="sidebar")
        self._info: Static | None = None
        self._todos: Static | None = None

    def compose(self):
        yield Label("session", classes="side-head")
        self._info = Static("", classes="side-info")
        yield self._info
        yield Label("todos", classes="side-head")
        self._todos = Static("no todos", classes="side-todos")
        yield self._todos

    def set_info(self, text: str) -> None:
        if self._info is not None:
            self._info.update(Text(text, style="dim"))

    def set_todos(self, todos: list[dict]) -> None:
        if self._todos is None:
            return
        if not todos:
            self._todos.update(Text("no todos", style="dim"))
            return
        lines = []
        for t in todos:
            mark = {"completed": "✓", "in_progress": "→",
                    "cancelled": "✗"}.get(t.get("status"), "·")
            lines.append(f"{mark} {t.get('content', '')}")
        self._todos.update(Text("\n".join(lines)))
