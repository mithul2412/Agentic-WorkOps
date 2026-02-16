from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from models.state import RunStatus, TimelineEvent, WorkflowState


class JiraWebhookPayload(BaseModel):
    webhookEvent: str = "jira:issue_created"
    issue: dict
    user: dict | None = None

    @property
    def ticket_id(self) -> str:
        if "key" in self.issue:
            return str(self.issue["key"])
        if "id" in self.issue:
            return str(self.issue["id"])
        raise ValueError("issue.key or issue.id must be present")

    @property
    def summary(self) -> str:
        fields = self.issue.get("fields", {})
        return str(fields.get("summary", "No summary provided")).strip()

    @property
    def description(self) -> str:
        fields = self.issue.get("fields", {})
        value = fields.get("description", "No description provided")
        return str(value).strip()

    @property
    def labels(self) -> list[str]:
        fields = self.issue.get("fields", {})
        labels = fields.get("labels", [])
        if not isinstance(labels, list):
            return []
        return [str(item).strip() for item in labels if str(item).strip()]


class RunStartResponse(BaseModel):
    ticket_id: str
    run_id: str
    status: RunStatus


class ApprovalRequest(BaseModel):
    ticket_id: str
    reviewer: str
    approved: bool = True
    comments: str | None = None

    @field_validator("ticket_id", "reviewer")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("value must be non-empty")
        return text


class ApprovalResponse(BaseModel):
    ticket_id: str
    status: RunStatus
    resumed: bool
    message: str


class StatusResponse(BaseModel):
    ticket_id: str
    run_id: str
    status: RunStatus
    current_step: str
    risk_tier: str | None = None
    approval_required: bool = True
    artifacts_summary: dict
    errors: list[str] = Field(default_factory=list)
    updated_at: str

    @classmethod
    def from_state(cls, state: WorkflowState) -> "StatusResponse":
        current_step = state.timeline[-1].step if state.timeline else "workflow"
        risk_tier = state.manager_output.risk_tier.value if state.manager_output else None
        return cls(
            ticket_id=state.ticket_id,
            run_id=state.run_id,
            status=state.status,
            current_step=current_step,
            risk_tier=risk_tier,
            approval_required=True,
            artifacts_summary=state.artifact_summary(),
            errors=state.errors,
            updated_at=state.updated_at.isoformat(),
        )


class ReplayResponse(BaseModel):
    ticket_id: str
    run_id: str
    timeline: list[TimelineEvent]
