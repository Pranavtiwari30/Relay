"""Provider-neutral contract for tool-calling chat models.

Each provider owns its native conversation history behind a ``ProviderSession``.
That matters: some APIs require the assistant turn to be echoed back verbatim
(including reasoning blocks) and reject a history that was rebuilt or edited.
The investigator therefore never sees or rewrites provider messages; it only
exchanges these normalised types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ErrorKind = Literal[
    "timeout", "unavailable", "auth", "rate_limited", "refused", "truncated", "bad_response"
]


class ProviderError(Exception):
    """A provider could not produce a usable turn. Triggers fallback to the next one."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.message = message


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Any


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


@dataclass(frozen=True)
class ModelTurn:
    text: str | None
    tool_calls: list[ToolCall]
    model: str
    usage: Usage = field(default_factory=Usage)


class ProviderSession(Protocol):
    def send(
        self,
        *,
        tool_results: list[ToolResult] | None = None,
        user_text: str | None = None,
        timeout: float,
    ) -> ModelTurn:
        """Advance the conversation by one model turn.

        The first call takes no arguments beyond ``timeout``. Later calls pass
        the results for *every* tool call in the previous turn, optionally with
        an extra user instruction. Raises ``ProviderError``.
        """
        ...


class ChatProvider(Protocol):
    name: str
    model: str

    def start(
        self, *, system: str, user: str, tools: list[ToolDefinition], max_output_tokens: int
    ) -> ProviderSession: ...
