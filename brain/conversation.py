"""The conversation loop.

Keeps history across turns, runs the tool-calling round trip, and streams
text out sentence by sentence so that a TTS stage can start speaking before
the model has finished writing.

Deliberately knows nothing about audio, Whisper, Piper or Wyoming. It takes
text in and gives text out, which is why it can be exercised from a terminal
long before any of the hardware pipeline exists.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Callable, Iterable

from llm import Provider, ToolSpec, get_provider
from tools import Toolbox, tool_specs

MAX_TOOL_ROUNDS = 5

SYSTEM_PROMPT = """\
You are a spoken voice assistant. Your replies are read aloud, so:

- Be brief to the point of clipped. One sentence is usually right; two is
  the normal maximum. Every extra word is extra seconds of speech the user
  waits through, so drop preamble entirely — no "sure", no "let me check",
  no restating the question.
- Prefer fragments to full sentences where they are still clear: "Seven to
  eighteen, drizzle, twenty percent chance" beats "Tomorrow in Ottawa it
  will be seven to eighteen degrees with drizzle and a twenty percent
  chance of precipitation."
- Never read out UUIDs, URLs or markdown. Refer to issues by their
  identifier ("ENG-142") or, better, by their title.
- When listing issues, summarise. "Six open, the urgent one is the mic
  calibration" beats reading all six.
- Never invent an issue, a state or a date. If a tool call fails, say so
  plainly in one sentence.
- After any change you make, say what changed in a short confirmation —
  there is no screen for the user to check.
- If a request is ambiguous about which issue is meant, ask rather than
  guess. Guessing wrong means a silent wrong write.

Only use the tools you have been given. If you have no tool for something
the user asks about, say so in one sentence rather than guessing or
apologising at length.

Today is {today} ({weekday}). The user is in {timezone}. Resolve relative
dates like "Friday" or "next week" yourself and pass absolute YYYY-MM-DD
dates to tools.
"""


def build_system_prompt(timezone: str = "America/Toronto") -> str:
    today = _dt.date.today()
    return SYSTEM_PROMPT.format(
        today=today.isoformat(),
        weekday=today.strftime("%A"),
        timezone=timezone,
    )


# --------------------------------------------------------------------------
# sentence streaming
# --------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")

# Below this, a chunk is too short to synthesize naturally — Piper needs
# enough context for prosody, and the per-invocation overhead dominates.
MIN_CLAUSE_CHARS = 25


class SentenceStreamer:
    """Turns a stream of text deltas into a stream of speakable chunks.

    Sentence boundaries alone are not enough. A tight spoken assistant
    answers in ONE sentence, so waiting for a full stop means waiting for
    the entire reply — streaming that buys exactly nothing, which is what
    the first measurement on the Pi showed.

    So chunks also break at clause boundaries (commas, semicolons, colons)
    once there is enough text to synthesize naturally. "Eight to eighteen
    degrees," goes to Piper while the model is still writing "drizzle,
    nineteen percent chance of precipitation."

    The trade-off is prosody: Piper synthesizes each chunk independently,
    so break too often and speech sounds clipped. MIN_CLAUSE_CHARS is the
    guard.
    """

    def __init__(self, emit: Callable[[str], None], min_chunk: int = MIN_CLAUSE_CHARS):
        self._emit = emit
        self._buffer = ""
        self._min_chunk = min_chunk
        self._emitted_any = False

    def _take(self, upto: int, resume: int) -> None:
        chunk = self._buffer[:upto].strip()
        self._buffer = self._buffer[resume:]
        if chunk:
            self._emitted_any = True
            self._emit(chunk)

    def feed(self, delta: str) -> None:
        self._buffer += delta

        while True:
            match = _SENTENCE_END.search(self._buffer)
            if match:
                self._take(match.start(), match.end())
                continue

            # No sentence boundary yet. Break on the first clause boundary
            # that leaves a chunk worth speaking on its own — not merely the
            # first one found, since an early "Tomorrow in Ottawa:" is too
            # short and must not stop us looking at the comma after it.
            match = next(
                (m for m in _CLAUSE_END.finditer(self._buffer)
                 if m.start() >= self._min_chunk),
                None,
            )
            if match:
                self._take(match.start(), match.end())
                continue

            break

    def flush(self) -> None:
        remaining = self._buffer.strip()
        self._buffer = ""
        if remaining:
            self._emit(remaining)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

class Conversation:
    def __init__(
        self,
        provider: Provider | None = None,
        toolbox: Toolbox | None = None,
        specs: Iterable[ToolSpec] | None = None,
        system: str | None = None,
        timezone: str = "America/Toronto",
    ):
        self.provider = provider or get_provider()
        self.toolbox = toolbox or Toolbox()
        self.specs = list(specs if specs is not None else tool_specs())
        self.system = system or build_system_prompt(timezone)
        self.history: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.history.clear()

    def ask(
        self,
        text: str,
        on_text: Callable[[str], None] | None = None,
        on_sentence: Callable[[str], None] | None = None,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> str:
        """One user utterance in, one spoken reply out.

        on_text     -- raw deltas, for echoing to a terminal
        on_sentence -- whole sentences, for handing to TTS
        on_tool     -- called before each tool runs, for logging
        """
        self.history.append({"role": "user", "content": text})

        streamer = SentenceStreamer(on_sentence) if on_sentence else None

        def sink(delta: str) -> None:
            if on_text:
                on_text(delta)
            if streamer:
                streamer.feed(delta)

        reply = ""

        for _ in range(MAX_TOOL_ROUNDS):
            turn = self.provider.stream_turn(
                system=self.system,
                messages=self.history,
                tools=self.specs,
                on_text=sink,
            )

            self.history.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": turn.tool_calls,
                }
            )
            reply = turn.text

            if not turn.wants_tools:
                break

            for call in turn.tool_calls:
                if on_tool:
                    on_tool(call.name, call.args)
                result = self.toolbox.run(call.name, call.args)
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result,
                    }
                )
        else:
            # Ran out of rounds with tools still pending.
            reply = "Sorry — I got stuck working that out. Try asking a different way."
            self.history.append({"role": "assistant", "content": reply, "tool_calls": []})
            if on_text:
                on_text(reply)
            if streamer:
                streamer.feed(reply)

        if streamer:
            streamer.flush()
        return reply
