"""Context estimation and compaction planning."""
from __future__ import annotations


def estimate_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    """Rough token estimate: ~4 chars per token + per-message/tool overhead."""
    chars = 0
    for m in messages:
        chars += 4  # role overhead
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    chars += len(str(b.get("text", b.get("input", ""))))
        for tc in m.get("tool_calls") or []:
            chars += len(str(tc.get("arguments", ""))) + 16
    if tools:
        chars += sum(len(str(t.get("description", ""))) +
                     len(str(t.get("parameters", ""))) + 64 for t in tools)
    return max(1, chars // 4)


def plan_compaction(messages: list[dict], context_window: int,
                    trigger_ratio: float = 0.85,
                    keep_tail_ratio: float = 0.15) -> tuple[int, int] | None:
    """Return (summarize_upto_index, keep_from_index) or None if not needed."""
    if estimate_tokens(messages) < context_window * trigger_ratio:
        return None
    keep_n = max(4, int(len(messages) * keep_tail_ratio))
    keep_from = max(1, len(messages) - keep_n)
    return (keep_from, keep_from)


def flatten_for_summary(messages: list[dict]) -> str:
    """Serialize messages (minus tail) into lines for the summarizer."""
    lines = []
    for m in messages:
        role = m.get("role", "?")
        if role == "user":
            lines.append(f"[User]\n{_short(m.get('content'))}")
        elif role == "assistant":
            text = m.get("content") or ""
            tcs = m.get("tool_calls") or []
            lines.append(f"[Assistant]\n{_short(text)}")
            for tc in tcs:
                lines.append(f"[Tool call] {tc.get('name')} "
                             f"{_short(str(tc.get('arguments', '')))}")
        elif role == "tool":
            flag = "ERROR " if m.get("is_error") else ""
            lines.append(f"[Tool result {flag}{m.get('name')}]\n"
                         f"{_short(str(m.get('content')))}")
    return "\n".join(lines)


def _short(text: str | None, limit: int = 1500) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…[truncated]"
