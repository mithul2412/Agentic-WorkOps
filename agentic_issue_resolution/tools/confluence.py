from __future__ import annotations

import hashlib
import html
import os
from typing import Any

import requests


class MockConfluenceClient:
    def search_pages(self, query: str, max_results: int = 2) -> list[dict[str, Any]]:
        return [
            {
                "id": f"KB-{idx + 1}",
                "title": f"Mock KB match for {query}",
                "url": f"https://confluence.example.com/display/KB/{idx + 1}",
            }
            for idx in range(max_results)
        ]

    def create_draft(self, title: str, body: str) -> dict[str, Any]:
        digest = hashlib.sha1(f"{title}:{body}".encode("utf-8")).hexdigest()[:8]
        return {
            "draft_id": f"DRAFT-{digest}",
            "title": title,
            "body": body,
        }


class RealConfluenceClient:
    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        space_key: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("CONFLUENCE_BASE_URL") or "").rstrip("/")
        self.email = (email or os.getenv("CONFLUENCE_EMAIL") or "").strip()
        self.api_token = (api_token or os.getenv("CONFLUENCE_API_TOKEN") or "").strip()
        self.space_key = (space_key or os.getenv("CONFLUENCE_SPACE_KEY") or "").strip()

    def _auth(self) -> tuple[str, str]:
        if not self.email or not self.api_token:
            raise RuntimeError("CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN are required")
        return self.email, self.api_token

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("CONFLUENCE_BASE_URL is required")
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

    def search_pages(self, query: str, max_results: int = 2) -> list[dict[str, Any]]:
        safe_query = query.replace('"', '\\"')
        payload = self._request(
            "GET",
            "/rest/api/content/search",
            params={"cql": f'type=page and title~"{safe_query}"', "limit": max(1, min(max_results, 20))},
        )
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for row in rows[:max_results]:
            if not isinstance(row, dict):
                continue
            links = row.get("_links", {})
            webui = links.get("webui", "") if isinstance(links, dict) else ""
            out.append(
                {
                    "id": str(row.get("id", "")).strip(),
                    "title": str(row.get("title", "")).strip(),
                    "url": f"{self.base_url}{webui}" if webui else self.base_url,
                }
            )
        return out

    def create_draft(self, title: str, body: str) -> dict[str, Any]:
        space_key = self.space_key or self._discover_space_key()
        if not space_key:
            raise RuntimeError("Confluence space key is required (set CONFLUENCE_SPACE_KEY)")
        storage_value = f"<pre>{html.escape(body)}</pre>"
        payload = self._request(
            "POST",
            "/rest/api/content",
            json={
                "type": "page",
                "title": title,
                "status": "draft",
                "space": {"key": space_key},
                "body": {"storage": {"value": storage_value, "representation": "storage"}},
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        return {
            "draft_id": str(payload.get("id", "")).strip() or f"DRAFT-{hashlib.sha1(title.encode('utf-8')).hexdigest()[:8]}",
            "title": str(payload.get("title", title)).strip(),
            "body": body,
        }

    def _discover_space_key(self) -> str:
        payload = self._request("GET", "/rest/api/space", params={"limit": 1})
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list) or not rows:
            return ""
        first = rows[0]
        if not isinstance(first, dict):
            return ""
        return str(first.get("key", "")).strip()
