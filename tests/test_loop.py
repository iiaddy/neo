"""Agent loop tests with a fake provider and stub tools.

Does not touch the network. Depends on neo.providers.retry (compute_delay).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from neo import events as E
from neo.agent.loop import AgentHarness, repair_history
from neo.agent.permissions import PermissionPolicy
from neo.config import NeoConfig


# -- fake provider events (shape mirrors neo.providers.base) -----------------

@dataclass
class TextDelta:
    kind = "text_delta"
    text: str = ""


@dataclass
class ToolCallReady:
    kind = "tool_call_ready"
    call_id: str = ""
    name: str = ""
    arguments: dict = field(default_factory=dict)


@dataclass
class UsageTick:
    kind = "usage_tick"
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class StreamEnd:
    kind = "stream_end"
    finish: str = "stop"


@dataclass
class StreamErrorEv:
    kind = "stream_error"
    message: str = "boom"
    retryable: bool = True


class FakeProvider:
    """Scripted provider: `turns` is a list of event-lists, one per stream()."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0

    async def stream(self, **kwargs):
        self.calls += 1
        turn = self.turns[min(self.calls - 1, len(self.turns) - 1)]
        for ev in turn:
            yield ev
            await asyncio.sleep(0)


class ReadTool:
    name = "read"
    description = "read a file"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, output="file contents"):
        self.output = output

    async def run(self, args, ctx):
        return SimpleNamespace(output=self.output, is_error=False)


def make_harness(provider, *, gate_answer="once", rules=None):
    async def gate(tool, target, detail):
        return gate_answer

    ctx = SimpleNamespace(
        workdir="/tmp",
        config=NeoConfig(),
        permissions=PermissionPolicy(rules if rules is not None
                                     else {"read": {"*": "allow"}}),
        gate=gate,
        emit=lambda e: None,
        todos=[],
        ui=None,
        locks={},
        skills={},
    )
    return AgentHarness(provider=provider, model="m", tools={"read": ReadTool()},
                        system="sys", config=NeoConfig(), ctx=ctx,
                        max_steps=10)


async def collect(harness, messages):
    return [ev async for ev in harness.run(messages)]


def kinds(events):
    return [e.kind for e in events]


# -- tests --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_only_turn():
    p = FakeProvider([[TextDelta("hello"), UsageTick(10, 5), StreamEnd("stop")]])
    h = make_harness(p)
    messages = [{"role": "user", "content": "hi"}]
    evs = await collect(h, messages)
    ks = kinds(evs)
    assert ks[0] == "run_start"
    assert "text_delta" in ks and "text_end" in ks
    assert ks[-1] == "run_end" and evs[-1].reason == "done"
    assert messages[-1]["role"] == "assistant"
    u = next(e for e in evs if e.kind == "usage")
    assert (u.input_tokens, u.output_tokens) == (10, 5)


@pytest.mark.asyncio
async def test_tool_turn_then_answer():
    p = FakeProvider([
        [ToolCallReady("c1", "read", {"path": "a.txt"}), StreamEnd("tool_calls")],
        [TextDelta("done"), StreamEnd("stop")],
    ])
    h = make_harness(p)
    messages = [{"role": "user", "content": "read it"}]
    evs = await collect(h, messages)
    ks = kinds(evs)
    assert "tool_start" in ks and "tool_end" in ks
    te = next(e for e in evs if e.kind == "tool_end")
    assert te.ok and te.output == "file contents"
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0]["content"] == "file contents"
    assert evs[-1].reason == "done"


@pytest.mark.asyncio
async def test_permission_reject_blocks_tool():
    p = FakeProvider([
        [ToolCallReady("c1", "read", {"path": "a.txt"}), StreamEnd("tool_calls")],
        [TextDelta("ok"), StreamEnd("stop")],
    ])
    h = make_harness(p, gate_answer="reject",
                     rules={"read": {"*": "ask"}})
    messages = [{"role": "user", "content": "read it"}]
    evs = await collect(h, messages)
    te = next(e for e in evs if e.kind == "tool_end")
    assert not te.ok and "not approved" in te.output
    assert p.calls == 2  # model still got to answer


@pytest.mark.asyncio
async def test_doom_loop_guard():
    turn = [ToolCallReady("c1", "read", {"path": "a"}), StreamEnd("tool_calls")]
    p = FakeProvider([turn, turn, turn, turn])
    h = make_harness(p)
    evs = await collect(h, [{"role": "user", "content": "x"}])
    assert evs[-1].kind == "run_end" and evs[-1].reason == "doom_loop"
    assert p.calls == 3


@pytest.mark.asyncio
async def test_retry_on_retryable_stream_error(monkeypatch):
    import neo.agent.loop as loop_mod
    monkeypatch.setattr(loop_mod, "compute_delay", lambda attempt: 0)
    p = FakeProvider([
        [StreamErrorEv("overloaded", True)],
        [TextDelta("recovered"), StreamEnd("stop")],
    ])
    h = make_harness(p)
    evs = await collect(h, [{"role": "user", "content": "x"}])
    assert p.calls == 2
    assert evs[-1].reason == "done"


@pytest.mark.asyncio
async def test_non_retryable_error_ends_run():
    p = FakeProvider([[StreamErrorEv("bad key", False)]])
    h = make_harness(p)
    evs = await collect(h, [{"role": "user", "content": "x"}])
    assert evs[-1].kind == "run_end" and evs[-1].reason == "error"
    assert any(e.kind == "notice" and "bad key" in e.text for e in evs)


@pytest.mark.asyncio
async def test_max_steps_forces_text_only():
    p = FakeProvider([[TextDelta("final"), StreamEnd("stop")]])
    h = make_harness(p)
    h.max_steps = 1
    captured = {}
    orig_stream = p.stream

    async def spy_stream(**kw):
        captured["tools"] = kw.get("tools")
        async for ev in orig_stream(**kw):
            yield ev

    p.stream = spy_stream
    evs = await collect(h, [{"role": "user", "content": "x"}])
    assert captured["tools"] == []
    assert evs[-1].reason == "done"


@pytest.mark.asyncio
async def test_steer_interrupts_run():
    p = FakeProvider([
        [TextDelta("first"), StreamEnd("stop")],
        [TextDelta("second"), StreamEnd("stop")],
    ])
    h = make_harness(p)
    messages = [{"role": "user", "content": "start"}]
    h.steer("actually do this instead")
    evs = await collect(h, messages)
    roles = [m["role"] for m in messages]
    assert roles.count("user") == 2  # steered message injected


def test_repair_history_in_run_path():
    msgs = [{"role": "assistant", "content": None,
             "tool_calls": [{"id": "z", "name": "read", "arguments": {}}]}]
    repair_history(msgs)
    assert msgs[-1]["role"] == "tool" and msgs[-1]["is_error"]


@pytest.mark.asyncio
async def test_auth_error_is_not_retried(monkeypatch):
    import neo.agent.loop as loop_mod
    from neo.providers.base import ProviderAuthError
    monkeypatch.setattr(loop_mod, "compute_delay", lambda attempt: 0)

    class AuthFailProvider:
        def __init__(self):
            self.calls = 0

        async def stream(self, **kwargs):
            self.calls += 1
            raise ProviderAuthError("no API key for provider 'x'")
            yield  # pragma: no cover - keeps this an async generator

    p = AuthFailProvider()
    h = make_harness(p)
    evs = await collect(h, [{"role": "user", "content": "x"}])
    assert p.calls == 1, "auth errors must fail fast without retry"
    assert evs[-1].kind == "run_end" and evs[-1].reason == "error"
    assert any(e.kind == "notice" and "no API key" in e.text for e in evs)
