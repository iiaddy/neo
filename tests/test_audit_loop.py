"""Regression tests for the agent-loop/security audit fixes (2026-09-26).

Covers: compound bash permission aggregation, dynamic shell constructs,
permission key mappings, unconditional undo approval, exact-command "always"
memory, subagent undo exclusion, plan-mode multi-file apply_patch, plan_exit
user confirmation, provider-turn retry isolation, doom guard ordering,
compaction tool-pair boundaries, and session record handling.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# NOTE: neo.tools must be imported before neo.patch (circular import guard).
from neo.tools.base import ToolContext  # noqa: E402
from neo.tools import build_toolset  # noqa: E402
from neo.tools.delegate import _GENERAL_CHILD_EXCLUDE  # noqa: E402

from neo.agent import loop as loop_mod  # noqa: E402
from neo.agent.loop import AgentHarness  # noqa: E402
from neo.agent.permissions import PermissionPolicy  # noqa: E402
from neo.agent.session import SessionStore  # noqa: E402
from neo.config import NeoConfig  # noqa: E402
from neo.patch.parser import parse_patch  # noqa: E402
from neo.plan.tools import PlanExitTool, plan_mode_allows_paths  # noqa: E402
from neo.scan.bashscan import is_dynamic, scan, scan_commands  # noqa: E402


# -- fakes (shape mirrors tests/test_loop.py) ---------------------------------

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


class FakeBash:
    name = "bash"
    description = "run shell"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.runs = 0

    async def run(self, args, ctx):
        self.runs += 1
        return SimpleNamespace(output="ok", is_error=False)


class FakeUndo:
    name = "undo"
    description = "restore snapshot"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.runs = 0

    async def run(self, args, ctx):
        self.runs += 1
        return SimpleNamespace(output="restored", is_error=False)


class FakeRead:
    name = "read"
    description = "read a file"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.runs = 0

    async def run(self, args, ctx):
        self.runs += 1
        return SimpleNamespace(output="file contents", is_error=False)


def make_harness(provider, tools, *, gate_answer="once", rules=None,
                 workdir="/tmp"):
    gate_log: list[tuple] = []

    async def gate(tool, target, detail):
        gate_log.append((tool, target, detail))
        return gate_answer

    ctx = SimpleNamespace(
        workdir=workdir,
        config=NeoConfig(),
        permissions=PermissionPolicy(rules if rules is not None else {}),
        gate=gate,
        emit=lambda e: None,
        todos=[],
        ui=None,
        locks={},
        skills={},
    )
    harness = AgentHarness(provider=provider, model="m", tools=tools,
                           system="sys", config=NeoConfig(), ctx=ctx,
                           max_steps=10)
    harness.gate_log = gate_log
    return harness


async def collect(harness, messages):
    return [ev async for ev in harness.run(messages)]


# --- finding 1: compound bash aggregation ------------------------------------

@pytest.mark.asyncio
async def test_bash_compound_allow_then_deny_denies():
    """An allow rule on the first command must not permit a denied later one."""
    bash = FakeBash()
    turn = [ToolCallReady("c1", "bash", {"command": "echo ok && rm -rf /"}),
            StreamEnd("tool_calls")]
    h = make_harness(FakeProvider([turn]), {"bash": bash},
                     rules={"bash": {"echo *": "allow", "rm *": "deny"}})
    evs = await collect(h, [{"role": "user", "content": "x"}])
    assert bash.runs == 0
    te = next(e for e in evs if e.kind == "tool_end")
    assert not te.ok and "denied" in te.output


@pytest.mark.asyncio
async def test_bash_compound_deny_then_allow_denies():
    """A denied first command is still denied when a later one is allowed."""
    bash = FakeBash()
    turn = [ToolCallReady("c1", "bash", {"command": "rm -rf / && echo ok"}),
            StreamEnd("tool_calls")]
    h = make_harness(FakeProvider([turn]), {"bash": bash},
                     rules={"bash": {"echo *": "allow", "rm *": "deny"}})
    evs = await collect(h, [{"role": "user", "content": "x"}])
    assert bash.runs == 0
    te = next(e for e in evs if e.kind == "tool_end")
    assert not te.ok and "denied" in te.output


@pytest.mark.asyncio
async def test_bash_compound_ask_aggregates_to_gate():
    """Ask on any command in a compound routes the whole call to the gate."""
    bash = FakeBash()
    turn = [ToolCallReady("c1", "bash", {"command": "echo a && echo b"}),
            StreamEnd("tool_calls")]
    h = make_harness(FakeProvider([turn]), {"bash": bash},
                     gate_answer="reject",
                     rules={"bash": {"echo *": "ask"}})
    evs = await collect(h, [{"role": "user", "content": "x"}])
    assert bash.runs == 0
    assert h.gate_log  # the gate was consulted
    te = next(e for e in evs if e.kind == "tool_end")
    assert not te.ok and "not approved" in te.output


# --- finding 3: scanner raw candidates + dynamic detection --------------------

def test_scan_commands_registers_raw_source():
    """The raw command source is a signature candidate, so exact-match
    rules work on quoted text shlex would otherwise reshape."""
    cmds = scan_commands('echo "a b"')
    assert len(cmds) == 1
    raw, sigs = cmds[0]
    assert raw == 'echo "a b"'
    assert sigs[0] == 'echo "a b"'
    assert "echo a b" in sigs  # shlex-derived signatures still present


def test_scan_commands_compound_splits():
    cmds = scan_commands("cd /tmp; ls -la")
    assert [raw for raw, _ in cmds] == ["cd /tmp", "ls -la"]


def test_scan_unchanged_for_existing_cases():
    """The legacy scan() API keeps its exact historical output."""
    assert scan("git checkout main") == ["git checkout main", "git checkout *",
                                         "git *"]
    assert scan("rm -rf x") == ["rm -rf x", "rm -rf *", "rm *"]


def test_is_dynamic():
    assert is_dynamic("echo $(rm -rf /)")
    assert is_dynamic("echo `whoami`")
    assert is_dynamic("echo ${HOME}/x")
    assert is_dynamic("VAR=1 rm -rf /")
    assert is_dynamic("A=1 B=2 make install")
    assert not is_dynamic("rm -rf /")
    assert not is_dynamic("git commit -m msg")
    # FOO=bar is an argument here, not a leading env assignment.
    assert not is_dynamic("echo FOO=bar")


@pytest.mark.asyncio
async def test_bash_dynamic_substitution_forces_ask():
    """A static allow rule must not silently permit command substitution."""
    bash = FakeBash()
    turn = [ToolCallReady("c1", "bash", {"command": "echo $(date)"}),
            StreamEnd("tool_calls")]
    h = make_harness(FakeProvider([turn]), {"bash": bash},
                     gate_answer="reject",
                     rules={"bash": {"echo *": "allow"}})
    evs = await collect(h, [{"role": "user", "content": "x"}])
    assert bash.runs == 0
    assert h.gate_log
    te = next(e for e in evs if e.kind == "tool_end")
    assert not te.ok and "not approved" in te.output


@pytest.mark.asyncio
async def test_bash_env_prefix_forces_ask():
    """VAR=1 prefixes change what runs; a blanket allow must still ask."""
    bash = FakeBash()
    turn = [ToolCallReady("c1", "bash", {"command": "VAR=1 rm -rf /"}),
            StreamEnd("tool_calls")]
    h = make_harness(FakeProvider([turn]), {"bash": bash},
                     gate_answer="reject",
                     rules={"bash": {"*": "allow"}})
    evs = await collect(h, [{"role": "user", "content": "x"}])
    assert bash.runs == 0
    assert h.gate_log


# --- finding 2: permission keys + undo ---------------------------------------

def test_permission_key_mappings():
    policy = PermissionPolicy(rules={})
    assert policy.key_for_tool("apply_patch") == "edit"
    assert policy.key_for_tool("plan_enter") == "session"
    assert policy.key_for_tool("plan_exit") == "session"
    # existing mappings unchanged
    assert policy.key_for_tool("bash") == "bash"
    assert policy.key_for_tool("write") == "edit"


@pytest.mark.asyncio
async def test_undo_always_invokes_gate():
    """undo bypasses policy rules: even an explicit allow rule cannot skip
    the user gate."""
    undo = FakeUndo()
    turn = [ToolCallReady("c1", "undo", {"handle": "abc"}),
            StreamEnd("tool_calls")]
    h = make_harness(FakeProvider([turn]), {"undo": undo},
                     gate_answer="reject",
                     rules={"undo": {"*": "allow"}})
    evs = await collect(h, [{"role": "user", "content": "undo it"}])
    assert undo.runs == 0
    assert h.gate_log and h.gate_log[0][0] == "undo"
    te = next(e for e in evs if e.kind == "tool_end")
    assert not te.ok and "not approved" in te.output


@pytest.mark.asyncio
async def test_undo_ignores_cached_always_memory():
    """A poisoned always-allow entry for undo must not bypass the gate."""
    undo = FakeUndo()
    h = make_harness(FakeProvider([]), {"undo": undo},
                     gate_answer="reject",
                     rules={"undo": {"*": "allow"}})
    h.ctx.permissions.allow_always("undo", "undo *")
    ok, _ = await h._permission("undo", "undo", "", args={})
    assert not ok
    assert h.gate_log  # still asked


@pytest.mark.asyncio
async def test_undo_gate_approve():
    h = make_harness(FakeProvider([]), {"undo": FakeUndo()},
                     gate_answer="once", rules={})
    ok, reason = await h._permission("undo", "undo", "", args={})
    assert ok and reason == ""


def test_general_child_toolset_excludes_undo():
    """Delegated subagents must never receive the undo tool."""
    assert "undo" in _GENERAL_CHILD_EXCLUDE
    assert {"task", "todo_write"} <= _GENERAL_CHILD_EXCLUDE

    async def gate(tool, target, detail):
        return "reject"

    ctx = ToolContext(workdir=Path("/tmp"), config=NeoConfig(),
                      permissions=PermissionPolicy(rules={}),
                      gate=gate, emit=lambda e: None, todos=[])
    tools = build_toolset(ctx)
    assert "undo" in tools  # sanity: the parent toolset has it
    child_tools = {k: v for k, v in tools.items()
                   if k not in _GENERAL_CHILD_EXCLUDE}
    assert "undo" not in child_tools


# --- finding 9: exact-command "always" memory ----------------------------------

def test_always_pattern_remembers_full_command():
    assert AgentHarness._always_pattern("bash", "rm -rf /") == "rm -rf /"
    assert AgentHarness._always_pattern("bash", "VAR=1 cmd") == "VAR=1 cmd"
    assert AgentHarness._always_pattern("read", "/tmp/a.txt") == "/tmp/a.txt"


@pytest.mark.asyncio
async def test_bash_always_does_not_broaden_to_first_word():
    """Answering 'always' for one command must not permit other commands
    sharing its first word."""
    bash = FakeBash()
    h = make_harness(FakeProvider([]), {"bash": bash},
                     gate_answer="always",
                     rules={"bash": {"echo *": "ask"}})
    ok, _ = await h._permission("bash", "echo hi", "",
                                args={"command": "echo hi"})
    assert ok
    assert h.ctx.permissions.session_allows == [("bash", "echo hi")]
    # A different command with the same first word still asks.
    ok, _ = await h._permission("bash", "echo bye", "",
                                args={"command": "echo bye"})
    assert len(h.gate_log) == 2
    # The remembered command itself does not ask again.
    ok, _ = await h._permission("bash", "echo hi", "",
                                args={"command": "echo hi"})
    assert ok and len(h.gate_log) == 2


# --- findings 4/5: plan mode --------------------------------------------------

TWO_FILE_PATCH = """*** Begin Patch
*** Update File: .neo/plans/a.md
@@
+# plan
*** Update File: src/evil.py
@@
+# evil
*** End Patch
"""

PLANS_ONLY_PATCH = """*** Begin Patch
*** Update File: .neo/plans/a.md
@@
+# plan
*** Update File: .neo/plans/b.md
@@
+# more plan
*** End Patch
"""


def test_plan_mode_allows_paths():
    wd = Path("/tmp/neo-wd")
    assert plan_mode_allows_paths(
        [".neo/plans/a.md", ".neo/plans/b.md"], wd) == (True, "")
    ok, reason = plan_mode_allows_paths(
        [".neo/plans/a.md", "src/x.py"], wd)
    assert not ok and "src/x.py" in reason
    ok, _ = plan_mode_allows_paths([], wd)
    assert not ok


@pytest.mark.asyncio
async def test_plan_mode_apply_patch_multi_file_escape_denied(tmp_path):
    """A patch touching one file outside .neo/plans/ is denied even when
    the other files are inside."""
    ops = parse_patch(TWO_FILE_PATCH)
    assert sorted(op.path for op in ops) == [".neo/plans/a.md", "src/evil.py"]
    h = make_harness(FakeProvider([]), {}, rules={}, workdir=str(tmp_path))
    h.ctx.plan_mode = {"active": True}
    ok, reason = await h._permission("apply_patch", "apply_patch", "",
                                     args={"patch": TWO_FILE_PATCH})
    assert not ok and "src/evil.py" in reason


@pytest.mark.asyncio
async def test_plan_mode_apply_patch_all_inside_allowed(tmp_path):
    h = make_harness(FakeProvider([]), {}, rules={}, workdir=str(tmp_path))
    h.ctx.plan_mode = {"active": True}
    ok, reason = await h._permission("apply_patch", "apply_patch", "",
                                     args={"patch": PLANS_ONLY_PATCH})
    assert ok, reason


class FakeUI:
    def __init__(self, answer):
        self.answer = answer
        self.asked: list = []

    async def ask(self, questions):
        self.asked.append(questions)
        return self.answer


def _plan_ctx(tmp_path, ui, gate_answer="once"):
    (tmp_path / ".neo" / "plans").mkdir(parents=True)
    (tmp_path / ".neo" / "plans" / "p.md").write_text("# plan\n")

    async def gate(tool, target, detail):
        return gate_answer

    ctx = ToolContext(workdir=tmp_path, config=NeoConfig(),
                      permissions=PermissionPolicy(rules={}),
                      gate=gate, emit=lambda e: None, todos=[], ui=ui)
    ctx.plan_mode = {"active": True}
    return ctx


@pytest.mark.asyncio
async def test_plan_exit_reject_keeps_plan_mode(tmp_path):
    ui = FakeUI({"0": "No, keep planning", "plan_exit": "No, keep planning"})
    ctx = _plan_ctx(tmp_path, ui)
    res = await PlanExitTool().run(
        {"plan_path": ".neo/plans/p.md", "summary": "does things"}, ctx)
    assert res.is_error and "Staying in plan mode" in res.output
    assert ctx.plan_mode["active"] is True
    question = ui.asked[0][0]["question"]
    assert "Switch to build mode and implement this plan?" in question


@pytest.mark.asyncio
async def test_plan_exit_escape_dismiss_keeps_plan_mode(tmp_path):
    """Escape/Dismiss in the modal (None answer) is a graceful rejection."""
    ui = FakeUI(None)
    ctx = _plan_ctx(tmp_path, ui)
    res = await PlanExitTool().run(
        {"plan_path": ".neo/plans/p.md", "summary": "does things"}, ctx)
    assert res.is_error
    assert ctx.plan_mode["active"] is True


@pytest.mark.asyncio
async def test_plan_exit_approve_leaves_plan_mode(tmp_path):
    ui = FakeUI({"0": "Yes, implement it"})
    ctx = _plan_ctx(tmp_path, ui)
    res = await PlanExitTool().run(
        {"plan_path": ".neo/plans/p.md", "summary": "does things"}, ctx)
    assert not res.is_error
    assert ctx.plan_mode["active"] is False
    assert ctx.plan_mode["plan_path"].endswith("p.md")


@pytest.mark.asyncio
async def test_plan_exit_gate_fallback_without_ui(tmp_path):
    """Headless runs (ui=None) fall back to the permission gate."""
    ctx = _plan_ctx(tmp_path, None, gate_answer="once")
    res = await PlanExitTool().run(
        {"plan_path": ".neo/plans/p.md", "summary": "does things"}, ctx)
    assert not res.is_error
    assert ctx.plan_mode["active"] is False


@pytest.mark.asyncio
async def test_plan_exit_gate_reject_without_ui(tmp_path):
    ctx = _plan_ctx(tmp_path, None, gate_answer="reject")
    res = await PlanExitTool().run(
        {"plan_path": ".neo/plans/p.md", "summary": "does things"}, ctx)
    assert res.is_error
    assert ctx.plan_mode["active"] is True


# --- finding 6: retry isolation ------------------------------------------------

@pytest.mark.asyncio
async def test_provider_turn_retry_resets_buffers_and_usage(monkeypatch):
    """A failed attempt must not leak text or usage into the retry."""
    monkeypatch.setattr(loop_mod, "compute_delay", lambda attempt: 0)
    fail = [TextDelta("partial"), UsageTick(10, 5),
            StreamErrorEv("boom", True)]
    ok_turn = [TextDelta("final"), UsageTick(3, 1), StreamEnd("stop")]
    p = FakeProvider([fail, ok_turn])
    h = make_harness(p, {"bash": FakeBash()})
    messages = [{"role": "user", "content": "hi"}]
    evs = await collect(h, messages)
    assert p.calls == 2
    assert messages[-1]["content"] == "final"  # not "partialfinal"
    u = next(e for e in evs if e.kind == "usage")
    assert (u.input_tokens, u.output_tokens) == (3, 1)  # failed attempt dropped


# --- finding 8: doom guard before execution ------------------------------------

@pytest.mark.asyncio
async def test_doom_guard_runs_before_execute():
    """The third identical tool call is stopped BEFORE it executes."""
    turn = [ToolCallReady("c1", "read", {"path": "a"}), StreamEnd("tool_calls")]
    p = FakeProvider([turn, turn, turn, turn])
    read = FakeRead()
    h = make_harness(p, {"read": read}, rules={"read": {"*": "allow"}})
    messages = [{"role": "user", "content": "x"}]
    evs = await collect(h, messages)
    assert evs[-1].kind == "run_end" and evs[-1].reason == "doom_loop"
    assert p.calls == 3
    assert read.runs == 2  # the third identical call never executed
    # transcript stays well-formed: the blocked call gets a synthetic result
    blocked = [m for m in messages
               if m.get("role") == "tool"
               and "already attempted 3 times" in str(m.get("content", ""))]
    assert len(blocked) == 1


# --- finding 7: compaction tool-pair boundary -----------------------------------

def test_pair_safe_keep_from():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "name": "read", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "out1"},
        {"role": "tool", "tool_call_id": "c1", "content": "out2"},
        {"role": "user", "content": "next"},
    ]
    assert AgentHarness._pair_safe_keep_from(msgs, 2) == 4
    assert AgentHarness._pair_safe_keep_from(msgs, 3) == 4
    assert AgentHarness._pair_safe_keep_from(msgs, 4) == 4
    assert AgentHarness._pair_safe_keep_from(msgs, 0) == 0


@pytest.mark.asyncio
async def test_maybe_compact_keeps_tool_pair_intact():
    """A cut landing on tool results moves the whole pair into the summarized
    head instead of orphaning results in the kept tail."""
    h = make_harness(FakeProvider([]), {})
    h.config.context_window = 1  # force compaction

    async def fake_summarize(messages):
        return "SUMMARY"

    h._summarize = fake_summarize
    messages = [
        {"role": "user", "content": "x" * 200},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "name": "read", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "out1"},
        {"role": "tool", "tool_call_id": "c1", "content": "out2"},
        {"role": "tool", "tool_call_id": "c1", "content": "out3"},
        {"role": "user", "content": "y" * 200},
        {"role": "user", "content": "z" * 200},
        {"role": "user", "content": "w" * 200},
    ]
    # raw plan_compaction cut: keep_from = 8 - 4 = 4 -> lands on a tool msg
    evs = [ev async for ev in h._maybe_compact(messages)]
    assert any(e.kind == "compact_start" for e in evs)
    assert messages[0]["role"] == "user" and "SUMMARY" in messages[0]["content"]
    tail = messages[1:]
    assert tail and tail[0]["role"] != "tool"  # no orphaned tool results


# --- finding 11: session records ------------------------------------------------

def test_session_load_warns_on_corrupt_lines(tmp_path, caplog):
    store = SessionStore(root=tmp_path)
    (tmp_path / "1.jsonl").write_text(
        '{"t": "user", "text": "hi"}\nNOT JSON\n{"t": "user", "text": "yo"}\n')
    with caplog.at_level(logging.WARNING):
        recs = store.load("1")
    assert [r["text"] for r in recs] == ["hi", "yo"]
    assert any("corrupt" in r.message for r in caplog.records)


def test_messages_from_records_ignores_compact_record():
    """The dead 'compact' record type is no longer rehydrated."""
    store = SessionStore(root=Path("/tmp"))
    msgs = store.messages_from_records([
        {"t": "compact", "summary": "old stuff"},
        {"t": "user", "text": "hi"},
    ])
    assert msgs == [{"role": "user", "content": "hi"}]
