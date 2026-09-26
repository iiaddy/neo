"""Plan-mode tools and enforcement for neo.

``plan_enter`` / ``plan_exit`` are the two ends of plan mode. While plan
mode is active, :func:`plan_mode_allows` decides which tool calls the loop
may let through.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..tools.base import Tool, ToolContext, ToolResult

PLANS_DIRNAME = ".neo/plans"

# Tools that are always fine while plan mode is active: pure research plus
# the question and plan_exit tools themselves.
_PLAN_READ_TOOLS = {
    "read",
    "list_dir",
    "glob",
    "grep",
    "webfetch",
    "websearch",
    "question",
    "plan_exit",
}

# Tools that write or execute — allowed only when every target path resolves
# inside the plans directory.
_PLAN_WRITE_TOOLS = {"write", "edit", "apply_patch"}

# The only read-only subagent kind permitted in plan mode.
_PLAN_TASK_AGENT = "explore"


def plans_dir(workdir: Path) -> Path:
    """Absolute path of the ``.neo/plans`` directory under workdir."""
    return Path(workdir).resolve() / PLANS_DIRNAME


def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path or ".")
    if not p.is_absolute():
        p = Path(ctx.workdir) / p
    return p.resolve()


def is_under_plans(path: Path, workdir: Path) -> bool:
    """True when `path` (already resolved) lives inside .neo/plans/."""
    try:
        path.resolve().relative_to(plans_dir(workdir))
        return True
    except (ValueError, OSError):
        return False


def slugify(goal: str) -> str:
    """Turn a goal into a filesystem-safe slug for the plan file name."""
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")
    return slug[:40] or "plan"


def _under_plans(target: str, workdir: Path) -> bool:
    """True when `target` resolves to a path inside .neo/plans/."""
    p = Path(target or ".")
    if not p.is_absolute():
        p = Path(workdir) / p
    return is_under_plans(p, workdir)


def plan_mode_active(ctx: ToolContext) -> bool:
    """True when this session is currently in plan mode."""
    state = getattr(ctx, "plan_mode", None)
    return isinstance(state, dict) and bool(state.get("active", False))


def plan_mode_allows(tool_name: str, target: str,
                     workdir: Path) -> tuple[bool, str]:
    """Decide a tool call while plan mode is active.

    Only call this when plan mode is active (the caller checks first).
    Returns ``(ok, reason)``; ``reason`` is empty when ok.
    """
    if tool_name in _PLAN_READ_TOOLS:
        return True, ""

    if tool_name == "plan_enter":
        return False, ("Already in plan mode. Finish the plan and call "
                       "plan_exit first.")

    if tool_name == "task":
        # `target` is the subagent kind ("explore" | "general").
        if target == _PLAN_TASK_AGENT:
            return True, ""
        return False, (f"In plan mode the task tool may only spawn the "
                       f"'{_PLAN_TASK_AGENT}' specialist (got {target!r}). "
                       "Research yourself or ask questions instead.")

    if tool_name in _PLAN_WRITE_TOOLS:
        if target and _under_plans(target, Path(workdir)):
            return True, ""
        return False, (f"In plan mode {tool_name} is denied on '{target}'. "
                       "You may only write the plan document under "
                       f"{PLANS_DIRNAME}/. Call plan_exit when the plan "
                       "is ready.")

    if tool_name == "bash":
        # A shell command string carries no provably-safe path target, so
        # it stays denied in plan mode. Use write/edit on the plan file.
        return False, ("In plan mode bash is denied. Research with read, "
                       "glob, grep and web tools; write the plan document "
                       f"under {PLANS_DIRNAME}/ with write/edit.")

    return False, (f"In plan mode the '{tool_name}' tool is not available. "
                   "Allowed: read, list_dir, glob, grep, webfetch, "
                   "websearch, question, task (explore specialist), "
                   "plan_exit, and write/edit only under "
                   f"{PLANS_DIRNAME}/.")


def plan_mode_allows_paths(paths: list[str],
                           workdir: Path) -> tuple[bool, str]:
    """Decide an apply_patch call while plan mode is active.

    Every touched file must resolve under .neo/plans/; a single file
    outside denies the whole patch.
    """
    targets = [str(p) for p in paths]
    if not targets:
        return False, ("In plan mode apply_patch with no touched files is "
                       "denied. You may only write the plan document under "
                       f"{PLANS_DIRNAME}/.")
    for target in targets:
        if not _under_plans(target, Path(workdir)):
            return False, (f"In plan mode apply_patch is denied on "
                           f"'{target}'. You may only write the plan "
                           f"document under {PLANS_DIRNAME}/. Call plan_exit "
                           "when the plan is ready.")
    return True, ""


def build_handoff(plan_path: Path, summary: str) -> str:
    """Render the approved plan as the user message that starts the build turn."""
    plan_text = Path(plan_path).read_text(encoding="utf-8").strip()
    return (
        "[Approved plan]\n\n"
        "The following plan was written in plan mode and approved. "
        "Implement it now, step by step.\n\n"
        "--- plan ---\n"
        f"{plan_text}\n"
        "--- end plan ---\n\n"
        f"Plan summary: {summary.strip()}\n\n"
        "Proceed: implement the plan exactly as written. Verify your work "
        "(tests, type checks, builds) as you go and report what changed."
    )


class PlanEnterTool(Tool):
    name = "plan_enter"
    description = (
        "Enter plan mode: switch to research-only planning for the given "
        "goal. While in plan mode you may read and search but must not "
        "write code — write the plan to the returned path and call "
        "plan_exit when it is ready."
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "The goal to plan for, in one sentence.",
            },
        },
        "required": ["goal"],
        "additionalProperties": False,
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        goal = str(args.get("goal", "")).strip()
        if not goal:
            return ToolResult(is_error=True, output="goal is required.",
                              title=self.name)

        if plan_mode_active(ctx):
            return ToolResult(
                is_error=True,
                output=("Already in plan mode. Finish the current plan with "
                        "plan_exit before starting a new one."),
                title=self.name,
            )

        slug = slugify(goal)
        pdir = plans_dir(ctx.workdir)
        try:
            pdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(is_error=True,
                              output=f"Could not create {pdir}: {exc}",
                              title=self.name)
        plan_file = pdir / f"{slug}.md"

        setattr(ctx, "plan_mode",
                {"goal": goal, "active": True,
                 "plan_path": str(plan_file), "slug": slug})

        instructions = (
            f"Plan mode active. Goal: {goal}\n\n"
            "Rules while in plan mode:\n"
            "- Research only: read, list_dir, glob, grep, webfetch, websearch, "
            "question, and the 'explore' task specialist are allowed.\n"
            "- Do NOT write or edit code, and do not run bash. You may only "
            f"write/edit files under {PLANS_DIRNAME}/.\n"
            f"- Write your plan to: {plan_file}\n"
            "  The plan must include: goal, current state, proposed changes "
            "file by file, risks/open questions, and verification steps.\n"
            "- When the plan file is complete, call plan_exit with "
            "plan_path set to that file and a one-paragraph summary.\n"
            "- Do not start implementing. Implementation begins after the "
            "plan is approved."
        )
        return ToolResult(output=instructions, title=f"plan: {slug}")


class PlanExitTool(Tool):
    name = "plan_exit"
    description = (
        "Leave plan mode with a finished plan document. Validates that the "
        "plan file exists under .neo/plans/ and returns the approved plan "
        "as the handoff for the build turn."
    )
    parameters = {
        "type": "object",
        "properties": {
            "plan_path": {
                "type": "string",
                "description": "Path to the plan file under .neo/plans/.",
            },
            "summary": {
                "type": "string",
                "description": "One-paragraph summary of the plan.",
            },
        },
        "required": ["plan_path", "summary"],
        "additionalProperties": False,
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        raw = str(args.get("plan_path", "")).strip()
        summary = str(args.get("summary", "")).strip()
        if not raw:
            return ToolResult(is_error=True,
                              output="plan_path is required.", title=self.name)
        if not summary:
            return ToolResult(is_error=True,
                              output="summary is required.", title=self.name)

        resolved = _resolve(ctx, raw)
        pdir = plans_dir(ctx.workdir)
        try:
            resolved.relative_to(pdir)
        except ValueError:
            return ToolResult(
                is_error=True,
                output=(f"plan_path must be inside {pdir} (got '{raw}'). "
                        "Plans live under .neo/plans/ only."),
                title=self.name,
            )
        if not resolved.is_file():
            return ToolResult(
                is_error=True,
                output=(f"Plan file not found: {resolved}. Write the plan "
                        "first, then call plan_exit."),
                title=self.name,
            )

        # Leaving plan mode hands control to the build turn: ask the user
        # first. A rejected answer is a graceful error and plan mode stays
        # active; only an explicit approval flips it off.
        ui = ctx.ui
        ask = getattr(ui, "ask", None) if ui is not None else None
        if callable(ask):
            answers = await ask([{
                "header": "plan_exit",
                "question": ("Switch to build mode and implement this plan?\n\n"
                             f"Plan: {resolved}\n{summary}"),
                "options": [
                    {"label": "Yes, implement it",
                     "description": "Leave plan mode and start building."},
                    {"label": "No, keep planning",
                     "description": "Stay in plan mode; the plan is unchanged."},
                ],
            }])
            chosen = ""
            if isinstance(answers, dict):
                chosen = answers.get("0", answers.get("plan_exit", "")) or ""
            approved = (isinstance(chosen, str)
                        and chosen.strip().lower().startswith("yes"))
        else:
            # No interactive UI (headless/scripted runs): fall back to the
            # standard permission gate so plan_exit still needs approval.
            gate = getattr(ctx, "gate", None)
            if not callable(gate):
                return ToolResult(
                    is_error=True,
                    output=("plan_exit needs approval but no interactive UI "
                            "or permission gate is available."),
                    title=self.name)
            answer = await gate(self.name, str(resolved), summary)
            approved = answer in ("once", "always")
        if not approved:
            return ToolResult(
                is_error=True,
                output=("Staying in plan mode — the plan was not approved. "
                        "Refine the plan and call plan_exit again when ready."),
                title=self.name)

        state = getattr(ctx, "plan_mode", None)
        if isinstance(state, dict):
            state["active"] = False
            state["plan_path"] = str(resolved)
        return ToolResult(output=build_handoff(resolved, summary),
                          title="plan approved")
