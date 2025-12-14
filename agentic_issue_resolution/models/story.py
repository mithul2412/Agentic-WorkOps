from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from agentic_issue_resolution.models.state import RunStatus


class TicketListItem(BaseModel):
    ticket_id: str
    run_id: str
    status: RunStatus
    summary: str
    risk_tier: str | None = None
    current_step: str
    updated_at: str
    assignee: str | None = None


class TicketListResponse(BaseModel):
    total: int
    items: list[TicketListItem]


class StoryEventCreateRequest(BaseModel):
    kind: str
    source: str = "MANUAL"
    actor: str | None = None
    team: str | None = None
    payload: dict = Field(default_factory=dict)
    ts: str | None = None

    @field_validator("kind", "source")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("value cannot be empty")
        return text


class StoryEventUpdateRequest(BaseModel):
    kind: str | None = None
    actor: str | None = None
    team: str | None = None
    payload: dict | None = None
    ts: str | None = None


class StoryEventResponse(BaseModel):
    event_id: str
    ticket_id: str
    ts: str
    kind: str
    source: str
    actor: str | None = None
    team: str | None = None
    payload: dict = Field(default_factory=dict)
    deleted: bool = False
    deleted_at: str | None = None


class TicketStoryResponse(BaseModel):
    ticket_id: str
    run_id: str
    status: RunStatus
    summary: str
    description: str
    risk_tier: str | None = None
    assignee: str | None = None
    artifacts: dict = Field(default_factory=dict)
    timeline: list[StoryEventResponse] = Field(default_factory=list)
