"""neo command-line interface."""
from __future__ import annotations

import argparse
import asyncio
import importlib.resources
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import NeoConfig, config_dir, discover_config, split_model


def build_runtime(workdir: str | Path, config: NeoConfig,
                  gate=None, emit=None, ui=None, session_id: str = ""):
    """Assemble provider, tools, system prompt and harness (shared by TUI/CLI)."""
    from .agent.discovery import find_agents, find_commands, find_skills
    from .agent.loop import AgentHarness
    from .agent.permissions import PermissionPolicy
    from .agent.prompts import build_system_prompt, load_project_notes
    from .providers import resolve_provider
    from .tools import build_toolset
    from .tools.base import ToolContext

    workdir = Path(workdir).resolve()
    provider_id, model_id = split_model(config.model)
    _, small_model_id = split_model(config.small_model)
    provider = resolve_provider(provider_id, config)

    permissions = PermissionPolicy(config.permissions)
    skills = find_skills(workdir)
    ctx = ToolContext(
        workdir=workdir,
        config=config,
        permissions=permissions,
        gate=gate or _deny_gate,
        emit=emit or (lambda e: None),
        todos=[],
        ui=ui,
        skills={name: s.content for name, s in skills.items()},
    )
    from .plugins import PluginManager
    ctx.plugins = PluginManager(workdir, config, emit=ctx.emit)
    tools = build_toolset(ctx)
    for name in config.disabled_tools:
        tools.pop(name, None)

    # Agent roster: filter the toolset and prepend the agent's system prompt.
    from .agents import load_agents, toolset_for
    _agents = load_agents(getattr(config, "agents", None))
    _agent_name = getattr(config, "agent", "build") or "build"
    if _agent_name not in _agents:
        _agent_name = "build"
    _agent_def = _agents.get(_agent_name) or {}
    tools = toolset_for(_agent_name, tools, _agents)

    notes = load_project_notes(workdir)
    skills_index = "\n".join(f"- {n}: {s.description or 'no description'}"
                             for n, s in sorted(skills.items()))
    system = build_system_prompt(tools=tools, project_notes=notes,
                                 skills_index=skills_index)
    _agent_system = (_agent_def.get("system") or "").strip()
    if _agent_system:
        system = _agent_system + "\n\n" + system

    harness = AgentHarness(
        provider=provider, model=model_id or config.model, tools=tools,
        system=system, config=config, ctx=ctx,
        max_steps=config.max_steps, small_model=small_model_id or model_id,
        session_id=session_id)
    discovered = {"skills": skills, "commands": find_commands(workdir),
                  "agents": find_agents(workdir)}
    return provider, harness, ctx, discovered


async def _deny_gate(tool: str, target: str, detail: str) -> str:
    return "reject"


def _headless_gate_factory(allow_all: bool):
    async def gate(tool: str, target: str, detail: str) -> str:
        if allow_all:
            return "once"
        if sys.stdin.isatty():
            print(f"\n── permission ──\n{tool}  →  {target}")
            if detail:
                print(detail[:1500])
            try:
                ans = input("allow? [o]nce / [a]lways / [r]eject (r): ").strip().lower()
            except EOFError:
                return "reject"
            if ans.startswith("a"):
                return "always"
            if ans.startswith("o") or ans == "":
                return "once"
            return "reject"
        print(f"[deny] {tool} → {target} (non-interactive; use --allow-all)",
              file=sys.stderr)
        return "reject"
    return gate


async def run_print(prompt: str, workdir: str, config: NeoConfig,
                    allow_all: bool, resume: str | None):
    from .agent.session import SessionStore
    from . import events as E

    store = SessionStore()
    sid = resume or store.new(title=prompt[:60], model=config.model)
    if resume:
        messages = store.messages_from_records(store.load(sid))
        print(f"[resumed {sid}]")
    else:
        messages = []
    messages.append({"role": "user", "content": prompt})
    store.append(sid, {"t": "user", "text": prompt})

    gate = _headless_gate_factory(allow_all)
    _, harness, _ctx, _ = build_runtime(workdir, config, gate=gate,
                                        session_id=sid)
    mcp = None
    try:
        mcp = await boot_mcp(config, workdir, harness.tools, harness)
        if mcp is not None and getattr(mcp, "errors", None):
            for srv, err in mcp.errors.items():
                print(f"[mcp] server '{srv}' failed: {err}")
    except Exception as e:  # noqa: BLE001 - MCP is optional, never fatal
        print(f"[mcp] {e}")
    print(f"neo {__version__} · {config.model} · session {sid}\n")
    try:
        async for ev in harness.run(messages):
            k = ev.kind
            if k == "text_delta":
                print(ev.text, end="", flush=True)
            elif k == "reason_delta":
                pass
            elif k == "tool_start":
                print(f"\n● {ev.tool} — {ev.title}")
            elif k == "tool_end":
                mark = "✓" if ev.ok else "✗"
                print(f"  {mark} {ev.tool} ({ev.ms}ms)")
                if not ev.ok and ev.output:
                    print("  " + ev.output[:2000].replace("\n", "\n  "))
            elif k == "verify_start":
                print(f"\n● verify: {ev.command}")
            elif k == "verify_end":
                print(f"  {'✓' if ev.ok else '✗'} verify")
                if not ev.ok:
                    print("  " + ev.output[:1500].replace("\n", "\n  "))
            elif k == "notice":
                print(f"\n[{ev.level}] {ev.text}")
            elif k == "todo_update":
                n = len(ev.todos)
                done = sum(1 for t in ev.todos if t.get("status") == "completed")
                print(f"\n[todos {done}/{n}]")
            elif k == "usage":
                cost = f" · ${ev.cost_usd:.4f}" if ev.cost_usd else ""
                print(f"\n── {ev.input_tokens} in / {ev.output_tokens} out{cost}")
            elif k == "run_end":
                print(f"\n<run ended: {ev.reason}>")
            elif k == "turn_end":
                for m in reversed(messages):
                    if m.get("role") == "assistant":
                        store.append(sid, {"t": "assistant",
                                           "text": m.get("content"),
                                           "tool_calls": m.get("tool_calls", [])})
                        break
            if k == "tool_end":
                store.append(sid, {"t": "tool_result", "call_id": ev.call_id,
                                   "tool": ev.tool, "output": ev.output[:8000],
                                   "is_error": not ev.ok})
    except KeyboardInterrupt:
        print("\n<interrupted>")
        harness.cancel()
    except RuntimeError as exc:
        print(f"\nneo: error: {exc}")
    finally:
        plugins = getattr(_ctx, "plugins", None)
        if plugins is not None:
            try:
                await plugins.trigger("session.end",
                                      {"session_id": sid, "reason": "headless"})
            except Exception:
                pass
        if mcp is not None:
            try:
                await mcp.stop()
            except Exception:
                pass


def cmd_init(args) -> int:
    dest = Path.home() / ".config" / "neo" if args.global_ else Path.cwd() / ".neo"
    src_root = importlib.resources.files("neo") / "templates"
    if not src_root.is_dir():
        print("templates not found in package", file=sys.stderr)
        return 1
    copied = 0
    for src in sorted(src_root.rglob("*")):
        if not src.is_file() or src.name.startswith("."):
            continue
        rel = src.relative_to(src_root)
        dst = dest / rel
        if dst.exists() and not args.force:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        copied += 1
    print(f"neo init → {dest} ({copied} files)")
    print("Edit .neo/AGENTS.md to describe your project.")
    return 0


async def boot_mcp(config, workdir: str, tools: dict, harness):
    """Start configured MCP servers and merge their tools + instructions.

    Mutates *tools* (the harness's dict) in place and appends the manager's
    system instructions to the harness. Returns the MCPManager or None when
    no MCP servers are configured.
    """
    from .mcp import MCPManager
    from .tools import attach_mcp_tools
    cfg = getattr(config, "mcp", None) or {}
    if not cfg.get("servers"):
        return None
    manager = MCPManager(cfg, workdir=workdir)
    await manager.start()
    attach_mcp_tools(tools, manager)
    # Re-apply the active agent's toolset filter to the new tools.
    try:
        from .agents import load_agents, toolset_for
        agent_name = getattr(config, "agent", "build") or "build"
        agents = load_agents(getattr(config, "agents", None))
        if agent_name in agents:
            allowed = set(toolset_for(agent_name, tools, agents))
            for name in list(tools):
                if name not in allowed:
                    del tools[name]
    except Exception:
        pass
    try:
        instr = manager.system_instructions()
    except Exception:
        instr = ""
    if instr:
        harness.system = ((harness.system + "\n\n" + instr)
                          if harness.system else instr)
    return manager


def cmd_snapshot(args) -> int:
    from pathlib import Path
    from .vcs import VCSError, list_snapshots, snapshot
    wd = Path(".")
    if args.list:
        try:
            snaps = list_snapshots(wd)
        except VCSError as exc:
            print(f"neo: {exc}")
            return 1
        if not snaps:
            print("no snapshots.")
            return 0
        import datetime as _dt
        for s in snaps:
            when = _dt.datetime.fromtimestamp(
                s.get("ts", 0)).strftime("%Y-%m-%d %H:%M")
            msg = s.get("message", "")
            print(f"{s['handle'][:12]}  {when}  {msg}")
        return 0
    try:
        handle = snapshot(wd, message="manual")
    except VCSError as exc:
        print(f"neo: {exc}")
        return 1
    if not handle:
        print("neo: not a git repo (or git missing); no snapshot taken.")
        return 1
    print(f"snapshot {handle}")
    return 0


def cmd_restore(args) -> int:
    from pathlib import Path
    from .vcs import VCSError, preview_restore, restore
    wd = Path(".")
    try:
        if args.dry_run:
            diff = preview_restore(wd, args.handle, paths=args.paths or None)
            print(diff if diff else "(no differences)")
            return 0
        result = restore(wd, args.handle, paths=args.paths or None)
    except VCSError as exc:
        print(f"neo: {exc}")
        return 1
    print(result)
    return 0


def cmd_fork(args) -> int:
    from .agent.session import SessionStore
    from .vcs import fork_session
    try:
        new_id = fork_session(SessionStore(), args.session)
    except ValueError as exc:
        print(f"neo: {exc}")
        return 1
    print(f"forked {args.session} → {new_id}")
    return 0


def cmd_models(args) -> int:
    from .providers import list_providers
    if args.provider:
        specs = [s for s in list_providers() if s.id == args.provider]
        if not specs:
            print(f"unknown provider '{args.provider}'", file=sys.stderr)
            return 1
    else:
        specs = list_providers()
    for s in specs:
        envs = ",".join(s.env_vars) if s.env_vars else "-"
        print(f"{s.id:18} {s.title:22} {s.protocol:9} default: {s.default_model}")
        print(f"{'':18} {s.base_url or '(configure base_url)'}")
        print(f"{'':18} env: {envs}")
    return 0


def cmd_config(args, config: NeoConfig, path) -> int:
    print(f"# resolved from: {path or '(defaults)'}")
    print(json.dumps({
        "model": config.model, "small_model": config.small_model,
        "max_steps": config.max_steps, "context_window": config.context_window,
        "theme": config.theme, "thinking": config.thinking,
        "verify_commands": config.verify_commands,
        "disabled_tools": config.disabled_tools,
        "providers": {k: {"base_url": v.get("base_url", ""),
                          "api_key": "***" if v.get("api_key") else ""}
                      for k, v in config.providers.items()},
    }, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="neo",
                                 description="neo — autonomous terminal coding agent")
    ap.add_argument("-p", "--print", dest="prompt", metavar="PROMPT",
                    help="headless mode: run one prompt and print the result")
    ap.add_argument("--allow-all", action="store_true",
                    help="headless: auto-allow permission prompts")
    ap.add_argument("--resume", metavar="SESSION",
                    help="resume a previous session id")
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sp_init = sub.add_parser("init", help="scaffold .neo/ project files")
    sp_init.add_argument("--global", dest="global_", action="store_true",
                         help="install to ~/.config/neo instead of ./.neo")
    sp_init.add_argument("--force", action="store_true")
    sp_models = sub.add_parser("models", help="list providers")
    sp_models.add_argument("provider", nargs="?", default=None)
    sub.add_parser("config", help="show resolved configuration")
    sp_snap = sub.add_parser("snapshot", help="git snapshot (undo checkpoint)")
    sp_snap.add_argument("--list", action="store_true",
                         help="list existing snapshots")
    sp_rest = sub.add_parser("restore", help="restore a git snapshot")
    sp_rest.add_argument("handle", help="snapshot tree-hash handle")
    sp_rest.add_argument("--dry-run", action="store_true",
                         help="show the diff without changing anything")
    sp_rest.add_argument("paths", nargs="*", default=[],
                         help="restore only these paths")
    sp_fork = sub.add_parser("fork", help="fork a JSONL session")
    sp_fork.add_argument("session", help="session id to clone")
    args = ap.parse_args(argv)

    if args.version:
        print(f"neo {__version__}")
        return 0
    if args.cmd == "init":
        return cmd_init(args)

    config, cfg_path = discover_config(".")

    if args.cmd == "models":
        return cmd_models(args)
    if args.cmd == "config":
        return cmd_config(args, config, cfg_path)
    if args.cmd == "snapshot":
        return cmd_snapshot(args)
    if args.cmd == "restore":
        return cmd_restore(args)
    if args.cmd == "fork":
        return cmd_fork(args)

    if args.prompt:
        asyncio.run(run_print(args.prompt, ".", config, args.allow_all,
                              args.resume))
        return 0

    # default: TUI
    from .tui import run_tui
    run_tui(".", config, resume=args.resume, theme=config.theme or "neo-dark")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
