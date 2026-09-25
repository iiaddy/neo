"""Permission policy: ask/allow/deny with wildcard rules.

Rule shape: {"bash": {"git *": "allow", "*": "ask"}, "edit": "*", ...}
A bare string value means pattern "*". Last matching rule wins.
"""
from __future__ import annotations

import re

TOOL_PERMISSION_KEYS = {
    "read": "read",
    "list_dir": "read",
    "glob": "read",
    "grep": "read",
    "write": "edit",
    "edit": "edit",
    "bash": "bash",
    "webfetch": "web",
    "websearch": "web",
    "task": "task",
    "todo_write": "session",
    "todo_read": "session",
    "question": "session",
    "skill": "session",
}


def wildcard_match(pattern: str, value: str) -> bool:
    """Match with ``*`` (any run without /), ``?`` (one char), ``**`` (any).

    A pattern ending in ``" *"`` also matches the bare prefix
    (``"git *"`` matches ``"git"``).
    """
    if pattern == "*":
        return True
    if pattern.endswith(" *"):
        prefix = pattern[:-2]
        if value == prefix or value.startswith(prefix + " "):
            return True
    rx = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                rx.append(".*")
                i += 2
            else:
                rx.append("[^/]*")
                i += 1
        elif c == "?":
            rx.append("[^/]")
            i += 1
        else:
            rx.append(re.escape(c))
            i += 1
    return re.fullmatch("".join(rx), value) is not None


class PermissionPolicy:
    def __init__(self, rules: dict | None = None):
        from ..config import default_permissions

        self.rules: dict = rules if rules is not None else default_permissions()
        self.session_allows: list[tuple[str, str]] = []

    def allow_always(self, key: str, pattern: str) -> None:
        self.session_allows.append((key, pattern))

    def _iter(self):
        for key, val in self.rules.items():
            if isinstance(val, dict):
                for pattern, action in val.items():
                    yield key, pattern, action
            elif isinstance(val, str):
                yield key, "*", val
        for key, pattern in self.session_allows:
            yield key, pattern, "allow"

    def check(self, key: str, target: str) -> str:
        """Return 'allow' | 'ask' | 'deny'. Last matching rule wins."""
        decision = "ask"
        for rule_key, pattern, action in self._iter():
            if rule_key != "*" and rule_key != key:
                continue
            if wildcard_match(pattern, target):
                decision = action
        return decision if decision in ("allow", "ask", "deny") else "ask"

    def key_for_tool(self, tool_name: str) -> str:
        if tool_name in TOOL_PERMISSION_KEYS:
            return TOOL_PERMISSION_KEYS[tool_name]
        # MCP tools (<server>_<tool>) run third-party code; gate them
        # under their own policy key when any are registered.
        try:
            from ..mcp.manager import MCP_TOOL_NAMES
        except Exception:
            MCP_TOOL_NAMES = set()
        return "mcp" if tool_name in MCP_TOOL_NAMES else "session"


#: Public alias kept for tests and external callers.
match_pattern = wildcard_match
