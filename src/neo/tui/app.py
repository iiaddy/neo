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
from ..cli import boot_mcp, build_runtime
from .composer import Composer
from .dialogs import ChoiceModal, PermissionModal, QuestionModal, SecretModal
from .model_picker import (ModelPicker, load_favorites, load_recents,
                           push_recent)
from .session_dialog import SessionDialog
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
.modal-count { color: $text-muted; text-align: right; margin-top: 1; }
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
        self._mcp = None
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
        # MCP servers boot after the base runtime (async); their tools merge
        # into the same dict the harness holds, plus system instructions.
        try:
            self._mcp = await boot_mcp(self.config, self.workdir, harness.tools,
                                       harness)
            if self._mcp is not None and getattr(self._mcp, "errors", None):
                for srv, err in self._mcp.errors.items():
                    self._notice(f"mcp: server '{srv}' failed: {err}", "warn")
        except Exception as e:  # noqa: BLE001 - MCP is optional, never fatal
            self._notice(f"mcp: {e}", "warn")
            self._mcp = None
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
            self._harness.queue(text)
            self._notice("queued — runs when the current turn finishes.", "info")
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
            # Drain messages queued while busy: start a fresh turn.
            if self._harness is not None:
                queued = self._harness.take_queued()
                if queued:
                    await self._start_turn("\n\n".join(queued))

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
            ("login", "log in to a provider (store API key)"),
            ("logout", "log out from a provider"),
            ("theme", "switch theme"),
            ("sessions", "list / resume sessions"),
            ("new", "start a new session"),
            ("clear", "clear the transcript view"),
            ("init", "scaffold AGENTS.md"),
            ("plan", "enter plan mode for a goal"),
            ("todos", "show todo list"),
            ("exit", "quit neo"),
        ]
        custom = [(c.name, c.description or "project command")
                  for c in self._discovered_cmds.values()]
        mcp_cmds = []
        if self._mcp is not None:
            try:
                mcp_cmds = [(c["name"], c.get("description") or "mcp prompt")
                            for c in self._mcp.prompt_commands()]
            except Exception:
                mcp_cmds = []
        seen = {n for n, _ in builtins}
        out = builtins + [(n, d) for n, d in custom if n not in seen]
        seen.update(n for n, _ in out)
        return out + [(n, d) for n, d in mcp_cmds if n not in seen]

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
        elif name == "login":
            await self._login()
        elif name == "logout":
            await self._logout()
        elif name == "theme":
            await self._pick_theme()
        elif name == "init":
            from argparse import Namespace
            from ..cli import cmd_init
            cmd_init(Namespace(global_=False, force=False))
            self._notice(".neo/ scaffolded.", "info")
        elif name == "plan":
            if not argstr.strip():
                self._notice("usage: /plan <goal>", "warn")
            else:
                import importlib.resources
                from ..agent.discovery import (expand_command_template,
                                              parse_frontmatter)
                tmpl = (importlib.resources.files("neo") / "templates"
                        / "commands" / "plan.md").read_text(encoding="utf-8")
                _, body = parse_frontmatter(tmpl)
                prompt = expand_command_template(body.strip(), [argstr])
                await self._on_submit(prompt)
        elif name == "todos":
            if self._sidebar:
                self._sidebar.display = True
        else:
            if name in self._discovered_cmds:
                from ..agent.discovery import expand_command_template
                prompt = expand_command_template(
                    self._discovered_cmds[name].template, argstr.split())
                await self._on_submit(prompt)
            elif self._mcp is not None and await self._run_mcp_prompt(name, argstr):
                return
            else:
                self._notice(f"unknown command /{name}", "warn")

    async def _run_mcp_prompt(self, name: str, argstr: str) -> bool:
        """Execute an MCP prompt as a slash command. Returns True if handled."""
        assert self._mcp is not None
        try:
            cmds = self._mcp.prompt_commands()
        except Exception:
            return False
        cmd = next((c for c in cmds if c["name"] == name), None)
        if cmd is None:
            return False
        parts = argstr.split()
        args: dict[str, str] = {}
        for i, spec in enumerate(cmd.get("arguments", [])):
            aname = spec.get("name", f"arg{i + 1}")
            args[aname] = parts[i] if i < len(parts) else ""
        try:
            text = await self._mcp.resolve_prompt(cmd["server"], cmd["prompt"], args)
        except Exception as e:  # noqa: BLE001
            self._notice(f"mcp prompt failed: {e}", "error")
            return True
        await self._on_submit(text)
        return True

    # -- dialogs ---------------------------------------------------------------------

    async def _pick_model(self) -> None:
        from ..providers import list_providers
        provider_id = self._active_provider_id()
        if provider_id is None:
            # No active provider: let the user pick one first.
            provider_id = await self._pick_provider("model — select provider")
            if not provider_id:
                return
        specs = {p.id: p for p in list_providers()}
        spec = specs.get(provider_id)
        if spec is None:
            self._notice(f"unknown provider '{provider_id}'.", "error")
            return
        ids = list(spec.models) or ([spec.default_model] if spec.default_model else [])
        models = [f"{provider_id}/{m}" for m in ids]
        if not models:
            self._notice(f"provider '{provider_id}' has no models listed.", "warn")
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.push_screen(ModelPicker(models, favorites=load_favorites(),
                                     recents=load_recents(), future=fut,
                                     title=f"model — {spec.title}"))
        picked = await fut
        if picked and self._harness:
            push_recent(picked)
            self.config.model = picked
            await self._rebuild_runtime()
            self._notice(f"model → {self.config.model}", "info")

    def _active_provider_id(self) -> str | None:
        """Provider backing the current model (``provider/model``).

        Falls back to the single logged-in provider when exactly one key
        is stored; None when the choice is ambiguous.
        """
        from ..auth import AuthStore
        from ..providers import list_providers
        ids = {p.id for p in list_providers()}
        model = (self.config.model or "").strip()
        if "/" in model:
            pid = model.split("/", 1)[0]
            if pid in ids:
                return pid
        try:
            logged = [pid for pid in AuthStore().providers() if pid in ids]
        except Exception:
            logged = []
        if len(logged) == 1:
            return logged[0]
        return None

    async def _pick_provider(self, title: str,
                             only: list[str] | None = None) -> str | None:
        """Searchable provider picker. Resolves the provider id or None."""
        from ..providers import list_providers
        specs = list_providers()
        if only is not None:
            keep = set(only)
            specs = [p for p in specs if p.id in keep]
        items = [f"{p.id} — {p.title}" for p in specs]
        index = {item: p.id for item, p in zip(items, specs)}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.push_screen(ModelPicker(items, future=fut, title=title,
                                     record_recent=False))
        picked = await fut
        return index.get(picked) if picked else None

    async def _prompt_secret(self, title: str, placeholder: str) -> str | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.push_screen(SecretModal(title, placeholder, fut))
        return await fut

    async def _login(self) -> None:
        from ..auth import AuthStore
        from ..providers import list_providers
        provider_id = await self._pick_provider("login — select provider")
        if not provider_id:
            return
        key = await self._prompt_secret(
            f"API key for {provider_id}",
            "paste key — stored in ~/.config/neo/auth.json (0600)")
        if not key:
            self._notice("login cancelled.", "warn")
            return
        AuthStore().set(provider_id, key)
        # The logged-in provider becomes the active one, OpenCode-style,
        # so /model immediately lists its models.
        specs = {p.id: p for p in list_providers()}
        spec = specs.get(provider_id)
        default = spec.default_model if spec else ""
        self.config.model = (f"{provider_id}/{default}"
                             if default else provider_id)
        await self._rebuild_runtime()
        self._notice(f"logged in to {provider_id} — model → {self.config.model}",
                     "info")

    async def _logout(self) -> None:
        from ..auth import AuthStore
        try:
            logged = sorted(AuthStore().providers())
        except Exception:
            logged = []
        if not logged:
            self._notice("no providers logged in.", "warn")
            return
        provider_id = await self._pick_provider("logout — select provider",
                                                only=logged)
        if not provider_id:
            return
        AuthStore().delete(provider_id)
        self._notice(f"logged out from {provider_id}.", "info")

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
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.push_screen(SessionDialog(
            [{"id": "__new__", "title": "new session", "model": ""}] + sessions,
            fut))
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
        if self._mcp is not None:
            try:
                await self._mcp.stop()
            except Exception:
                pass
            self._mcp = None
        try:
            self._mcp = await boot_mcp(self.config, self.workdir, harness.tools,
                                       harness)
        except Exception as e:  # noqa: BLE001 - MCP is optional, never fatal
            self._notice(f"mcp: {e}", "warn")
            self._mcp = None
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
        plugins = getattr(self._ctx, "plugins", None)
        if plugins is not None:
            try:
                await plugins.trigger("session.end",
                                      {"session_id": self.session_id,
                                       "reason": "shutdown"})
            except Exception:
                pass
        if self._mcp is not None:
            try:
                await self._mcp.stop()
            except Exception:
                pass
            self._mcp = None
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
