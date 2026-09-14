#!/usr/bin/env python3
"""Pre-flight checks. Run this before chat.py when something looks wrong.

Checks each layer independently so a failure points at one thing:
  1. environment variables present
  2. Linear key valid, and the schema fields this code depends on exist
  3. weather API reachable (needs no key)
  4. LLM provider constructible

Deliberately does no writes to Linear.
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

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
SKIP = "\033[33m–\033[0m"


def main() -> int:
    if load_dotenv:
        load_dotenv(Path(__file__).resolve().parent / ".env")

    failures = 0

    provider_name = os.environ.get("LLM_PROVIDER", "anthropic")
    key_var = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider_name)
    def is_set(var: str) -> bool:
        return bool((os.environ.get(var) or "").strip())

    if key_var and is_set(key_var):
        print(f"{OK} {key_var} set")
    elif key_var:
        print(f"{BAD} {key_var} missing or empty")
        failures += 1

    linear_ready = is_set("LINEAR_API_KEY")
    print(f"{OK} LINEAR_API_KEY set" if linear_ready
          else f"{SKIP} LINEAR_API_KEY not set — Linear tools disabled (fine for now)")

    # -- Linear (optional) --------------------------------------------------
    if not linear_ready:
        print(f"{SKIP} Linear checks skipped")
    else:
      try:
          from linear_client import Linear

          linear = Linear()
          viewer = linear.viewer()
          print(f"{OK} Linear key valid — {viewer['name']} <{viewer['email']}>")

          teams = linear.teams()
          print(f"{OK} teams: {', '.join(t['key'] for t in teams) or '(none)'}")

          issues = linear.assigned_issues(limit=5)
          print(f"{OK} assigned open issues: {len(issues)}")
          for issue in issues[:3]:
              print(f"    {issue['identifier']}  {issue['priorityLabel']:<12} {issue['title'][:48]}")

          if teams:
              states = linear.team_states(teams[0]["id"])
              names = ", ".join(s["name"] for s in states)
              print(f"{OK} {teams[0]['key']} states: {names}")
      except Exception as exc:  # noqa: BLE001
          print(f"{BAD} Linear: {exc.__class__.__name__}: {exc}")
          failures += 1

    # -- weather ------------------------------------------------------------
    try:
        from tools import Toolbox

        print(f"{OK} weather: {Toolbox().get_weather('Ottawa')}")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} weather: {exc.__class__.__name__}: {exc}")
        failures += 1

    # -- provider -----------------------------------------------------------
    try:
        from llm import get_provider

        provider = get_provider()
        print(f"{OK} provider {provider.name} / model {getattr(provider, 'model', '?')}")
        if getattr(provider, "workspace", None):
            print(f"{OK} anthropic workspace: {provider.workspace}")

        from tools import tool_specs

        names = ", ".join(s.name for s in tool_specs())
        print(f"{OK} tools offered to the model: {names}")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} provider: {exc.__class__.__name__}: {exc}")
        failures += 1

    print()
    print("all good" if not failures else f"{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
