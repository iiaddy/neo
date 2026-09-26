# neo

[![PyPI](https://img.shields.io/pypi/v/neo-agnt)](https://pypi.org/project/neo-agnt/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**neo** is a lightweight, autonomous terminal coding agent. Describe a task —
neo plans the work, executes it with sandboxed tools, verifies the result,
and streams everything live in your terminal.

```
you:  neo -p "add rate limiting to the API"
neo:  planning… wrote .neo/plans/rate-limit.md
      ✓ read src/api.py (212 lines)
      ✓ edited src/api.py (+38 −4) · ruff clean · pyright: 0 new errors
      ✓ bash: pytest -x -q → 41 passed
      done in 34s · 12 steps · $0.021
```

One install, no daemon, no browser, no IDE extension, no account.

## Highlights

| | |
| --- | --- |
| Autonomous loop | plan → act → verify, with parallel tool calls, retries, doom-loop guard, and auto-compaction |
| Sandboxed shell | bubblewrap isolation on Linux: read-only root, filtered network, hidden secrets |
| 228 providers | OpenAI / Anthropic / Gemini protocols, any OpenAI-compatible `base_url`, `/login` wizard |
| Plan mode | research-only planning with approval gate before any code is touched |
| Long-term memory | `MEMORY.md` (project + global), auto-loaded every session |
| Safety net | git snapshots, selective restore, session forks, approval-gated undo |
| Live TUI | command palette, streaming markdown, permission prompts, 3 themes |

## Install

Requires Python 3.10+.

```bash
pipx install neo-agnt        # recommended (isolated, neo lands on PATH)
# or
pip install neo-agnt
```

Update to the latest version:

```bash
pipx upgrade neo-agnt
# or
pip install --upgrade neo-agnt
```

If the upgrade still shows the old version (stale pip cache), reinstall
bypassing the cache:

```bash
pipx uninstall neo-agnt
pipx install --pip-args="--no-cache-dir --index-url https://pypi.org/simple" neo-agnt
```

Sandboxed `bash` needs bubblewrap on Linux:

```bash
sudo apt install bubblewrap socat
```

Set an API key — first match wins (env → `~/.config/neo/auth.json` → `neo.json`):

```bash
export ANTHROPIC_API_KEY="sk-..."
```

Ubuntu 24.04+ blocks system-wide `pip install` (PEP 668) — use `pipx` or a
virtualenv instead of `--break-system-packages`.

## Use

```bash
neo                                          # interactive TUI
neo -p "fix the failing test in test_loop"   # headless, print reply and exit
neo -p "..." --allow-all                     # headless, skip permission prompts
neo --resume                                 # continue the last session
neo init                                     # scaffold .neo/ (AGENTS.md, MEMORY.md, skills, commands, agents)
neo models                                   # list providers and models
neo config                                   # show resolved configuration
neo snapshot | neo restore | neo fork        # git snapshots and session forks
```

In the TUI: `/` opens the command palette, `@` completes file paths,
`Ctrl+C` cancels the running turn, typing mid-run queues your message for
the next turn. `/login` stores a provider key, `/logout` removes it,
`/model` switches models within the active provider, `/memory` shows what's
remembered.

| Keys | Action |
| --- | --- |
| `/` | command palette |
| `@` | file-path completion |
| `pageup` / `pagedown` | scroll transcript a page |
| `shift+up` / `shift+down` | scroll transcript a line |
| mouse wheel | scroll transcript |
| `Ctrl+C` | cancel the running turn |

The transcript stays pinned to new output only while you're already at the
bottom, so reading history never jumps.

## Configure

`~/.config/neo/neo.json` (global); `./neo.json` or `./.neo/neo.json`
(project overrides global):

```json
{
  "model": "anthropic/claude-sonnet-4-6",
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
  "mcp": { "servers": {
    "fs": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"] }
  } }
}
```

Permissions are `{tool: {pattern: allow|ask|deny}}`, last match wins. Every
command in a compound (`a && b`) is evaluated — one deny blocks the whole
thing. `neo config` prints the fully resolved configuration.

Custom OpenAI-compatible providers are easiest via `/login` → `custom`:
a wizard collects the provider id, display name, base URL, API key (literal
or `{env:VAR}`), and model ids with display names, then saves everything to
`~/.config/neo/neo.json`:

```json
{ "providers": { "my-proxy": {
    "title": "My Proxy",
    "base_url": "https://proxy.internal/v1",
    "protocol": "openai",
    "models": ["model-a", "model-b"],
    "model_names": {"model-a": "Model A"},
    "default_model": "model-a",
    "api_key_env": "PROXY_KEY"
} } }
```

`/model` then lists those models directly — including their display names.

## Sandbox

On Linux, `bash` runs in bubblewrap: namespaces, read-only root, filtered
egress. Every result is tagged `[sandbox] active (network=…)` so you always
know what isolation was in effect.

```json
{ "sandbox": {
    "mode": "auto",
    "network": "filtered",
    "allowedDomains": ["github.com", "*.pypi.org"],
    "denyRead": ["~/.ssh", "~/.aws"],
    "denyWrite": [".env"],
    "passEnv": ["PATH", "HOME", "LANG"]
} }
```

`mode`: `auto` (warn, run unsandboxed if bubblewrap is missing) ·
`strict` (fail loudly) · `off`.
`network`: `none` (offline) · `filtered` (domain-allowlist proxy) · `full`.
Environment is cleared except `passEnv`; secret-looking variables are
stripped.

## Plan mode

Non-trivial work is drafted to `.neo/plans/` first; edits stay locked to
the plan until you approve the `plan_exit` prompt. Nothing outside the plan
is touched before approval.

## Memory

neo remembers durable facts across sessions in plain markdown files,
loaded into every session's context automatically:

- `./MEMORY.md` / `./.neo/MEMORY.md` — project facts: conventions,
  decisions, gotchas
- `~/.config/neo/MEMORY.md` — facts about you that apply everywhere

Say "remember that I prefer…" and neo appends the fact itself — project
vs global, it decides. `/memory` shows what's stored. Secrets are never
written there, only a note that they exist and where.

## Safety net

```bash
neo snapshot               # snapshot the worktree (git tree-hash handle)
neo restore <id>           # restore files (post-snapshot files are deleted)
neo restore <id> --dry-run # preview what a restore would change
neo fork                   # fork the session (copy-on-write)
```

Snapshots are taken automatically before risky tool batches, and the `undo`
tool (approval-gated) rolls back the last mutation.

## Extend

**MCP** — servers in `neo.json`; their tools appear as `server_tool`, their
prompts become slash commands. One failing server never takes down the rest.

**Plugins** — `~/.neo/plugins/*.py` exposing `hooks`:

```python
def on_tool_after(event):
    if event.tool == "bash" and event.result.is_error:
        notify("bash failed")

hooks = {"tool.execute.after": on_tool_after}
```

Hooks: `tool.execute.before/after`, `permission.ask`, `chat.params`,
`session.end`. Plugins can also register their own tools.

**Commands** — `.neo/commands/*.md` with optional frontmatter; `$1`…`$n`
and `$ARGUMENTS` interpolate arguments.
**Tools** — `.neo/tools/*.py` loaded automatically.

## Internals

| Area | Notes |
| --- | --- |
| Loop | plan → act → verify; parallel tool calls, backoff retries, doom-loop guard, transcript repair, auto-compaction |
| Tools (18) | `read` `write` `edit` `apply_patch` `glob` `grep` `list_dir` `bash`(+pty) `webfetch` `websearch` `todo_write` `todo_read` `task` `question` `skill` `plan_enter` `plan_exit` `undo` |
| Providers | 228 in the catalog (OpenAI / Anthropic / Gemini protocols); any OpenAI-compatible `base_url` works |
| Code intel | LSP diagnostics (pyright, tsserver, gopls, rust-analyzer, eslint) + formatters after every edit |

```
src/neo/
  agent/        loop, permissions, sessions, plans, compaction
  tools/        18 built-in tools (files, shell+sandbox, web, todos, subagents…)
  providers/    streaming clients + 228-entry provider catalog (JSON)
  sandbox/      bubblewrap argv builder, detection, filtering proxy
  mcp/          clients (stdio, StreamableHTTP), tool/prompt discovery
  lsp/  format/ language servers + formatters wired into every edit
  vcs/          snapshots, selective restore, forks, worktrees
  plugins/      plugin loader and hook dispatch
  custom_tools/ .neo/tools/*.py loader
  agents/ plan/ agent roster, per-agent toolsets, plan-mode enforcement
  tui/          Textual interface: palette, dialogs, themes, session list
  auth.py       ~/.config/neo/auth.json key store (0600)
```

## Tests

```bash
python -m pytest          # 493 passed, 4 skipped
```

## License

MIT. See [LICENSE](LICENSE).
