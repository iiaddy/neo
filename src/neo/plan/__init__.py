"""neo plan mode — real plan-then-build workflow.

Plan mode locks the agent into research: reads, searches and questions are
fine, but code writes are denied everywhere except the plan document itself
(``.neo/plans/<slug>.md``). The flow:

1. ``plan_enter`` (tool) — model (or ``/plan``) declares a goal; the session
   switches to the ``plan`` agent toolset and ``ctx.plan_mode`` is marked
   active.
2. The model researches and writes the plan file.
3. ``plan_exit`` (tool) — validates the plan file, deactivates plan mode,
   and returns a handoff message for the build turn.

Enforcement lives in :func:`plan_mode_allows`, which the agent loop's
``_permission`` check calls first.
"""

from .tools import (
    PlanEnterTool,
    PlanExitTool,
    build_handoff,
    plan_mode_active,
    plan_mode_allows,
)

__all__ = [
    "PlanEnterTool",
    "PlanExitTool",
    "build_handoff",
    "plan_mode_active",
    "plan_mode_allows",
]
