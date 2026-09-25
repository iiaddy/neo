# neo — autonomous terminal coding agent

A Python terminal coding agent: type a task, watch it plan, act, and verify —
with a Textual TUI, a `/` command palette, and 227 model providers
(every provider from the opencode catalog except the one literally named
`opencode`, plus 5 curated extras).

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e .
```

Needs `ANTHROPIC_API_KEY` (or another provider key) in the environment.

## Use

```bash
neo                  # TUI
neo -p "fix the failing test in tests/test_loop.py" --allow-all
neo init             # scaffold .neo/ (AGENTS.md, skills, commands, agents)
neo models           # list providers
neo config           # show resolved config
```

## Configure

`neo.json` — global at `~/.config/neo/neo.json`, per-project `./neo.json` or
`./.neo/neo.json`:

```json
{
  "model": "anthropic/claude-sonnet-4-6",
  "small_model": "anthropic/claude-haiku-4-5",
  "max_steps": 40,
  "theme": "neo-dark",
  "verify_commands": ["ruff check .", "pytest -x -q"],
  "permissions": {
    "bash": { "git *": "allow", "*": "ask" },
    "edit": { "*": "ask" }
  },
  "providers": {
    "my-proxy": { "base_url": "https://proxy.local/v1" }
  }
}
```

Any unknown provider id falls back to the OpenAI-compatible protocol, so custom
proxies need zero code — just a `base_url`.

## Sandbox

Every `bash` call can run inside an OS-level sandbox (Linux, via
[bubblewrap](https://github.com/containers/bubblewrap) — the same primitive
OpenAI Codex and Anthropic's sandbox runtime use):

- **filesystem** — whole tree read-only, project dir read-write, `~/.ssh`
  etc. hidden, `denyWrite` paths re-mounted read-only, private `/tmp`
- **network** — `none` (fully offline), `filtered` (isolated net namespace +
  a domain-allowlist HTTP proxy bridged in over `socat`), or `full`
- **environment** — cleared; only a small `passEnv` whitelist survives, and
  secret-looking variables are stripped unless opted in via `allowSecrets`

```json
{
  "sandbox": {
    "mode": "auto",
    "network": "filtered",
    "allowedDomains": ["github.com", "*.github.com", "pypi.org"],
    "deniedDomains": [],
    "allowWrite": ["."],
    "denyRead": ["~/.ssh", "~/.aws"],
    "denyWrite": [".env"],
    "passEnv": ["PATH", "HOME"],
    "allowSecrets": [],
    "privateTmp": true
  }
}
```

`mode`: `auto` (warn once and run directly if bubblewrap is missing),
`strict` (fail loudly instead), `off`. Requires `bubblewrap` (+ `socat`
for `filtered`); on kernels that forbid a fresh `/proc` mount the sandbox
degrades gracefully without pid isolation and says so.

## Layout

- `src/neo/providers/` — provider-neutral streaming (OpenAI / Anthropic / Gemini),
  228 providers in the catalog
- `src/neo/sandbox/` — bubblewrap argv builder, capability detection,
  domain-filtering proxy, session orchestration
- `src/neo/tools/` — read, write, edit (fuzzy), apply_patch, glob, grep,
  list_dir, bash (+pty), webfetch, websearch, todo_write/read, task
  (subagents), question, skill, plan_enter/exit, undo
- `src/neo/agent/` — the autonomous loop: plan → act → verify, parallel tools,
  doom-loop guard, transcript repair, retry/backoff, auto-compaction,
  ask/allow/deny permissions, message queueing while busy
- `src/neo/tui/` — Textual interface: `/` palette, streaming, permission
  dialogs, themes, session dialog, model picker
- `src/neo/mcp/` — Model Context Protocol: stdio + StreamableHTTP clients,
  tool/prompt discovery with per-server failure isolation
- `src/neo/lsp/` + `src/neo/format/` — real LSP diagnostics after edits and
  formatter runs (ruff/black, prettier, gofmt, rustfmt)
- `src/neo/vcs/` — git snapshots (shadow index, tree-hash handles), preview +
  selective restore, session forks, worktrees; `neo snapshot|restore|fork`
- `src/neo/plugins/`, `src/neo/commands/`, `src/neo/custom_tools/` —
  project/global Python plugins with hooks, `.neo/commands/*.md` slash
  commands, `.neo/tools/*.py` custom tools
- `src/neo/agents/`, `src/neo/plan/` — agent roster with per-agent toolsets,
  plan mode with handoff; `src/neo/auth.py` — `~/.config/neo/auth.json`
  key store (0600)
