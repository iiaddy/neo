"""neo tools — inquiry (question): ask the human a structured question."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult


class InquiryTool(Tool):
    name = "question"
    description = (
        "Ask the user a structured question with labeled options. "
        "Requires an interactive UI; fails without one."
    )
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "Questions to ask.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "header": {"type": "string"},
                        "multiple": {"type": "boolean"},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["question", "options"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["questions"],
        "additionalProperties": False,
    }
    needs_approval = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        questions = args.get("questions")
        if not isinstance(questions, list) or not questions:
            return ToolResult(is_error=True, output="questions must be a non-empty list", title=self.name)
        for i, q in enumerate(questions):
            if not isinstance(q, dict):
                return ToolResult(is_error=True, output=f"question {i} is not an object", title=self.name)
            opts = q.get("options")
            if not isinstance(opts, list) or not opts:
                return ToolResult(
                    is_error=True,
                    output=f"question {i}: options must be a non-empty list",
                    title=self.name,
                )
            for j, opt in enumerate(opts):
                if not isinstance(opt, dict) or not str(opt.get("label", "")).strip():
                    return ToolResult(
                        is_error=True,
                        output=f"question {i}, option {j}: label is required",
                        title=self.name,
                    )
        ui = ctx.ui
        ask = getattr(ui, "ask", None)
        if ui is None or not callable(ask):
            return ToolResult(is_error=True, output="no interactive UI available", title=self.name)
        answers = await ask(questions)
        if not isinstance(answers, dict):
            return ToolResult(is_error=True, output="UI returned no answers", title=self.name)
        lines = []
        for i, q in enumerate(questions):
            header = q.get("header") or f"Q{i + 1}"
            chosen = answers.get(str(i), answers.get(header, ""))
            if isinstance(chosen, list):
                chosen = ", ".join(str(c) for c in chosen)
            lines.append(f"Q: {header} — {q.get('question', '')}\nA: {chosen}")
        return ToolResult(output="\n".join(lines), title="question")
