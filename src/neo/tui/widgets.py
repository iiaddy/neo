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
    """A user prompt rendered as an accent-bar quote block (no role label)."""

    def __init__(self, text: str) -> None:
        super().__init__(classes="user-msg")
        self._text = text

    def compose(self):
        yield Static(Text(self._text), classes="user-msg-body")


class AssistantMessage(Vertical):
    """Assistant reply with throttled incremental markdown rendering."""

    def __init__(self) -> None:
        super().__init__(classes="assistant-msg")
        self._buf: list[str] = []
        self._len = 0
        self._last_flush = 0.0
        self._md: Markdown | None = None
        self._done = False
        # finish() may run before compose() (fast event bursts); the final
        # text is stashed here so compose() can apply it.
        self._final: str | None = None

    def compose(self):
        # NOTE: never call self._md.update() here. Markdown._on_mount
        # re-applies the constructor text, which would clobber any update
        # made during compose. Pending text goes through the constructor.
        pending = self._final if self._final is not None else "".join(self._buf)
        self._md = Markdown(pending, classes="assistant-msg-body")
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
        self._final = text or "…"
        if self._md is not None:
            self._md.update(self._final)


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
# Working indicator
# ---------------------------------------------------------------------------

class WorkingIndicator(Horizontal):
    """Prominent animated 'neo is working' bar, pinned above the status bar.

    Visible for the whole turn: spinner + phase + elapsed seconds. The
    phase tracks what the agent is doing — thinking, writing, tool names —
    so a turn with no model output yet still feels alive. The animated
    ellipsis (thinking → thinking. → thinking.. → thinking...) is the
    "thinking animation".
    """

    def __init__(self) -> None:
        super().__init__(classes="working")
        self._label: Label | None = None
        self._frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._fi = 0
        self._dots = 0
        self._phase = "thinking"
        self._started = 0.0
        self._timer = None
        self.display = False
        self._text = ""  # last painted line (handy for tests)

    def compose(self):
        self._label = Label("", classes="working-label")
        yield self._label

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._tick)

    async def on_unmount(self) -> None:
        try:
            self._timer.stop()
        except Exception:
            pass

    def start(self, phase: str = "thinking") -> None:
        """Show the indicator and restart the elapsed timer."""
        self._phase = phase
        self._started = time.monotonic()
        self._fi = 0
        self._dots = 0
        self.display = True
        self._paint()

    def set_phase(self, phase: str) -> None:
        self._phase = phase
        self._paint()

    def stop(self) -> None:
        self.display = False

    @property
    def elapsed(self) -> int:
        return int(time.monotonic() - self._started) if self._started else 0

    def _tick(self) -> None:
        if not self.display:
            return
        self._fi = (self._fi + 1) % len(self._frames)
        self._dots = (self._dots + 1) % 4
        self._paint()

    def _paint(self) -> None:
        if self._label is None:
            return
        dots = "." * self._dots
        self._text = (f"{self._frames[self._fi]} {self._phase}{dots} "
                      f"· {self.elapsed}s")
        self._label.update(self._text)


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

    def set_model(self, model: str) -> None:
        """Update the model readout immediately (e.g. after /model switch)."""
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
