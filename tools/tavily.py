from __future__ import annotations

import os
from typing import Any

import requests


class MockTavilyClient:
    def search(self, query: str, max_results: int = 2) -> list[dict[str, Any]]:
        return [
            {
                "id": f"WEB-{idx + 1}",
                "title": f"Mock web result {idx + 1} for {query}",
                "url": f"https://example.com/search?q={query.replace(' ', '+')}&r={idx + 1}",
                "snippet": f"Web evidence snippet {idx + 1} for '{query}'.",
            }
            for idx in range(max_results)
        ]


class RealTavilyClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("TAVILY_API_KEY") or "").strip()
        self.base_url = (base_url or os.getenv("TAVILY_BASE_URL") or "https://api.tavily.com").rstrip("/")

    def search(self, query: str, max_results: int = 2) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("TAVILY_API_KEY is required for real Tavily integration")
        response = requests.post(
            f"{self.base_url}/search",
            json={
                "api_key": self.api_key,
                "query": query,
                "max_results": max(1, min(max_results, 10)),
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for idx, item in enumerate(rows[:max_results], start=1):
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "id": str(item.get("url") or f"TAVILY-{idx}"),
                    "title": str(item.get("title", "")).strip() or f"Tavily result {idx}",
                    "url": str(item.get("url", "")).strip(),
                    "snippet": str(item.get("content", "")).strip()[:600],
                }
            )
        return out
