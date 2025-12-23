from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import os
import requests


class MockJiraClient:
    def read_ticket(self, ticket_id: str, webhook_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = webhook_payload or {}
        issue = payload.get("issue", {})
        fields = issue.get("fields", {})
        return {
            "ticket_id": ticket_id,
            "summary": fields.get("summary", "Mock Jira summary"),
            "description": fields.get("description", "Mock Jira description"),
            "labels": fields.get("labels", []),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def add_comment(self, ticket_id: str, body: str) -> dict[str, Any]:
        digest = hashlib.sha1(f"{ticket_id}:{body}".encode("utf-8")).hexdigest()[:8]
        return {
            "ticket_id": ticket_id,
            "comment_id": f"{ticket_id}-c-{digest}",
            "body": body,
        }

    def transition_status(self, ticket_id: str, status: str) -> dict[str, Any]:
        return {
            "ticket_id": ticket_id,
            "status": status,
        }


class RealJiraClient:
    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("JIRA_BASE_URL") or "").rstrip("/")
        self.email = (email or os.getenv("JIRA_EMAIL") or "").strip()
        self.api_token = (api_token or os.getenv("JIRA_API_TOKEN") or "").strip()

    def _auth(self) -> tuple[str, str]:
        if not self.email or not self.api_token:
            raise RuntimeError("JIRA_EMAIL and JIRA_API_TOKEN are required for real Jira integration")
        return self.email, self.api_token

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("JIRA_BASE_URL is required for real Jira integration")
        headers = kwargs.pop("headers", {}) or {}
        headers = {"Accept": "application/json", **headers}
        response = requests.request(
            method=method,
            url=f"{self.base_url}{path}",
            auth=self._auth(),
            headers=headers,
            timeout=45,
            **kwargs,
        )
        response.raise_for_status()
        if not response.text.strip():
            return {}
        return response.json()

    def read_ticket(self, ticket_id: str, webhook_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"/rest/api/2/issue/{ticket_id}",
            params={"fields": "summary,description,labels"},
        )
        fields = payload.get("fields", {}) if isinstance(payload, dict) else {}
        summary = str(fields.get("summary", "")).strip()
        description = _jira_to_text(fields.get("description"))
        labels = fields.get("labels", [])
        if not isinstance(labels, list):
            labels = []
        return {
            "ticket_id": ticket_id,
            "summary": summary,
            "description": description,
            "labels": [str(item).strip() for item in labels if str(item).strip()],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def add_comment(self, ticket_id: str, body: str) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/rest/api/2/issue/{ticket_id}/comment",
            json={"body": body},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        comment_id = str(payload.get("id", "")).strip()
        return {
            "ticket_id": ticket_id,
            "comment_id": comment_id or f"{ticket_id}-comment",
            "body": body,
        }

    def transition_status(self, ticket_id: str, status: str) -> dict[str, Any]:
        listing = self._request("GET", f"/rest/api/2/issue/{ticket_id}/transitions")
        transitions = listing.get("transitions", []) if isinstance(listing, dict) else []
        transition_id = None
        for item in transitions:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip().lower()
            if name == status.strip().lower():
                transition_id = str(item.get("id", "")).strip()
                break
        if not transition_id:
            return {"ticket_id": ticket_id, "status": status, "transitioned": False}
        self._request(
            "POST",
            f"/rest/api/2/issue/{ticket_id}/transitions",
            json={"transition": {"id": transition_id}},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        return {"ticket_id": ticket_id, "status": status, "transitioned": True}


def _jira_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    chunks: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
            content = node.get("content")
            if isinstance(content, list):
                for child in content:
                    visit(child)
            return
        if isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return "\n".join(chunks).strip()
