from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DecisionType(str, Enum):
    ASK_FOR_INFO = "ASK_FOR_INFO"
    READY_TO_PATCH = "READY_TO_PATCH"


class TicketType(str, Enum):
    BUG = "bug"
    FEATURE_INSERT = "feature_insert"
    FEATURE_UPDATE = "feature_update"


class RiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceSource(str, Enum):
    JIRA = "jira"
    BITBUCKET = "bitbucket"
    GITHUB = "github"
    CODE = "code"
    WEB = "web"


class EvidenceItem(BaseModel):
    source: EvidenceSource
    id: str
    title: str = ""
    snippet: str = ""
    url: str | None = None
    score: float = 0.0


class ManagerCodingBrief(BaseModel):
    suspected_files: list[str] = Field(default_factory=list)
    hypothesis: str
    acceptance_criteria: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("suspected_files")
    @classmethod
    def _sanitize_files(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        deduped: list[str] = []
        for path in cleaned:
            if path not in deduped:
                deduped.append(path)
        return deduped


class ManagerOutput(BaseModel):
    """
    Runtime contract is intentionally aligned with the manager triage target JSON.
    """

    decision: DecisionType
    ticket_type: TicketType
    risk_tier: RiskTier
    summary: str
    error_signature: str
    suspected_components: list[str] = Field(default_factory=list)
    questions_needed: list[str] = Field(default_factory=list)
    coding_brief: ManagerCodingBrief

    model_config = ConfigDict(extra="forbid")

    @field_validator("summary", "error_signature")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("field must be non-empty")
        return text

    @field_validator("suspected_components", "questions_needed")
    @classmethod
    def _normalize_list(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "ManagerOutput":
        if self.decision == DecisionType.READY_TO_PATCH and self.questions_needed:
            raise ValueError("questions_needed must be empty when decision is READY_TO_PATCH")
        if self.decision == DecisionType.ASK_FOR_INFO and not self.questions_needed:
            raise ValueError("questions_needed must have at least one item when decision is ASK_FOR_INFO")
        return self


class CodingBrief(BaseModel):
    ticket_id: str
    ticket_type: TicketType
    risk_tier: RiskTier
    summary: str
    error_signature: str
    suspected_files: list[str] = Field(min_length=1)
    hypothesis: str
    acceptance_criteria: list[str] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("suspected_files", "acceptance_criteria", "constraints")
    @classmethod
    def _strip_lists(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]

    @field_validator("ticket_id", "summary", "hypothesis")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("field must be non-empty")
        return text


class PatchArtifact(BaseModel):
    format: str = Field(default="unified_diff")
    diff: str
    changed_files: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("diff")
    @classmethod
    def _validate_diff(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("diff cannot be empty")
        return text


class ManagerPack(BaseModel):
    ticket: dict[str, Any]
    evidence: list[EvidenceItem] = Field(default_factory=list)
    top_k: int = 5


class EngineerPack(BaseModel):
    coding_brief: CodingBrief
    file_texts: dict[str, str]
    token_budget: int = 3500


class AuditorPack(BaseModel):
    diff: str
    test_output: str
    risk_summary: str
    changed_files: list[str] = Field(default_factory=list)


class JiraCommentDraft(BaseModel):
    body: str


class PullRequestArtifact(BaseModel):
    pr_number: int
    url: str
    title: str
    body: str


class ConfluenceDraft(BaseModel):
    title: str
    body: str
    draft_id: str | None = None


class CalendarProposal(BaseModel):
    slots: list[str]
    duration_minutes: int
    timezone: str = "UTC"
    ics: str | None = None


class EmailDraftArtifact(BaseModel):
    to: list[str]
    subject: str
    body: str
    provider_draft_id: str | None = None
