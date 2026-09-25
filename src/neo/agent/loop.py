"""The autonomous agent loop.

Plan -> Act -> Verify, with parallel tool execution, doom-loop guard,
transcript repair, retry with backoff, auto-compaction and cost tracking.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from .. import events as E
from ..providers.retry import compute_delay
from .compact import estimate_tokens, flatten_for_summary, plan_compaction
from .prompts import SUMMARY_PROMPT
from .verify import run_verification

MAX_TOOL_OUTPUT = 60_000
DOOM_LOOP_THRESHOLD = 3
MAX_RETRIES = 5


def repair_history(messages: list[dict]) -> None:
    """Ensure every assistant tool_call has exactly one adjacent tool result."""
    seen_results = set()
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            seen_results.add(m["tool_call_id"])
    out: list[dict] = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            dangling = [tc for tc in m["tool_calls"]
                        if tc.get("id") not in seen_results]
            kept = [tc for tc in m["tool_calls"]
                    if tc.get("id") in seen_results]
            if kept != m["tool_calls"]:
                m = {**m, "tool_calls": kept}
            out.append(m)
            for tc in dangling:
                out.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                            "name": tc.get("name", ""),
                            "content": "Tool call interrupted before it could run.",
                            "is_error": True})
        else:
            out.append(m)
    messages[:] = out


class AgentHarness:
    """Runs the agent loop, yielding AgentEvents. Owns no UI."""

    def __init__(self, *, provider, model: str, tools: dict,
                 system: str, config, ctx,
                 max_steps: int = 40, small_model: str | None = None,
                 usage_prices: tuple[float, float] | None = None,
                 session_id: str = ""):
        self.provider = provider
        self.model = model
        self.tools = tools
        self.system = system
        self.config = config
        self.ctx = ctx
        self.max_steps = max_steps
        self.small_model = small_model or model
        self.usage_prices = usage_prices
        self.session_id = session_id
        self._cancel = asyncio.Event()
        self._steer_q: asyncio.Queue[str] = asyncio.Queue()
        self._inbox: list[str] = []  # queued while busy; drained via take_queued()
        self._last_sig: tuple | None = None
        self._doom_count = 0
        self._verify_streak = 0
        self._in_tokens = 0
        self._out_tokens = 0

    def cancel(self) -> None:
        self._cancel.set()

    def steer(self, text: str) -> None:
        self._steer_q.put_nowait(text)

    def queue(self, text: str) -> None:
        """Queue a message while the harness is busy.

        Unlike steer() (which injects at the next step boundary), queued
        messages wait for the current run() to finish. The caller drains
        them with take_queued() and starts a fresh run.
        """
        self._inbox.append(text)

    def take_queued(self) -> list[str]:
        """Return and clear messages queued while busy."""
        queued, self._inbox = self._inbox, []
        return queued

    # -- public ---------------------------------------------------------

    async def run(self, messages: list[dict]) -> AsyncIterator[E.AgentEvent]:
        repair_history(messages)
        yield E.RunStart(session_id=self.session_id, model=self.model)
        step = 0
        try:
            while True:
                self._drain_steer(messages)
                if self._cancel.is_set():
                    yield E.RunEnd(reason="aborted")
                    return
                if step >= self.max_steps:
                    yield E.Notice("Step budget exhausted.", level="warn")
                    yield E.RunEnd(reason="max_steps")
                    return

                last_step = step >= self.max_steps - 1
                yield E.TurnStart(index=step)
                sink: list[E.AgentEvent] = []
                finish, text, tool_calls = await self._provider_turn(
                    messages, step, disable_tools=last_step, sink=sink)
                for ev in sink:
                    yield ev
                yield E.TurnEnd(index=step, finish=finish)

                if self._cancel.is_set():
                    yield E.RunEnd(reason="aborted")
                    return

                if not tool_calls:
                    if text:
                        yield E.TextEnd(text=text)
                    yield E.RunEnd(reason="done")
                    return

                tsink: list[E.AgentEvent] = []
                edited = await self._execute_tools(messages, tool_calls,
                                                   sink=tsink)
                for ev in tsink:
                    yield ev

                if self._doom_guard(tool_calls):
                    yield E.Notice(
                        "Same tool call repeated 3 times — stopping to avoid a loop. "
                        "Tell me how to proceed.", level="warn")
                    yield E.RunEnd(reason="doom_loop")
                    return

                if edited:
                    async for ev in self._maybe_verify(messages):
                        yield ev

                async for ev in self._maybe_compact(messages):
                    yield ev

                step += 1
        except asyncio.CancelledError:
            yield E.RunEnd(reason="aborted")
        except Exception as exc:  # never let the loop die silently
            yield E.Notice(f"Agent error: {exc}", level="error")
            yield E.RunEnd(reason="error")

    # -- internals ------------------------------------------------------

    def _drain_steer(self, messages: list[dict]) -> None:
        while not self._steer_q.empty():
            try:
                text = self._steer_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            messages.append({"role": "user", "content": text})

    def _tool_schemas(self) -> list[dict]:
        return [{"name": t.name, "description": t.description,
                 "parameters": t.parameters} for t in self.tools.values()]

    async def _provider_turn(self, messages: list[dict], step: int,
                             disable_tools: bool,
                             sink: list) -> tuple[str, str, list]:
        """Stream one assistant turn. Appends agent events to sink.

        Returns (finish, text, tool_calls).
        """
        system = self.system
        schemas: list[dict] = []
        if disable_tools:
            system += ("\n\n[SYSTEM REMINDER: your step budget is exhausted. "
                       "Do NOT call any tools. Answer in text only.]")
        else:
            schemas = self._tool_schemas()

        plugins = getattr(self.ctx, "plugins", None)
        if plugins is not None:
            params = await plugins.trigger("chat.params", {
                "system": system, "tools": schemas, "model": self.model})
            system = params.get("system", system)
            schemas = params.get("tools", schemas)

        attempt = 0
        text_parts: list[str] = []
        reason_parts: list[str] = []
        calls: dict[str, dict] = {}
        finish = "stop"
        text_started = reason_started = False

        while True:
            if self._cancel.is_set():
                return "aborted", "".join(text_parts), []
            stream_error = None
            try:
                async for pev in self.provider.stream(
                        model=self.model, system=system, messages=messages,
                        tools=schemas, max_tokens=8192, signal=self._cancel):
                    k = pev.kind
                    if k == "text_delta":
                        if not text_started:
                            text_started = True
                        text_parts.append(pev.text)
                    elif k == "reason_delta":
                        reason_parts.append(pev.text)
                    elif k == "tool_arg_delta":
                        c = calls.setdefault(pev.call_id,
                                             {"name": pev.name, "args": ""})
                        c["args"] += pev.args_text
                    elif k == "tool_call_ready":
                        calls[pev.call_id] = {"name": pev.name,
                                             "args": json.dumps(pev.arguments)}
                    elif k == "usage_tick":
                        self._in_tokens += pev.input_tokens
                        self._out_tokens += pev.output_tokens
                    elif k == "stream_end":
                        finish = pev.finish
                    elif k == "stream_error":
                        stream_error = pev
                        break
            except Exception as exc:
                # Auth/config errors are permanent: never retry a missing key.
                from ..providers.base import ProviderAuthError, ProviderConfigError
                retryable = not isinstance(exc, (ProviderAuthError,
                                                ProviderConfigError))
                stream_error = type("SE", (), {"message": str(exc),
                                              "retryable": retryable})()
            if stream_error is None:
                break
            if not stream_error.retryable or attempt >= MAX_RETRIES:
                raise RuntimeError(f"Provider error: {stream_error.message}")
            delay = compute_delay(attempt)
            attempt += 1
            await asyncio.sleep(delay)

        # Re-emit text/reasoning as agent events
        full_text = "".join(text_parts)
        full_reason = "".join(reason_parts)
        if text_started:
            sink.append(E.TextStart())
            sink.append(E.TextDelta(text=full_text))
        if reason_parts:
            sink.append(E.ReasonStart())
            sink.append(E.ReasonDelta(text=full_reason))
            sink.append(E.ReasonEnd(text=full_reason))

        tool_calls = []
        for cid, c in calls.items():
            try:
                args = json.loads(c["args"]) if c["args"] else {}
                if not isinstance(args, dict):
                    args = {"_raw": c["args"]}
            except json.JSONDecodeError:
                args = {"_raw": c["args"]}
            tool_calls.append({"id": cid, "name": c["name"], "arguments": args})

        messages.append({"role": "assistant",
                         "content": full_text or None,
                         "tool_calls": tool_calls})
        sink.append(E.Usage(input_tokens=self._in_tokens,
                            output_tokens=self._out_tokens,
                            cost_usd=self._cost()))
        return finish, full_text, tool_calls

    def _cost(self) -> float | None:
        if not self.usage_prices:
            return None
        pin, pout = self.usage_prices
        return self._in_tokens / 1e6 * pin + self._out_tokens / 1e6 * pout

    async def _execute_tools(self, messages: list[dict],
                             tool_calls: list[dict],
                             sink: list) -> bool:
        """Permission checks (sequential) then concurrent execution.

        Appends Tool events to sink. Returns True if any write/edit tool
        succeeded (triggers verification).
        """
        approved: list[tuple[dict, Any]] = []
        denied: list[tuple[dict, str]] = []

        for tc in tool_calls:
            tool = self.tools.get(tc["name"])
            if tool is None:
                denied.append((tc, f"Unknown tool '{tc['name']}'."))
                continue
            target = self._target_for(tool.name, tc["arguments"])
            detail = self._detail_for(tool.name, tc["arguments"])
            ok, reason = await self._permission(tool.name, target, detail)
            if ok:
                approved.append((tc, tool))
            else:
                denied.append((tc, reason))

        # Snapshot the worktree before any batch that can mutate it.
        # auto_snapshot() never raises; it returns None outside git repos.
        _MUTATING = {"write", "edit", "apply_patch", "bash"}
        if any(tc["name"] in _MUTATING for tc, _ in approved):
            from pathlib import Path
            from ..vcs import auto_snapshot
            auto_snapshot(Path(self.ctx.workdir), reason="pre-tool-batch")

        results: dict[str, tuple[Any, Any, int]] = {}

        async def _one(tc, tool):
            start = time.monotonic()
            try:
                plugins = getattr(self.ctx, "plugins", None)
                args = dict(tc["arguments"])
                if plugins is not None:
                    payload = await plugins.trigger(
                        "tool.execute.before",
                        {"tool": tool.name, "args": args})
                    new_args = payload.get("args", args)
                    args = new_args if isinstance(new_args, dict) else args
                if tool.name in ("write", "edit"):
                    path = str(args.get("path", ""))
                    lock = self.ctx.locks.setdefault(path, asyncio.Lock())
                    async with lock:
                        res = await tool.run(args, self.ctx)
                else:
                    res = await tool.run(args, self.ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from ..tools.base import ToolResult
                res = ToolResult(output=f"{type(exc).__name__}: {exc}",
                                 is_error=True)
            ms = int((time.monotonic() - start) * 1000)
            if plugins is not None:
                payload = await plugins.trigger(
                    "tool.execute.after",
                    {"tool": tool.name, "args": args, "result": res})
                res = payload.get("result", res)
                if payload.get("output") not in (None, res.output):
                    res.output = payload["output"]
            return tc, res, ms

        for tc, tool in approved:
            title = self._title_for(tool.name, tc["arguments"])
            sink.append(E.ToolStart(call_id=tc["id"], tool=tool.name,
                                    args=tc["arguments"], title=title))
        for tc, reason in denied:
            sink.append(E.ToolStart(call_id=tc["id"], tool=tc["name"],
                                    args=tc["arguments"], title="denied"))
            sink.append(E.ToolEnd(call_id=tc["id"], tool=tc["name"], ok=False,
                                  output=reason, ms=0))

        if approved:
            outs = await asyncio.gather(
                *(_one(tc, tool) for tc, tool in approved))
            for tc, res, ms in outs:
                output = res.output or ""
                if len(output) > MAX_TOOL_OUTPUT:
                    output = (output[:MAX_TOOL_OUTPUT] +
                              f"\n…[truncated {len(output) - MAX_TOOL_OUTPUT} chars]")
                sink.append(E.ToolEnd(call_id=tc["id"], tool=tc["name"],
                                      ok=not res.is_error, output=output, ms=ms))
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "name": tc["name"], "content": output,
                                 "is_error": res.is_error})
                results[tc["id"]] = (tc, res, ms)

        for tc, reason in denied:
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "name": tc["name"], "content": reason,
                             "is_error": True})

        edited = any(tc["name"] in ("write", "edit", "apply_patch") and not res.is_error
                     for tc, res, _ in results.values())
        if edited:
            self._verify_streak = 0
        return edited

    async def _permission(self, tool_name: str, target: str,
                          detail: str) -> tuple[bool, str]:
        # Plan mode is authoritative while active: it allows research tools
        # without approval popups and hard-denies code writes outside
        # .neo/plans/. A deny here is final.
        from ..plan.tools import plan_mode_active, plan_mode_allows
        if plan_mode_active(self.ctx):
            return plan_mode_allows(tool_name, target, self.ctx.workdir)
        policy = self.ctx.permissions
        key = policy.key_for_tool(tool_name)
        # Bash: probe shlex-derived signatures from most to least specific.
        # Per signature, the last matching rule wins (same as check());
        # the first signature with any matching rule decides.
        if tool_name == "bash":
            from ..scan import scan
            from .permissions import wildcard_match
            for sig in scan(target):
                sig_decision = None
                for rule_key, pattern, action in policy._iter():
                    if rule_key != "*" and rule_key != key:
                        continue
                    if wildcard_match(pattern, sig):
                        sig_decision = action
                if sig_decision == "allow":
                    return True, ""
                if sig_decision == "deny":
                    return False, (
                        f"Permission denied: bash '{sig}' is denied by policy.")
                if sig_decision == "ask":
                    break  # explicit ask rule: fall through to the gate below
        decision = policy.check(key, target)
        plugins = getattr(self.ctx, "plugins", None)
        if plugins is not None:
            payload = await plugins.trigger("permission.ask", {
                "tool": tool_name, "target": target,
                "detail": detail, "decision": decision,
            })
            hook_decision = payload.get("decision", decision)
            if hook_decision in ("allow", "deny", "ask"):
                decision = hook_decision
        if decision == "allow":
            return True, ""
        if decision == "deny":
            return False, (f"Permission denied: {tool_name} on "
                           f"'{target}' is denied by policy.")
        answer = await self.ctx.gate(tool_name, target, detail)
        if answer == "always":
            pattern = self._always_pattern(tool_name, target)
            policy.allow_always(key, pattern)
            return True, ""
        if answer == "once":
            return True, ""
        return False, (f"Rejected by user: {tool_name} on '{target}' was not "
                       "approved. Adjust your plan or ask what to do instead.")

    @staticmethod
    def _always_pattern(tool_name: str, target: str) -> str:
        if tool_name == "bash":
            from ..scan import first_word
            first = first_word(target) or target
            return f"{first} *"
        return target

    @staticmethod
    def _target_for(tool_name: str, args: dict) -> str:
        if tool_name in ("read", "write", "edit", "list_dir"):
            return str(args.get("path", ""))
        if tool_name in ("glob", "grep"):
            return str(args.get("pattern", args.get("path", "")))
        if tool_name == "bash":
            return str(args.get("command", ""))
        if tool_name == "apply_patch":
            # stable target for the permission pattern: files touched
            from ..patch import parse_patch
            try:
                ops = parse_patch(str(args.get("patch", "")))
            except ValueError:
                return "apply_patch"
            paths = sorted({op.move_to or op.path for op in ops})
            return " ".join(paths) if paths else "apply_patch"
        if tool_name == "webfetch":
            return str(args.get("url", ""))
        if tool_name == "websearch":
            return str(args.get("query", ""))
        if tool_name == "task":
            return str(args.get("agent", "general"))
        return tool_name

    @staticmethod
    def _detail_for(tool_name: str, args: dict) -> str:
        if tool_name == "edit":
            old = str(args.get("old", ""))
            new = str(args.get("new", ""))
            return f"--- {args.get('path')}\n-{old[:600]}\n+{new[:600]}"
        if tool_name == "write":
            return f"Write {args.get('path')} ({len(str(args.get('content', '')))} chars)"
        if tool_name == "bash":
            return str(args.get("command", ""))
        if tool_name == "apply_patch":
            from ..patch import parse_patch
            try:
                ops = parse_patch(str(args.get("patch", "")))
                summary = ", ".join(f"{op.op} {op.move_to or op.path}" for op in ops)
            except ValueError as exc:
                summary = f"unparseable: {exc}"
            return f"Apply patch ({summary})"[:800]
        return json.dumps(args)[:800]

    @staticmethod
    def _title_for(tool_name: str, args: dict) -> str:
        short = {"read": "path", "write": "path", "edit": "path",
                 "list_dir": "path", "bash": "command", "webfetch": "url",
                 "websearch": "query", "grep": "pattern", "glob": "pattern",
                 "task": "description"}.get(tool_name)
        if tool_name == "apply_patch":
            from ..patch import parse_patch
            try:
                ops = parse_patch(str(args.get("patch", "")))
                files = ", ".join(f"{op.op} {op.move_to or op.path}" for op in ops)
                val = f"apply_patch: {files}"
            except ValueError:
                val = "apply_patch"
            return val if len(val) <= 80 else val[:77] + "…"
        if short and args.get(short):
            val = str(args[short])
            return val if len(val) <= 80 else val[:77] + "…"
        return tool_name

    def _doom_guard(self, tool_calls: list[dict]) -> bool:
        sig = tuple(sorted(
            (tc["name"], json.dumps(tc["arguments"], sort_keys=True,
                                    default=str)) for tc in tool_calls))
        if sig == self._last_sig:
            self._doom_count += 1
        else:
            self._doom_count = 1
            self._last_sig = sig
        return self._doom_count >= DOOM_LOOP_THRESHOLD

    async def _maybe_verify(self, messages: list[dict]):
        commands = getattr(self.config, "verify_commands", []) or []
        if not commands or self._verify_streak >= 2:
            return
        self._verify_streak += 1
        workdir = getattr(self.ctx, "workdir", ".")
        results = run_verification(commands, workdir)
        for cmd, ok, out in results:
            yield E.VerifyStart(command=cmd)
            yield E.VerifyEnd(command=cmd, ok=ok, output=out)
        lines = [f"$ {cmd}\n{'OK' if ok else 'FAILED'}\n{out}"
                 for cmd, ok, out in results]
        tail = "" if all(ok for _, ok, _ in results) else "\nFix any failures above."
        messages.append({"role": "user",
                         "content": "[Verification]\n" + "\n".join(lines) + tail})

    async def _maybe_compact(self, messages: list[dict]):
        plan = plan_compaction(messages, self.config.context_window)
        if plan is None:
            return
        _, keep_from = plan
        yield E.CompactStart()
        head, tail = messages[:keep_from], messages[keep_from:]
        summary_text = await self._summarize(head)
        messages[:] = ([{"role": "user",
                          "content": "[Summary of earlier conversation]\n" +
                          summary_text}] + tail)
        yield E.CompactEnd(kept=len(messages))

    async def _summarize(self, messages: list[dict]) -> str:
        content = flatten_for_summary(messages)
        parts: list[str] = []
        try:
            async for pev in self.provider.stream(
                    model=self.small_model, system=SUMMARY_PROMPT,
                    messages=[{"role": "user", "content": content}],
                    tools=[], max_tokens=2000, signal=self._cancel):
                if pev.kind == "text_delta":
                    parts.append(pev.text)
                elif pev.kind == "stream_error":
                    break
        except Exception:
            pass
        text = "".join(parts).strip()
        return text or "(summary unavailable — earlier context dropped)"
