# neo — Design Contract

`neo` is a Python terminal coding agent (Textual TUI). Architecture is
informed by opencode (tools, providers, permissions, loop) and tau
(event-driven core, TUI patterns) — all code is written fresh, with
renamed identifiers and original wording. Nothing is copied verbatim.

## Layout

```
src/neo/
  __init__.py        __version__ = "0.1.0"
  cli.py             argparse entry: main()
  config.py          NeoConfig, discover_config(), load/save
  events.py          agent-level event dataclasses (flat, kind discriminator)
  providers/
    __init__.py      get_provider(provider_id, config) -> Provider; list_providers()
    base.py          ProviderSpec, ModelInfo, Provider (ABC), message shapes, ProviderEvent union
    openai_compat.py OpenAICompatProvider  (POST {base}/chat/completions, SSE)
    anthropic.py     AnthropicProvider      (POST {base}/v1/messages, SSE)
    google.py        GoogleProvider         (POST {base}/models/{m}:streamGenerateContent?key=, SSE)
    catalog.py       PROVIDERS: list[ProviderSpec]  (~50 entries, no "opencode")
    retry.py         compute_delay(attempt), is_retryable(status_code, body_text)
  tools/
    __init__.py      build_toolset(ctx) -> dict[str, Tool]
    base.py          ToolResult, ToolContext, Tool (ABC), TOOL_REGISTRY
    files.py         ReadTool, WriteTool, EditTool, GlobTool, GrepTool, ListDirTool
    shell.py         BashTool
    web.py           WebFetchTool, WebSearchTool
    todos.py         TodoWriteTool, TodoReadTool
    delegate.py      TaskTool  (subagents; imports neo.agent.loop lazily)
    inquire.py       InquiryTool (question)
    skill.py         SkillTool
  agent/
    __init__.py
    loop.py          AgentHarness: run(), cancel(), steer(); repair_history(); token estimate
    permissions.py   PermissionPolicy.check(tool, target) -> "allow"|"ask"|"deny"; wildcard_match
    session.py       SessionStore: JSONL append/load, new/list/get
    compact.py       maybe_compact(messages, ...) -> list[message] | None; summarize()
    prompts.py       build_system_prompt(...) -> str; load_project_notes()
    discovery.py     find_skills(), find_commands(), find_agents() -> dicts of parsed .md
    verify.py        run_verification(commands, workdir) -> list[(cmd, ok, output)]
  tui/
    __init__.py
    app.py           NeoApp(Textual App), run_tui(...)
    widgets.py       transcript widgets, tool rows, streaming message
    palette.py       build_completions(), fuzzy_score(), Completion dataclass
    dialogs.py       PermissionDialog, pickers (model/theme/session)
    themes/          neo-dark.json, neo-light.json  (tokens: colors + roles)
  templates/         AGENTS.md, skills/*, commands/*, agents/*  (for `neo init`)
tests/
  test_permissions.py  test_edit.py  test_events.py  test_catalog.py
  test_providers.py    test_tools.py  test_loop.py
```

## events.py — agent-level events

Flat frozen dataclasses, each with `kind: ClassVar[str]`. The TUI renders
from these; the loop yields them.

- `RunStart(session_id, model)` / `RunEnd(reason)`           # reason: done|aborted|error|max_steps
- `TurnStart(index)` / `TurnEnd(index, finish)`
- `TextStart()` / `TextDelta(text)` / `TextEnd(text)`
- `ReasonStart()` / `ReasonDelta(text)` / `ReasonEnd(text)`
- `ToolStart(call_id, tool, args: dict, title)` / `ToolProgress(call_id, text)` / `ToolEnd(call_id, tool, ok, output, ms)`
- `Usage(input_tokens, output_tokens, cost_usd: float|None)`
- `Notice(text, level)`  # level: info|warn|error
- `CompactStart()` / `CompactEnd(kept: int)`
- `VerifyStart(command)` / `VerifyEnd(command, ok, output)`
- `TodoUpdate(todos: list[dict])`

## providers/base.py

```python
PROTOCOL_OPENAI = "openai"       # chat/completions SSE
PROTOCOL_ANTHROPIC = "anthropic" # /v1/messages SSE
PROTOCOL_GOOGLE = "google"       # :streamGenerateContent SSE

@dataclass(frozen=True)
class ProviderSpec:
    id: str            # e.g. "anthropic"
    title: str         # e.g. "Anthropic"
    protocol: str      # one of the PROTOCOL_* constants
    base_url: str
    env_vars: tuple[str, ...]   # tried in order
    default_model: str
    extra_headers: dict = {}    # e.g. openrouter referer headers

@dataclass(frozen=True)
class ModelInfo:
    id: str; context_window: int = 200_000; max_output: int = 8192
    cost_in: float = 0.0; cost_out: float = 0.0   # USD per 1M tokens

# Provider-neutral message shapes (plain dicts):
# {"role":"user","content": str | [{"type":"text","text":...}]}
# {"role":"assistant","content": str|None, "tool_calls":[{"id","name","arguments":dict}]}
# {"role":"tool","tool_call_id":str,"name":str,"content":str,"is_error":bool}

# ProviderEvent union (frozen dataclasses, kind ClassVar):
#   TextDelta(text) | ReasonDelta(text)
#   | ToolArgDelta(call_id, name, args_text)   # streaming args fragment
#   | ToolCallReady(call_id, name, arguments: dict)
#   | UsageTick(input_tokens, output_tokens)   # cumulative for the response
#   | StreamEnd(finish: str)                   # stop|length|tool_calls|error
#   | StreamError(message, retryable: bool)

class Provider(abc.ABC):
    def __init__(self, spec: ProviderSpec, api_key: str|None,
                 base_url: str|None = None, timeout: float = 120.0,
                 extra_headers: dict|None = None): ...
    @abc.abstractmethod
    async def stream(self, *, model: str, system: str, messages: list[dict],
                     tools: list[dict], max_tokens: int,
                     signal: asyncio.Event|None = None) -> AsyncIterator[ProviderEvent]: ...
    # tools: [{"name","description","parameters": <jsonschema dict>}]

async def aclose_all()  # not needed; each provider owns an httpx.AsyncClient, expose aclose()
```

`providers/__init__.py`:
- `list_providers() -> list[ProviderSpec]`
- `resolve_provider(provider_id: str, cfg) -> Provider` — cfg overrides
  (cfg.providers[pid] = {"api_key","base_url"}); api_key = override else
  first set env var else None (raise ProviderAuthError on stream if None).
- Unknown provider_id -> synthesize ProviderSpec(protocol=openai,
  base_url=cfg.providers[pid]["base_url"] or raise, env_vars=()).

`providers/retry.py`:
- `is_retryable(status: int|None, text: str) -> bool`
- `compute_delay(attempt: int) -> float`  # 2s * 2**attempt * (1+jitter*.25), cap 30

## tools/base.py

```python
@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    title: str = ""          # short human label, e.g. "src/neo/cli.py:12-40"
    details: dict = field(default_factory=dict)

@dataclass
class ToolContext:
    workdir: Path
    config: "NeoConfig"                 # typing.TYPE_CHECKING import
    permissions: "PermissionPolicy"
    gate: Callable[[str,str,str], Awaitable[str]]
        # async (tool_name, target, detail) -> "once"|"always"|"reject"
    emit: Callable[[object], None]      # push an AgentEvent (sync callback)
    todos: list[dict]                   # shared session todo list (mutated by todos.py)
    ui: Any = None                      # optional; must provide async ask(questions)->dict for InquiryTool
    locks: dict = field(default_factory=dict)  # path -> asyncio.Lock

class Tool(abc.ABC):
    name: ClassVar[str]; description: ClassVar[str]
    parameters: ClassVar[dict]          # JSON Schema for the LLM
    needs_approval: ClassVar[bool] = True   # False for pure readers
    async def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...
```

`tools/__init__.py`: `TOOL_CLASSES: list[type[Tool]]`,
`build_toolset(ctx, include: set[str]|None = None) -> dict[str, Tool]`.

Tool names (LLM-facing): `read write edit glob grep list_dir bash
webfetch websearch todo_write todo_read task inquire skill`.

Permission mapping (`agent/permissions.py` maps tool -> permission key):
read/glob/grep/list_dir -> "read"; write/edit -> "edit";
bash -> "bash"; webfetch/websearch -> "web"; task -> "task";
todo_write/todo_read/inquire/skill -> "session" (ask never; default allow).
Target string per tool for pattern matching:
- read/list_dir/glob/grep: path or pattern; write/edit: file path;
- bash: full command text; webfetch: url; websearch: query; task: subagent type.

`PermissionPolicy` (`agent/permissions.py`):
```python
class PermissionPolicy:
    def __init__(self, rules: dict): ...   # {"bash": {"git *": "allow", "*": "ask"}, "edit": {"*": "allow"}, ...}
    def check(self, key: str, target: str) -> str  # "allow"|"ask"|"deny"; last matching rule wins
def wildcard_match(pattern: str, value: str) -> bool  # supports * ? ** ; trailing " *" also matches bare prefix
```
Defaults: `{"*": "allow", "read": {"*": "allow", "*.env": "ask"}, "edit": {"*": "ask"}, "bash": {"*": "ask"}, "web": {"*": "allow"}, "task": {"*": "ask"}, "session": {"*": "allow"}}`.
"always" approvals: session-scoped in-memory list appended as allow rules.

The loop's permission flow per tool call:
1. `decision = policy.check(key, target)`; "deny" -> ToolResult error (no exec).
2. "ask" -> `answer = await ctx.gate(tool, target, detail)`; "reject" -> error result; "always" -> append allow rule then proceed; "once" -> proceed.

## agent/loop.py

```python
class AgentHarness:
    def __init__(self, *, provider: Provider, model: str, tools: dict[str,Tool],
                 system: str, config, ctx: ToolContext,
                 max_steps: int = 40, small_model: str|None = None): ...
    async def run(self, messages: list[dict]) -> AsyncIterator[AgentEvent]:
        # yields events; mutates messages in place (appends assistant + tool messages)
    def cancel(self): ...
    def steer(self, text: str): ...   # queue a user message injected next turn
```
Loop behavior:
- `repair_history(messages)`: drop assistant tool_calls without a matching
  tool result; synthesize `{"role":"tool",...,"is_error":True,
  "content":"Tool call interrupted"}` for dangling calls.
- Per step: stream provider events -> re-emit as agent events, accumulate
  text/reasoning/tool calls (ToolArgDelta fragments concatenated, JSON-parsed
  at end; on parse failure pass raw string as {"_raw": text}).
- If tool calls: for each, permission flow (above), then run. Independent
  calls run concurrently via asyncio.gather; write/edit calls take
  per-path locks from ctx.locks. Emit ToolStart/Progress/End. Append one
  `{"role":"tool",...}` message per call. Truncate outputs > 60_000 chars
  (note truncation).
- No tool calls -> TurnEnd, RunEnd("done").
- Doom guard: same (tool, normalized args) 3 turns in a row ->
  Notice(warn) + RunEnd("doom_loop") (frontend may steer).
- max_steps: on last step append system reminder "tools disabled; answer in
  text only" and force a final text-only request.
- After a turn containing successful write/edit: if config.verify_commands
  non-empty -> run at most 2 rounds: VerifyStart/End events; append results
  as a user message "Verification (cmd): <output or ok>"; model fixes issues.
- Token estimate: `estimate_tokens(messages, tools)` ~ chars/4 + 4/msg +
  16/tool. If estimate > config.context_window*0.85 -> CompactStart,
  summarize via small_model (or same model) with SUMMARY_PROMPT, keep tail
  ~15% messages verbatim, CompactEnd.
- Usage: accumulate UsageTick -> emit Usage(input, output, cost) using
  ModelInfo costs from catalog (0.0 when unknown).
- Retry: on StreamError(retryable) -> retry.compute_delay up to 5 attempts,
  honoring cancel.
- steer queue: pending user texts injected as user messages between turns.
- Cancellation: signal asyncio.Event; abort provider stream; synthesize
  interrupted tool results; RunEnd("aborted").

`agent/session.py`:
```python
class SessionStore:
    def __init__(self, root: Path = ~/.neo/sessions)
    def new(self, title="") -> str            # id: ses_<ts>
    def append(self, sid, record: dict)       # JSONL line
    def load(self, sid) -> list[dict]
    def list(self) -> list[dict]              # id, title, updated_at
    def set_title(self, sid, title)
```
Record types: {"t":"user","text"}, {"t":"assistant","text","tool_calls":[...]},
{"t":"tool_result","call_id","tool","output","is_error"}, {"t":"usage",...},
{"t":"compact","summary"}, {"t":"meta","model":...}.

`agent/prompts.py`:
- `NEO_SYSTEM_BASE: str` (original neo identity + tool policy; ~40 lines)
- `build_system_prompt(*, base, project_notes: list[(path,str)], skills_index: str, tools: dict, extra: str) -> str`
- `load_project_notes(workdir) -> list[(path, content)]`: walk up from
  workdir to git root (or home): first AGENTS.md wins per dir? Use: collect
  `./AGENTS.md`, `./.neo/AGENTS.md`, `~/.config/neo/AGENTS.md` (dedupe).
- `SUMMARY_PROMPT: str` for compaction.

`agent/discovery.py`:
- `find_skills(workdir) -> dict[name, SkillInfo(path, description, content)]`:
  scan `.neo/skills/*/SKILL.md`, `~/.config/neo/skills/*/SKILL.md`.
  SkillInfo has frontmatter parsed (name, description).
- `find_commands(workdir) -> dict[name, CommandInfo(path, description, template)]`:
  `.neo/commands/*.md`, `~/.config/neo/commands/*.md`; template supports
  `$1..$n`, `$ARGUMENTS`.
- `find_agents(workdir) -> dict[name, AgentInfo(path, description, prompt, model, permission)]`:
  `.neo/agents/*.md`, `~/.config/neo/agents/*.md`; frontmatter: description,
  model, mode.

`agent/verify.py`:
- `run_verification(commands: list[str], workdir: Path, timeout=120) -> list[tuple[str,bool,str]]`

## config.py

```python
@dataclass
class NeoConfig:
    model: str = "anthropic/claude-sonnet-4-6"   # "provider/model"
    small_model: str = "anthropic/claude-haiku-4-5"
    max_steps: int = 40
    context_window: int = 200_000
    theme: str = "neo-dark"
    thinking: str = "medium"          # off|low|medium|high (provider-mapped)
    permissions: dict = default_factory(default rules above)
    providers: dict = {}              # pid -> {"api_key","base_url"}
    verify_commands: list[str] = ["ruff check ."]  # hmm; keep [] default? 
    keybindings: dict = {}
    disabled_tools: list[str] = []
```
Keep `verify_commands` default `[]` (user opts in) — safer.
Discovery: `discover_config(workdir) -> NeoConfig`: defaults <- 
`~/.config/neo/neo.json` <- walk-up `neo.json` / `.neo/neo.json`.
`split_model("anthropic/claude-x") -> ("anthropic","claude-x")`.

## tools detail

- ReadTool(args: path, offset=1, limit=2000): text with line numbers;
  directories rejected (use list_dir). Binary -> error. Images -> note
  "binary omitted".
- ListDirTool(args: path): entries with trailing / for dirs, sorted.
- GlobTool(args: pattern, path="."): recursive glob, cap 200 results.
- GrepTool(args: pattern, path=".", include="*.py" optional, literal=false):
  use `rg` if available else python fallback; cap 100 matches "file:line:text".
- WriteTool(args: path, content): create dirs, write; title=path.
- EditTool(args: path, old, new, replace_all=false): strategies in order:
  exact; line-trimmed (strip each line); whitespace-normalized (collapse);
  indentation-flexible (dedent both); escape-normalized (\\n etc.).
  Guards: old==new -> error; empty old -> error; non-replace_all requires
  exactly one match; fuzzy match must not be >3x len(old) ("re-read file").
  After write, run `config.format_on_save`? skip — verify covers it.
- BashTool(args: command, timeout=120000ms, workdir=None): subprocess,
  merged stdout+stderr, cap 60k chars, returns exit code in details;
  stdin=/dev/null; kill process group on timeout.
- WebFetchTool(args: url, format="markdown"|"text"): httpx GET (15s),
  html->text via regex strip (no bs4 dep); cap 30k chars.
- WebSearchTool(args: query, count=5): DuckDuckGo html endpoint
  (https://html.duckduckgo.com/html/?q=) parsed with regex; no API key.
  Fallback: error "no search backend".
- TodoWriteTool(args: todos=[{content,status,priority}]): validates statuses
  (pending|in_progress|completed|cancelled), exactly one in_progress
  (auto-demote others to pending); writes ctx.todos in place; emits TodoUpdate.
- TodoReadTool: returns ctx.todos.
- TaskTool(args: description, prompt, agent="general", background=false):
  lazily imports AgentHarness; child run with restricted toolset
  (general: all minus task/todo_write; explore: read,glob,grep,list_dir,webfetch only);
  depth guard via ctx (ctx.depth, max 2); returns child summary text.
  background=true: asyncio.create_task, returns immediately with handle id;
  TaskTool(args: ..., resume=handle_id) polls. Keep simple: background runs
  and stores result in ctx.background dict; resume returns result or "running".
- InquiryTool(args: questions=[{question, header, options:[{label,description}], multiple}]):
  requires ctx.ui with `async ask(questions)->dict`; no ui -> error result
  "no interactive UI". Emits Notice.
- SkillTool(args: name): loads skill content from ctx.skills dict
  (populated by app from discovery); returns content wrapped
  "<skill name=...>\n...\n</skill>"; unknown -> error listing available.

All tools: catch exceptions -> ToolResult(is_error=True).

## cli.py

argparse:
- `neo` -> TUI
- `neo -p "prompt"` / `neo --print "prompt"` -> headless: run once, print events as text
- `neo init [--global]` -> scaffold .neo/ (AGENTS.md, skills/, commands/, agents/) from templates/
- `neo models [provider]` -> list providers + default models
- `neo config` -> print resolved config path + JSON
- `neo --version`

Headless run: build provider/tools/harness, iterate events, render text to
stdout (tool calls as dim lines). Gate: allow per policy without asking
(default allow in print mode? Use policy: ask->allow with Notice "auto-allowed
(no TTY)"... safer: ask->deny in print mode unless --allow-all flag).
Add `--allow-all` flag.

## tui/ contract (built after core)

- `NeoApp`: compose = sidebar | transcript + prompt row + status line.
  Input: TextArea-like Input widget; `/` opens palette popup (palette.py).
- palette.py: `fuzzy_score(query, target) -> float|None` (subsequence bonus:
  start-of-word, consecutive, case); `build_completions(text, cursor, ctx)
  -> list[Completion(kind, name, description, category, apply_text)]`;
  kinds: command, agent, model, theme, skill, file(@), shell(!).
- dialogs.py: `PermissionDialog(request) -> "once"|"always"|"reject"`;
  pickers via Textual ModalScreen.
- themes/*.json: {"name":..., "colors": {token: "#hex", ...}, "roles": {...}}.
  Tokens (superset of tau's, renamed): bg, bg_panel, bg_input, fg, fg_dim,
  border, border_focus, accent, accent_dim, success, warning, error, info,
  user_border, assistant_border, tool_border, diff_add, diff_del,
  code_bg, link, heading, completion_sel ...

The TUI agent wires ctx.gate to a modal and ctx.ui.ask to a dialog.

## Naming rules (avoid clone look)

- Config file `neo.json` (not opencode.json); dirs `.neo/`, `~/.config/neo/`.
- Tool names: keep functional names (read/write/edit/...) — these are generic.
- No copied prompt text; write original system prompt.
- No copied theme hex values; craft original palettes.
