"""Audit regression tests: TUI crash fixes, keyboard UX, and runtime safety.

Covers the audit findings fixed in tui/session_dialog.py, tui/dialogs.py
and tui/app.py. Pilot-based where UI interaction is involved, following the
pattern in tests/test_tui.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _sessions(n: int = 30) -> list[dict]:
    return [
        {"id": f"ses_{i:02d}", "title": f"task number {i}",
         "model": "m", "updated_at": 0}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# session_dialog: filter-rebuild crash (DuplicateIds class of bug)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_dialog_filter_no_crash():
    """First keystroke used to raise DuplicateIds: lst.clear() is async, so
    the old ses-{i} items were still in the DOM when new ones mounted with
    reused ids. Typing must now filter in place."""
    import asyncio

    from textual.app import App
    from textual.widgets import Input, ListItem

    from neo.tui.session_dialog import SessionDialog

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(SessionDialog(_sessions(), self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, SessionDialog)
        assert app.screen.query_one("#session-filter", Input).has_focus
        await pilot.press(*"task number 1")
        await pilot.pause(0.6)
        # still alive — no crash
        assert isinstance(app.screen, SessionDialog)
        visible = app.screen._visible
        assert visible, "filter must leave matches"
        # UI shows exactly what the filter function returns
        from neo.tui.session_dialog import filter_sessions
        sessions = app.screen._sessions
        assert [s["id"] for s in visible] == [
            s["id"] for s in filter_sessions(sessions, "task number 1")]
        assert "ses_01" in [s["id"] for s in visible]
        ids = [item.id for item in app.screen.query(ListItem)]
        assert len(ids) == len(set(ids)), "duplicate ListItem ids in DOM"
        await pilot.press("escape")
        await pilot.pause(0.3)
    assert app._fut.done() and app._fut.result() is None


@pytest.mark.asyncio
async def test_session_dialog_rapid_typing_coalesces():
    import asyncio

    from textual.app import App
    from textual.widgets import ListItem

    from neo.tui.session_dialog import SessionDialog

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(SessionDialog(_sessions(120), self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press(*"task number 1")
        await pilot.press(*["backspace"] * 13)
        await pilot.press(*"task number 2")
        await pilot.pause(0.8)
        assert isinstance(app.screen, SessionDialog)
        ids = [item.id for item in app.screen.query(ListItem)]
        assert len(ids) == len(set(ids)), "duplicate ListItem ids in DOM"
        assert app.screen._visible
        from neo.tui.session_dialog import filter_sessions
        sessions = app.screen._sessions
        assert [s["id"] for s in app.screen._visible] == [
            s["id"] for s in filter_sessions(sessions, "task number 2")]
        assert "ses_02" in [s["id"] for s in app.screen._visible]


@pytest.mark.asyncio
async def test_session_dialog_keyboard_nav_enter():
    """Arrows move the highlight, Enter selects the session id."""
    import asyncio

    from textual.app import App
    from textual.widgets import ListView

    from neo.tui.session_dialog import SessionDialog

    sessions = [
        {"id": "ses_b", "title": "bravo", "model": "", "updated_at": 0},
        {"id": "ses_a", "title": "alpha", "model": "", "updated_at": 0},
        {"id": "ses_c", "title": "charlie", "model": "", "updated_at": 0},
    ]

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(SessionDialog(sessions, self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        lst = app.screen.query_one(ListView)
        assert lst.index == 0
        await pilot.press("down")
        await pilot.pause(0.2)
        assert lst.index == 1
        await pilot.press("enter")
        await pilot.pause(0.3)
    assert app._fut.done()
    assert app._fut.result() == "ses_a"


@pytest.mark.asyncio
async def test_session_dialog_enter_empty_list_keeps_open():
    import asyncio

    from textual.app import App

    from neo.tui.session_dialog import SessionDialog

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(SessionDialog(_sessions(5), self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press(*"zzz-no-match")
        await pilot.pause(0.5)
        assert app.screen._visible == []
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, SessionDialog)
        assert not app._fut.done()


# ---------------------------------------------------------------------------
# dialogs: QuestionModal escape + degenerate questions
# ---------------------------------------------------------------------------

def _question_app(questions):
    import asyncio

    from textual.app import App

    from neo.tui.dialogs import QuestionModal

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(QuestionModal(questions, self._fut))

    return _A()


@pytest.mark.asyncio
async def test_question_modal_escape_resolves_none():
    """Escape used to do nothing, leaving the agent loop hung on the
    never-resolving future. It now rejects with None."""
    questions = [{"header": "Q1", "question": "pick",
                  "options": [{"label": "A"}, {"label": "B"}]}]
    app = _question_app(questions)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.3)
    assert app._fut.done()
    assert app._fut.result() is None


@pytest.mark.asyncio
async def test_question_modal_empty_options_keyboard_dismissable():
    """A question with no options rendered zero buttons and could not be
    dismissed by keyboard. It now renders a Dismiss path."""
    from textual.widgets import Button

    from neo.tui.dialogs import QuestionModal

    questions = [{"header": "Q1", "question": "no options", "options": []}]
    app = _question_app(questions)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, QuestionModal)
        assert app.screen.query(Button), "expected a dismiss button"
        await pilot.press("escape")
        await pilot.pause(0.3)
    assert app._fut.done()
    assert app._fut.result() is None


@pytest.mark.asyncio
async def test_question_modal_empty_questions_dismissable():
    from neo.tui.dialogs import QuestionModal

    app = _question_app([])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, QuestionModal)
        await pilot.press("escape")
        await pilot.pause(0.3)
    assert app._fut.done()
    assert app._fut.result() is None


@pytest.mark.asyncio
async def test_question_modal_option_still_selects():
    questions = [{"header": "Q1", "question": "pick",
                  "options": [{"label": "A"}, {"label": "B"}]}]
    app = _question_app(questions)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.click("#opt-1")
        await pilot.pause(0.3)
    assert app._fut.done()
    assert app._fut.result()["Q1"] == "B"


# ---------------------------------------------------------------------------
# dialogs: ChoiceModal keyboard nav
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_choice_modal_keyboard_filter_enter():
    """Filter, arrow through visible matches, Enter selects the value —
    previously the user had to click."""
    import asyncio

    from textual.app import App
    from textual.widgets import Input, ListView

    from neo.tui.dialogs import ChoiceModal

    choices = [("alpha", "a"), ("beta", "b"), ("gamma", "c"),
               ("alphabet", "ab")]

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(ChoiceModal("pick", choices, self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        lst = app.screen.query_one(ListView)
        assert lst.index == 0  # first row highlighted on open
        assert app.screen.query_one("#choice-filter", Input).has_focus
        await pilot.press(*"alph")
        await pilot.pause(0.4)
        # only alpha/alphabet visible; highlight on first match
        assert lst.index == 0
        await pilot.press("down")
        await pilot.pause(0.2)
        assert lst.index == 3  # skips hidden rows
        await pilot.press("up")
        await pilot.pause(0.2)
        assert lst.index == 0
        await pilot.press("enter")
        await pilot.pause(0.3)
    assert app._fut.done()
    assert app._fut.result() == "a"


@pytest.mark.asyncio
async def test_choice_modal_escape_still_dismisses():
    import asyncio

    from textual.app import App

    from neo.tui.dialogs import ChoiceModal

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(ChoiceModal("pick", [("x", 1)], self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.3)
    assert app._fut.done()
    assert app._fut.result() is None


# ---------------------------------------------------------------------------
# app: _rebuild_runtime keeps the old runtime on ProviderConfigError
# ---------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, pid: str = "openai"):
        self.spec = SimpleNamespace(id=pid)
        self.closed = False

    async def aclose(self):
        self.closed = True


def _runtime_stub(**overrides):
    notices: list[tuple[str, str]] = []
    stub = SimpleNamespace(
        _provider=_FakeProvider("openai"),
        _harness=object(),
        _ctx=object(),
        workdir="/tmp",
        config=SimpleNamespace(model="azure/gpt-4.1"),
        session_id="ses_test",
        _gate=None,
        _emit=lambda *a: None,
        _mcp=None,
        _model_label="",
        _discovered_cmds={},
        _refresh_topbar=lambda: None,
    )
    stub._notice = lambda msg, kind="info": notices.append((msg, kind))
    stub._notices = notices
    for k, v in overrides.items():
        setattr(stub, k, v)
    return stub


@pytest.mark.asyncio
async def test_rebuild_runtime_keeps_old_on_config_error(monkeypatch):
    """Picking a blank-base_url provider in /model used to propagate
    ProviderConfigError through _run_slash. Now the old runtime survives
    and the user gets an error notice."""
    import neo.tui.app as app_mod
    from neo.providers import ProviderConfigError
    from neo.tui.app import NeoApp

    def boom(*a, **k):
        raise ProviderConfigError(
            "provider 'azure' has no default base_url: set "
            "providers.azure.base_url in neo.json")

    monkeypatch.setattr(app_mod, "build_runtime", boom)
    stub = _runtime_stub()
    old_provider = stub._provider
    await NeoApp._rebuild_runtime(stub)
    assert stub._provider is old_provider
    assert not old_provider.closed, "old provider must not be closed on failure"
    assert stub._notices, "expected an error notice"
    assert stub._notices[-1][1] == "error"
    assert "azure" in stub._notices[-1][0]


@pytest.mark.asyncio
async def test_rebuild_runtime_success_swaps(monkeypatch):
    """The success path still closes the old provider and swaps in the
    new runtime."""
    import neo.tui.app as app_mod
    from neo.tui.app import NeoApp

    new_provider = _FakeProvider("groq")

    def fake_build(*a, **k):
        return (new_provider,
                SimpleNamespace(model="llama-3.3-70b-versatile", tools={}),
                object(), {"commands": {"c": 1}})

    async def fake_boot_mcp(*a, **k):
        return None

    monkeypatch.setattr(app_mod, "build_runtime", fake_build)
    monkeypatch.setattr(app_mod, "boot_mcp", fake_boot_mcp)
    stub = _runtime_stub()
    old_provider = stub._provider
    await NeoApp._rebuild_runtime(stub)
    assert stub._provider is new_provider
    assert old_provider.closed
    assert stub._model_label == "groq/llama-3.3-70b-versatile"
    assert stub._discovered_cmds == {"c": 1}
    assert not stub._notices
