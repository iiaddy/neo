"""Typed agent-level events.

The agent loop yields these; every frontend (TUI, print mode, RPC)
renders from them. Flat frozen dataclasses with a ``kind`` discriminator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Union


@dataclass(frozen=True)
class RunStart:
    kind: ClassVar[str] = "run_start"
    session_id: str = ""
    model: str = ""


@dataclass(frozen=True)
class RunEnd:
    kind: ClassVar[str] = "run_end"
    reason: str = "done"  # done|aborted|error|max_steps|doom_loop


@dataclass(frozen=True)
class TurnStart:
    kind: ClassVar[str] = "turn_start"
    index: int = 0


@dataclass(frozen=True)
class TurnEnd:
    kind: ClassVar[str] = "turn_end"
    index: int = 0
    finish: str = "stop"


@dataclass(frozen=True)
class TextStart:
    kind: ClassVar[str] = "text_start"


@dataclass(frozen=True)
class TextDelta:
    kind: ClassVar[str] = "text_delta"
    text: str = ""


@dataclass(frozen=True)
class TextEnd:
    kind: ClassVar[str] = "text_end"
    text: str = ""


@dataclass(frozen=True)
class ReasonStart:
    kind: ClassVar[str] = "reason_start"


@dataclass(frozen=True)
class ReasonDelta:
    kind: ClassVar[str] = "reason_delta"
    text: str = ""


@dataclass(frozen=True)
class ReasonEnd:
    kind: ClassVar[str] = "reason_end"
    text: str = ""


@dataclass(frozen=True)
class ToolStart:
    kind: ClassVar[str] = "tool_start"
    call_id: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    title: str = ""


@dataclass(frozen=True)
class ToolProgress:
    kind: ClassVar[str] = "tool_progress"
    call_id: str = ""
    text: str = ""


@dataclass(frozen=True)
class ToolEnd:
    kind: ClassVar[str] = "tool_end"
    call_id: str = ""
    tool: str = ""
    ok: bool = True
    output: str = ""
    ms: int = 0


@dataclass(frozen=True)
class Usage:
    kind: ClassVar[str] = "usage"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class Notice:
    kind: ClassVar[str] = "notice"
    text: str = ""
    level: str = "info"  # info|warn|error


@dataclass(frozen=True)
class CompactStart:
    kind: ClassVar[str] = "compact_start"


@dataclass(frozen=True)
class CompactEnd:
    kind: ClassVar[str] = "compact_end"
    kept: int = 0


@dataclass(frozen=True)
class VerifyStart:
    kind: ClassVar[str] = "verify_start"
    command: str = ""


@dataclass(frozen=True)
class VerifyEnd:
    kind: ClassVar[str] = "verify_end"
    command: str = ""
    ok: bool = True
    output: str = ""


@dataclass(frozen=True)
class TodoUpdate:
    kind: ClassVar[str] = "todo_update"
    todos: tuple = field(default_factory=tuple)


AgentEvent = Union[
    RunStart, RunEnd, TurnStart, TurnEnd,
    TextStart, TextDelta, TextEnd,
    ReasonStart, ReasonDelta, ReasonEnd,
    ToolStart, ToolProgress, ToolEnd,
    Usage, Notice, CompactStart, CompactEnd,
    VerifyStart, VerifyEnd, TodoUpdate,
]
