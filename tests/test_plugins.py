"""Tests for the neo plugin system (loader, hooks, PluginAPI, PluginManager).

Plugin fixtures are written as real .py files into tmp dirs -- no mocks
of the plugin code itself.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from neo import events as E  # noqa: E402
from neo.plugins import (  # noqa: E402
    HOOK_POINTS,
    HookManager,
    PluginAPI,
    PluginLoader,
    PluginManager,
)
from neo.tools.base import Tool, ToolContext, ToolResult  # noqa: E402

SRC = str(Path(__file__).resolve().parents[1] / "src")


def write_plugin(directory: Path, stem: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{stem}.py"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def make_manager(tmp_path, monkeypatch, plugins_cfg=None, project_plugins=()):
    """Build a PluginManager with an isolated HOME and project plugin dir."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    projdir = tmp_path / "proj"
    plugdir = projdir / ".neo" / "plugins"
    for stem, body in project_plugins:
        write_plugin(plugdir, stem, body)
    events: list = []
    cfg = SimpleNamespace(plugins=plugins_cfg or {"enabled": True, "dirs": []})
    mgr = PluginManager(workdir=projdir, config=cfg, emit=events.append)
    return mgr, events, projdir, home


# ---------------------------------------------------------------- hooks


def test_hook_points_exported():
    assert set(HOOK_POINTS) == {
        "tool.execute.before",
        "tool.execute.after",
        "permission.ask",
        "chat.params",
        "command.execute.before",
        "session.end",
    }


def test_before_hook_modifies_tool_args(tmp_path, monkeypatch):
    mgr, _, _, _ = make_manager(tmp_path, monkeypatch, project_plugins=[(
        "zz_args_mod",
        """
        def inject(payload):
            args = dict(payload["args"])
            args["injected"] = True
            return {"args": args}

        hooks = {"tool.execute.before": inject}
        """,
    )])
    payload = {"tool": "bash", "args": {"command": "ls"}}
    out = asyncio.run(mgr.trigger("tool.execute.before", payload))
    assert out["args"] == {"command": "ls", "injected": True}


def test_hook_receives_api_as_second_arg(tmp_path, monkeypatch):
    mgr, events, projdir, _ = make_manager(tmp_path, monkeypatch, project_plugins=[(
        "zz_api_arg",
        """
        def check(payload, api):
            assert str(api.workdir) == WORKDIR
            api.log("hook ran with api")
            return None

        hooks = {"session.end": check}
        """.replace("WORKDIR", repr(str(tmp_path / "proj"))),
    )])
    out = asyncio.run(mgr.trigger("session.end",
                                  {"session_id": "s1", "reason": "done"}))
    assert out["reason"] == "done"
    notices = [e for e in events
               if isinstance(e, E.Notice) and e.text == "hook ran with api"]
    assert len(notices) == 1


def test_async_hook_supported(tmp_path, monkeypatch):
    mgr, _, _, _ = make_manager(tmp_path, monkeypatch, project_plugins=[(
        "zz_async_hook",
        """
        async def tweak(payload):
            return {"model": "swapped-model"}

        hooks = {"chat.params": tweak}
        """,
    )])
    out = asyncio.run(mgr.trigger(
        "chat.params",
        {"system": "sys", "tools": [], "model": "orig"}))
    assert out["model"] == "swapped-model"


def test_after_hook_modifies_result_output(tmp_path, monkeypatch):
    mgr, _, _, _ = make_manager(tmp_path, monkeypatch, project_plugins=[(
        "zz_after_mod",
        """
        def annotate(payload):
            payload["result"].output += " [via plugin]"

        hooks = {"tool.execute.after": annotate}
        """,
    )])
    result = ToolResult(output="base output", title="t")
    out = asyncio.run(mgr.trigger(
        "tool.execute.after",
        {"tool": "read", "args": {}, "result": result}))
    assert out["result"].output == "base output [via plugin]"


def test_permission_hook_overrides_decision(tmp_path, monkeypatch):
    mgr, _, _, _ = make_manager(tmp_path, monkeypatch, project_plugins=[(
        "zz_perm_deny",
        """
        def deny_writes(payload):
            if payload["tool"] == "write":
                return {"decision": "deny"}
            return None

        hooks = {"permission.ask": deny_writes}
        """,
    )])
    payload = {"tool": "write", "target": "x.py", "detail": "",
               "decision": "ask"}
    out = asyncio.run(mgr.trigger("permission.ask", payload))
    assert out["decision"] == "deny"
    payload2 = {"tool": "read", "target": "x.py", "detail": "",
                "decision": "ask"}
    out2 = asyncio.run(mgr.trigger("permission.ask", payload2))
    assert out2["decision"] == "ask"


def test_failing_hook_does_not_stop_others(tmp_path, monkeypatch):
    mgr, _, _, _ = make_manager(tmp_path, monkeypatch, project_plugins=[(
        "zz_hook_errs",
        """
        def boom(payload):
            raise RuntimeError("boom")

        def second(payload):
            payload.setdefault("markers", []).append("second-ran")

        hooks = {"session.end": [boom, second]}
        """,
    )])
    out = asyncio.run(mgr.trigger("session.end",
                                  {"session_id": "s", "reason": "x"}))
    assert out["markers"] == ["second-ran"]
    assert len(out["_hook_errors"]) == 1
    assert "boom" in out["_hook_errors"][0]


def test_broken_plugin_is_skipped_with_warning(tmp_path, monkeypatch):
    mgr, events, _, _ = make_manager(tmp_path, monkeypatch, project_plugins=[
        ("zz_syntax_bad", "def broken(:\n  this is not python\n"),
        ("zz_still_good",
         """
         def mark(payload):
             payload["ok"] = True

         hooks = {"session.end": mark}
         """),
    ])
    assert [p.name for p in mgr.plugins] == ["zz_still_good"]
    warns = [e for e in events
             if isinstance(e, E.Notice) and e.level == "warn"]
    assert any("zz_syntax_bad" in w.text for w in warns)
    out = asyncio.run(mgr.trigger("session.end",
                                  {"session_id": "s", "reason": "x"}))
    assert out["ok"] is True


def test_deterministic_load_order_project_first_then_stem(tmp_path, monkeypatch):
    body = """
    def mark(payload, api):
        payload.setdefault("order", []).append(NAME)

    hooks = {"session.end": mark}
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    projdir = tmp_path / "proj"
    plugdir = projdir / ".neo" / "plugins"
    write_plugin(plugdir, "zz_b_proj", body.replace("NAME", "'proj-b'"))
    write_plugin(plugdir, "zz_a_proj", body.replace("NAME", "'proj-a'"))
    write_plugin(home / ".neo" / "plugins", "zz_c_glob",
                 body.replace("NAME", "'global-c'"))
    cfg = SimpleNamespace(plugins={"enabled": True, "dirs": []})
    mgr = PluginManager(workdir=projdir, config=cfg, emit=lambda e: None)
    assert [(p.scope, p.name) for p in mgr.plugins] == [
        ("project", "zz_a_proj"),
        ("project", "zz_b_proj"),
        ("global", "zz_c_glob"),
    ]
    out = asyncio.run(mgr.trigger("session.end",
                                  {"session_id": "s", "reason": "x"}))
    assert out["order"] == ["proj-a", "proj-b", "global-c"]


def test_reload_picks_up_new_plugin(tmp_path, monkeypatch):
    mgr, _, _, _ = make_manager(tmp_path, monkeypatch, project_plugins=[(
        "zz_reload_one",
        'hooks = {"session.end": lambda p: None}\n',
    )])
    assert len(mgr.plugins) == 1
    plugdir = tmp_path / "proj" / ".neo" / "plugins"
    write_plugin(plugdir, "zz_reload_two",
                 'hooks = {"session.end": lambda p: None}\n')
    mgr.reload()
    assert sorted(p.name for p in mgr.plugins) == [
        "zz_reload_one", "zz_reload_two"]
    assert mgr.hooks.handler_count("session.end") == 2


def test_disabled_plugins_load_nothing(tmp_path, monkeypatch):
    mgr, _, _, _ = make_manager(
        tmp_path, monkeypatch,
        plugins_cfg={"enabled": False, "dirs": []},
        project_plugins=[(
            "zz_disabled_plug",
            'hooks = {"session.end": lambda p: {"touched": True}}\n',
        )])
    assert mgr.plugins == []
    out = asyncio.run(mgr.trigger("session.end",
                                  {"session_id": "s", "reason": "x"}))
    assert "touched" not in out


def test_unknown_hook_name_in_emit_raises(tmp_path, monkeypatch):
    mgr, _, _, _ = make_manager(tmp_path, monkeypatch)
    try:
        asyncio.run(mgr.trigger("nope.not.a.hook", {}))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown hook")


# ---------------------------------------------------------------- tools


def test_plugin_registers_custom_tool(tmp_path, monkeypatch):
    mgr, _, _, _ = make_manager(tmp_path, monkeypatch, project_plugins=[(
        "zz_tool_plug",
        """
        from neo.tools.base import Tool, ToolResult

        class GreetTool(Tool):
            name = "greet"
            description = "says hello"
            parameters = {"type": "object", "properties": {}}
            needs_approval = False

            async def run(self, args, ctx):
                return ToolResult(output="hello from plugin", title="greet")

        tools = [GreetTool]
        """,
    )])
    classes = mgr.tools()
    assert len(classes) == 1 and classes[0].name == "greet"
    tool = classes[0]()
    ctx = SimpleNamespace()
    result = asyncio.run(tool({"who": "world"}, ctx))
    assert isinstance(result, ToolResult)
    assert result.output == "hello from plugin"
    assert result.is_error is False


def test_bad_tool_class_is_skipped_with_warning(tmp_path, monkeypatch):
    mgr, events, _, _ = make_manager(tmp_path, monkeypatch, project_plugins=[(
        "zz_bad_tool",
        """
        class NotATool:
            name = "nope"

        tools = [NotATool]
        """,
    )])
    assert mgr.tools() == []
    warns = [e for e in events
             if isinstance(e, E.Notice) and e.level == "warn"]
    assert any("nope" in w.text or "bad tool" in w.text for w in warns)


def test_plugin_tool_is_real_tool_subclass():
    # a plugin-registered Tool gets the shared never-raise wrapper
    from neo.tools.base import ToolResult

    class Exploding(Tool):
        name = "exploding"
        description = "d"
        parameters = {"type": "object", "properties": {}}
        needs_approval = False

        async def run(self, args, ctx):
            raise RuntimeError("kaput")

    api = PluginAPI(workdir=Path("."), config=None, emit=lambda e: None)
    assert api.register_tool(Exploding) is Exploding
    result = asyncio.run(Exploding()({}, SimpleNamespace()))
    assert isinstance(result, ToolResult) and result.is_error
    assert "kaput" in result.output


def test_register_tool_rejects_invalid():
    api = PluginAPI(workdir=Path("."), config=None, emit=lambda e: None)
    for bad in (object(), "nope", 42):
        try:
            api.register_tool(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")

    class MissingAttrs(Tool):
        name = "x"
        description = "d"
        parameters = {}

        async def run(self, args, ctx):  # pragma: no cover
            return ToolResult(output="")

    del MissingAttrs.parameters  # type: ignore[attr-defined]
    try:
        api.register_tool(MissingAttrs)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing parameters")


def test_hookmanager_register_rejects_unknown_hook():
    hm = HookManager()
    try:
        hm.register("nope", lambda p: None)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_loader_discover_order_and_underscore_skip(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    projdir = tmp_path / "proj"
    plugdir = projdir / ".neo" / "plugins"
    write_plugin(plugdir, "zz_b", "x = 1\n")
    write_plugin(plugdir, "zz_a", "x = 1\n")
    write_plugin(plugdir, "_private", "x = 1\n")
    loader = PluginLoader(projdir, emit=lambda e: None)
    found = loader.discover()
    assert [(s, p.stem) for s, p in found] == [
        ("project", "zz_a"), ("project", "zz_b")]
