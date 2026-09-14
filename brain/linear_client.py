"""Thin Linear GraphQL client.

Only the operations the assistant needs. Everything is resolved by human
identifier (ENG-142) rather than UUID, because that is what a voice
conversation can actually say out loud.

Verified against Linear's GraphQL API, September 2026:
  - auth header is the raw personal API key, no "Bearer " prefix
  - priority is an integer: 0 none, 1 urgent, 2 high, 3 medium, 4 low
  - dueDate is a TimelessDate, "YYYY-MM-DD"
  - commentCreate wants the issue UUID, not the identifier
"""

from __future__ import annotations

import os
import re
from typing import Any

import requests

ENDPOINT = "https://api.linear.app/graphql"

PRIORITY_TO_INT = {
    "none": 0, "no priority": 0, "clear": 0,
    "urgent": 1,
    "high": 2,
    "medium": 3, "normal": 3,
    "low": 4,
}
INT_TO_PRIORITY = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}

OPEN_STATE_TYPES = ["backlog", "unstarted", "started"]
DONE_STATE_TYPES = ["completed", "canceled"]

_IDENTIFIER = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*-\s*(\d+)\s*$")


class LinearError(RuntimeError):
    pass


class Linear:
    def __init__(self, api_key: str | None = None, timeout: float = 20.0):
        key = api_key or os.environ.get("LINEAR_API_KEY")
        if not key:
            raise LinearError("LINEAR_API_KEY is not set")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": key, "Content-Type": "application/json"}
        )

    # -- transport ----------------------------------------------------------

    def query(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._session.post(
            ENDPOINT,
            json={"query": document, "variables": variables or {}},
            timeout=self.timeout,
        )
        if response.status_code == 401:
            raise LinearError("Linear rejected the API key (401)")
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            messages = "; ".join(e.get("message", str(e)) for e in payload["errors"])
            raise LinearError(f"Linear GraphQL error: {messages}")
        return payload["data"]

    # -- reads --------------------------------------------------------------

    def viewer(self) -> dict[str, Any]:
        return self.query("{ viewer { id name email } }")["viewer"]

    def assigned_issues(self, limit: int = 25, include_done: bool = False) -> list[dict[str, Any]]:
        document = """
        query($limit: Int!, $types: [String!]) {
          viewer {
            assignedIssues(
              first: $limit
              filter: { state: { type: { nin: $types } } }
              orderBy: updatedAt
            ) {
              nodes {
                id identifier title priority dueDate url
                state { name type }
                team { id key name }
              }
            }
          }
        }
        """
        types = [] if include_done else DONE_STATE_TYPES
        data = self.query(document, {"limit": limit, "types": types})
        nodes = data["viewer"]["assignedIssues"]["nodes"]
        for node in nodes:
            node["priorityLabel"] = INT_TO_PRIORITY.get(node.get("priority") or 0, "No priority")
        return nodes

    def resolve_issue(self, identifier: str) -> dict[str, Any]:
        """ENG-142 -> the issue, including its UUID and team."""
        match = _IDENTIFIER.match(identifier or "")
        if not match:
            raise LinearError(
                f"{identifier!r} is not a Linear identifier — expected something like ENG-142"
            )
        team_key, number = match.group(1).upper(), int(match.group(2))
        document = """
        query($key: String!, $number: Float!) {
          issues(filter: { team: { key: { eq: $key } }, number: { eq: $number } }, first: 1) {
            nodes {
              id identifier title priority dueDate url
              state { name type }
              team { id key name }
            }
          }
        }
        """
        nodes = self.query(document, {"key": team_key, "number": number})["issues"]["nodes"]
        if not nodes:
            raise LinearError(f"No issue {team_key}-{number}")
        return nodes[0]

    def team_states(self, team_id: str) -> list[dict[str, Any]]:
        document = """
        query($id: String!) {
          team(id: $id) { states { nodes { id name type position } } }
        }
        """
        return self.query(document, {"id": team_id})["team"]["states"]["nodes"]

    def teams(self) -> list[dict[str, Any]]:
        return self.query("{ teams { nodes { id key name } } }")["teams"]["nodes"]

    # -- writes -------------------------------------------------------------

    def update_issue(self, issue_uuid: str, payload: dict[str, Any]) -> dict[str, Any]:
        document = """
        mutation($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success
            issue { identifier title priority dueDate state { name } url }
          }
        }
        """
        result = self.query(document, {"id": issue_uuid, "input": payload})["issueUpdate"]
        if not result.get("success"):
            raise LinearError("Linear reported the update as unsuccessful")
        return result["issue"]

    def add_comment(self, issue_uuid: str, body: str) -> dict[str, Any]:
        document = """
        mutation($input: CommentCreateInput!) {
          commentCreate(input: $input) { success comment { id url } }
        }
        """
        result = self.query(document, {"input": {"issueId": issue_uuid, "body": body}})
        result = result["commentCreate"]
        if not result.get("success"):
            raise LinearError("Linear reported the comment as unsuccessful")
        return result["comment"]

    def create_issue(
        self,
        team_id: str,
        title: str,
        description: str | None = None,
        priority: int | None = None,
        assign_to_me: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"teamId": team_id, "title": title}
        if description:
            payload["description"] = description
        if priority is not None:
            payload["priority"] = priority
        if assign_to_me:
            payload["assigneeId"] = self.viewer()["id"]

        document = """
        mutation($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue { identifier title url state { name } }
          }
        }
        """
        result = self.query(document, {"input": payload})["issueCreate"]
        if not result.get("success"):
            raise LinearError("Linear reported the issue creation as unsuccessful")
        return result["issue"]
