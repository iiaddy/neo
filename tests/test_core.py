"""Core tests: config, permissions, session, discovery, prompts, compact."""
from __future__ import annotations

import json
from pathlib import Path

from neo.agent.compact import estimate_tokens, flatten_for_summary, plan_compaction
from neo.agent.discovery import (builtin_agents, expand_command_template,
                                 find_commands, find_skills, parse_frontmatter)
from neo.agent.loop import repair_history
from neo.agent.permissions import PermissionPolicy, match_pattern
from neo.agent.prompts import build_system_prompt
from neo.agent.session import SessionStore
from neo.config import NeoConfig, default_permissions, split_model


def test_match_pattern():
    assert match_pattern("*", "git status")
    assert match_pattern("git *", "git status")
    assert match_pattern("git *", "git")
    assert not match_pattern("git *", "npm test")
    assert match_pattern("src/*.py", "src/main.py")
    assert not match_pattern("src/*.py", "src/a/b.py")
    assert match_pattern("src/**", "src/a/b.py")
    assert match_pattern("*.py", "x.py")
    assert not match_pattern("*.py", "x.pyc")


def test_permissions_last_match_wins():
    p = PermissionPolicy({"bash": {"*": "ask", "git *": "allow", "git push *": "deny"}})
    assert p.check("bash", "git status") == "allow"
    assert p.check("bash", "git push origin main") == "deny"
    assert p.check("bash", "npm test") == "ask"


def test_permissions_allow_always_session_only():
    p = PermissionPolicy({"edit": {"*": "ask"}})
    assert p.check("edit", "a.py") == "ask"
    p.allow_always("edit", "a.py")
    assert p.check("edit", "a.py") == "allow"
    assert p.check("edit", "b.py") == "ask"


def test_permissions_unknown_tool_defaults_ask():
    p = PermissionPolicy({})
    assert p.check("mystery", "x") == "ask"
    assert p.key_for_tool("webfetch") == "web"


def test_config_defaults_and_split():
    c = NeoConfig()
    assert c.model == "anthropic/claude-sonnet-4-6"
    assert split_model("anthropic/claude-sonnet-4-6") == ("anthropic", "claude-sonnet-4-6")
    assert split_model("gpt-5") == ("openai", "gpt-5")
    assert split_model("custom/model-x") == ("custom", "model-x")


def test_config_merge(tmp_path):
    f = tmp_path / "neo.json"
    f.write_text(json.dumps({"model": "openai/gpt-5",
                             "permissions": {"bash": {"git *": "allow"}}}))
    from neo.config import discover_config
    c, path = discover_config(tmp_path)
    assert c.model == "openai/gpt-5"
    assert c.permissions["bash"]["git *"] == "allow"
    # defaults preserved for other keys
    assert "edit" in c.permissions


def test_default_permissions_shape():
    dp = default_permissions()
    assert dp["bash"]["git status *"] == "allow"
    assert dp["edit"]["*"] == "ask"


def test_session_roundtrip(tmp_path):
    s = SessionStore(tmp_path)
    sid = s.new(title="hello", model="m")
    assert any(e["id"] == sid for e in s.list())
    s.append(sid, {"t": "user", "text": "hi"})
    s.append(sid, {"t": "assistant", "text": "hello", "tool_calls": []})
    s.append(sid, {"t": "usage", "input_tokens": 10, "output_tokens": 5})
    msgs = s.messages_from_records(s.load(sid))
    assert msgs[0] == {"role": "user", "content": "hi"}
    assert msgs[1]["role"] == "assistant"
    s.set_title(sid, "new title")
    assert s.list()[0]["title"] == "new title"


def test_discovery(tmp_path):
    skills = tmp_path / ".neo" / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        '---\nname: demo\ndescription: demo skill\n---\n# Demo\nbody\n')
    cmds = tmp_path / ".neo" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "hi.md").write_text('---\ndescription: say hi\n---\nHello $1\n')
    sk = find_skills(tmp_path)
    assert sk["demo"].description == "demo skill"
    assert "body" in sk["demo"].content
    cm = find_commands(tmp_path)
    assert cm["hi"].description == "say hi"
    assert expand_command_template(cm["hi"].template, ["bob"]) == "Hello bob"
    assert expand_command_template("no args here", ["x"]) == "no args here\n\nx"


def test_parse_frontmatter_no_fm():
    meta, body = parse_frontmatter("just text")
    assert meta == {} and body == "just text"


def test_builtin_agents():
    agents = builtin_agents()
    assert set(agents) == {"explore", "general", "plan"}
    assert agents["plan"].mode == "primary"


def test_repair_history_dangling():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "name": "read", "arguments": {}}]},
    ]
    repair_history(msgs)
    assert msgs[0]["role"] == "user"
    assert msgs[-1]["role"] == "tool" and msgs[-1]["is_error"]


def test_repair_history_keeps_valid():
    msgs = [
        {"role": "assistant", "content": "x",
         "tool_calls": [{"id": "c1", "name": "read", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read",
         "content": "ok", "is_error": False},
    ]
    repair_history(msgs)
    assert len(msgs) == 2


def test_compact_planning():
    msgs = [{"role": "user", "content": "x" * 1000} for _ in range(100)]
    assert estimate_tokens(msgs) > 1000
    plan = plan_compaction(msgs, context_window=1000)
    assert plan is not None
    assert plan_compaction([{"role": "user", "content": "hi"}],
                           context_window=1_000_000) is None


def test_flatten_for_summary():
    msgs = [{"role": "assistant", "content": "hello",
             "tool_calls": [{"name": "read", "arguments": {"path": "a"}}]},
            {"role": "tool", "name": "read", "content": "data",
             "is_error": False}]
    flat = flatten_for_summary(msgs)
    assert "[Tool call] read" in flat and "data" in flat


def test_system_prompt_assembly():
    class T:
        description = "does things"
    sys_prompt = build_system_prompt(
        tools={"read": T()},
        project_notes=[("/x/AGENTS.md", "be nice")],
        skills_index="- demo: demo skill")
    assert "You are neo" in sys_prompt
    assert "`read`" in sys_prompt
    assert "be nice" in sys_prompt
    assert "demo skill" in sys_prompt


def test_templates_exist():
    from importlib.resources import files
    root = files("neo") / "templates"
    assert (root / "AGENTS.md").is_file()
    assert (root / "skills" / "code-review" / "SKILL.md").is_file()
    assert (root / "commands" / "commit.md").is_file()
    assert (root / "agents" / "reviewer.md").is_file()


def test_cli_init(tmp_path, monkeypatch):
    from neo.cli import cmd_init
    import argparse
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(global_=False, force=False)
    assert cmd_init(args) == 0
    assert (tmp_path / ".neo" / "AGENTS.md").is_file()
    assert (tmp_path / ".neo" / "skills" / "code-review" / "SKILL.md").is_file()
