from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from agentic_issue_resolution.models.artifacts import ManagerOutput
from agentic_issue_resolution.operate.metrics import compute_cost_proxy
from agentic_issue_resolution.operate.policy_registry import PolicyDefinition, PolicyRegistry
from agentic_issue_resolution.tools.llm_provider import (
    GeminiDirectClient,
    GroqClient,
    LLMRequest,
    OpenRouterClient,
)


SYSTEM_PROMPT = """You are a triage manager model.
Return ONLY one strict JSON object with no markdown and no extra text.
Required schema:
{
  "decision": "ASK_FOR_INFO" | "READY_TO_PATCH",
  "ticket_type": "bug" | "feature_insert" | "feature_update",
  "risk_tier": "low" | "medium" | "high",
  "summary": string,
  "error_signature": string,
  "suspected_components": string[],
  "questions_needed": string[],
  "coding_brief": {
    "suspected_files": string[],
    "hypothesis": string,
    "acceptance_criteria": string[]
  }
}
Rules:
- If decision == READY_TO_PATCH, questions_needed must be [].
- If decision == ASK_FOR_INFO, questions_needed must contain at least 1 item.
- Always include all keys.
"""


@dataclass
class PolicyRunResult:
    policy_id: str
    output: dict[str, Any]
    raw_output: str
    metrics: dict[str, Any]
    meta: dict[str, Any]


class ManagerPolicyExecutor:
    def __init__(self, registry: PolicyRegistry):
        self.registry = registry

    def run_policy(
        self,
        policy_id: str,
        ticket_payload: dict[str, Any],
        evidence: list[dict[str, Any]],
        repo_file_candidates: list[str] | None,
    ) -> PolicyRunResult:
        _ = repo_file_candidates
        policy = self.registry.get_policy(policy_id)
        input_text = json.dumps(
            {"ticket_payload": ticket_payload, "evidence": evidence},
            ensure_ascii=False,
        )
        start = time.perf_counter()
        attempts = 0
        violations = 0
        output: dict[str, Any] | None = None
        raw_output = ""
        mode = "unknown"
        reason: str | None = None

        for _ in range(policy.max_attempts):
            attempts += 1
            try:
                provider = policy.provider.strip().lower()
                policy_type = policy.type.strip().lower()
                if provider not in {"openrouter", "groq", "gemini"} and policy_type not in {"sota_api", "llm_api"}:
                    raise RuntimeError(
                        f"unsupported manager policy for API-only runtime: policy={policy.id} provider={provider} type={policy_type}"
                    )
                parsed, meta, raw = self._run_api_policy(policy, ticket_payload, evidence)
                output = parsed.model_dump(mode="json")
                raw_output = raw
                mode = meta.get("mode", policy.type)
                reason = meta.get("reason")
                break
            except Exception as exc:  # noqa: BLE001
                reason = str(exc)
                violations += 1

        if output is None:
            raise RuntimeError(
                f"manager policy '{policy.id}' failed after {attempts} attempts; last_error={reason or 'unknown'}"
            )

        runtime_ms = int((time.perf_counter() - start) * 1000)
        schema_pass_rate = 1.0
        tool_correctness = 1.0 if mode.startswith("sota_api") else 0.95
        output_text = raw_output or json.dumps(output, ensure_ascii=False)
        cost_proxy = compute_cost_proxy(
            input_text=input_text,
            output_text=output_text,
            input_cost_coef=policy.input_cost_coef,
            output_cost_coef=policy.output_cost_coef,
        )
        metrics = {
            "schema_pass_rate": schema_pass_rate,
            "tool_correctness": round(tool_correctness, 4),
            "violations": violations,
            "attempts": attempts,
            "runtime_ms": runtime_ms,
            "cost_proxy": cost_proxy,
        }
        return PolicyRunResult(
            policy_id=policy_id,
            output=output,
            raw_output=output_text,
            metrics=metrics,
            meta={"mode": mode, "reason": reason},
        )

    def _run_api_policy(
        self,
        policy: PolicyDefinition,
        ticket_payload: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> tuple[ManagerOutput, dict[str, Any], str]:
        provider = policy.provider.strip().lower() or "openrouter"
        model = self._resolve_model(provider=provider, model_env=policy.model_env, default=policy.model_default)
        if not model:
            raise RuntimeError(f"missing model configuration for policy {policy.id}")

        prompt_payload = {
            "ticket_id": ticket_payload.get("ticket_id", ""),
            "project": ticket_payload.get("project", ""),
            "title": ticket_payload.get("title", ""),
            "description": ticket_payload.get("description", ""),
            "comments": ticket_payload.get("comments", []),
            "source": ticket_payload.get("source", "jira"),
            "labels": ticket_payload.get("labels", []),
            "mode": ticket_payload.get("mode", "incident"),
            "team_profile": ticket_payload.get("team_profile", "platform"),
            "evidence": evidence[:5],
        }
        client = self._provider_client(policy=policy, provider=provider, model=model)
        response = client.chat(
            LLMRequest(
                system=SYSTEM_PROMPT,
                user=json.dumps(prompt_payload, ensure_ascii=False),
                temperature=policy.temperature,
                max_tokens=policy.max_new_tokens,
                expect_json=True,
            ),
            model=model,
        )
        raw = response.text
        parsed = ManagerOutput.model_validate(json.loads(self._extract_first_json(raw)))
        reason = (
            f"provider={response.provider};model={response.model};latency_ms={response.latency_ms};policy={policy.id}"
        )
        return parsed, {"mode": "sota_api", "reason": reason}, raw

    def _provider_client(self, policy: PolicyDefinition, provider: str, model: str):
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

        raise RuntimeError(f"unsupported manager policy provider: {provider}")

    def _resolve_model(self, provider: str, model_env: str | None, default: str) -> str:
        primary = os.getenv(model_env or "", "").strip() if model_env else ""
        if primary:
            return primary
        fallback_envs = {
            "openrouter": ("MANAGER_OPENROUTER_MODEL", "OPENROUTER_MODEL"),
            "groq": ("MANAGER_GROQ_MODEL", "GROQ_MODEL"),
            "gemini": ("MANAGER_GEMINI_MODEL", "GEMINI_MODEL"),
        }.get(provider, ())
        for env_name in fallback_envs:
            value = os.getenv(env_name, "").strip()
            if value:
                return value
        return default.strip()

    def _extract_first_json(self, text: str) -> str:
        start = text.find("{")
        if start < 0:
            raise ValueError("no JSON object found in model response")
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
        raise ValueError("incomplete JSON payload from model response")
