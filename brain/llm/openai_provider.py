"""OpenAI adapter."""

from __future__ import annotations

import json
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

DEFAULT_MODEL = "gpt-4.1"


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model: str | None = None, max_tokens: int = 1024):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pip install openai") from exc
        self._client = OpenAI(api_key=_require("OPENAI_API_KEY"))
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self.max_tokens = max_tokens

    # -- history translation ------------------------------------------------

    @staticmethod
    def _to_wire(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            role = m["role"]

            if role == "user":
                out.append({"role": "user", "content": m["content"]})

            elif role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                        }
                        for tc in m["tool_calls"]
                    ]
                out.append(msg)

            elif role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m["tool_call_id"],
                    "content": m["content"],
                })

        return out

    @staticmethod
    def _tools(tools: Iterable[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
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
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self._to_wire(system, messages),
            "stream": True,
        }
        wire_tools = self._tools(tools)
        if wire_tools:
            kwargs["tools"] = wire_tools

        text_parts: list[str] = []
        # index -> partial {id, name, args_json}
        partial: dict[int, dict[str, str]] = {}

        for event in self._client.chat.completions.create(**kwargs):
            if not event.choices:
                continue
            delta = event.choices[0].delta

            if getattr(delta, "content", None):
                text_parts.append(delta.content)
                if on_text:
                    on_text(delta.content)

            for tc in (getattr(delta, "tool_calls", None) or []):
                slot = partial.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments

        turn = Turn(text="".join(text_parts))
        for _, slot in sorted(partial.items()):
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            turn.tool_calls.append(ToolCall(id=slot["id"], name=slot["name"], args=args))
        return turn
