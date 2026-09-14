"""Provider-neutral LLM interface.

The rest of the app only knows about these types. Swapping provider means
writing one new subclass of Provider and adding it to llm/__init__.py.

History format (provider-neutral) is a list of dicts:

    {"role": "user",      "content": "what's on my list"}
    {"role": "assistant", "content": "let me look",
                          "tool_calls": [ToolCall, ...]}   # either may be empty
    {"role": "tool",      "tool_call_id": "...", "name": "get_my_issues",
                          "content": "<json string>"}

Each adapter converts this to its own wire format. Nothing above this layer
should ever see a provider-specific shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Turn:
    """One assistant turn: some prose, and/or some tool calls."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class ToolSpec:
    """A tool, described once, in JSON Schema. Adapters reshape as needed."""
    name: str
    description: str
    parameters: dict[str, Any]


class Provider(ABC):
    """Streaming, tool-calling chat."""

    name: str = "provider"

    @abstractmethod
    def stream_turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: Iterable[ToolSpec],
        on_text: Callable[[str], None] | None = None,
    ) -> Turn:
        """Run one assistant turn.

        Calls on_text with each text delta as it arrives, then returns the
        completed Turn. Must not mutate `messages`.
        """
        raise NotImplementedError
