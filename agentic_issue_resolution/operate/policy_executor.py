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
    OllamaClient,
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

OLLAMA_SFT_SYSTEM_PROMPT = """You are a ticket triage manager model.
Return ONLY one strict JSON object with no markdown and no extra text.
Output schema (all keys always present):
{
  "decision": "ASK_FOR_INFO" | "READY_TO_PATCH",
  "ticket_type": "bug" | "feature_update" | "feature_insert",
  "risk_tier": "low" | "medium" | "high",
  "ask_for_info_questions": string[],
  "ready_to_patch_acceptance_criteria": string[]
}
Rules:
- If decision == ASK_FOR_INFO, ready_to_patch_acceptance_criteria must be [].
- If decision == READY_TO_PATCH, ask_for_info_questions must be [].
- Never omit keys.
"""

VALID_DECISIONS = {"ASK_FOR_INFO", "READY_TO_PATCH"}
VALID_TICKET_TYPES = {"bug", "feature_update", "feature_insert"}
VALID_RISK_TIERS = {"low", "medium", "high"}

DEFAULT_ASK_FOR_INFO_QUESTIONS = [
    "What is the expected behavior and what is the actual behavior observed?",
    "Please provide environment details (OS, runtime, and relevant package versions).",
    "Can you attach relevant logs or a stack trace from the failing run?",
]
DEFAULT_READY_ACCEPTANCE = [
    "The issue no longer reproduces after the patch with a clear validation path.",
    "A regression test or equivalent automated check covers the fix behavior.",
]


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
                if provider not in {"openrouter", "groq", "gemini", "ollama"} and policy_type not in {
                    "sota_api",
                    "llm_api",
                }:
                    raise RuntimeError(
                        f"unsupported manager policy provider: policy={policy.id} provider={provider} type={policy_type}"
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

        if provider == "ollama":
            prompt_payload = {
                "title": self._clean_text(ticket_payload.get("title", "")),
                "description": self._clean_text(ticket_payload.get("description", "")),
                "comments": self._normalize_string_list(ticket_payload.get("comments", [])),
            }
            system_prompt = OLLAMA_SFT_SYSTEM_PROMPT
        else:
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
            system_prompt = SYSTEM_PROMPT

        client = self._provider_client(policy=policy, provider=provider, model=model)
        response = client.chat(
            LLMRequest(
                system=system_prompt,
                user=json.dumps(prompt_payload, ensure_ascii=False),
                temperature=policy.temperature,
                max_tokens=policy.max_new_tokens,
                expect_json=True,
            ),
            model=model,
        )
        raw = response.text
        extracted = json.loads(self._extract_first_json(raw))
        if provider == "ollama":
            normalized = self._normalize_ollama_output(raw_output=extracted, ticket_payload=ticket_payload)
            parsed = ManagerOutput.model_validate(normalized)
        else:
            parsed = ManagerOutput.model_validate(extracted)

        mode = "local_ollama" if provider == "ollama" else "sota_api"
        reason = (
            f"provider={response.provider};model={response.model};latency_ms={response.latency_ms};policy={policy.id}"
        )
        return parsed, {"mode": mode, "reason": reason}, raw

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

        if provider == "ollama":
            base_url = os.getenv(policy.base_url_env or "OLLAMA_BASE_URL", "").strip() or None
            return OllamaClient(default_model=model, base_url=base_url)

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

    def _normalize_ollama_output(
        self,
        raw_output: dict[str, Any],
        ticket_payload: dict[str, Any],
    ) -> dict[str, Any]:
        decision = self._normalize_decision(raw_output.get("decision"))
        ticket_type = self._normalize_ticket_type(raw_output)
        risk_tier = self._normalize_risk_tier(raw_output.get("risk_tier"))

        ask_questions = self._normalize_string_list(raw_output.get("ask_for_info_questions"))
        if not ask_questions:
            ask_questions = self._normalize_string_list(raw_output.get("questions_needed"))

        ready_acceptance = self._normalize_string_list(raw_output.get("ready_to_patch_acceptance_criteria"))
        coding_brief = raw_output.get("coding_brief")
        if not ready_acceptance and isinstance(coding_brief, dict):
            ready_acceptance = self._normalize_string_list(coding_brief.get("acceptance_criteria"))

        if decision == "ASK_FOR_INFO":
            ready_acceptance = []
            if not ask_questions:
                ask_questions = list(DEFAULT_ASK_FOR_INFO_QUESTIONS)
        else:
            ask_questions = []
            if not ready_acceptance:
                ready_acceptance = list(DEFAULT_READY_ACCEPTANCE)

        summary = self._clean_text(raw_output.get("summary"))
        if not summary:
            summary = self._derive_summary(ticket_payload)

        error_signature = self._clean_text(raw_output.get("error_signature")) or "NONE_PROVIDED"
        suspected_components = self._normalize_string_list(raw_output.get("suspected_components"))

        suspected_files: list[str] = []
        hypothesis = ""
        if isinstance(coding_brief, dict):
            suspected_files = self._normalize_string_list(coding_brief.get("suspected_files"))
            hypothesis = self._clean_text(coding_brief.get("hypothesis"))
        if not hypothesis:
            hypothesis = self._derive_hypothesis(ticket_payload)

        return {
            "decision": decision,
            "ticket_type": ticket_type,
            "risk_tier": risk_tier,
            "summary": summary,
            "error_signature": error_signature,
            "suspected_components": suspected_components,
            "questions_needed": ask_questions,
            "coding_brief": {
                "suspected_files": suspected_files,
                "hypothesis": hypothesis,
                "acceptance_criteria": ready_acceptance,
            },
        }

    def _normalize_decision(self, value: Any) -> str:
        text = self._clean_text(value).upper()
        return text if text in VALID_DECISIONS else "ASK_FOR_INFO"

    def _normalize_ticket_type(self, raw_output: dict[str, Any]) -> str:
        text = self._clean_text(raw_output.get("ticket_type")).lower()
        if text in VALID_TICKET_TYPES:
            return text

        feature_insert = raw_output.get("feature_insert")
        feature_update = raw_output.get("feature_update")
        if self._truthy_signal(feature_insert):
            return "feature_insert"
        if self._truthy_signal(feature_update):
            return "feature_update"
        return "bug"

    def _normalize_risk_tier(self, value: Any) -> str:
        text = self._clean_text(value).lower()
        return text if text in VALID_RISK_TIERS else "low"

    def _normalize_string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [self._clean_text(item) for item in value if self._clean_text(item)]
        if isinstance(value, tuple):
            return [self._clean_text(item) for item in value if self._clean_text(item)]
        if isinstance(value, str):
            text = self._clean_text(value)
            if not text:
                return []
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        return [self._clean_text(item) for item in parsed if self._clean_text(item)]
                except Exception:
                    pass
            return [text]
        return []

    def _derive_summary(self, ticket_payload: dict[str, Any]) -> str:
        title = self._clean_text(ticket_payload.get("title", ""))
        description = self._clean_text(ticket_payload.get("description", ""))
        if title:
            return title[:200]
        if description:
            return description[:200]
        return "Ticket triage summary generated from available context."

    def _derive_hypothesis(self, ticket_payload: dict[str, Any]) -> str:
        title = self._clean_text(ticket_payload.get("title", ""))
        if title:
            return f"Likely localized issue related to: {title[:120]}."
        return "Likely localized issue based on provided ticket context."

    def _clean_text(self, value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        return text

    def _truthy_signal(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized not in {"", "none", "null", "false", "0"}
        if isinstance(value, (list, tuple, dict)):
            return len(value) > 0
        return True

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
