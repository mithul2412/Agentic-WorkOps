from __future__ import annotations

import hashlib
import os
from typing import Any

import requests


class MockBitbucketClient:
    def search_issues(self, query: str, max_results: int = 3) -> list[dict[str, Any]]:
        base = hashlib.sha1(query.encode("utf-8")).hexdigest()[:6]
        out: list[dict[str, Any]] = []
        for idx in range(max_results):
            issue_id = f"BB-{base}-{idx + 1}"
            out.append(
                {
                    "id": issue_id,
                    "title": f"Related Bitbucket issue for '{query}' #{idx + 1}",
                    "url": f"https://bitbucket.org/example/repo/issues/{idx + 101}",
                    "snippet": f"Mock Bitbucket issue evidence {idx + 1} for query: {query}",
                }
            )
        return out

    def create_pr(self, title: str, body: str, diff: str) -> dict[str, Any]:
        digest = hashlib.sha1(f"{title}:{diff}".encode("utf-8")).hexdigest()[:6]
        pr_number = int(digest, 16) % 9000 + 1000
        return {
            "pr_number": pr_number,
            "url": f"https://bitbucket.org/example/repo/pull-requests/{pr_number}",
            "title": title,
            "body": body,
        }


class RealBitbucketClient:
    def __init__(
        self,
        api_base: str | None = None,
        workspace: str | None = None,
        repo_slug: str | None = None,
        api_token: str | None = None,
        username: str | None = None,
        app_password: str | None = None,
        source_branch: str | None = None,
        destination_branch: str | None = None,
    ) -> None:
        self.api_base = (api_base or os.getenv("BITBUCKET_API_BASE") or "https://api.bitbucket.org/2.0").rstrip("/")
        self.workspace = (workspace or os.getenv("BITBUCKET_WORKSPACE") or "").strip()
        self.repo_slug = (repo_slug or os.getenv("BITBUCKET_REPO_SLUG") or "").strip()
        self.api_token = (api_token or os.getenv("BITBUCKET_API_TOKEN") or "").strip()
        self.username = (username or os.getenv("BITBUCKET_USERNAME") or "").strip()
        self.app_password = (app_password or os.getenv("BITBUCKET_APP_PASSWORD") or "").strip()
        self.source_branch = (
            source_branch
            or os.getenv("BITBUCKET_SOURCE_BRANCH")
            or os.getenv("BITBUCKET_HEAD_BRANCH")
            or ""
        ).strip()
        self.destination_branch = (
            destination_branch
            or os.getenv("BITBUCKET_DESTINATION_BRANCH")
            or os.getenv("BITBUCKET_BASE_BRANCH")
            or "main"
        ).strip()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _auth(self) -> tuple[str, str] | None:
        if self.api_token:
            return None
        if self.username and self.app_password:
            return self.username, self.app_password
        raise RuntimeError(
            "Bitbucket auth not configured. Set BITBUCKET_API_TOKEN or BITBUCKET_USERNAME + BITBUCKET_APP_PASSWORD."
        )

    def _require_repo(self) -> None:
        if not self.workspace or not self.repo_slug:
            raise RuntimeError("BITBUCKET_WORKSPACE and BITBUCKET_REPO_SLUG are required")

    def search_issues(self, query: str, max_results: int = 3) -> list[dict[str, Any]]:
        self._require_repo()
        safe_query = query.replace('"', '\\"')
        try:
            response = requests.get(
                f"{self.api_base}/repositories/{self.workspace}/{self.repo_slug}/issues",
                headers=self._headers(),
                auth=self._auth(),
                params={"q": f'title ~ "{safe_query}"', "pagelen": max(1, min(max_results, 20))},
                timeout=45,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            # Some workspaces disable Issues API. Continue without this evidence.
            if status in {400, 401, 403, 404}:
                return []
            raise
        rows = response.json().get("values", [])
        if not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for item in rows[:max_results]:
            if not isinstance(item, dict):
                continue
            links = item.get("links", {})
            html_link = links.get("html", {}) if isinstance(links, dict) else {}
            url = html_link.get("href", "") if isinstance(html_link, dict) else ""
            out.append(
                {
                    "id": str(item.get("id", item.get("local_id", ""))),
                    "title": str(item.get("title", "")).strip(),
                    "url": str(url).strip(),
                    "snippet": str(item.get("content", {}).get("raw", "") if isinstance(item.get("content"), dict) else "")[
                        :500
                    ],
                }
            )
        return out

    def create_pr(self, title: str, body: str, diff: str) -> dict[str, Any]:
        self._require_repo()
        if not self.source_branch:
            raise RuntimeError("BITBUCKET_SOURCE_BRANCH (or BITBUCKET_HEAD_BRANCH) is required to create PR")
        preview = diff.strip()
        if len(preview) > 5000:
            preview = preview[:5000] + "\n...[truncated]"
        description = body.strip() + "\n\n---\nPatch preview:\n```diff\n" + preview + "\n```"
        response = requests.post(
            f"{self.api_base}/repositories/{self.workspace}/{self.repo_slug}/pullrequests",
            headers={**self._headers(), "Content-Type": "application/json"},
            auth=self._auth(),
            json={
                "title": title,
                "description": description,
                "source": {"branch": {"name": self.source_branch}},
                "destination": {"branch": {"name": self.destination_branch}},
                "close_source_branch": False,
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        links = payload.get("links", {})
        html_link = links.get("html", {}) if isinstance(links, dict) else {}
        url = html_link.get("href", "") if isinstance(html_link, dict) else ""
        return {
            "pr_number": int(payload.get("id", 0)),
            "url": str(url).strip(),
            "title": str(payload.get("title", title)).strip(),
            "body": str(payload.get("description", description)),
        }
