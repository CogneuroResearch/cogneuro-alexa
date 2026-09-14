#!/usr/bin/env python3
"""Terminal chat — the voice assistant without the voice.

Build-order step 4: prove the LLM can read, discuss and update Linear
reliably before any microphone is involved. Everything here except the
input() and print() is the same code the voice pipeline will use.

    cd brain
    pip install -r requirements.txt anthropic
    cp .env.example .env && $EDITOR .env
    python chat.py

Commands: /reset clears history, /history dumps it, /quit exits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conversation import Conversation  # noqa: E402
from llm import get_provider  # noqa: E402

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def main() -> int:
    if load_dotenv:
        load_dotenv(Path(__file__).resolve().parent / ".env")

    try:
        provider = get_provider()
    except KeyError as exc:
        print(f"Missing environment variable: {exc}. See .env.example.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Could not start provider: {exc}", file=sys.stderr)
        return 1

    conversation = Conversation(provider=provider)

    print(f"{DIM}provider {provider.name} / model {getattr(provider, 'model', '?')}{RESET}")
    print(f"{DIM}tools: {', '.join(s.name for s in conversation.specs)}{RESET}")
    print(f"{DIM}/reset  /history  /quit{RESET}\n")

    show_tools = os.environ.get("SHOW_TOOL_CALLS", "1") != "0"

    while True:
        try:
            line = input(f"{BOLD}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue
        if line in {"/quit", "/exit"}:
            return 0
        if line == "/reset":
            conversation.reset()
            print(f"{DIM}history cleared{RESET}\n")
            continue
        if line == "/history":
            for message in conversation.history:
                calls = message.get("tool_calls") or []
                suffix = f"  +{len(calls)} tool call(s)" if calls else ""
                content = (message.get("content") or "")[:200]
                print(f"{DIM}{message['role']:>9}: {content}{suffix}{RESET}")
            print()
            continue

        def on_tool(name: str, args: dict) -> None:
            if show_tools:
                print(f"\n{DIM}  → {name}({args}){RESET}")

        print(f"{BOLD}box ›{RESET} ", end="", flush=True)
        try:
            conversation.ask(
                line,
                on_text=lambda delta: print(delta, end="", flush=True),
                on_tool=on_tool,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"\n{DIM}error: {exc.__class__.__name__}: {exc}{RESET}")
        print("\n")


if __name__ == "__main__":
    raise SystemExit(main())
