"""Tests for the TUI working/thinking indicator and its event wiring."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from textual.app import App  # noqa: E402
from textual.containers import Vertical  # noqa: E402

from neo.tui.widgets import WorkingIndicator  # noqa: E402


@pytest.mark.asyncio
async def test_working_indicator_animates_and_hides():
    seen = {}

    class _A(App):
        def compose(self):
            yield Vertical(WorkingIndicator(), id="t")

    app = _A()
    async with app.run_test(size=(100, 30)) as pilot:
        w = app.query_one(WorkingIndicator)
        assert w.display is False
        w.start("thinking")
        assert w.display is True
        await pilot.pause(0.35)
        label = w.query_one(".working-label")
        text1 = w._text
        await pilot.pause(0.25)
        text2 = w._text
        # spinner frame and/or animated dots must have advanced
        seen["animated"] = text1 != text2
        seen["thinking"] = "thinking" in text1
        seen["elapsed"] = text1.rstrip().endswith("s")
        w.set_phase("bash")
        await pilot.pause(0.15)
        seen["phase"] = "bash" in w._text
        w.stop()
        await pilot.pause(0.1)
        seen["hidden"] = w.display is False
    assert seen["animated"], "indicator did not animate"
    assert seen["thinking"], "phase text missing"
    assert seen["elapsed"], "elapsed timer missing"
    assert seen["phase"], "set_phase did not update"
    assert seen["hidden"], "stop() did not hide"


@pytest.mark.asyncio
async def test_app_working_indicator_follows_turn():
    """_on_submit shows it; tool/text events move the phase; end hides it."""
    import neo.tui.app as appmod
    from neo.tui.app import NeoApp

    class FakeHarness:
        def __init__(self):
            self.model = "fake/model"
            self.tools = {}

        def queue(self, t): ...
        def take_queued(self): return []
        def cancel(self): ...

        async def run(self, messages):
            yield SimpleNamespace(kind="turn_start", index=0)
            await asyncio.sleep(0.3)
            yield SimpleNamespace(kind="reason_start")
            yield SimpleNamespace(kind="reason_delta", text="hmm")
            yield SimpleNamespace(kind="reason_end", text="hmm")
            yield SimpleNamespace(kind="tool_start", tool="read",
                                  title="read x.py", call_id="c1")
            yield SimpleNamespace(kind="tool_end", call_id="c1", tool="read",
                                  output="ok", ok=True, ms=5)
            yield SimpleNamespace(kind="text_start")
            yield SimpleNamespace(kind="text_delta", text="hi")
            yield SimpleNamespace(kind="text_end", text="hi")
            yield SimpleNamespace(kind="turn_end")
            yield SimpleNamespace(kind="run_end", reason="done")

    def fake_build_runtime(workdir, config, session_id="", gate=None,
                           emit=None, ui=None):
        h = FakeHarness()
        provider = SimpleNamespace(spec=SimpleNamespace(id="fake"))
        return provider, h, SimpleNamespace(), {"commands": {}}

    async def no_mcp(*a, **k):
        return None

    appmod.build_runtime = fake_build_runtime
    appmod.boot_mcp = no_mcp
    cfg = SimpleNamespace(model="fake/model", small_model="", permissions={},
                          disabled_tools=[], agents=None, agent="build")
    app = NeoApp("/tmp", cfg)
    phases = {}
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.5)
        w = app.query_one(WorkingIndicator)
        await app._on_submit("hello")
        await pilot.pause(0.15)
        phases["shown"] = w.display is True
        phases["thinking"] = "thinking" in w._text
        await pilot.pause(0.6)  # reason + tool phases
        mid = w._text
        phases["mid_phase"] = ("read" in mid or "thinking" in mid
                               or "working" in mid or "writing" in mid)
        await pilot.pause(1.0)  # let the turn finish
        phases["hidden"] = w.display is False
        phases["not_busy"] = app._status._busy is False
    assert phases["shown"], "indicator not shown on submit"
    assert phases["thinking"], "indicator did not start in thinking phase"
    assert phases["mid_phase"], f"phase did not track events: {mid!r}"
    assert phases["hidden"], "indicator not hidden after turn"
    assert phases["not_busy"], "status bar still busy after turn"
