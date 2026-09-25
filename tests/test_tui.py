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
