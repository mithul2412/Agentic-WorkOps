from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from models.artifacts import (
    AuditorPack,
    CalendarProposal,
    CodingBrief,
    ConfluenceDraft,
    EmailDraftArtifact,
    EngineerPack,
    JiraCommentDraft,
    ManagerOutput,
    ManagerPack,
    PatchArtifact,
    PullRequestArtifact,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, Enum):
    RECEIVED = "RECEIVED"
    TRIAGING = "TRIAGING"
    WAITING_FOR_INFO = "WAITING_FOR_INFO"
    CODING = "CODING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TimelineEvent(BaseModel):
    ts: datetime = Field(default_factory=utc_now)
    step: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalRecord(BaseModel):
    reviewer: str
    approved: bool
    comments: str | None = None
    at: datetime = Field(default_factory=utc_now)


class WorkflowState(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    ticket_id: str
    status: RunStatus = RunStatus.RECEIVED

    jira_payload: dict[str, Any] = Field(default_factory=dict)
    jira_ticket: dict[str, Any] = Field(default_factory=dict)

    manager_pack: ManagerPack | None = None
    manager_output: ManagerOutput | None = None
    manager_decision_meta: dict[str, Any] | None = None
    coding_brief: CodingBrief | None = None
    engineer_pack: EngineerPack | None = None
    patch_artifact: PatchArtifact | None = None
    auditor_pack: AuditorPack | None = None
    mode: str | None = None
    team_profile: str | None = None

    jira_comment_draft: JiraCommentDraft | None = None
    jira_comment_id: str | None = None
    approval: ApprovalRecord | None = None

    pr_artifact: PullRequestArtifact | None = None
    confluence_draft: ConfluenceDraft | None = None
    calendar_proposal: CalendarProposal | None = None
    email_draft: EmailDraftArtifact | None = None

    stable_context_cache: dict[str, Any] = Field(default_factory=dict)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def init_from_webhook(
        cls,
        ticket_id: str,
        payload: dict[str, Any],
        config_snapshot: dict[str, Any],
    ) -> "WorkflowState":
        return cls(
            run_id=f"run_{uuid4().hex[:12]}",
            ticket_id=ticket_id,
            status=RunStatus.RECEIVED,
            jira_payload=payload,
            config_snapshot=config_snapshot,
        )

    def add_event(self, step: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self.timeline.append(
            TimelineEvent(
                step=step,
                event_type=event_type,
                payload=payload or {},
            )
        )
        self.updated_at = utc_now()

    def fail(self, message: str) -> None:
        self.status = RunStatus.FAILED
        self.errors.append(message)
        self.add_event(
            step="workflow",
            event_type="error",
            payload={"message": message},
        )

    def artifact_summary(self) -> dict[str, Any]:
        return {
            "manager_output": self.manager_output is not None,
            "manager_decision_meta": self.manager_decision_meta or {},
            "mode": self.mode,
            "team_profile": self.team_profile,
            "coding_brief": self.coding_brief is not None,
            "patch_artifact": self.patch_artifact is not None,
            "approval": self.approval.model_dump() if self.approval else None,
            "pr_url": self.pr_artifact.url if self.pr_artifact else None,
            "confluence_draft": self.confluence_draft.title if self.confluence_draft else None,
            "calendar_slots": self.calendar_proposal.slots if self.calendar_proposal else [],
            "email_subject": self.email_draft.subject if self.email_draft else None,
            "jira_comment_id": self.jira_comment_id,
        }
