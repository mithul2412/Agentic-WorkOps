from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from models.state import RunStatus


class FlowNodeView(BaseModel):
    id: str
    label: str
    description: str = ""
    state: Literal["pending", "active", "done", "skipped"] = "pending"


class FlowEdgeView(BaseModel):
    source: str
    target: str
    condition: str | None = None


class FlowSnapshotResponse(BaseModel):
    ticket_id: str
    run_id: str
    status: RunStatus
    current_step: str
    updated_at: str
    timeline_size: int = 0
    nodes: list[FlowNodeView] = Field(default_factory=list)
    edges: list[FlowEdgeView] = Field(default_factory=list)
    last_event: dict | None = None
