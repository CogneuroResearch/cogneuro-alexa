#!/usr/bin/env python3
"""Offline smoke test — no API keys, no network.

Exercises the parts most likely to break silently:
  - the tool-calling round trip (call -> result -> follow-up turn)
  - provider-neutral history shape
  - both adapters' history translation
  - sentence streaming

Run: python smoke_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conversation import Conversation, SentenceStreamer  # noqa: E402
from llm.base import Provider, ToolCall, ToolSpec, Turn  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  ok   " if condition else "  FAIL ") + label + (f"  {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


class ScriptedProvider(Provider):
    """Returns a canned sequence of turns, recording what it was sent."""

    name = "scripted"

    def __init__(self, turns: list[Turn]):
        self._turns = list(turns)
        self.seen: list[list[dict]] = []

    def stream_turn(self, system, messages, tools, on_text=None):
        self.seen.append([dict(m) for m in messages])
        turn = self._turns.pop(0)
        if on_text and turn.text:
            for word in turn.text.split(" "):
                on_text(word + " ")
        return turn


class FakeToolbox:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def run(self, name, args):
        self.calls.append((name, args))
        return "ENG-1: mic calibration | state Todo | priority Urgent"


print("tool-calling round trip")
toolbox = FakeToolbox()
provider = ScriptedProvider([
    Turn(text="", tool_calls=[ToolCall(id="t1", name="get_my_issues", args={"limit": 5})]),
    Turn(text="One open issue. The mic calibration is urgent."),
])
conversation = Conversation(
    provider=provider,
    toolbox=toolbox,
    specs=[ToolSpec(name="get_my_issues", description="d", parameters={"type": "object"})],
    system="test",
)
reply = conversation.ask("what's on my list")

check("tool was dispatched", toolbox.calls == [("get_my_issues", {"limit": 5})], str(toolbox.calls))
check("final reply returned", reply.startswith("One open issue"), reply)
roles = [m["role"] for m in conversation.history]
check("history shape", roles == ["user", "assistant", "tool", "assistant"], str(roles))
check("tool result linked to call id", conversation.history[2]["tool_call_id"] == "t1")
check("second turn saw the tool result", any(m["role"] == "tool" for m in provider.seen[1]))

print("\nno-tool path")
provider2 = ScriptedProvider([Turn(text="It is sunny.")])
conversation2 = Conversation(provider=provider2, toolbox=FakeToolbox(), specs=[], system="test")
check("plain answer", conversation2.ask("weather?") == "It is sunny.")
check("history is user+assistant", [m["role"] for m in conversation2.history] == ["user", "assistant"])

print("\nsentence streaming")
sentences: list[str] = []
streamer = SentenceStreamer(sentences.append)
for delta in ["Six ", "open. ", "The ", "urgent ", "one ", "is ", "the ", "mic. ", "Trailing"]:
    streamer.feed(delta)
check("emits on sentence boundary", sentences == ["Six open.", "The urgent one is the mic."], str(sentences))
streamer.flush()
check("flush emits remainder", sentences[-1] == "Trailing", str(sentences))

print("\nadapter history translation")
history = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "checking", "tool_calls": [ToolCall("t1", "get_my_issues", {})]},
    {"role": "tool", "tool_call_id": "t1", "name": "get_my_issues", "content": "none"},
]

from llm.anthropic_provider import AnthropicProvider  # noqa: E402
from llm.openai_provider import OpenAIProvider  # noqa: E402

wire = AnthropicProvider._to_wire(history)
check("anthropic: 3 messages", len(wire) == 3, str(len(wire)))
check("anthropic: tool_result is a user turn", wire[2]["role"] == "user")
check("anthropic: tool_use block present",
      any(b["type"] == "tool_use" for b in wire[1]["content"]))

wire2 = OpenAIProvider._to_wire("sys", history)
check("openai: system first", wire2[0]["role"] == "system")
check("openai: tool role preserved", wire2[3]["role"] == "tool")
check("openai: arguments serialised", isinstance(wire2[2]["tool_calls"][0]["function"]["arguments"], str))

print()
if failures:
    print(f"{len(failures)} failure(s): " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
