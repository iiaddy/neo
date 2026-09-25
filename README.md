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

## Layout

- `src/neo/providers/` — provider-neutral streaming (OpenAI / Anthropic / Gemini),
  227 providers in the catalog
- `src/neo/tools/` — read, write, edit (fuzzy), glob, grep, list_dir, bash,
  webfetch, websearch, todo_write/read, task (subagents), question, skill
- `src/neo/agent/` — the autonomous loop: plan → act → verify, parallel tools,
  doom-loop guard, transcript repair, retry/backoff, auto-compaction,
  ask/allow/deny permissions
- `src/neo/tui/` — Textual interface: `/` palette, streaming, permission
  dialogs, themes
