"""neo Textual TUI — polished terminal interface for the agent harness."""
from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import Label, Static

from ..agent.compact import estimate_tokens
from ..agent.session import SessionStore
from ..cli import build_runtime
from .composer import Composer
from .dialogs import ChoiceModal, PermissionModal, QuestionModal
from .themes import THEMES
from .widgets import (AssistantMessage, NoticeLine, ReasoningBlock, Sidebar,
                      StatusBar, ToolRow, UserMessage, VerifyRow)

CSS = """
Screen { background: $background; }

#topbar {
    height: 1; width: 1fr;
    background: $surface; color: $text-muted;
    padding: 0 1;
}

#main { height: 1fr; }
#transcript {
    width: 1fr; height: 1fr;
    padding: 0 1;
    scrollbar-size: 1 1;
}
#transcript:focus { border: none; }

.msg-role { color: $accent; text-style: bold; margin-top: 1; }
.msg-body { margin-left: 1; }

.reasoning { margin-left: 1; border-left: solid $text-muted; padding-left: 1; }
.reasoning-body { color: $text-muted; }

.notice { margin: 0 1; }

.tool-row { margin: 0 1; }
.tool-head { height: 1; }
.tool-icon { width: 2; color: $accent; }
.tool-name { color: $accent; width: auto; margin-right: 1; }
.tool-title { color: $text-muted; }
.tool-ok { color: $success; }
.tool-err { color: $error; }
.tool-output {
    margin-left: 2; border-left: solid $surface;
    padding-left: 1; color: $text-muted;
    max-height: 20; overflow-y: auto;
}

.sidebar {
    width: 32; height: 1fr;
    background: $surface; border-left: solid $surface;
    padding: 0 1;
}
.side-head { color: $accent; text-style: bold; margin-top: 1; }
.side-info, .side-todos { color: $text-muted; }

.statusbar {
    height: 1; width: 1fr;
    background: $surface;
}
.status-left { width: 1fr; color: $text-muted; padding: 0 1; }
.status-right { width: auto; color: $text-muted; padding: 0 1; }

.composer { height: auto; }
.prompt-input {
    border: solid $surface; background: $surface;
    margin: 0 1 1 1;
}
.prompt-input:focus { border: solid $accent; }
.suggest {
    max-height: 8; margin: 0 1;
    background: $panel; border: solid $surface;
}

.modal {
    width: 60; max-height: 24;
    background: $panel; border: solid $accent;
    padding: 1 2; margin-top: 4;
}
.modal-title { color: $accent; text-style: bold; margin-bottom: 1; }
.modal-detail { margin: 1 0; max-height: 10; overflow-y: auto; }
.modal-btns { margin-top: 1; }
.modal-btns Button { margin-bottom: 1; width: 1fr; }
.modal-list { max-height: 12; margin-top: 1; }
"""


def _register_themes(app: App) -> None:
    for name, c in THEMES.items():
        app.register_theme(Theme(
            name=name,
            primary=c["accent"],
            accent=c["accent"],
            success=c["green"],
            warning=c["yellow"],
            error=c["red"],
            foreground=c["text"],
            background=c["background"],
            surface=c["surface"],
            panel=c["panel"],
            dark=not name.endswith("light"),
            variables={
                "text-muted": c["muted"],
                "user-bg": c["user_bg"],
            },
        ))


class _UIBridge:
    """Implements the ctx.ui.ask() contract via a modal."""

    def __init__(self, app: "NeoApp") -> None:
        self._app = app

    async def ask(self, questions: list[dict]) -> dict:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._app.push_screen(QuestionModal(questions, fut))
        return await fut


class NeoApp(App):
    """Main neo terminal application."""

    CSS = CSS
    BINDINGS = [
        ("ctrl+c", "cancel_run", "Cancel run"),
        ("ctrl+b", "toggle_sidebar", "Sidebar"),
        ("ctrl+l", "clear_transcript", "Clear view"),
    ]

    def __init__(self, workdir: str | Path, config, resume: str | None = None,
                 theme: str = "neo-dark") -> None:
        super().__init__()
        _register_themes(self)
        self.theme = theme if theme in THEMES else "neo-dark"
        self.workdir = Path(workdir).resolve()
        self.config = config
        self.store = SessionStore()
        self.messages: list[dict] = []
        self.session_id: str = resume or ""
        self._provider = None
        self._harness = None
        self._ctx = None
        self._discovered_cmds: dict = {}
        self._running = False
        self._pump: asyncio.Task | None = None
        self._transcript: Vertical | None = None
        self._status: StatusBar | None = None
        self._sidebar: Sidebar | None = None
        self._composer: Composer | None = None
        self._assistant: AssistantMessage | None = None
        self._reasoning: ReasoningBlock | None = None
        self._reason_buf: list[str] = []
        self._tool_rows: dict[str, ToolRow] = {}
        self._verify_rows: dict[str, VerifyRow] = {}
        self._in_tokens = 0
        self._out_tokens = 0
        self._model_label = ""

    # -- compose ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Horizontal(id="main"):
            self._transcript = Vertical(id="transcript")
            self._transcript.can_focus = True
            yield self._transcript
            self._sidebar = Sidebar()
            self._sidebar.display = False
            yield self._sidebar
        self._status = StatusBar()
        yield self._status
        self._composer = Composer(
            on_submit=self._on_submit,
            get_commands=self._slash_commands,
            complete_files=self._complete_files,
        )
        yield self._composer

    async def on_mount(self) -> None:
        self._refresh_topbar()
        try:
            if not self.session_id:
                self.session_id = self.store.new(title="neo session",
                                                 model=self.config.model)
            provider, harness, ctx, discovered = build_runtime(
                self.workdir, self.config,
                session_id=self.session_id,
                gate=self._gate,
                emit=self._emit,
                ui=_UIBridge(self),
            )
        except Exception as e:  # noqa: BLE001 - surface config errors in the TUI
            self._notice(f"startup error: {e}", "error")
            return
        self._provider, self._harness, self._ctx = provider, harness, ctx
        self._discovered_cmds = discovered.get("commands", {})
        self._model_label = f"{provider.spec.id}/{harness.model}"
        self._refresh_topbar()
        if self._sidebar:
            self._sidebar.set_info(
                f"session {self.session_id[:8]}\n"
                f"model {self._model_label}\n"
                f"dir {self.workdir}")
        if self.store.load(self.session_id):
            await self._load_session()
        else:
            self._notice("neo ready — type /help for commands.", "info")
        self._update_context_gauge()
        if self._composer:
            self._composer.focus_input()

    async def _load_session(self) -> None:
        records = self.store.load(self.session_id)
        self.messages = self.store.messages_from_records(records)
        for m in self.messages:
            role, content = m.get("role"), m.get("content", "")
            if role == "user" and isinstance(content, str):
                await self._transcript.mount(UserMessage(content))
            elif role == "assistant":
                w = AssistantMessage()
                await self._transcript.mount(w)
                w.finish(content if isinstance(content, str) else "")
        if self.messages:
            self._notice(f"resumed session {self.session_id[:8]} "
                         f"({len(self.messages)} messages).", "info")
            self._transcript.scroll_end(animate=False)

    def _refresh_topbar(self) -> None:
        try:
            bar = self.query_one("#topbar", Static)
            bar.update(f"neo   {self._model_label}   {self.workdir.name}")
        except Exception:
            pass

    # -- permission gate ---------------------------------------------------

    async def _gate(self, tool: str, target: str, detail: str) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.push_screen(PermissionModal(tool, target, detail, fut))
        if self._status:
            self._status.set_phase("waiting for approval")
        choice = await fut
        if self._status and self._running:
            self._status.set_phase("working")
        return choice

    # -- tool emit (sync callback from tools, same event loop) -------------

    def _emit(self, event) -> None:
        kind = getattr(event, "kind", "")
        if kind == "todo_update":
            if self._sidebar:
                self._sidebar.set_todos(list(event.todos))
        elif kind == "tool_progress" and self._status:
            self._status.set_phase(f"{event.tool}…")

    # -- submit / run pump --------------------------------------------------

    async def _on_submit(self, text: str) -> None:
        if text.startswith("/"):
            await self._run_slash(text[1:])
            return
        if self._running and self._harness:
            self._harness.steer(text)
            self._notice("steered the running turn.", "info")
            return
        await self._start_turn(text)

    async def _start_turn(self, text: str) -> None:
        if self._harness is None:
            self._notice("runtime not ready.", "error")
            return
        text = self._expand_mentions(text)
        self.messages.append({"role": "user", "content": text})
        self.store.append(self.session_id, {"t": "user", "text": text})
        if len(self.messages) == 1:
            self.store.set_title(self.session_id, text[:60])
        await self._transcript.mount(UserMessage(text))
        self._transcript.scroll_end(animate=False)
        self._running = True
        if self._status:
            self._status.set_busy(True, "working")
        self._pump = asyncio.create_task(self._pump_events())

    async def _pump_events(self) -> None:
        assert self._harness is not None
        try:
            async for ev in self._harness.run(self.messages):
                self._handle(ev)
                k = ev.kind
                if k == "turn_end":
                    for m in reversed(self.messages):
                        if m.get("role") == "assistant":
                            self.store.append(self.session_id, {
                                "t": "assistant",
                                "text": m.get("content"),
                                "tool_calls": m.get("tool_calls", [])})
                            break
                elif k == "tool_end":
                    self.store.append(self.session_id, {
                        "t": "tool_result", "call_id": ev.call_id,
                        "tool": ev.tool, "output": ev.output[:8000],
                        "is_error": not ev.ok})
        except asyncio.CancelledError:
            self._notice("run cancelled.", "warn")
        except Exception as e:  # noqa: BLE001
            self._notice(f"run error: {e}", "error")
        finally:
            self._running = False
            if self._status:
                self._status.set_busy(False)
            self._update_context_gauge()
            if self._composer:
                self._composer.focus_input()

    # -- event dispatch -------------------------------------------------------

    def _handle(self, ev) -> None:
        k = ev.kind
        if k == "turn_start":
            if self._status:
                self._status.set_phase(f"turn {ev.index + 1}")
        elif k == "text_start":
            self._assistant = AssistantMessage()
            self._transcript.mount(self._assistant)
            self._transcript.scroll_end(animate=False)
        elif k == "text_delta":
            if self._assistant:
                self._assistant.append_text(ev.text)
                self._transcript.scroll_end(animate=False)
        elif k == "text_end":
            if self._assistant:
                self._assistant.finish(ev.text)
                self._assistant = None
        elif k == "reason_start":
            self._reasoning = ReasoningBlock()
            self._reason_buf = []
            self._transcript.mount(self._reasoning)
        elif k == "reason_delta":
            self._reason_buf.append(ev.text)
            if self._reasoning:
                self._reasoning.set_text("".join(self._reason_buf))
        elif k == "reason_end":
            if self._reasoning:
                self._reasoning.set_text(ev.text)
                self._reasoning = None
                self._reason_buf = []
        elif k == "tool_start":
            row = ToolRow(ev.tool, ev.title or ev.tool)
            self._tool_rows[ev.call_id] = row
            self._transcript.mount(row)
            self._transcript.scroll_end(animate=False)
            if self._status:
                self._status.set_phase(f"{ev.tool}…")
        elif k == "tool_end":
            row = self._tool_rows.pop(ev.call_id, None)
            if row:
                row.finish(ev.ok, ev.output, ev.ms)
        elif k == "usage":
            self._in_tokens = ev.input_tokens
            self._out_tokens = ev.output_tokens
            if self._status:
                self._status.set_usage(self._in_tokens, self._out_tokens)
        elif k == "notice":
            self._notice(ev.text, ev.level)
        elif k == "verify_start":
            row = VerifyRow(ev.command)
            self._verify_rows[ev.command] = row
            self._transcript.mount(row)
        elif k == "verify_end":
            row = self._verify_rows.pop(ev.command, None)
            if row:
                row.finish(ev.ok)
        elif k == "todo_update":
            if self._sidebar:
                self._sidebar.set_todos(list(ev.todos))
        elif k == "compact_start":
            self._notice("compacting context…", "info")
        elif k == "compact_end":
            self._notice("context compacted.", "info")
        elif k == "run_end":
            if ev.reason != "done":
                self._notice(f"run ended: {ev.reason}", "warn")
        self._transcript.scroll_end(animate=False)

    def _notice(self, text: str, level: str = "info") -> None:
        if self._transcript:
            self._transcript.mount(NoticeLine(text, level))
            self._transcript.scroll_end(animate=False)

    def _update_context_gauge(self) -> None:
        if not self._status or not self._harness:
            return
        schemas = [t.schema() for t in self._harness.tools.values()]
        used = estimate_tokens(self.messages, schemas)
        self._status.set_usage(self._in_tokens, self._out_tokens,
                               ctx_used=used, ctx_total=200_000,
                               model=self._model_label)

    # -- @ mentions -------------------------------------------------------------

    def _complete_files(self, prefix: str) -> list[str]:
        out: list[str] = []
        base = self.workdir / prefix if prefix else self.workdir
        parent = base.parent if prefix and not prefix.endswith("/") else base
        needle = "" if (not prefix or prefix.endswith("/")) else base.name
        try:
            for child in sorted(parent.iterdir()):
                if child.name.startswith("."):
                    continue
                if needle and not child.name.startswith(needle):
                    continue
                rel = child.relative_to(self.workdir).as_posix()
                out.append(rel + ("/" if child.is_dir() else ""))
                if len(out) >= 30:
                    break
        except OSError:
            pass
        return out

    def _expand_mentions(self, text: str) -> str:
        import re

        def repl(m: re.Match) -> str:
            rel = m.group(1).rstrip("/")
            p = (self.workdir / rel).resolve()
            try:
                p.relative_to(self.workdir)
            except ValueError:
                return m.group(0)
            if not p.is_file():
                return m.group(0)
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return m.group(0)
            if len(content) > 20000:
                content = content[:20000] + "\n…[truncated]"
            return f"\n\n[File: {rel}]\n```\n{content}\n```\n"

        return re.sub(r"@([\w\-./]+)", repl, text)

    # -- slash commands -----------------------------------------------------------

    def _slash_commands(self) -> list[tuple[str, str]]:
        builtins = [
            ("help", "show commands"),
            ("model", "switch model/provider"),
            ("theme", "switch theme"),
            ("sessions", "list / resume sessions"),
            ("new", "start a new session"),
            ("clear", "clear the transcript view"),
            ("init", "scaffold AGENTS.md"),
            ("todos", "show todo list"),
            ("exit", "quit neo"),
        ]
        custom = [(c.name, c.description or "project command")
                  for c in self._discovered_cmds.values()]
        seen = {n for n, _ in builtins}
        return builtins + [(n, d) for n, d in custom if n not in seen]

    async def _run_slash(self, line: str) -> None:
        parts = line.split(None, 1)
        name, argstr = parts[0], (parts[1] if len(parts) > 1 else "")

        if name == "help":
            cmds = self._slash_commands()
            lines = "\n".join(f"/{n:<10} {d}" for n, d in cmds)
            self._notice("commands:\n" + lines)
        elif name == "exit":
            await self._quit()
        elif name == "clear":
            await self.action_clear_transcript()
        elif name == "new":
            await self._new_session()
        elif name == "sessions":
            await self._pick_session()
        elif name == "model":
            await self._pick_model()
        elif name == "theme":
            await self._pick_theme()
        elif name == "init":
            from argparse import Namespace
            from ..cli import cmd_init
            cmd_init(Namespace(global_=False, force=False))
            self._notice(".neo/ scaffolded.", "info")
        elif name == "todos":
            if self._sidebar:
                self._sidebar.display = True
        else:
            if name in self._discovered_cmds:
                from ..agent.discovery import expand_command_template
                prompt = expand_command_template(
                    self._discovered_cmds[name].template, argstr.split())
                await self._on_submit(prompt)
            else:
                self._notice(f"unknown command /{name}", "warn")

    # -- dialogs ---------------------------------------------------------------------

    async def _pick_model(self) -> None:
        from ..providers import list_providers
        choices = [(f"{p.title}  ({p.default_model or '—'})", (p.id, p.default_model))
                   for p in list_providers() if p.default_model]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.push_screen(ChoiceModal("model", choices, fut))
        picked = await fut
        if picked and self._harness:
            pid, model = picked
            self.config.model = f"{pid}/{model}"
            await self._rebuild_runtime()
            self._notice(f"model → {self.config.model}", "info")

    async def _pick_theme(self) -> None:
        choices = [(n, n) for n in THEMES]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.push_screen(ChoiceModal("theme", choices, fut))
        picked = await fut
        if picked:
            self.theme = picked
            self._notice(f"theme → {picked}", "info")

    async def _pick_session(self) -> None:
        sessions = self.store.list()
        choices = [("new session", "__new__")] + [
            (f"{s['title'][:50]}  ({s['id'][:8]})", s["id"]) for s in sessions]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.push_screen(ChoiceModal("sessions", choices, fut))
        picked = await fut
        if picked == "__new__":
            await self._new_session()
        elif picked:
            await self._resume_session(picked)

    async def _new_session(self) -> None:
        if self._running:
            self._notice("finish the current run first.", "warn")
            return
        self.messages = []
        self.session_id = self.store.new(title="neo session",
                                         model=self.config.model)
        await self._rebuild_runtime(keep_session=True)
        if self._transcript:
            await self._transcript.remove_children()
        self._notice("new session started.", "info")
        self._refresh_topbar()

    async def _resume_session(self, sid: str) -> None:
        if self._running:
            self._notice("finish the current run first.", "warn")
            return
        self.session_id = sid
        self.messages = []
        if self._transcript:
            await self._transcript.remove_children()
        await self._rebuild_runtime(keep_session=True)
        await self._load_session()
        self._refresh_topbar()

    async def _rebuild_runtime(self, keep_session: bool = False) -> None:
        if self._provider is not None:
            try:
                await self._provider.aclose()
            except Exception:
                pass
        provider, harness, ctx, discovered = build_runtime(
            self.workdir, self.config,
            session_id=self.session_id,
            gate=self._gate, emit=self._emit, ui=_UIBridge(self))
        self._provider, self._harness, self._ctx = provider, harness, ctx
        self._discovered_cmds = discovered.get("commands", {})
        self._model_label = f"{provider.spec.id}/{harness.model}"
        self._refresh_topbar()

    # -- actions ------------------------------------------------------------------------

    async def action_cancel_run(self) -> None:
        if self._running and self._harness:
            self._harness.cancel()
        elif self._composer:
            self._composer.clear()

    async def action_toggle_sidebar(self) -> None:
        if self._sidebar:
            self._sidebar.display = not self._sidebar.display

    async def action_clear_transcript(self) -> None:
        if self._transcript and not self._running:
            await self._transcript.remove_children()
            self._notice("view cleared (history kept).", "info")

    async def _quit(self) -> None:
        if self._pump and not self._pump.done():
            self._pump.cancel()
        if self._provider is not None:
            try:
                await self._provider.aclose()
            except Exception:
                pass
        self.exit()

    def on_resize(self) -> None:
        # narrow terminals: auto-hide the sidebar
        if self._sidebar and self.size.width < 100:
            self._sidebar.display = False
