"""Builtin agent definitions, config merging, and per-agent tool filtering."""

from __future__ import annotations

import copy

_EXPLORE_SYSTEM = """\
You are a codebase search specialist. Your only job is to find things and
report them precisely. You never modify anything — no writes, no edits,
no shell commands that change state.

Method:
- Start broad: glob for file patterns that match the topic, then grep for
  the specific symbols, strings, or concepts.
- Read the files that look relevant; skim for structure, then read the
  key sections in full.
- Follow references outward (imports, callers, config) but stay on topic;
  stop when you have the answer, not when you have read everything.
- Prefer the task tool's explore specialist pattern: one focused pass per
  question, then synthesize.

Report format:
1. Answer — the direct answer to the question asked.
2. Evidence — file paths with line numbers for each claim (absolute paths).
3. Gaps — what you could not find or were unsure about.

Be thorough but terse. No emojis, no filler, no code changes."""

_TITLE_SYSTEM = """\
Generate a session title from the conversation so far.

Rules:
- At most 50 characters.
- Same language as the user's messages.
- Name the concrete task or topic (e.g. "Fix OAuth redirect loop"), not
  the tools used.
- No tool names, no quotes, no trailing period.
- Output only the title, nothing else."""

_SUMMARY_SYSTEM = """\
Summarize this coding session the way a pull-request description reads.

Rules:
- 2-3 sentences, first person ("I added…", "I fixed…").
- Cover: what changed, why it mattered, and the current state (done,
  blocked, needs review).
- Include exact file paths and commit/test results when they matter.
- Never ask questions; this is a record, not a conversation.
- Output only the summary, nothing else."""

_BUILD_SYSTEM = """\
You are neo's build agent: an autonomous coding agent that turns approved
plans and user requests into working code.

How you work:
- Understand first: read the relevant files and project notes before
  changing anything. Prefer small, verifiable steps over big rewrites.
- Match the codebase: follow existing patterns, naming, and style. New
  abstractions earn their place; copy-paste is a smell.
- Every change is a hypothesis: after editing, verify — run the tests,
  type checks, or builds that cover what you touched. If verification is
  slow or unavailable, say so instead of claiming it passed.
- Respect permissions: if a tool call is denied or rejected, adjust your
  approach or ask; do not route around the user.
- Use the todo list for multi-step work so progress is visible. Delegate
  independent research to the explore subagent when it saves time.
- You may start plan mode with plan_enter when a task is large or unclear;
  write the plan, get it approved via plan_exit, then build.

Output: report what you changed (file:line), what you verified and the
result, and anything left open. Keep it tight."""

_PLAN_SYSTEM = """\
You are neo's plan agent. You research and design; you never implement.

How you work:
- Read the codebase first: relevant source files, configs, tests, and
  project notes. Cite every claim with a path.
- Ask the user questions when the goal is ambiguous — a plan built on a
  wrong assumption is worse than no plan.
- Write the plan document to .neo/plans/<slug>.md with this structure:
  1. Goal (one line). 2. Current state (what exists today).
  3. Proposed changes, file by file, with the key edits.
  4. Risks and open questions. 5. Verification (how to confirm it works).
- While in plan mode you cannot write code or run shell commands. If you
  need a read-only subagent, spawn the explore specialist.
- When the plan is complete, call plan_exit with the plan path and a
  one-paragraph summary. Do not start implementing."""

AGENT_DEFS: dict[str, dict] = {
    "explore": {
        "description": "Read-only codebase search specialist; finds code and reports paths, never modifies anything.",
        "tools": ["read", "list_dir", "glob", "grep", "webfetch", "websearch"],
        "system": _EXPLORE_SYSTEM,
        "mode": "subagent",
        "hidden": False,
        "dynamic_tools": False,
    },
    "title": {
        "description": "Generates a <=50 char session title from the transcript.",
        "tools": [],
        "system": _TITLE_SYSTEM,
        "mode": "primary",
        "hidden": True,
        "dynamic_tools": False,
    },
    "summary": {
        "description": "Summarizes a session PR-style.",
        "tools": [],
        "system": _SUMMARY_SYSTEM,
        "mode": "primary",
        "hidden": True,
        "dynamic_tools": False,
    },
    "build": {
        "description": "Default agent: implements, verifies, and reports.",
        "tools": [
            "read", "list_dir", "glob", "grep",
            "write", "edit", "apply_patch", "bash",
            "webfetch", "websearch",
            "todo_write", "todo_read",
            "task", "question", "skill",
            "plan_enter", "plan_exit", "undo",
        ],
        "system": _BUILD_SYSTEM,
        "mode": "primary",
        "hidden": False,
    },
    "plan": {
        "description": "Plan mode: researches and designs; writes plan docs only, never code.",
        "tools": [
            "read", "list_dir", "glob", "grep",
            "webfetch", "websearch",
            "question", "plan_exit", "task",
            # write/edit are offered so the plan document can be saved;
            # plan_mode_allows restricts them to .neo/plans/ at runtime.
            "write", "edit",
        ],
        "system": _PLAN_SYSTEM,
        "mode": "primary",
        "hidden": False,
    },
}


def load_agents(config_agents: dict | None) -> dict[str, dict]:
    """Merge user config over the builtin agent defs.

    - ``{"name": {"disable": True}}`` removes a builtin.
    - Any other dict is merged field-by-field over the builtin of the
      same name (missing keys keep their builtin values).
    - A name with no builtin becomes a new agent; it should define
      ``description``, ``tools`` and ``system`` (missing fields get
      safe defaults).

    The builtin table is never mutated; a deep copy is returned.
    """
    agents = copy.deepcopy(AGENT_DEFS)
    for name, override in (config_agents or {}).items():
        if not isinstance(override, dict):
            continue
        if override.get("disable"):
            agents.pop(name, None)
            continue
        base = agents.get(name, {})
        merged = copy.deepcopy(base)
        for key, value in override.items():
            if key == "disable":
                continue
            merged[key] = copy.deepcopy(value)
        merged.setdefault("description", f"Custom agent '{name}'.")
        merged.setdefault("tools", [])
        merged.setdefault("system", "")
        merged.setdefault("mode", "all")
        merged.setdefault("hidden", False)
        agents[name] = merged
    return agents


def toolset_for(agent_name: str, all_tools: dict,
                agents: dict | None = None) -> dict:
    """Filter an instantiated tool dict down to one agent's toolset.

    Raises KeyError for an unknown agent name.

    Tools not listed in *any* builtin agent's tool list (dynamic tools:
    MCP `<server>_<tool>`, plugin tools, custom `.neo/tools/` tools) are
    included for agents whose def sets ``dynamic_tools`` (default True),
    so the build agent automatically picks up newly connected tools while
    read-only agents (explore/title/summary) stay locked down.
    """
    table = agents if agents is not None else AGENT_DEFS
    try:
        agent_def = table[agent_name]
    except KeyError:
        raise KeyError(f"unknown agent {agent_name!r}") from None
    wanted = agent_def["tools"]
    out = {name: tool for name, tool in all_tools.items() if name in wanted}
    if agent_def.get("dynamic_tools", True):
        known: set[str] = set()
        for d in table.values():
            known.update(d.get("tools", []))
        for name, tool in all_tools.items():
            if name not in known and name not in out:
                out[name] = tool
    return out
