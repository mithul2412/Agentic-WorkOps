from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agentic_issue_resolution.graph.workflow import WorkflowOrchestrator
from agentic_issue_resolution.models.flow import FlowSnapshotResponse
from agentic_issue_resolution.models.api import (
    ApprovalRequest,
    ApprovalResponse,
    JiraWebhookPayload,
    ReplayResponse,
    RunStartResponse,
    StatusResponse,
)
from agentic_issue_resolution.models.operate import (
    OperateABRunDetailResponse,
    OperateABRunRequest,
    OperateABRunResponse,
    OperateJudgeRequest,
    OperateJudgeResponse,
    OperateSelectorResponse,
)
from agentic_issue_resolution.models.state import RunStatus
from agentic_issue_resolution.models.story import (
    StoryEventCreateRequest,
    StoryEventResponse,
    StoryEventUpdateRequest,
    TicketListItem,
    TicketListResponse,
    TicketStoryResponse,
)
from agentic_issue_resolution.operate.ab_runner import OperateABRunner
from agentic_issue_resolution.operate.judge import OperateJudge
from agentic_issue_resolution.operate.policy_executor import ManagerPolicyExecutor
from agentic_issue_resolution.operate.policy_registry import PolicyRegistry
from agentic_issue_resolution.operate.selector import ManagerPolicySelector
from agentic_issue_resolution.storage.sqlite_store import SQLiteRunStore

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PACKAGE_ROOT / "config" / "project_config.yaml"
POLICY_CONFIG_PATH = PACKAGE_ROOT / "config" / "manager_policies.yaml"
DB_PATH = PACKAGE_ROOT / "storage" / "runs.db"
ENV_PATH = PACKAGE_ROOT / ".env"


def _load_env_fallback(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        parsed = os.path.expandvars(value.strip())
        if (parsed.startswith('"') and parsed.endswith('"')) or (parsed.startswith("'") and parsed.endswith("'")):
            parsed = parsed[1:-1]
        os.environ[key] = parsed


if ENV_PATH.exists():
    if load_dotenv:
        load_dotenv(dotenv_path=ENV_PATH, override=False)
    else:
        _load_env_fallback(ENV_PATH)

store = SQLiteRunStore(DB_PATH)
orchestrator = WorkflowOrchestrator(repo_root=REPO_ROOT, config_path=CONFIG_PATH, store=store)
app = FastAPI(title="Agentic Issue Resolution System", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

policy_registry = PolicyRegistry(POLICY_CONFIG_PATH) if POLICY_CONFIG_PATH.exists() else None
policy_executor = ManagerPolicyExecutor(policy_registry) if policy_registry else None
operate_ab_runner = OperateABRunner(store, policy_registry, policy_executor) if policy_registry and policy_executor else None
operate_judge = OperateJudge(store, policy_registry) if policy_registry else None
operate_selector = ManagerPolicySelector(store, policy_registry) if policy_registry else None

FLOW_NODES = [
    {"id": "webhook", "label": "Webhook", "description": "Jira webhook received"},
    {"id": "init", "label": "Init", "description": "Initialize run state and context cache"},
    {"id": "manager", "label": "Manager", "description": "API policy triage + evidence selection"},
    {"id": "ask_for_info", "label": "Ask For Info", "description": "Jira request for missing details"},
    {"id": "engineer", "label": "Engineer", "description": "Gemini file-locked patch generation"},
    {"id": "auditor_prepare", "label": "Auditor Prepare", "description": "Schema/scope/risk checks"},
    {"id": "auditor_gate", "label": "Auditor Gate", "description": "Pause for human approval"},
    {"id": "finalizer", "label": "Finalizer", "description": "Create PR + Jira update"},
    {"id": "kb_and_comms", "label": "KB + Comms", "description": "Confluence draft + calendar + email"},
    {"id": "rejected", "label": "Rejected", "description": "Approval rejected path"},
    {"id": "failed", "label": "Failed", "description": "Unhandled failure path"},
]

FLOW_EDGES = [
    {"source": "webhook", "target": "init"},
    {"source": "init", "target": "manager"},
    {"source": "manager", "target": "ask_for_info", "condition": "decision=ASK_FOR_INFO"},
    {"source": "manager", "target": "engineer", "condition": "decision=READY_TO_PATCH"},
    {"source": "engineer", "target": "auditor_prepare"},
    {"source": "auditor_prepare", "target": "auditor_gate"},
    {"source": "auditor_gate", "target": "finalizer", "condition": "approved=true"},
    {"source": "auditor_gate", "target": "rejected", "condition": "approved=false"},
    {"source": "finalizer", "target": "kb_and_comms"},
]


@app.post("/webhook/jira", response_model=RunStartResponse)
def webhook_jira(payload: JiraWebhookPayload) -> RunStartResponse:
    try:
        return orchestrator.start_from_jira(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/status/{ticket_id}", response_model=StatusResponse)
def status(ticket_id: str) -> StatusResponse:
    state = store.get_state(ticket_id)
    if not state:
        raise HTTPException(status_code=404, detail="ticket not found")
    return StatusResponse.from_state(state)


@app.post("/approve", response_model=ApprovalResponse)
def approve(request: ApprovalRequest) -> ApprovalResponse:
    state = store.get_state(request.ticket_id)
    if not state:
        raise HTTPException(status_code=404, detail="ticket not found")
    if state.status != RunStatus.AWAITING_APPROVAL:
        raise HTTPException(status_code=409, detail=f"ticket is not awaiting approval (status={state.status.value})")
    try:
        return orchestrator.resume_after_approval(request)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/replay/{ticket_id}", response_model=ReplayResponse)
def replay(ticket_id: str) -> ReplayResponse:
    state = store.get_state(ticket_id)
    if not state:
        raise HTTPException(status_code=404, detail="ticket not found")
    return ReplayResponse(ticket_id=ticket_id, run_id=state.run_id, timeline=state.timeline)


@app.get("/integrations/status")
def integrations_status() -> dict:
    keys = [
        "OLLAMA_BASE_URL",
        "MANAGER_OLLAMA_MODEL",
        "MANAGER_OPENROUTER_MODEL",
        "OPENROUTER_MODEL",
        "MANAGER_GROQ_MODEL",
        "GROQ_MODEL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "JUDGE_OPENROUTER_MODEL",
        "GROQ_API_KEY",
        "GROQ_BASE_URL",
        "JUDGE_GROQ_MODEL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_BASE",
        "GEMINI_MODEL",
        "ENGINEER_GEMINI_MODEL",
        "JUDGE_GEMINI_MODEL",
        "COMMS_LLM_PROVIDER",
        "COMMS_OLLAMA_MODEL",
        "COMMS_OPENROUTER_MODEL",
        "COMMS_GROQ_MODEL",
        "COMMS_GEMINI_MODEL",
        "COMMS_OPENROUTER_API_KEY",
        "COMMS_GROQ_API_KEY",
        "COMMS_GEMINI_API_KEY",
        "COMMS_LLM_TEMPERATURE",
        "COMMS_LLM_MAX_TOKENS",
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
        "TAVILY_API_KEY",
        "BITBUCKET_API_TOKEN",
        "BITBUCKET_USERNAME",
        "BITBUCKET_APP_PASSWORD",
        "BITBUCKET_WORKSPACE",
        "BITBUCKET_REPO_SLUG",
        "BITBUCKET_API_BASE",
        "BITBUCKET_SOURCE_BRANCH",
        "BITBUCKET_DESTINATION_BRANCH",
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "CONFLUENCE_BASE_URL",
        "CONFLUENCE_EMAIL",
        "CONFLUENCE_API_TOKEN",
        "GOOGLE_CLIENT_SECRET_FILE",
        "GOOGLE_TOKEN_FILE",
        "GOOGLE_CALENDAR_ID",
        "NOTIFY_EMAILS",
    ]
    import os

    env_status = {key: bool(os.getenv(key, "").strip()) for key in keys}
    return {"providers": orchestrator.integration_modes, "env": env_status}


@app.get("/flow/{ticket_id}", response_model=FlowSnapshotResponse)
def flow_snapshot(ticket_id: str) -> FlowSnapshotResponse:
    state = store.get_state(ticket_id)
    if not state:
        raise HTTPException(status_code=404, detail="ticket not found")
    return _build_flow_snapshot(state)


@app.get("/stream/{ticket_id}")
async def flow_stream(ticket_id: str, request: Request):
    state = store.get_state(ticket_id)
    if not state:
        raise HTTPException(status_code=404, detail="ticket not found")

    async def event_generator():
        last_marker = ""
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break
            current = store.get_state(ticket_id)
            if not current:
                payload = {"type": "error", "ticket_id": ticket_id, "message": "ticket not found"}
                yield f"event: error\ndata: {json.dumps(payload)}\n\n"
                break

            snapshot = _build_flow_snapshot(current).model_dump(mode="json")
            marker = f"{snapshot['updated_at']}::{snapshot['timeline_size']}::{snapshot['status']}"
            if marker != last_marker:
                event_payload = {"type": "flow_update", "flow": snapshot}
                yield f"event: flow_update\ndata: {json.dumps(event_payload)}\n\n"
                last_marker = marker
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks >= 15:
                    yield "event: heartbeat\ndata: {}\n\n"
                    idle_ticks = 0
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/tickets", response_model=TicketListResponse)
def list_tickets(limit: int = 100, offset: int = 0) -> TicketListResponse:
    states = store.list_states(limit=limit, offset=offset)
    items: list[TicketListItem] = []
    for state in states:
        summary = _ticket_summary(state)
        risk_tier = state.manager_output.risk_tier.value if state.manager_output else None
        current_step = state.timeline[-1].step if state.timeline else "workflow"
        assignee = _latest_assignee(state.ticket_id)
        items.append(
            TicketListItem(
                ticket_id=state.ticket_id,
                run_id=state.run_id,
                status=state.status,
                summary=summary,
                risk_tier=risk_tier,
                current_step=current_step,
                updated_at=state.updated_at.isoformat(),
                assignee=assignee,
            )
        )
    return TicketListResponse(total=len(items), items=items)


@app.get("/ticket/{ticket_id}/story", response_model=TicketStoryResponse)
def ticket_story(ticket_id: str) -> TicketStoryResponse:
    state = store.get_state(ticket_id)
    if not state:
        raise HTTPException(status_code=404, detail="ticket not found")

    auto_events: list[dict] = []
    for idx, event in enumerate(state.timeline):
        auto_events.append(
            {
                "event_id": f"auto_{idx + 1}",
                "ticket_id": ticket_id,
                "ts": event.ts.isoformat(),
                "kind": f"{event.step}:{event.event_type}",
                "source": "AUTO",
                "actor": "system",
                "team": "workflow",
                "payload": event.payload,
                "deleted": False,
                "deleted_at": None,
            }
        )
    manual_events = store.list_story_events(ticket_id=ticket_id, include_deleted=False)
    merged = auto_events + manual_events
    merged.sort(key=lambda row: row.get("ts", ""))
    story_events = [StoryEventResponse.model_validate(row) for row in merged]
    fields = state.jira_payload.get("issue", {}).get("fields", {})
    summary = _ticket_summary(state)
    description = str(fields.get("description", state.jira_ticket.get("description", "")))
    risk_tier = state.manager_output.risk_tier.value if state.manager_output else None
    artifacts = state.artifact_summary()
    artifacts["patch_artifact_detail"] = (
        state.patch_artifact.model_dump(mode="json") if state.patch_artifact is not None else None
    )
    return TicketStoryResponse(
        ticket_id=ticket_id,
        run_id=state.run_id,
        status=state.status,
        summary=summary,
        description=description,
        risk_tier=risk_tier,
        assignee=_latest_assignee(ticket_id),
        artifacts=artifacts,
        timeline=story_events,
    )


@app.post("/ticket/{ticket_id}/story-events", response_model=StoryEventResponse)
def create_story_event(ticket_id: str, request: StoryEventCreateRequest) -> StoryEventResponse:
    state = store.get_state(ticket_id)
    if not state:
        raise HTTPException(status_code=404, detail="ticket not found")
    row = store.create_story_event(
        ticket_id=ticket_id,
        kind=request.kind,
        source=request.source,
        actor=request.actor,
        team=request.team,
        payload=request.payload,
        ts=request.ts,
    )
    return StoryEventResponse.model_validate(row)


@app.patch("/ticket/{ticket_id}/story-events/{event_id}", response_model=StoryEventResponse)
def update_story_event(ticket_id: str, event_id: str, request: StoryEventUpdateRequest) -> StoryEventResponse:
    state = store.get_state(ticket_id)
    if not state:
        raise HTTPException(status_code=404, detail="ticket not found")
    row = store.update_story_event(
        event_id=event_id,
        kind=request.kind,
        actor=request.actor,
        team=request.team,
        payload=request.payload,
        ts=request.ts,
    )
    if not row or row.get("ticket_id") != ticket_id:
        raise HTTPException(status_code=404, detail="story event not found")
    return StoryEventResponse.model_validate(row)


@app.delete("/ticket/{ticket_id}/story-events/{event_id}")
def delete_story_event(ticket_id: str, event_id: str) -> dict:
    state = store.get_state(ticket_id)
    if not state:
        raise HTTPException(status_code=404, detail="ticket not found")
    existing = store.get_story_event(event_id)
    if not existing or existing.get("ticket_id") != ticket_id:
        raise HTTPException(status_code=404, detail="story event not found")
    ok = store.soft_delete_story_event(event_id)
    return {"deleted": ok, "event_id": event_id}


@app.post("/operate/ab_run", response_model=OperateABRunResponse)
def operate_ab_run(request: OperateABRunRequest) -> OperateABRunResponse:
    if not operate_ab_runner:
        raise HTTPException(status_code=500, detail="operate runner is not configured")
    try:
        return operate_ab_runner.run(request)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/operate/judge", response_model=OperateJudgeResponse)
def operate_judge_endpoint(request: OperateJudgeRequest) -> OperateJudgeResponse:
    if not operate_judge:
        raise HTTPException(status_code=500, detail="operate judge is not configured")
    try:
        return operate_judge.run(request)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/operate/selector", response_model=OperateSelectorResponse)
def operate_selector_endpoint(min_samples: int | None = None) -> OperateSelectorResponse:
    if not operate_selector:
        raise HTTPException(status_code=500, detail="operate selector is not configured")
    view = operate_selector.selector_view(min_samples=min_samples)
    return OperateSelectorResponse.model_validate(view)


@app.get("/operate/ab_run/{ab_run_id}", response_model=OperateABRunDetailResponse)
def operate_ab_run_detail(ab_run_id: str) -> OperateABRunDetailResponse:
    ab_run = store.get_ab_run(ab_run_id)
    if not ab_run:
        raise HTTPException(status_code=404, detail="ab run not found")
    return OperateABRunDetailResponse(
        ab_run=ab_run,
        items=store.list_ab_items(ab_run_id),
        judgments=store.list_judgments(ab_run_id),
    )


@app.get("/operate/ab_runs")
def operate_ab_runs(limit: int = 20) -> dict:
    return {"items": store.list_ab_runs(limit=max(1, min(limit, 200)))}


@app.get("/operate/live_decisions")
def operate_live_decisions(ticket_id: str | None = None, limit: int = 100) -> dict:
    rows = store.list_live_policy_decisions(ticket_id=ticket_id, limit=max(1, min(limit, 500)))
    return {"items": rows}


@app.get("/metrics/summary")
def metrics_summary() -> dict:
    states = store.list_states(limit=1000, offset=0)
    total_tickets = len(states)
    completed_states = [state for state in states if state.status == RunStatus.COMPLETED]

    cycle_minutes: list[float] = []
    reopened_or_regression = 0
    handoff_scores: list[float] = []
    handoff_required = {"TEAM_INVOLVED", "MEETING_NOTES_ADDED", "QA_REQUESTED", "CONFLUENCE_UPDATED"}
    for state in states:
        if state.status == RunStatus.COMPLETED:
            manager_ts = next((event.ts for event in state.timeline if event.step == "manager"), None)
            done_ts = next((event.ts for event in state.timeline if event.step == "kb_and_comms"), None)
            if manager_ts and done_ts and done_ts >= manager_ts:
                cycle_minutes.append((done_ts - manager_ts).total_seconds() / 60.0)

        manual_events = store.list_story_events(ticket_id=state.ticket_id, include_deleted=False)
        kinds = {str(event.get("kind", "")).strip().upper() for event in manual_events}
        if "REOPENED" in kinds or "REGRESSION_FOUND" in kinds:
            reopened_or_regression += 1
        score = len(kinds.intersection(handoff_required)) / max(1, len(handoff_required))
        handoff_scores.append(score)

    cycle_minutes_sorted = sorted(cycle_minutes)
    avg_cycle = round(sum(cycle_minutes_sorted) / len(cycle_minutes_sorted), 2) if cycle_minutes_sorted else 0.0
    p50 = _percentile(cycle_minutes_sorted, 0.50)
    p90 = _percentile(cycle_minutes_sorted, 0.90)
    reopen_rate = round((reopened_or_regression / total_tickets), 4) if total_tickets else 0.0
    avg_handoff = round(sum(handoff_scores) / len(handoff_scores), 4) if handoff_scores else 0.0

    selector_rows = store.get_selector_stats(min_samples=0)
    by_team: dict[str, dict[str, dict[str, float]]] = {}
    for row in selector_rows:
        category = str(row.get("category", ""))
        parts = category.split("|")
        team = parts[0] if len(parts) >= 3 else "unknown"
        policy = str(row.get("policy_id", "unknown"))
        wins = float(row.get("wins", 0))
        total = float(row.get("total", 0))
        team_bucket = by_team.setdefault(team, {})
        policy_bucket = team_bucket.setdefault(policy, {"wins": 0.0, "total": 0.0})
        policy_bucket["wins"] += wins
        policy_bucket["total"] += total

    policy_win_rate_by_team: list[dict] = []
    for team, policy_rows in by_team.items():
        best_policy = None
        best_rate = -1.0
        best_total = 0.0
        for policy, aggregate in policy_rows.items():
            total = aggregate["total"]
            rate = (aggregate["wins"] / total) if total > 0 else 0.0
            if rate > best_rate:
                best_policy = policy
                best_rate = rate
                best_total = total
        policy_win_rate_by_team.append(
            {
                "team_profile": team,
                "best_policy": best_policy or "unknown",
                "win_rate": round(max(0.0, best_rate), 4),
                "sample_size": int(best_total),
            }
        )
    policy_win_rate_by_team.sort(key=lambda item: item["team_profile"])

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "totals": {
            "tickets": total_tickets,
            "completed_tickets": len(completed_states),
        },
        "triage_to_fix_cycle_time": {
            "avg_minutes": avg_cycle,
            "p50_minutes": p50,
            "p90_minutes": p90,
            "sample_size": len(cycle_minutes_sorted),
        },
        "reopen_regression_rate": {
            "tickets_with_reopen_or_regression": reopened_or_regression,
            "rate": reopen_rate,
        },
        "handoff_quality": {
            "avg_score": avg_handoff,
            "checklist_items": sorted(handoff_required),
        },
        "policy_win_rate_by_team": policy_win_rate_by_team,
    }


@app.get("/metrics/model_ops")
def metrics_model_ops() -> dict:
    states = store.list_states(limit=2000, offset=0)
    live_rows = store.list_live_policy_decisions(limit=5000)
    selector_rows = store.get_selector_stats(min_samples=0)
    confidence_threshold = float(os.getenv("JUDGE_MIN_CONFIDENCE_FOR_SELECTOR", "0.55"))

    manager_decisions = 0
    fallback_decisions = 0
    for state in states:
        for event in state.timeline:
            if event.step != "manager" or event.event_type != "decision":
                continue
            manager_decisions += 1
            mode = str(event.payload.get("model_mode", "")).strip().lower()
            if "fallback" in mode:
                fallback_decisions += 1

    total_live = len(live_rows)
    explored_count = sum(1 for row in live_rows if bool(row.get("explored")))
    exploration_rate = (explored_count / total_live) if total_live else 0.0

    runtime_cost_by_policy: dict[str, dict[str, float | str | int]] = {}
    for row in live_rows:
        policy_id = str(row.get("selected_policy_id", "")).strip() or "unknown"
        provider = "unknown"
        if policy_registry and policy_registry.has_policy(policy_id):
            provider = policy_registry.get_policy(policy_id).provider
        bucket = runtime_cost_by_policy.setdefault(
            policy_id,
            {
                "policy_id": policy_id,
                "provider": provider,
                "count": 0,
                "runtime_sum": 0.0,
                "cost_sum": 0.0,
            },
        )
        bucket["count"] = int(bucket["count"]) + 1
        bucket["runtime_sum"] = float(bucket["runtime_sum"]) + float(row.get("runtime_ms", 0.0))
        bucket["cost_sum"] = float(bucket["cost_sum"]) + float(row.get("cost_proxy", 0.0))

    runtime_cost_rows: list[dict] = []
    for bucket in runtime_cost_by_policy.values():
        count = max(1, int(bucket["count"]))
        runtime_cost_rows.append(
            {
                "policy_id": bucket["policy_id"],
                "provider": bucket["provider"],
                "sample_size": int(bucket["count"]),
                "avg_runtime_ms": round(float(bucket["runtime_sum"]) / count, 2),
                "avg_cost_proxy": round(float(bucket["cost_sum"]) / count, 8),
            }
        )
    runtime_cost_rows.sort(key=lambda item: item["policy_id"])

    win_rate_by_category: list[dict] = []
    best_by_category: dict[str, dict] = {}
    for row in selector_rows:
        category = str(row.get("category", ""))
        if not category:
            continue
        existing = best_by_category.get(category)
        if not existing or float(row.get("win_rate", 0.0)) > float(existing.get("win_rate", 0.0)):
            best_by_category[category] = row
    for category, row in sorted(best_by_category.items(), key=lambda item: item[0]):
        win_rate_by_category.append(
            {
                "category": category,
                "best_policy": str(row.get("policy_id", "unknown")),
                "win_rate": round(float(row.get("win_rate", 0.0)), 4),
                "sample_size": int(row.get("total", 0)),
            }
        )

    confidence_values: list[float] = []
    total_judgments = 0
    skipped_low_confidence = 0
    for ab_run in store.list_ab_runs(limit=5000):
        for judgment in store.list_judgments(str(ab_run.get("ab_run_id", ""))):
            confidence_values.append(float(judgment.get("confidence", 0.0)))
            total_judgments += 1
            if not bool(judgment.get("selector_updated", True)):
                skipped_low_confidence += 1
    sorted_conf = sorted(confidence_values)
    confidence_distribution = {
        "p50": _percentile(sorted_conf, 0.50),
        "p90": _percentile(sorted_conf, 0.90),
        "avg": round(sum(sorted_conf) / len(sorted_conf), 4) if sorted_conf else 0.0,
        "sample_size": len(sorted_conf),
        "min_confidence_for_selector": confidence_threshold,
        "skipped_low_confidence": skipped_low_confidence,
        "low_confidence_skip_rate": round(skipped_low_confidence / total_judgments, 4) if total_judgments else 0.0,
    }

    fallback_rate = round(fallback_decisions / manager_decisions, 4) if manager_decisions else 0.0
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "manager": {
            "decisions": manager_decisions,
            "fallback_decisions": fallback_decisions,
            "fallback_rate": fallback_rate,
        },
        "selector": {
            "exploration_rate": round(exploration_rate, 4),
            "explored_count": explored_count,
            "total_live_decisions": total_live,
            "win_rate_by_category": win_rate_by_category,
        },
        "policy_runtime_cost": runtime_cost_rows,
        "judge_confidence": confidence_distribution,
    }


def _ticket_summary(state) -> str:
    fields = state.jira_payload.get("issue", {}).get("fields", {})
    summary = str(fields.get("summary", "")).strip()
    if summary:
        return summary
    return str(state.jira_ticket.get("summary", "No summary")).strip() or "No summary"


def _latest_assignee(ticket_id: str) -> str | None:
    events = store.list_story_events(ticket_id=ticket_id, include_deleted=False)
    assignment_events = [event for event in events if event.get("kind") in {"ASSIGNMENT_COMMITTED", "ASSIGNMENT_SUGGESTED"}]
    if not assignment_events:
        return None
    latest = assignment_events[-1]
    payload = latest.get("payload", {})
    assignee = payload.get("assignee") or payload.get("selected_assignee")
    return str(assignee).strip() if assignee else None


def _build_flow_snapshot(state) -> FlowSnapshotResponse:
    known_ids = {row["id"] for row in FLOW_NODES}
    visited_steps: list[str] = []
    for event in state.timeline:
        step = str(event.step).strip()
        if step and step in known_ids and step not in visited_steps:
            visited_steps.append(step)

    current_step = _resolve_current_flow_step(state)
    node_rows: list[dict] = []
    for node in FLOW_NODES:
        node_id = node["id"]
        node_state = "pending"
        if node_id in visited_steps:
            node_state = "done"
        if node_id == current_step:
            node_state = "active"
        if _is_skipped_node(node_id=node_id, status=state.status.value):
            node_state = "skipped"
        if node_id == current_step:
            node_state = "active"
        node_rows.append({**node, "state": node_state})

    last_event = state.timeline[-1].model_dump(mode="json") if state.timeline else None
    current_step_label = current_step
    return FlowSnapshotResponse(
        ticket_id=state.ticket_id,
        run_id=state.run_id,
        status=state.status,
        current_step=current_step_label,
        updated_at=state.updated_at.isoformat(),
        timeline_size=len(state.timeline),
        nodes=node_rows,
        edges=FLOW_EDGES,
        last_event=last_event,
    )


def _resolve_current_flow_step(state) -> str:
    status = state.status.value
    if status == RunStatus.WAITING_FOR_INFO.value:
        return "ask_for_info"
    if status == RunStatus.CODING.value:
        return "engineer"
    if status == RunStatus.AWAITING_APPROVAL.value:
        return "auditor_gate"
    if status in {RunStatus.APPROVED.value, RunStatus.FINALIZING.value}:
        return "finalizer"
    if status == RunStatus.COMPLETED.value:
        return "kb_and_comms"
    if status == RunStatus.REJECTED.value:
        return "rejected"
    if status == RunStatus.FAILED.value:
        return "failed"
    if status in {RunStatus.RECEIVED.value, RunStatus.TRIAGING.value}:
        return "manager"
    if state.timeline:
        step = str(state.timeline[-1].step).strip()
        if step:
            return step
    return "webhook"


def _is_skipped_node(node_id: str, status: str) -> bool:
    if status == RunStatus.WAITING_FOR_INFO.value and node_id in {
        "engineer",
        "auditor_prepare",
        "auditor_gate",
        "finalizer",
        "kb_and_comms",
        "rejected",
        "failed",
    }:
        return True
    if status == RunStatus.REJECTED.value and node_id in {"finalizer", "kb_and_comms"}:
        return True
    if status == RunStatus.COMPLETED.value and node_id in {"ask_for_info", "rejected", "failed"}:
        return True
    if status == RunStatus.FAILED.value and node_id in {"finalizer", "kb_and_comms", "rejected", "ask_for_info"}:
        return True
    return False


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return round(sorted_values[0], 2)
    idx = int(round((len(sorted_values) - 1) * quantile))
    idx = max(0, min(idx, len(sorted_values) - 1))
    return round(sorted_values[idx], 2)
