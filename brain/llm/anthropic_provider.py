"""Anthropic adapter."""

from __future__ import annotations

import os
from typing import Any, Callable, Iterable

from .base import Provider, ToolCall, ToolSpec, Turn


def _require(var: str) -> str:
    value = (os.environ.get(var) or "").strip()
    if not value:
        raise RuntimeError(
            f"{var} is not set (an empty value in .env counts as unset)"
        )
    return value

DEFAULT_MODEL = "claude-sonnet-5"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str | None = None, max_tokens: int = 1024):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pip install anthropic") from exc
        # An org-scoped key must name a workspace on every request; a
        # workspace-scoped key carries it already and needs no header.
        headers: dict[str, str] = {}
        workspace = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
        if workspace:
            headers["anthropic-workspace-id"] = workspace

        self._client = anthropic.Anthropic(
            api_key=_require("ANTHROPIC_API_KEY"),
            default_headers=headers or None,
        )
        self.workspace = workspace or None
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self.max_tokens = max_tokens

    # -- history translation ------------------------------------------------

    @staticmethod
    def _to_wire(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Neutral history -> Anthropic messages.

        Anthropic wants tool results as a *user* message containing
        tool_result blocks, so consecutive tool results are coalesced.
        """
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]

            if role == "user":
                out.append({"role": "user", "content": m["content"]})

            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.args,
                    })
                if blocks:
                    out.append({"role": "assistant", "content": blocks})

            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"],
                }
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})

        return out

    @staticmethod
    def _tools(tools: Iterable[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]

    # -- the one method that matters ----------------------------------------

    def stream_turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: Iterable[ToolSpec],
        on_text: Callable[[str], None] | None = None,
    ) -> Turn:
        # The system prompt and tool schemas are identical on every call, so
        # mark the end of that prefix as cacheable. Saves re-processing them
        # each turn, which is time-to-first-token straight off the latency
        # budget. Below the provider's minimum cacheable length it is simply
        # ignored, so there is no downside.
        cache = os.environ.get("PROMPT_CACHE", "1") != "0"

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": (
                [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                if cache else system
            ),
            "messages": self._to_wire(messages),
        }
        wire_tools = self._tools(tools)
        if wire_tools:
            if cache:
                wire_tools = [dict(t) for t in wire_tools]
                wire_tools[-1]["cache_control"] = {"type": "ephemeral"}
            kwargs["tools"] = wire_tools

        with self._client.messages.stream(**kwargs) as stream:
            for chunk in stream.text_stream:
                if on_text:
                    on_text(chunk)
            final = stream.get_final_message()

        turn = Turn()
        for block in final.content:
            if block.type == "text":
                turn.text += block.text
            elif block.type == "tool_use":
                turn.tool_calls.append(
                    ToolCall(id=block.id, name=block.name, args=dict(block.input or {}))
                )
        return turn
