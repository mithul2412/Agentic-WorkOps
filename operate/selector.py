from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Any

from operate.policy_registry import PolicyRegistry
from storage.sqlite_store import SQLiteRunStore


@dataclass(frozen=True)
class SelectionDecision:
    policy_id: str
    explored: bool
    epsilon: float
    reason: str


class ManagerPolicySelector:
    def __init__(
        self,
        store: SQLiteRunStore,
        registry: PolicyRegistry,
    ) -> None:
        self.store = store
        self.registry = registry

    def choose_policy(self, category: str, candidate_policy_ids: list[str] | None = None) -> SelectionDecision:
        selector = self.registry.selector
        candidates = candidate_policy_ids or list(selector.candidate_policy_ids)
        if not candidates:
            candidates = [selector.default_policy_id]
        candidates = [
            item
            for item in candidates
            if self.registry.has_policy(item) and self._policy_available(item)
        ]
        if not candidates:
            return SelectionDecision(
                policy_id=selector.default_policy_id,
                explored=False,
                epsilon=selector.epsilon,
                reason="no valid candidates; using default policy",
            )

        stats = self.store.get_selector_stats_for_category(category)
        filtered = [row for row in stats if row.get("policy_id") in candidates]
        best_policy = selector.default_policy_id
        if filtered:
            filtered.sort(key=lambda row: (float(row.get("win_rate", 0.0)), int(row.get("total", 0))), reverse=True)
            top = filtered[0]
            if int(top.get("total", 0)) >= selector.min_samples:
                best_policy = str(top.get("policy_id"))
            else:
                best_policy = selector.default_policy_id

        explore = random.random() < selector.epsilon and len(candidates) > 1
        if explore:
            pool = [item for item in candidates if item != best_policy] or candidates
            chosen = random.choice(pool)
            return SelectionDecision(
                policy_id=chosen,
                explored=True,
                epsilon=selector.epsilon,
                reason=f"epsilon exploration in category={category}",
            )

        return SelectionDecision(
            policy_id=best_policy if best_policy in candidates else candidates[0],
            explored=False,
            epsilon=selector.epsilon,
            reason=f"exploit best policy for category={category}",
        )

    def selector_view(self, min_samples: int | None = None) -> dict[str, Any]:
        selector = self.registry.selector
        threshold = min_samples if min_samples is not None else selector.min_samples
        rows = self.store.get_selector_stats(min_samples=threshold)
        best_map: dict[str, str] = {}
        for row in rows:
            category = str(row.get("category", ""))
            policy_id = str(row.get("policy_id", ""))
            if not category or not policy_id:
                continue
            existing = best_map.get(category)
            if not existing:
                best_map[category] = policy_id
                continue
            existing_row = next((item for item in rows if item["category"] == category and item["policy_id"] == existing), None)
            if existing_row is None:
                best_map[category] = policy_id
                continue
            if float(row.get("win_rate", 0.0)) > float(existing_row.get("win_rate", 0.0)):
                best_map[category] = policy_id

        out_rows: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["best_policy"] = bool(best_map.get(item["category"]) == item["policy_id"])
            out_rows.append(item)
        return {
            "category_key": selector.category_key,
            "min_samples": threshold,
            "default_policy_id": selector.default_policy_id,
            "epsilon": selector.epsilon,
            "rows": out_rows,
        }

    def _policy_available(self, policy_id: str) -> bool:
        policy = self.registry.get_policy(policy_id)
        provider = policy.provider.strip().lower()
        if provider == "openrouter":
            key_env = policy.api_key_env or "OPENROUTER_API_KEY"
            return bool(os.getenv(key_env, "").strip())
        if provider in {"groq", "grok"}:
            key_env = policy.api_key_env or "GROQ_API_KEY"
            return bool(os.getenv(key_env, "").strip())
        if provider == "gemini":
            key_env = policy.api_key_env or "GEMINI_API_KEY"
            return bool(os.getenv(key_env, "").strip()) or bool(os.getenv("GOOGLE_API_KEY", "").strip())
        if provider == "ollama":
            if policy.model_env and os.getenv(policy.model_env, "").strip():
                return True
            return bool((policy.model_default or "").strip())
        return False
