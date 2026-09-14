"""Provider registry.

Set LLM_PROVIDER in the environment (anthropic | openai) and the rest of the
app never needs to know which one is in play.

Adding a provider is one file plus one line here. The interface it must
satisfy is in base.py and is deliberately small: one streaming method.
"""

from __future__ import annotations

import os

from .base import Provider, ToolCall, ToolSpec, Turn

__all__ = ["Provider", "ToolCall", "ToolSpec", "Turn", "get_provider"]


def get_provider(name: str | None = None, model: str | None = None) -> Provider:
    name = (name or os.environ.get("LLM_PROVIDER") or "anthropic").strip().lower()

    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(model=model)

    if name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(model=model)

    raise ValueError(
        f"Unknown LLM_PROVIDER {name!r}. Known: anthropic, openai. "
        "Adding another means subclassing Provider in llm/base.py."
    )
