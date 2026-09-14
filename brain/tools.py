"""Tool definitions and dispatch.

Each tool is described once in JSON Schema (provider-neutral) and implemented
once here. Adding a capability to the assistant is: write a function, add a
ToolSpec, register it in Toolbox.HANDLERS. Nothing else changes — not the
conversation loop, not the provider adapters.

Results are returned as short strings. They are read by an LLM that will
speak the answer aloud, so they are terse and free of UUIDs.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Callable

import requests

from linear_client import (
    INT_TO_PRIORITY,
    PRIORITY_TO_INT,
    Linear,
    LinearError,
)
from llm.base import ToolSpec

# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="get_my_issues",
        description=(
            "List the Linear issues assigned to me. This is the breakfast read-out. "
            "Returns open issues by default, newest activity first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many to return. Keep it small for speech; default 10.",
                    "minimum": 1,
                    "maximum": 50,
                },
                "include_done": {
                    "type": "boolean",
                    "description": "Include completed and cancelled issues. Default false.",
                },
            },
        },
    ),
    ToolSpec(
        name="set_priority",
        description="Change an issue's priority. Use when I say things like 'bump that up' or 'drop that'.",
        parameters={
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Issue identifier, e.g. ENG-142"},
                "priority": {
                    "type": "string",
                    "enum": ["urgent", "high", "medium", "low", "none"],
                },
            },
            "required": ["identifier", "priority"],
        },
    ),
    ToolSpec(
        name="set_due_date",
        description=(
            "Set or clear an issue's due date. Resolve relative dates like 'Friday' "
            "or 'next week' yourself before calling — pass an absolute date."
        ),
        parameters={
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Issue identifier, e.g. ENG-142"},
                "date": {
                    "type": "string",
                    "description": "Due date as YYYY-MM-DD, or an empty string to clear it.",
                },
            },
            "required": ["identifier", "date"],
        },
    ),
    ToolSpec(
        name="set_state",
        description=(
            "Move an issue to a different workflow state — done, in progress, todo, "
            "cancelled, and so on. State names vary by team; match loosely."
        ),
        parameters={
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Issue identifier, e.g. ENG-142"},
                "state": {
                    "type": "string",
                    "description": "Target state name, e.g. 'Done', 'In Progress', 'Todo'.",
                },
            },
            "required": ["identifier", "state"],
        },
    ),
    ToolSpec(
        name="add_comment",
        description="Add a comment to an issue. Use this to capture a decision I made out loud.",
        parameters={
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Issue identifier, e.g. ENG-142"},
                "text": {"type": "string", "description": "Comment body. Markdown is fine."},
            },
            "required": ["identifier", "text"],
        },
    ),
    ToolSpec(
        name="create_issue",
        description="Create a new Linear issue assigned to me.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "team": {
                    "type": "string",
                    "description": (
                        "Team key or name. Optional — omit it if I only have one team, "
                        "and ask me if there is more than one and I did not say."
                    ),
                },
                "description": {"type": "string"},
                "priority": {
                    "type": "string",
                    "enum": ["urgent", "high", "medium", "low", "none"],
                },
            },
            "required": ["title"],
        },
    ),
    ToolSpec(
        name="get_weather",
        description=(
            "Current conditions and daily forecast for a place. Use for any question "
            "about weather — you cannot know it otherwise. Ask for more days when the "
            "question is about tomorrow, the weekend, or the week ahead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Place name, e.g. 'Ottawa' or 'Ottawa, Canada'.",
                },
                "days": {
                    "type": "integer",
                    "description": (
                        "Forecast days starting today. 1 for now/today, 2 to include "
                        "tomorrow, 7 for the week. Default 1."
                    ),
                    "minimum": 1,
                    "maximum": 7,
                },
            },
            "required": ["location"],
        },
    ),
]


# --------------------------------------------------------------------------
# implementations
# --------------------------------------------------------------------------

WEATHER_CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
    81: "showers", 82: "violent showers", 85: "snow showers",
    86: "heavy snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}


class Toolbox:
    """Holds the live clients and dispatches tool calls by name."""

    def __init__(self, linear: Linear | None = None, timeout: float = 15.0):
        self._linear = linear
        self.timeout = timeout
        self._geo_cache: dict[str, dict[str, Any] | None] = {}
        self.HANDLERS: dict[str, Callable[..., str]] = {
            "get_my_issues": self.get_my_issues,
            "set_priority": self.set_priority,
            "set_due_date": self.set_due_date,
            "set_state": self.set_state,
            "add_comment": self.add_comment,
            "create_issue": self.create_issue,
            "get_weather": self.get_weather,
        }

    @property
    def linear(self) -> Linear:
        if self._linear is None:
            self._linear = Linear()
        return self._linear

    # -- dispatch -----------------------------------------------------------

    def run(self, name: str, args: dict[str, Any]) -> str:
        handler = self.HANDLERS.get(name)
        if handler is None:
            return f"Error: no such tool {name!r}."
        try:
            return handler(**args)
        except LinearError as exc:
            return f"Linear error: {exc}"
        except TypeError as exc:
            return f"Error: bad arguments for {name}: {exc}"
        except requests.RequestException as exc:
            return f"Network error calling {name}: {exc}"
        except Exception as exc:  # noqa: BLE001 - the model should see, not crash
            return f"Error running {name}: {exc.__class__.__name__}: {exc}"

    # -- Linear -------------------------------------------------------------

    def get_my_issues(self, limit: int = 10, include_done: bool = False) -> str:
        issues = self.linear.assigned_issues(limit=limit, include_done=include_done)
        if not issues:
            return "No open issues assigned to you."
        lines = []
        for issue in issues:
            bits = [f"{issue['identifier']}: {issue['title']}"]
            bits.append(f"state {issue['state']['name']}")
            bits.append(f"priority {issue['priorityLabel']}")
            if issue.get("dueDate"):
                bits.append(f"due {issue['dueDate']}")
            lines.append(" | ".join(bits))
        return "\n".join(lines)

    def set_priority(self, identifier: str, priority: str) -> str:
        value = PRIORITY_TO_INT.get(priority.strip().lower())
        if value is None:
            return f"Error: unknown priority {priority!r}."
        issue = self.linear.resolve_issue(identifier)
        updated = self.linear.update_issue(issue["id"], {"priority": value})
        return f"{updated['identifier']} priority is now {INT_TO_PRIORITY[value]}."

    def set_due_date(self, identifier: str, date: str) -> str:
        issue = self.linear.resolve_issue(identifier)
        value = date.strip() or None
        updated = self.linear.update_issue(issue["id"], {"dueDate": value})
        if value is None:
            return f"{updated['identifier']} no longer has a due date."
        return f"{updated['identifier']} is now due {updated.get('dueDate') or value}."

    def set_state(self, identifier: str, state: str) -> str:
        issue = self.linear.resolve_issue(identifier)
        states = self.linear.team_states(issue["team"]["id"])
        wanted = state.strip().lower()

        match = next((s for s in states if s["name"].lower() == wanted), None)
        if match is None:
            match = next((s for s in states if wanted in s["name"].lower()), None)
        if match is None:
            available = ", ".join(s["name"] for s in states)
            return f"No state matching {state!r} on team {issue['team']['key']}. Available: {available}."

        updated = self.linear.update_issue(issue["id"], {"stateId": match["id"]})
        return f"{updated['identifier']} moved to {updated['state']['name']}."

    def add_comment(self, identifier: str, text: str) -> str:
        issue = self.linear.resolve_issue(identifier)
        self.linear.add_comment(issue["id"], text)
        return f"Comment added to {issue['identifier']}."

    def create_issue(
        self,
        title: str,
        team: str | None = None,
        description: str | None = None,
        priority: str | None = None,
    ) -> str:
        teams = self.linear.teams()
        if not teams:
            return "You have no Linear teams."

        if team:
            wanted = team.strip().lower()
            chosen = next(
                (t for t in teams if t["key"].lower() == wanted or t["name"].lower() == wanted),
                None,
            )
            if chosen is None:
                available = ", ".join(f"{t['key']} ({t['name']})" for t in teams)
                return f"No team matching {team!r}. Available: {available}."
        elif len(teams) == 1:
            chosen = teams[0]
        else:
            available = ", ".join(f"{t['key']} ({t['name']})" for t in teams)
            return f"Which team? Available: {available}."

        priority_value = PRIORITY_TO_INT.get((priority or "").strip().lower())
        created = self.linear.create_issue(
            team_id=chosen["id"],
            title=title,
            description=description,
            priority=priority_value,
        )
        return f"Created {created['identifier']}: {created['title']} in {chosen['key']}."

    # -- weather ------------------------------------------------------------

    def _geocode(self, location: str) -> dict[str, Any] | None:
        """Place name -> coordinates, cached.

        People ask about the same two or three places forever, and this is a
        whole network round trip inside the latency budget.
        """
        key = location.strip().lower()
        if key in self._geo_cache:
            return self._geo_cache[key]

        response = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        place = results[0] if results else None
        self._geo_cache[key] = place
        return place

    def get_weather(self, location: str, days: int = 1) -> str:
        days = max(1, min(int(days), 7))

        place = self._geocode(location)
        if place is None:
            return f"Could not find a place called {location!r}."

        forecast = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max,weather_code",
                "timezone": "auto",
                "forecast_days": days,
            },
            timeout=self.timeout,
        )
        forecast.raise_for_status()
        data = forecast.json()

        current = data["current"]
        daily = data["daily"]
        name = ", ".join(p for p in [place.get("name"), place.get("country")] if p)

        lines = [
            f"{name}: currently {round(current['temperature_2m'])}\u00b0C "
            f"(feels like {round(current['apparent_temperature'])}\u00b0C), "
            f"{WEATHER_CODES.get(current['weather_code'], 'unclear conditions')}, "
            f"wind {round(current['wind_speed_10m'])} km/h."
        ]

        for index, date in enumerate(daily["time"]):
            label = _day_label(date, index)
            lines.append(
                f"{label}: {round(daily['temperature_2m_min'][index])} to "
                f"{round(daily['temperature_2m_max'][index])}\u00b0C, "
                f"{WEATHER_CODES.get(daily['weather_code'][index], 'unclear')}, "
                f"{daily['precipitation_probability_max'][index]}% chance of precipitation."
            )

        return "\n".join(lines)


def _day_label(iso_date: str, index: int) -> str:
    if index == 0:
        return "Today"
    if index == 1:
        return "Tomorrow"
    return _dt.date.fromisoformat(iso_date).strftime("%A")


LINEAR_TOOL_NAMES = {
    "get_my_issues",
    "set_priority",
    "set_due_date",
    "set_state",
    "add_comment",
    "create_issue",
}


def linear_configured() -> bool:
    return bool(os.environ.get("LINEAR_API_KEY", "").strip())


def tool_specs(include_linear: bool | None = None) -> list[ToolSpec]:
    """The tools to offer the model.

    Linear tools are withheld unless LINEAR_API_KEY is set — offering a tool
    that is guaranteed to fail just teaches the model to apologise. Pass
    include_linear explicitly to override the auto-detection.
    """
    if include_linear is None:
        include_linear = linear_configured()
    if include_linear:
        return list(TOOL_SPECS)
    return [s for s in TOOL_SPECS if s.name not in LINEAR_TOOL_NAMES]
