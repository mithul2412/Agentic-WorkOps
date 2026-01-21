from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PolicyDefinition:
    id: str
    type: str
    provider: str = "openrouter"
    base_model: str = "Qwen/Qwen2.5-3B-Instruct"
    api_key_env: str | None = None
    base_url_env: str | None = None
    model_env: str | None = None
    model_default: str = "deepseek-chat"
    max_new_tokens: int = 280
    temperature: float = 0.0
    max_attempts: int = 1
    input_cost_coef: float = 0.001
    output_cost_coef: float = 0.002


@dataclass(frozen=True)
class SelectorSettings:
    enabled_live: bool = True
    category_key: str = "team_profile|ticket_type|risk_tier"
    default_policy_id: str = "manager_ollama_qwen25_sft_v1"
    candidate_policy_ids: tuple[str, ...] = ("manager_ollama_qwen25_sft_v1", "manager_ollama_gemma2_local_v1")
    epsilon: float = 0.10
    min_samples: int = 10


@dataclass(frozen=True)
class JudgeSettings:
    policy_id: str = "judge_groq_v1"
    max_tokens: int = 300
    temperature: float = 0.0


class PolicyRegistry:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self._raw = self._load(config_path)
        self._policies = self._parse_policies(self._raw.get("policies", []))
        self._selector = self._parse_selector(self._raw.get("selector", {}))
        self._judge = self._parse_judge(self._raw.get("judge", {}))

    def _load(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}

    def _parse_policies(self, rows: list[dict[str, Any]]) -> dict[str, PolicyDefinition]:
        policies: dict[str, PolicyDefinition] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            policy_id = str(row.get("id", "")).strip()
            if not policy_id:
                continue
            policy = PolicyDefinition(
                id=policy_id,
                type=str(row.get("type", "llm_api")).strip(),
                provider=_resolve_provider(row),
                base_model=str(row.get("base_model", "Qwen/Qwen2.5-3B-Instruct")).strip(),
                api_key_env=_opt_str(row.get("api_key_env")),
                base_url_env=_opt_str(row.get("base_url_env")),
                model_env=_opt_str(row.get("model_env")),
                model_default=str(row.get("model_default", "deepseek-chat")).strip(),
                max_new_tokens=int(row.get("max_new_tokens", 280)),
                temperature=float(row.get("temperature", 0.0)),
                max_attempts=max(1, int(row.get("max_attempts", 1))),
                input_cost_coef=float(row.get("input_cost_coef", 0.001)),
                output_cost_coef=float(row.get("output_cost_coef", 0.002)),
            )
            policies[policy_id] = policy
        return policies

    def _parse_selector(self, selector_row: dict[str, Any]) -> SelectorSettings:
        if not isinstance(selector_row, dict):
            return SelectorSettings()
        candidates_raw = selector_row.get(
            "candidate_policy_ids",
            ["manager_ollama_qwen25_sft_v1", "manager_ollama_gemma2_local_v1"],
        )
        candidate_ids = tuple(str(item).strip() for item in candidates_raw if str(item).strip())
        if not candidate_ids:
            candidate_ids = ("manager_ollama_qwen25_sft_v1", "manager_ollama_gemma2_local_v1")
        return SelectorSettings(
            enabled_live=bool(selector_row.get("enabled_live", True)),
            category_key=str(selector_row.get("category_key", "team_profile|ticket_type|risk_tier")).strip(),
            default_policy_id=str(
                selector_row.get("default_policy_id", "manager_ollama_qwen25_sft_v1")
            ).strip(),
            candidate_policy_ids=candidate_ids,
            epsilon=float(selector_row.get("epsilon", 0.10)),
            min_samples=max(1, int(selector_row.get("min_samples", 10))),
        )

    def _parse_judge(self, row: dict[str, Any]) -> JudgeSettings:
        if not isinstance(row, dict):
            return JudgeSettings()
        return JudgeSettings(
            policy_id=str(row.get("policy_id", "judge_groq_v1")).strip(),
            max_tokens=max(50, int(row.get("max_tokens", 300))),
            temperature=float(row.get("temperature", 0.0)),
        )

    @property
    def selector(self) -> SelectorSettings:
        return self._selector

    @property
    def judge(self) -> JudgeSettings:
        return self._judge

    def list_policy_ids(self) -> list[str]:
        return list(self._policies.keys())

    def get_policy(self, policy_id: str) -> PolicyDefinition:
        policy = self._policies.get(policy_id)
        if not policy:
            raise KeyError(f"unknown policy id: {policy_id}")
        return policy

    def has_policy(self, policy_id: str) -> bool:
        return policy_id in self._policies


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _resolve_provider(row: dict[str, Any]) -> str:
    explicit = str(row.get("provider", "")).strip().lower()
    if explicit:
        return explicit
    policy_type = str(row.get("type", "")).strip().lower()
    if policy_type in {"llm_api", "sota_api"}:
        return "openrouter"
    return "unknown"
