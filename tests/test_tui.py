"""Tests for the neo Textual TUI: palette fuzzy matching, themes, widgets."""
from __future__ import annotations

import pytest

from neo.tui.palette import fuzzy_filter, fuzzy_score
from neo.tui.themes import DEFAULT_THEME, THEMES, get_theme, theme_names


# --------------------------------------------------------------------------
# Fuzzy palette
# --------------------------------------------------------------------------

def test_fuzzy_score_prefix_wins():
    assert fuzzy_score("mod", "model") is not None
    assert fuzzy_score("mod", "model") > fuzzy_score("del", "model")
    assert fuzzy_score("xyz", "model") is None
    assert fuzzy_score("", "anything") == 1.0


def test_fuzzy_filter_orders_and_limits():
    items = ["model", "theme", "sessions", "new", "clear"]
    out = fuzzy_filter("mode", items)
    assert out == ["model"]
    assert "theme" not in fuzzy_filter("mo", items)
    assert len(fuzzy_filter("", items, limit=3)) == 3


def test_fuzzy_filter_subsequence():
    out = fuzzy_filter("ssn", ["sessions", "new", "clear"])
    assert out == ["sessions"]


# --------------------------------------------------------------------------
# Themes
# --------------------------------------------------------------------------

def test_themes_have_required_keys():
    required = {"background", "surface", "panel", "border", "text", "muted",
                "accent", "green", "red", "yellow"}
    for name in theme_names():
        assert required <= set(get_theme(name)), name
    assert DEFAULT_THEME in theme_names()


# --------------------------------------------------------------------------
# Widget smoke tests (headless)
# --------------------------------------------------------------------------

from textual.app import App  # noqa: E402
from textual.containers import Vertical  # noqa: E402


async def _mount_ok(make):
    class _A(App):
        def compose(self):
            yield Vertical(make(), id="t")
    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.4)
    return True


@pytest.mark.asyncio
async def test_widgets_mount():
    from neo.tui.widgets import (AssistantMessage, NoticeLine, ReasoningBlock,
                                 Sidebar, StatusBar, ToolRow, UserMessage,
                                 VerifyRow)
    w = AssistantMessage()
    assert await _mount_ok(lambda: UserMessage("hello"))
    assert await _mount_ok(lambda: w)
    assert await _mount_ok(lambda: NoticeLine("hi"))
    assert await _mount_ok(lambda: ReasoningBlock("thinking"))
    assert await _mount_ok(lambda: ToolRow("read", "read x.py"))
    assert await _mount_ok(lambda: VerifyRow("pytest -q"))
    assert await _mount_ok(lambda: StatusBar())
    assert await _mount_ok(lambda: Sidebar())


@pytest.mark.asyncio
async def test_tool_row_finishes():
    from neo.tui.widgets import ToolRow

    seen = {}

    class _A(App):
        def compose(self):
            yield Vertical(id="t")

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        row = ToolRow("bash", "run tests")
        await app.query_one("#t", Vertical).mount(row)
        await pilot.pause(0.3)
        row.finish(True, "ok output", 12)
        await pilot.pause(0.2)
        seen["status"] = row.status
    assert seen["status"] == "ok"


@pytest.mark.asyncio
async def test_assistant_message_streams_and_finishes():
    from neo.tui.widgets import AssistantMessage

    final = {}

    class _A(App):
        def compose(self):
            yield Vertical(id="t")

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        msg = AssistantMessage()
        await app.query_one("#t", Vertical).mount(msg)
        await pilot.pause(0.2)
        msg.append_text("Hello **world**")
        await pilot.pause(0.3)
        msg.finish("Hello **world**")
        final["done"] = msg._done
    assert final["done"] is True


@pytest.mark.asyncio
async def test_app_mounts_and_palette_opens():
    from neo.config import NeoConfig
    from neo.tui.app import NeoApp

    cfg = NeoConfig(model="openai/gpt-4o-mini")
    app = NeoApp("/tmp", cfg)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(1.0)
        for ch in "/mod":
            await pilot.press(ch)
        await pilot.pause(0.5)
        composer = app.query_one("Composer")
        overlay = composer.query_one(".suggest")
        assert overlay.display is True
        # escape closes the palette
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert overlay.display is False


@pytest.mark.asyncio
async def test_permission_modal_resolves():
    import asyncio

    from neo.tui.dialogs import PermissionModal

    class _A(App):
        def on_mount(self):
            self.fut = asyncio.get_running_loop().create_future()
            self.push_screen(PermissionModal("write", "x.py", "detail", self.fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.8)
        await pilot.press("escape")
        choice = await asyncio.wait_for(app.fut, 5)
    assert choice == "reject"


@pytest.mark.asyncio
async def test_permission_modal_allow_once():
    import asyncio

    from neo.tui.dialogs import PermissionModal

    class _A(App):
        def on_mount(self):
            self.fut = asyncio.get_running_loop().create_future()
            self.push_screen(PermissionModal("bash", "rm -rf /", "detail", self.fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.8)
        await pilot.press("enter")  # focused "Allow once" button
        choice = await asyncio.wait_for(app.fut, 5)
    assert choice == "once"


# ---------------------------------------------------------------------------
# ModelPicker crash regression: typing in the filter must not kill the app
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_picker_filter_rebuild_no_crash():
    """First keystroke used to raise DuplicateIds (clear() is async, old
    items still in DOM when new ones mount with reused ids) and drop the
    user back to the terminal. Typing must now filter in place."""
    import asyncio

    from textual.app import App
    from textual.widgets import Input

    from neo.tui.model_picker import ModelPicker

    models = [f"anthropic/model-{i}" for i in range(60)]

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(ModelPicker(models, future=self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, ModelPicker)
        assert app.screen.query_one("#model-filter", Input).has_focus
        await pilot.press(*"model-1")
        await pilot.pause(0.6)
        # still alive on the picker screen — no crash
        assert isinstance(app.screen, ModelPicker)
        visible = app.screen._visible
        assert visible, "filter must leave matches"
        assert "anthropic/model-1" in visible
        assert "anthropic/model-2" not in visible
        # escape dismisses cleanly with None
        await pilot.press("escape")
        await pilot.pause(0.3)
    assert app._fut.done() and app._fut.result() is None


@pytest.mark.asyncio
async def test_model_picker_rapid_typing_coalesces():
    """Burst typing must not leave duplicate widget ids in the DOM."""
    import asyncio

    from textual.app import App
    from textual.widgets import ListItem

    from neo.tui.model_picker import ModelPicker

    models = [f"openai/gpt-{i}" for i in range(120)]

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(ModelPicker(models, future=self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press(*"gpt-1")
        await pilot.press(*["backspace"] * 5)  # clear back to empty
        await pilot.press(*"gpt-2")
        await pilot.pause(0.8)
        assert isinstance(app.screen, ModelPicker)
        ids = [item.id for item in app.screen.query(ListItem)]
        assert len(ids) == len(set(ids)), "duplicate ListItem ids in DOM"
        assert "openai/gpt-2" in app.screen._visible


def test_active_provider_id_from_config_model():
    from types import SimpleNamespace

    from neo.tui.app import NeoApp

    stub = SimpleNamespace(config=SimpleNamespace(model="anthropic/claude-x"))
    assert NeoApp._active_provider_id(stub) == "anthropic"


def test_active_provider_id_single_logged_in(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import neo.auth
    from neo.tui.app import NeoApp

    store = neo.auth.AuthStore(path=tmp_path / "auth.json")
    store.set("groq", "sk-test")
    monkeypatch.setattr(neo.auth, "AuthStore", lambda *a, **k: store)

    stub = SimpleNamespace(config=SimpleNamespace(model=""))
    assert NeoApp._active_provider_id(stub) == "groq"

    store.set("openai", "sk-test-2")
    assert NeoApp._active_provider_id(stub) is None  # ambiguous


# ---------------------------------------------------------------------------
# ModelPicker keyboard UX: arrows move, Enter selects (OpenCode-style)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_picker_keyboard_navigation():
    import asyncio

    from textual.app import App
    from textual.widgets import ListView

    from neo.tui.model_picker import ModelPicker

    models = ["b-model", "a-model", "c-model"]

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(ModelPicker(models, future=self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        picker = app.screen
        lst = picker.query_one(ListView)
        assert lst.index == 0  # first row highlighted on open
        await pilot.press("down", "down")
        await pilot.pause(0.2)
        assert lst.index == 2
        await pilot.press("up")
        await pilot.pause(0.2)
        assert lst.index == 1
        # arrows clamp at the ends
        await pilot.press("down", "down", "down")
        await pilot.pause(0.2)
        assert lst.index == 2
        await pilot.press("enter")
        await pilot.pause(0.3)
    assert app._fut.done()
    assert app._fut.result() == "c-model"  # highlight was on index 2


@pytest.mark.asyncio
async def test_model_picker_enter_selects_first_match():
    import asyncio

    from textual.app import App

    from neo.tui.model_picker import ModelPicker

    models = ["anthropic/claude-x", "groq/llama-3", "openai/gpt-5"]

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(ModelPicker(models, future=self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press(*"groq")
        await pilot.pause(0.5)
        assert app.screen._visible == ["groq/llama-3"]
        await pilot.press("enter")
        await pilot.pause(0.3)
    assert app._fut.done()
    assert app._fut.result() == "groq/llama-3"


@pytest.mark.asyncio
async def test_model_picker_enter_empty_list_keeps_open():
    import asyncio

    from textual.app import App

    from neo.tui.model_picker import ModelPicker

    class _A(App):
        def on_mount(self):
            self._fut = asyncio.get_running_loop().create_future()
            self.push_screen(ModelPicker(["a-model"], future=self._fut))

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press(*"zzz-no-match")
        await pilot.pause(0.5)
        assert app.screen._visible == []
        await pilot.press("enter")
        await pilot.pause(0.3)
        # still open, nothing resolved
        assert isinstance(app.screen, ModelPicker)
        assert not app._fut.done()


@pytest.mark.asyncio
async def test_login_switches_active_provider(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import neo.auth
    from neo.tui.app import NeoApp

    store = neo.auth.AuthStore(path=tmp_path / "auth.json")
    monkeypatch.setattr(neo.auth, "AuthStore", lambda *a, **k: store)

    calls = {}

    async def fake_pick_provider(title, only=None):
        return "groq"

    async def fake_prompt_secret(title, placeholder):
        return "sk-test-key"

    async def fake_rebuild(keep_session=False):
        calls["rebuilt"] = True

    stub = SimpleNamespace(
        config=SimpleNamespace(model="anthropic/claude-sonnet-4-6"),
        _pick_provider=fake_pick_provider,
        _prompt_secret=fake_prompt_secret,
        _rebuild_runtime=fake_rebuild,
        _notice=lambda *a, **k: None,
    )
    await NeoApp._login(stub)
    assert store.get("groq") == "sk-test-key"
    assert stub.config.model.startswith("groq/")
    assert calls.get("rebuilt") is True


def test_active_provider_id_after_login_model():
    from types import SimpleNamespace

    from neo.tui.app import NeoApp

    stub = SimpleNamespace(config=SimpleNamespace(model="groq/llama-3.3-70b-versatile"))
    assert NeoApp._active_provider_id(stub) == "groq"


@pytest.mark.asyncio
async def test_status_bar_model_updates_immediately():
    """Regression: /model switch must update the bottom status bar's model
    readout at once, not wait for the next usage event."""
    from textual.app import App
    from textual.widgets import Label
    from textual.containers import Vertical

    from neo.tui.widgets import StatusBar

    seen = {}

    class _A(App):
        def compose(self):
            yield StatusBar()

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        bar = app.query_one(StatusBar)
        await pilot.pause(0.3)
        bar.set_model("anthropic/claude-sonnet-4-6")
        await pilot.pause(0.2)
        seen["old"] = bar.query_one(".status-right", Label).render()
        bar.set_model("gemini/gemini")
        await pilot.pause(0.2)
        seen["new"] = bar.query_one(".status-right", Label).render()
    assert "anthropic/claude-sonnet-4-6" in str(seen["old"])
    assert "gemini/gemini" in str(seen["new"])
