from __future__ import annotations

import json
import os
from typing import Any

from agentic_issue_resolution.models.operate import OperateJudgeRequest, OperateJudgeResponse
from agentic_issue_resolution.operate.policy_registry import PolicyRegistry
from agentic_issue_resolution.storage.sqlite_store import SQLiteRunStore
from agentic_issue_resolution.tools.llm_provider import (
    GeminiDirectClient,
    GroqClient,
    LLMRequest,
    OpenRouterClient,
)


class OperateJudge:
    def __init__(self, store: SQLiteRunStore, registry: PolicyRegistry):
        self.store = store
        self.registry = registry

    def run(self, request: OperateJudgeRequest) -> OperateJudgeResponse:
        ab_run = self.store.get_ab_run(request.ab_run_id)
        if not ab_run:
            raise ValueError(f"unknown ab_run_id: {request.ab_run_id}")
        items = self.store.list_ab_items(request.ab_run_id)
        existing = {row["item_id"] for row in self.store.list_judgments(request.ab_run_id)}
        min_confidence = float(os.getenv("JUDGE_MIN_CONFIDENCE_FOR_SELECTOR", "0.55"))

        a_wins = 0
        b_wins = 0
        ties = 0
        judged_count = 0
        selector_updates_applied = 0
        selector_updates_skipped_low_confidence = 0

        for item in items:
            if item["item_id"] in existing:
                continue

            preference, confidence, rationale = self._judge_item(item, request.judge_policy_id)
            category = item.get("category_actual") or item.get("category_estimate") or "unknown|unknown"
            selector_updated = confidence >= min_confidence
            self.store.add_judgment(
                ab_run_id=request.ab_run_id,
                item_id=item["item_id"],
                task_id=item["task_id"],
                category=category,
                preference=preference,
                confidence=confidence,
                rationale=rationale,
                judge_policy_id=request.judge_policy_id,
                selector_updated=selector_updated,
            )

            if preference == "A":
                a_wins += 1
                if selector_updated:
                    self.store.upsert_selector_stat(category, ab_run["policy_a_id"], wins_delta=1)
                    self.store.upsert_selector_stat(category, ab_run["policy_b_id"], losses_delta=1)
            elif preference == "B":
                b_wins += 1
                if selector_updated:
                    self.store.upsert_selector_stat(category, ab_run["policy_a_id"], losses_delta=1)
                    self.store.upsert_selector_stat(category, ab_run["policy_b_id"], wins_delta=1)
            else:
                ties += 1
                if selector_updated:
                    self.store.upsert_selector_stat(category, ab_run["policy_a_id"], ties_delta=1)
                    self.store.upsert_selector_stat(category, ab_run["policy_b_id"], ties_delta=1)

            if selector_updated:
                selector_updates_applied += 1
            else:
                selector_updates_skipped_low_confidence += 1
            judged_count += 1

        return OperateJudgeResponse(
            ab_run_id=request.ab_run_id,
            judged_count=judged_count,
            a_wins=a_wins,
            b_wins=b_wins,
            ties=ties,
            stored_preferences=judged_count,
            selector_updates_applied=selector_updates_applied,
            selector_updates_skipped_low_confidence=selector_updates_skipped_low_confidence,
            min_confidence_for_selector=min_confidence,
        )

    def _judge_item(self, item: dict[str, Any], judge_policy_id: str) -> tuple[str, float, str]:
        try:
            return self._judge_with_llm(item, judge_policy_id)
        except Exception as exc:  # noqa: BLE001
            fallback_pref, fallback_conf, fallback_reason = self._judge_deterministic(item)
            return (
                fallback_pref,
                fallback_conf,
                f"llm_judge_fallback={type(exc).__name__}: {exc}; {fallback_reason}",
            )

    def _judge_with_llm(self, item: dict[str, Any], judge_policy_id: str) -> tuple[str, float, str]:
        policy = self.registry.get_policy(judge_policy_id)
        provider = policy.provider.strip().lower() or "openrouter"
        if policy.type not in {"sota_api", "llm_api"} and provider not in {"openrouter", "groq", "gemini"}:
            raise RuntimeError("judge policy is not an API policy")

        model = self._resolve_model(provider=provider, model_env=policy.model_env, default=policy.model_default)
        if not model:
            raise RuntimeError(f"judge model is not configured for policy {policy.id}")

        client = self._provider_client(policy=policy, provider=provider, model=model)
        system = (
            "You are an evaluator. Compare two manager outputs and pick A, B, or TIE.\n"
            "Consider schema quality, safety, clarity, and lower violation/risk.\n"
            "Return JSON only with schema: "
            "{\"preference\":\"A|B|TIE\",\"confidence\":number,\"rationale\":string}"
        )
        user_payload = {
            "task_context": item.get("task_context", {}),
            "output_a": item.get("output_a", {}),
            "output_b": item.get("output_b", {}),
            "metrics_a": item.get("metrics_a", {}),
            "metrics_b": item.get("metrics_b", {}),
        }
        response = client.chat(
            LLMRequest(
                system=system,
                user=json.dumps(user_payload, ensure_ascii=False),
                temperature=self.registry.judge.temperature,
                max_tokens=self.registry.judge.max_tokens,
                expect_json=True,
            ),
            model=model,
        )
        payload = json.loads(self._extract_first_json(response.text))
        preference = str(payload.get("preference", "TIE")).strip().upper()
        if preference not in {"A", "B", "TIE"}:
            preference = "TIE"
        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.5))))
        rationale = str(payload.get("rationale", "LLM judge comparison"))
        rationale = (
            f"{rationale} [provider={response.provider};model={response.model};latency_ms={response.latency_ms}]"
        )
        return preference, confidence, rationale

    def _provider_client(self, policy, provider: str, model: str):
        if provider == "openrouter":
            api_key = os.getenv(policy.api_key_env or "OPENROUTER_API_KEY", "").strip()
            base_url = os.getenv(policy.base_url_env or "OPENROUTER_BASE_URL", "").strip() or None
            return OpenRouterClient(api_key=api_key, default_model=model, base_url=base_url)

        if provider == "groq":
            api_key = os.getenv(policy.api_key_env or "GROQ_API_KEY", "").strip()
            base_url = os.getenv(policy.base_url_env or "GROQ_BASE_URL", "").strip() or None
            return GroqClient(api_key=api_key, default_model=model, base_url=base_url)

        if provider == "gemini":
            api_key = os.getenv(policy.api_key_env or "GEMINI_API_KEY", "").strip()
            base_url = os.getenv(policy.base_url_env or "GEMINI_API_BASE", "").strip() or None
            return GeminiDirectClient(api_key=api_key, default_model=model, base_url=base_url)

        raise RuntimeError(f"unsupported judge provider: {provider}")

    def _resolve_model(self, provider: str, model_env: str | None, default: str) -> str:
        primary = os.getenv(model_env or "", "").strip() if model_env else ""
        if primary:
            return primary
        fallback_envs = {
            "openrouter": ("JUDGE_OPENROUTER_MODEL", "OPENROUTER_MODEL"),
            "groq": ("JUDGE_GROQ_MODEL", "GROQ_MODEL"),
            "gemini": ("JUDGE_GEMINI_MODEL", "GEMINI_MODEL"),
        }.get(provider, ())
        for env_name in fallback_envs:
            value = os.getenv(env_name, "").strip()
            if value:
                return value
        return default.strip()

    def _judge_deterministic(self, item: dict[str, Any]) -> tuple[str, float, str]:
        score_a = self._score_metrics(item.get("metrics_a", {}))
        score_b = self._score_metrics(item.get("metrics_b", {}))
        delta = score_a - score_b
        if abs(delta) < 0.05:
            return "TIE", 0.5, "Scores too close; deterministic tie"
        if delta > 0:
            return "A", min(1.0, 0.5 + abs(delta)), "Policy A scored higher on deterministic metrics"
        return "B", min(1.0, 0.5 + abs(delta)), "Policy B scored higher on deterministic metrics"

    def _score_metrics(self, metrics: dict[str, Any]) -> float:
        schema = float(metrics.get("schema_pass_rate", 0.0))
        tool = float(metrics.get("tool_correctness", 0.0))
        violations = float(metrics.get("violations", 0.0))
        attempts = float(metrics.get("attempts", 1.0))
        runtime = float(metrics.get("runtime_ms", 0.0))
        cost = float(metrics.get("cost_proxy", 0.0))
        return (schema * 0.45) + (tool * 0.35) - (violations * 0.08) - (attempts * 0.02) - (runtime * 0.00001) - (
            cost * 0.10
        )

    def _extract_first_json(self, text: str) -> str:
        start = text.find("{")
        if start < 0:
            raise ValueError("no JSON object found in judge response")
        depth = 0
        in_string = False
        escape = False
        for idx, ch in enumerate(text[start:], start=start):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == "\"":
                    in_string = False
                continue
            if ch == "\"":
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]
        raise ValueError("incomplete JSON object from judge response")
