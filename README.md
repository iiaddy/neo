# neo

[![PyPI](https://img.shields.io/pypi/v/neo-agnt)](https://pypi.org/project/neo-agnt/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-339%20passed-brightgreen)](https://github.com/iiaddy/neo)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Providers](https://img.shields.io/badge/providers-228-orange)](https://github.com/iiaddy/neo)

**neo** is a lightweight, autonomous terminal coding agent.
Describe a task in plain language — neo plans the work, executes it with
sandboxed tools, verifies the result, and streams everything live in your
terminal.

```
you:  neo -p "add rate limiting to the API"
neo:  planning… wrote .neo/plans/rate-limit.md
      ✓ read src/api.py (212 lines)
      ✓ edited src/api.py (+38 −4) · ruff clean · pyright: 0 new errors
      ✓ bash: pytest -x -q → 41 passed
      done in 34s · 12 steps · $0.021
```

Lightweight means: one install command, no daemon, no browser, no IDE
extension, no account. Plain terminal, plain JSON config.

## Capabilities

| Area | What you get |
| --- | --- |
| Agent loop | plan → act → verify; parallel tool calls, retries with backoff, doom-loop guard, transcript repair, auto-compaction |
| Tools (18) | `read` `write` `edit` `apply_patch` `glob` `grep` `list_dir` `bash` (+pty) `webfetch` `websearch` `todo_write` `todo_read` `task` `question` `skill` `plan_enter` `plan_exit` `undo` |
| Sandbox | every `bash` call can run in bubblewrap: user/IPC/PID/net namespaces, read-only root, hidden secrets, domain-filtered network |
| Providers | 228 in the catalog (OpenAI / Anthropic / Gemini protocols); any OpenAI-compatible endpoint works with just a `base_url` |
| Plan mode | changes are drafted to `.neo/plans/`, edits locked to the plan until you approve via `plan_exit` |
| Safety net | permission rules (`allow`/`ask`/`deny`), git snapshots before risky batches, `undo`, session forks, worktrees |
| Code intelligence | LSP diagnostics (pyright, tsserver, gopls, rust-analyzer, eslint) + formatters run after every edit |
| Extensibility | MCP servers, Python plugins with hooks, `.neo/commands/*.md` slash commands, `.neo/tools/*.py` custom tools, skills |

## Install

Requires Python 3.10+.

**Recommended — `pipx` (isolated, `neo` lands on your PATH):**

```bash
sudo apt install pipx          # debian / ubuntu
pipx ensurepath
pipx install neo-agnt
```

Log out and back in once (or `source ~/.bashrc`), then verify:

```bash
neo --version
```

**Alternative — virtualenv:**

```bash
python3 -m venv ~/.neo-venv
~/.neo-venv/bin/pip install neo-agnt
ln -s ~/.neo-venv/bin/neo ~/.local/bin/neo
```

For the sandboxed `bash` tool on Linux, also install:

```bash
sudo apt install bubblewrap socat
```

Then set an API key — environment variable, `auth.json`, or `neo.json`
(first match wins):

```bash
export ANTHROPIC_API_KEY="sk-..."
```

```bash
# ~/.config/neo/auth.json  (created with 0600 permissions)
{ "anthropic": "sk-..." }
```

### Troubleshooting

**`error: externally-managed-environment` on `pip install neo-agnt`**

Ubuntu 24.04+ blocks system-wide `pip install` (PEP 668). Do not fight it —
use one of the methods above:

```bash
# option 1: pipx (recommended for CLI apps)
sudo apt install pipx && pipx ensurepath && pipx install neo-agnt

# option 2: virtualenv
python3 -m venv ~/.neo-venv && ~/.neo-venv/bin/pip install neo-agnt

# option 3 (not recommended): override the guard
pip install --break-system-packages neo-agnt
```

**`neo: command not found` after `pipx install`**

`pipx ensurepath` adds `~/.local/bin` to PATH — it takes effect on next
login. Either re-login or run `source ~/.bashrc`, then check
`echo $PATH` contains `~/.local/bin`.

**`bubblewrap: command not found` when running bash**

The sandbox needs bubblewrap on PATH. Install it (`sudo apt install
bubblewrap`), or set `"sandbox": {"mode": "off"}` in `neo.json` to run
bash directly (you lose isolation).

**`No provider configured` / auth errors**

neo resolves keys in this order: environment variable → 
`~/.config/neo/auth.json` → `neo.json`. Run `neo config` to see the
resolved configuration and which provider it will use.

## Quickstart

```bash
neo                                          # interactive TUI
neo -p "fix the failing test in test_loop"   # headless, print reply and exit
neo -p "..." --allow-all                     # headless, skip permission prompts
neo --resume                                 # continue the last session
neo init                                     # scaffold .neo/ (AGENTS.md, skills, commands, agents)
neo models                                   # list providers and models
neo config                                   # show resolved configuration
neo snapshot | neo restore | neo fork        # git snapshots and session forks
```

Inside the TUI, `/` opens the command palette, `@` completes file paths,
`Ctrl+C` cancels the running turn, and typing while the agent works queues
your message for the next turn. `/login` stores a provider API key,
`/logout` removes it, and `/model` switches models within the active
provider.

## Configuration

`neo.json` — global at `~/.config/neo/neo.json`, per-project at `./neo.json`
or `./.neo/neo.json` (project overrides global):

```json
{
  "model": "anthropic/claude-sonnet-4-6",
  "small_model": "anthropic/claude-haiku-4-5",
  "max_steps": 40,
  "theme": "neo-dark",
  "verify_commands": ["ruff check .", "pytest -x -q"],
  "permissions": {
    "bash": { "git *": "allow", "rm -rf *": "deny", "*": "ask" },
    "edit": { "*": "ask" },
    "webfetch": { "*": "allow" }
  },
  "providers": {
    "my-proxy": { "base_url": "https://proxy.internal/v1", "api_key_env": "PROXY_KEY" }
  },
  "sandbox": { "mode": "auto", "network": "filtered" },
  "mcp": {
    "servers": {
      "fs": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"] }
    }
  }
}
```

Permission rules are `{tool: {pattern: allow|ask|deny}}`, last match wins.
`neo config` prints the fully resolved configuration.

## Sandbox

On Linux, `bash` runs inside bubblewrap — the same primitive OpenAI Codex
and Anthropic's sandbox runtime build on. The model sees a normal shell;
underneath it gets namespaces, a read-only root, and filtered egress.

```json
{
  "sandbox": {
    "mode": "auto",
    "network": "filtered",
    "allowedDomains": ["github.com", "*.github.com", "pypi.org", "*.pypi.org"],
    "deniedDomains": [],
    "allowWrite": ["."],
    "denyRead": ["~/.ssh", "~/.aws", "~/.gnupg"],
    "denyWrite": [".env"],
    "passEnv": ["PATH", "HOME", "LANG"],
    "allowSecrets": [],
    "privateTmp": true
  }
}
```

- `mode`: `auto` (warn once, run unsandboxed if bubblewrap is missing),
  `strict` (fail loudly instead of running unsandboxed), `off`.
- `network`: `none` (fully offline), `filtered` (isolated net namespace +
  domain-allowlist proxy), `full`.
- Environment is cleared; only `passEnv` survives, and secret-looking
  variables are stripped unless listed in `allowSecrets`.

Every bash result is tagged — `[sandbox] active (network=filtered)` —
so you always know what isolation was in effect.

## Plan mode

For non-trivial work, neo drafts a plan first and locks edits to it:

```
you:  /plan add oauth login
neo:  wrote .neo/plans/oauth-login.md — 5 steps, 3 files
      [plan_enter] edits restricted to the plan. review, then approve.
you:  looks good, proceed
neo:  [plan_exit] approved — executing as build turn…
      ✓ edited src/auth.py …  ✓ bash: pytest → 58 passed
```

Plans live in `.neo/plans/` as Markdown. Nothing outside the plan is
touched until you approve.

## Safety net

```bash
neo snapshot                  # snapshot the worktree (git tree-hash handle)
neo snapshot --list           # list snapshots
neo restore <id> --dry-run    # preview what a restore would change
neo restore <id>              # restore files from a snapshot
neo fork                      # fork the session (copy-on-write)
```

Snapshots are also taken automatically before risky tool batches, and the
`undo` tool (approval-gated) rolls back the last mutation.

## MCP

Add any MCP server in `neo.json`; its tools appear as `server_tool`,
its prompts become slash commands:

```json
{ "mcp": { "servers": {
  "github": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"] },
  "remote": { "url": "https://mcp.internal/v1", "headers": { "Authorization": "Bearer ${MCP_TOKEN}" } }
} } }
```

Each server is isolated — one failing server never takes down the rest.

## Plugins

Python plugins live in `~/.neo/plugins/` or `<project>/.neo/plugins/`.
A plugin is a module exposing `hooks`:

```python
# ~/.neo/plugins/notify.py
def on_tool_after(event):
    if event.tool == "bash" and event.result.is_error:
        desktop_notify("neo", f"bash failed: {event.result.output[:120]}")

hooks = {"tool.execute.after": on_tool_after}
```

Available hooks: `tool.execute.before/after`, `permission.ask`,
`chat.params`, `session.end`. Plugins can also
register their own tools.

## Custom commands and tools

```markdown
<!-- .neo/commands/review.md -->
---
description: review the current diff
agent: reviewer
---
Review `!git diff --stat` and leave findings on @src/.
```

`$1`/`$ARGUMENTS` interpolate arguments, `!cmd` runs shell upfront,
`@path` injects file contents. Python tools go in `.neo/tools/*.py`
and are loaded automatically.

## Project layout

```
src/neo/
  agent/        autonomous loop, permissions, sessions, plans, compaction
  tools/        18 built-in tools (files, shell+sandbox, web, todos, subagents…)
  providers/    streaming clients + 228-entry provider catalog (JSON)
  sandbox/      bubblewrap argv builder, capability detection, filtering proxy
  mcp/          MCP clients (stdio, StreamableHTTP), tool/prompt discovery
  lsp/  format/ language servers + formatters wired into every edit
  vcs/          git snapshots, selective restore, forks, worktrees
  plugins/      plugin loader and hook dispatch
  commands/     .neo/commands/*.md slash-command engine
  custom_tools/ .neo/tools/*.py loader
  agents/  plan/  agent roster, per-agent toolsets, plan-mode enforcement
  tui/          Textual interface: palette, dialogs, themes, session list
  auth.py       ~/.config/neo/auth.json key store (0600)
```

## Tests

```bash
python -m pytest          # 339 passed, 4 skipped
```

## License

MIT. See [LICENSE](LICENSE).
