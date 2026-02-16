from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class OperateABRunRequest(BaseModel):
    source: Literal["tasks_json", "saved_jira"]
    policy_a_id: str
    policy_b_id: str
    tasks_path: str | None = None
    ticket_ids: list[str] = Field(default_factory=list)
    max_tasks: int | None = None
    seed: int = 42

    @field_validator("policy_a_id", "policy_b_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("policy id cannot be empty")
        return text

    @model_validator(mode="after")
    def _cross_validate(self) -> "OperateABRunRequest":
        if self.source == "tasks_json" and not self.tasks_path:
            raise ValueError("tasks_path is required when source=tasks_json")
        if self.source == "saved_jira" and not self.ticket_ids:
            raise ValueError("ticket_ids is required when source=saved_jira")
        return self


class PolicyMetricsSummary(BaseModel):
    schema_pass_rate: float
    tool_correctness: float
    violations: float
    attempts: float
    runtime_ms: float
    cost_proxy: float


class OperateABRunResponse(BaseModel):
    ab_run_id: str
    total_tasks: int
    completed_tasks: int
    run_status: str
    summary_by_policy: dict[str, PolicyMetricsSummary]


class OperateJudgeRequest(BaseModel):
    ab_run_id: str
    judge_policy_id: str
    category_key: Literal["ticket_type|risk_tier", "team_profile|ticket_type|risk_tier"] = (
        "team_profile|ticket_type|risk_tier"
    )


class OperateJudgeResponse(BaseModel):
    ab_run_id: str
    judged_count: int
    a_wins: int
    b_wins: int
    ties: int
    stored_preferences: int
    selector_updates_applied: int = 0
    selector_updates_skipped_low_confidence: int = 0
    min_confidence_for_selector: float = 0.55


class SelectorCategoryRow(BaseModel):
    category: str
    policy_id: str
    wins: int
    losses: int
    ties: int
    total: int
    win_rate: float
    best_policy: bool = False


class OperateSelectorResponse(BaseModel):
    category_key: str
    min_samples: int
    default_policy_id: str
    epsilon: float
    rows: list[SelectorCategoryRow]


class OperateABRunDetailItem(BaseModel):
    item_id: str
    task_id: str
    ticket_id: str | None = None
    category_estimate: str | None = None
    category_actual: str | None = None
    task_context: dict
    output_a: dict
    output_b: dict
    metrics_a: dict
    metrics_b: dict
    created_at: str


class OperateABRunDetailResponse(BaseModel):
    ab_run: dict
    items: list[OperateABRunDetailItem]
    judgments: list[dict]
