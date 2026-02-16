from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

import yaml
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

try:
    from langgraph.checkpoint.memory import MemorySaver
except Exception:  # pragma: no cover
    from langgraph.checkpoint import MemorySaver  # type: ignore

from graph.context_bridge import (
    build_coding_brief,
    build_confluence_draft,
    build_jira_comment_draft,
)
from models.api import ApprovalRequest, JiraWebhookPayload, RunStartResponse, ApprovalResponse
from models.artifacts import (
    AuditorPack,
    CalendarProposal,
    CodingBrief,
    ConfluenceDraft,
    DecisionType,
    PatchArtifact,
    EmailDraftArtifact,
    EngineerPack,
    EvidenceItem,
    EvidenceSource,
    ManagerPack,
    ManagerCodingBrief,
    ManagerOutput,
    RiskTier,
    PullRequestArtifact,
)
from models.state import ApprovalRecord, RunStatus, WorkflowState
from models.validators import (
    apply_high_risk_strict_checks,
    validate_coding_brief,
    validate_patch_artifact,
    verify_patch_scope,
)
from operate.categorizer import category_from_manager_output, estimate_category
from operate.policy_executor import ManagerPolicyExecutor
from operate.policy_registry import PolicyRegistry
from operate.selector import ManagerPolicySelector
from storage.sqlite_store import SQLiteRunStore
from tools.bitbucket import MockBitbucketClient, RealBitbucketClient
from tools.calendar import GoogleCalendarClient, MockCalendarClient
from tools.code_search import code_search
from tools.confluence import MockConfluenceClient, RealConfluenceClient
from tools.email import GoogleGmailClient, MockEmailClient
from tools.engineer_llm import GeminiEngineerClient
from tools.file_ops import read_target_files
from tools.jira import MockJiraClient, RealJiraClient
from tools.llm_provider import (
    GeminiDirectClient,
    GroqClient,
    LLMRequest,
    OllamaClient,
    OpenRouterClient,
)
from tools.tavily import MockTavilyClient, RealTavilyClient


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ManagerRunMeta:
    mode: str
    reason: str | None = None


class GraphState(TypedDict, total=False):
    run_id: str
    ticket_id: str
    status: str
    jira_payload: dict[str, Any]
    jira_ticket: dict[str, Any]
    manager_pack: dict[str, Any]
    manager_output: dict[str, Any]
    manager_decision_meta: dict[str, Any]
    mode: str
    team_profile: str
    coding_brief: dict[str, Any]
    engineer_pack: dict[str, Any]
    patch_artifact: dict[str, Any]
    auditor_pack: dict[str, Any]
    jira_comment_draft: dict[str, Any]
    jira_comment_id: str
    approval: dict[str, Any]
    pr_artifact: dict[str, Any]
    confluence_draft: dict[str, Any]
    calendar_proposal: dict[str, Any]
    email_draft: dict[str, Any]
    stable_context_cache: dict[str, Any]
    config_snapshot: dict[str, Any]
    timeline: list[dict[str, Any]]
    errors: list[str]
    created_at: str
    updated_at: str


class WorkflowOrchestrator:
    def __init__(
        self,
        repo_root: Path,
        config_path: Path,
        store: SQLiteRunStore,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config_path = config_path
        self.store = store
        self.config = self._load_config(config_path)
        self.integration_modes: dict[str, str] = {}
        integration_cfg = self.config.get("integrations", {})

        jira_provider = self._provider_name("jira_provider", integration_cfg.get("jira_provider", "mock"))
        if jira_provider in {"real", "jira"} and self._env_ready("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            self.jira = RealJiraClient()
            self.integration_modes["jira"] = "real"
        else:
            self.jira = MockJiraClient()
            self.integration_modes["jira"] = "mock"

        bitbucket_provider = self._provider_name("bitbucket_provider", integration_cfg.get("bitbucket_provider", "mock"))
        if bitbucket_provider in {"real", "bitbucket"} and self._bitbucket_env_ready():
            self.bitbucket = RealBitbucketClient()
            self.integration_modes["bitbucket"] = "real"
        else:
            self.bitbucket = MockBitbucketClient()
            self.integration_modes["bitbucket"] = "mock"

        confluence_provider = self._provider_name("confluence_provider", integration_cfg.get("confluence_provider", "mock"))
        if confluence_provider in {"real", "confluence"} and self._env_ready(
            "CONFLUENCE_BASE_URL",
            "CONFLUENCE_EMAIL",
            "CONFLUENCE_API_TOKEN",
        ):
            self.confluence = RealConfluenceClient()
            self.integration_modes["confluence"] = "real"
        else:
            self.confluence = MockConfluenceClient()
            self.integration_modes["confluence"] = "mock"

        tavily_provider = self._provider_name("tavily_provider", integration_cfg.get("tavily_provider", "mock"))
        if tavily_provider in {"real", "tavily"} and self._env_ready("TAVILY_API_KEY"):
            self.tavily = RealTavilyClient()
            self.integration_modes["tavily"] = "real"
        else:
            self.tavily = MockTavilyClient()
            self.integration_modes["tavily"] = "mock"

        self.engineer = GeminiEngineerClient()
        self.policy_registry: PolicyRegistry | None = None
        self.policy_executor: ManagerPolicyExecutor | None = None
        self.policy_selector: ManagerPolicySelector | None = None
        try:
            policy_config_path = config_path.parent / "manager_policies.yaml"
            if policy_config_path.exists():
                self.policy_registry = PolicyRegistry(policy_config_path)
                self.policy_executor = ManagerPolicyExecutor(self.policy_registry)
                self.policy_selector = ManagerPolicySelector(store=store, registry=self.policy_registry)
        except Exception:
            self.policy_registry = None
            self.policy_executor = None
            self.policy_selector = None

        calendar_provider = self._provider_name("calendar_provider", integration_cfg.get("calendar_provider", "google"))
        if calendar_provider == "google":
            self.calendar = GoogleCalendarClient(timezone_name=self.config.get("timezone", "UTC"))
            self.integration_modes["calendar"] = "google"
        else:
            self.calendar = MockCalendarClient()
            self.integration_modes["calendar"] = "mock"

        email_provider = self._provider_name("email_provider", integration_cfg.get("email_provider", "google"))
        if email_provider == "google":
            self.email = GoogleGmailClient()
            self.integration_modes["email"] = "google"
        else:
            self.email = MockEmailClient()
            self.integration_modes["email"] = "mock"

        self.checkpointer = MemorySaver()
        self.graph = self._build_graph()

    def _load_config(self, config_path: Path) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "output_format": "unified_diff",
            "evidence_top_k": 5,
            "max_files_editable": 3,
            "token_budgets": {"manager_pack": 2500, "engineer_pack": 3500, "auditor_pack": 1200},
            "manager_file_max_bytes": 200000,
            "allowed_tools_by_role": {
                "manager": ["jira_read", "bitbucket_search", "code_search", "tavily_search"],
                "engineer": ["file_read", "gemini_generate"],
                "auditor": ["patch_scope_verify", "risk_verify"],
                "finalizer": ["bitbucket_create_pr", "jira_comment", "confluence_draft", "calendar", "email"],
            },
            "risk_tiers": {"low": "standard", "medium": "standard", "high": "strict"},
            "apply_patch_after_approval": False,
            "enable_tavily": True,
            "integrations": {
                "jira_provider": "mock",
                "bitbucket_provider": "mock",
                "confluence_provider": "mock",
                "tavily_provider": "mock",
                "calendar_provider": "google",
                "email_provider": "google",
            },
            "timezone": "UTC",
            "operate": {"enabled": True, "selector_enabled_live": True},
            "autonomy": {"min_confidence": 0.72},
            "demo": {"force_ready_for_seeded_tickets": False},
            "llm_comms": {
                "enabled": True,
                "provider": "ollama",
                "temperature": 0.1,
                "max_tokens": 900,
            },
        }
        if not config_path.exists():
            return defaults
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return _deep_merge(defaults, loaded)

    def _build_graph(self):
        graph = StateGraph(GraphState)
        graph.add_node("init", self._node_init)
        graph.add_node("manager", self._node_manager)
        graph.add_node("ask_for_info", self._node_ask_for_info)
        graph.add_node("engineer", self._node_engineer)
        graph.add_node("auditor_prepare", self._node_auditor_prepare)
        graph.add_node("auditor_gate", self._node_auditor_gate)
        graph.add_node("finalizer", self._node_finalizer)
        graph.add_node("kb_and_comms", self._node_kb_and_comms)
        graph.add_node("rejected", self._node_rejected)

        graph.set_entry_point("init")
        graph.add_edge("init", "manager")
        graph.add_conditional_edges(
            "manager",
            self._route_after_manager,
            {
                "ASK_FOR_INFO": "ask_for_info",
                "READY_TO_PATCH": "engineer",
                "FAILED": END,
            },
        )
        graph.add_edge("ask_for_info", END)
        graph.add_edge("engineer", "auditor_prepare")
        graph.add_conditional_edges(
            "auditor_prepare",
            self._route_after_auditor_prepare,
            {
                "READY": "auditor_gate",
                "FAILED": END,
            },
        )
        graph.add_conditional_edges(
            "auditor_gate",
            self._route_after_auditor,
            {
                "APPROVED": "finalizer",
                "REJECTED": "rejected",
                "FAILED": END,
            },
        )
        graph.add_edge("finalizer", "kb_and_comms")
        graph.add_edge("kb_and_comms", END)
        graph.add_edge("rejected", END)
        return graph.compile(checkpointer=self.checkpointer)

    def _graph_config(self, ticket_id: str, run_id: str) -> dict[str, Any]:
        thread_id = f"{ticket_id}:{run_id}" if run_id else ticket_id
        return {
            "configurable": {
                # Keep one checkpoint thread per run to avoid cross-run collisions.
                "thread_id": thread_id,
            }
        }

    def start_from_jira(self, payload: JiraWebhookPayload) -> RunStartResponse:
        state = WorkflowState.init_from_webhook(
            ticket_id=payload.ticket_id,
            payload=payload.model_dump(),
            config_snapshot=self.config,
        )
        state.add_event(
            step="webhook",
            event_type="state",
            payload={"message": "received jira webhook"},
        )
        config = self._graph_config(state.ticket_id, state.run_id)
        try:
            self.graph.invoke(state.model_dump(mode="json"), config=config)
            latest = self._load_graph_state(state.ticket_id, state.run_id)
        except Exception as exc:  # noqa: BLE001
            latest = self._safe_load_graph_state(state.ticket_id, state.run_id) or state
            latest.fail(str(exc))
            self.store.upsert_state(latest)
            raise
        self.store.upsert_state(latest)
        return RunStartResponse(ticket_id=latest.ticket_id, run_id=latest.run_id, status=latest.status)

    def resume_after_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        stored = self.store.get_state(request.ticket_id)
        if not stored:
            raise ValueError(f"ticket not found: {request.ticket_id}")
        config = self._graph_config(stored.ticket_id, stored.run_id)
        try:
            self.graph.invoke(Command(resume=request.model_dump(mode="json")), config=config)
            latest = self._load_graph_state(stored.ticket_id, stored.run_id)
        except Exception as exc:  # noqa: BLE001
            latest = self._safe_load_graph_state(stored.ticket_id, stored.run_id)
            if latest is None:
                # MemorySaver checkpoints are process-local; recover from persisted DB state on restart.
                latest = self._resume_from_persisted_state(stored=stored, request=request)
            else:
                latest.fail(str(exc))
                self.store.upsert_state(latest)
                raise
        self.store.upsert_state(latest)
        return ApprovalResponse(
            ticket_id=latest.ticket_id,
            status=latest.status,
            resumed=True,
            message="workflow resumed after approval submission",
        )

    def _resume_from_persisted_state(self, stored: WorkflowState, request: ApprovalRequest) -> WorkflowState:
        if stored.status != RunStatus.AWAITING_APPROVAL:
            raise ValueError(
                f"persisted state cannot be resumed from approval (status={stored.status.value}); "
                "expected AWAITING_APPROVAL"
            )

        state: GraphState = stored.model_dump(mode="json")
        approval_record = ApprovalRecord(
            reviewer=request.reviewer,
            approved=request.approved,
            comments=request.comments,
        )
        next_status = RunStatus.APPROVED.value if request.approved else RunStatus.REJECTED.value
        state = self._apply_updates_with_event(
            state,
            {
                "status": next_status,
                "approval": approval_record.model_dump(mode="json"),
            },
            step="auditor_gate",
            event_type="approval",
            payload={"reviewer": request.reviewer, "approved": request.approved},
        )
        if request.approved:
            state = self._node_finalizer(state)
            state = self._node_kb_and_comms(state)
        else:
            state = self._node_rejected(state)
        return WorkflowState.model_validate(state)

    def _load_graph_state(self, ticket_id: str, run_id: str) -> WorkflowState:
        config = self._graph_config(ticket_id, run_id)
        snapshot = self.graph.get_state(config)
        values = snapshot.values if snapshot else None
        if not values:
            raise RuntimeError(f"unable to load graph state for {ticket_id}")
        return WorkflowState.model_validate(values)

    def _safe_load_graph_state(self, ticket_id: str, run_id: str) -> WorkflowState | None:
        try:
            return self._load_graph_state(ticket_id, run_id)
        except Exception:  # noqa: BLE001
            return None

    def _node_init(self, state: GraphState) -> GraphState:
        stable_context = {
            "repo_root": str(self.repo_root),
            "kb_template_sections": ["Symptoms", "Root cause", "Fix", "Verification", "Prevention"],
            "output_format": self.config.get("output_format", "unified_diff"),
            "tool_policies": self.config.get("allowed_tools_by_role", {}),
            "integrations": self.integration_modes,
        }
        updates: GraphState = {
            "status": RunStatus.TRIAGING.value,
            "stable_context_cache": stable_context,
        }
        return self._apply_updates_with_event(
            state,
            updates,
            step="init",
            event_type="state",
            payload={"status": RunStatus.TRIAGING.value},
        )

    def _provider_name(self, env_key: str, default: str) -> str:
        value = os.getenv(env_key.upper(), default)
        return str(value).strip().lower()

    def _env_ready(self, *keys: str) -> bool:
        return all(bool(os.getenv(key, "").strip()) for key in keys)

    def _bitbucket_env_ready(self) -> bool:
        has_repo = self._env_ready("BITBUCKET_WORKSPACE", "BITBUCKET_REPO_SLUG")
        if not has_repo:
            return False
        has_token = bool(os.getenv("BITBUCKET_API_TOKEN", "").strip())
        has_basic = self._env_ready("BITBUCKET_USERNAME", "BITBUCKET_APP_PASSWORD")
        return has_token or has_basic

    def _node_manager(self, state: GraphState) -> GraphState:
        self._ensure_tool("manager", "jira_read")
        jira_payload = state.get("jira_payload", {})
        ticket_id = state["ticket_id"]
        ticket = self.jira.read_ticket(ticket_id, jira_payload)
        timeline = self._append_event(
            state,
            step="manager",
            event_type="tool_call",
            payload={"tool": "jira.read_ticket", "ticket_id": ticket_id},
        )

        summary = str(ticket.get("summary", "")).strip()
        description = str(ticket.get("description", "")).strip()
        labels = ticket.get("labels", []) if isinstance(ticket.get("labels"), list) else []
        mode, team_profile = self._derive_mode_and_team(
            jira_payload=jira_payload,
            ticket=ticket,
            labels=labels,
            summary=summary,
            description=description,
        )
        ticket_text = f"{summary}\n{description}"

        evidence_items: list[EvidenceItem] = [
            EvidenceItem(source=EvidenceSource.JIRA, id=ticket_id, title=summary, snippet=description[:350], score=1.0)
        ]

        self._ensure_tool("manager", "bitbucket_search")
        bb_results = self.bitbucket.search_issues(summary or ticket_id, max_results=3)
        timeline = self._append_event(
            {"timeline": timeline},
            step="manager",
            event_type="tool_call",
            payload={"tool": "bitbucket.search_issues", "count": len(bb_results)},
        )
        for item in bb_results:
            evidence_items.append(
                EvidenceItem(
                    source=EvidenceSource.BITBUCKET,
                    id=str(item.get("id", "")),
                    title=str(item.get("title", "")),
                    snippet=str(item.get("snippet", "")),
                    url=str(item.get("url", "")),
                    score=0.7,
                )
            )

        self._ensure_tool("manager", "code_search")
        code_hits = code_search(self.repo_root, summary or ticket_id, max_hits=5)
        timeline = self._append_event(
            {"timeline": timeline},
            step="manager",
            event_type="tool_call",
            payload={"tool": "code_search", "count": len(code_hits)},
        )
        for hit in code_hits:
            evidence_items.append(
                EvidenceItem(
                    source=EvidenceSource.CODE,
                    id=str(hit.get("file", "")),
                    title=str(hit.get("title", "")),
                    snippet=str(hit.get("snippet", "")),
                    score=0.8,
                )
            )

        if self.config.get("enable_tavily", True):
            self._ensure_tool("manager", "tavily_search")
            web_hits = self.tavily.search(summary or ticket_id, max_results=2)
            timeline = self._append_event(
                {"timeline": timeline},
                step="manager",
                event_type="tool_call",
                payload={"tool": "tavily.search", "count": len(web_hits)},
            )
            for item in web_hits:
                evidence_items.append(
                    EvidenceItem(
                        source=EvidenceSource.WEB,
                        id=str(item.get("id", "")),
                        title=str(item.get("title", "")),
                        snippet=str(item.get("snippet", "")),
                        url=str(item.get("url", "")),
                        score=0.5,
                    )
                )

        top_k = int(self.config.get("evidence_top_k", 5))
        top_evidence = evidence_items[:top_k]
        manager_pack = ManagerPack(ticket=ticket, evidence=top_evidence, top_k=top_k)

        repo_candidates = [str(hit.get("file", "")).strip() for hit in code_hits if str(hit.get("file", "")).strip()]
        if repo_candidates:
            repo_candidates = self._filter_existing_files(repo_candidates)
        if not repo_candidates:
            repo_candidates = self._filter_existing_files(self._default_repo_candidates())
        if not repo_candidates:
            raise ValueError("manager could not find any readable repo file candidates")
        model_input = {
            "ticket_id": ticket_id,
            "project": jira_payload.get("issue", {}).get("fields", {}).get("project", {}).get("key", "jira/project"),
            "title": summary,
            "description": description,
            "comments": [],
            "labels": labels,
            "source": "jira",
            "ticket_text": ticket_text,
            "mode": mode,
            "team_profile": team_profile,
        }
        (
            manager_output,
            manager_meta,
            selected_policy_id,
            explored,
            category_estimate,
            policy_metrics,
        ) = self._run_manager_policy(
            ticket_id=ticket_id,
            run_id=str(state.get("run_id", "")),
            model_input=model_input,
            evidence=[item.model_dump(mode="json") for item in top_evidence],
            repo_candidates=repo_candidates,
        )
        manager_output = self._apply_mode_behavior(manager_output, mode)
        if self._should_force_ready_for_seeded_ticket(labels):
            promoted = self._promote_seeded_ticket_decision(
                manager_output=manager_output,
                repo_candidates=repo_candidates,
                max_files_editable=int(self.config.get("max_files_editable", 3)),
            )
            if promoted:
                timeline = self._append_event(
                    {"timeline": timeline},
                    step="manager",
                    event_type="guard",
                    payload={
                        "guard": "seeded_ticket_ready_promotion",
                        "result": "pass",
                        "from_decision": manager_output.decision.value,
                        "to_decision": promoted.decision.value,
                    },
                )
                manager_output = promoted
        confidence, confidence_reason = self._estimate_manager_confidence(
            manager_output=manager_output,
            model_mode=manager_meta.mode,
            evidence_count=len(top_evidence),
            mode=mode,
        )
        min_confidence = float(self.config.get("autonomy", {}).get("min_confidence", 0.72))
        autonomy_gate_triggered = False
        acceptance_check_triggered = False
        coding_brief = None
        if manager_output.decision.value == "READY_TO_PATCH":
            if len(manager_output.coding_brief.acceptance_criteria) < 2:
                acceptance_check_triggered = True
                timeline = self._append_event(
                    {"timeline": timeline},
                    step="manager",
                    event_type="guard",
                    payload={
                        "guard": "acceptance_criteria_minimum",
                        "result": "fail",
                        "required": 2,
                        "actual": len(manager_output.coding_brief.acceptance_criteria),
                    },
                )
                manager_output = self._force_ask_for_info(
                    manager_output,
                    "Please provide at least two concrete acceptance criteria before patch generation.",
                )
            else:
                timeline = self._append_event(
                    {"timeline": timeline},
                    step="manager",
                    event_type="guard",
                    payload={
                        "guard": "acceptance_criteria_minimum",
                        "result": "pass",
                        "required": 2,
                        "actual": len(manager_output.coding_brief.acceptance_criteria),
                    },
                )

        if manager_output.decision.value == "READY_TO_PATCH" and confidence < min_confidence:
            autonomy_gate_triggered = True
            timeline = self._append_event(
                {"timeline": timeline},
                step="manager",
                event_type="guard",
                payload={
                    "guard": "autonomy_confidence_gate",
                    "result": "fail",
                    "confidence": round(confidence, 4),
                    "min_confidence": min_confidence,
                },
            )
            manager_output = self._force_ask_for_info(
                manager_output,
                f"Confidence {confidence:.2f} is below autonomy threshold {min_confidence:.2f}; request additional evidence.",
            )
        elif manager_output.decision.value == "READY_TO_PATCH":
            timeline = self._append_event(
                {"timeline": timeline},
                step="manager",
                event_type="guard",
                payload={
                    "guard": "autonomy_confidence_gate",
                    "result": "pass",
                    "confidence": round(confidence, 4),
                    "min_confidence": min_confidence,
                },
            )

        if manager_output.decision.value == "READY_TO_PATCH":
            coding_brief = build_coding_brief(
                ticket_id=ticket_id,
                manager_output=manager_output,
                evidence=top_evidence,
                max_files_editable=int(self.config.get("max_files_editable", 3)),
                repo_file_candidates=repo_candidates,
            )
            if mode == "feature":
                enriched_acceptance = list(coding_brief.acceptance_criteria)
                enriched_acceptance.append("Feature mode: validate UX/API handoff and rollout notes.")
                coding_brief = coding_brief.model_copy(update={"acceptance_criteria": enriched_acceptance})
            if mode == "customer_escalation":
                escalated_constraints = list(coding_brief.constraints)
                escalated_constraints.append("Customer escalation: include customer-facing impact summary in final handoff.")
                coding_brief = coding_brief.model_copy(update={"constraints": escalated_constraints})
            safe_files = self._filter_existing_files(coding_brief.suspected_files)
            if not safe_files:
                safe_files = self._filter_existing_files(repo_candidates)
            if not safe_files:
                raise ValueError("manager produced READY_TO_PATCH but no readable target files were found")
            coding_brief = coding_brief.model_copy(
                update={"suspected_files": safe_files[: int(self.config.get("max_files_editable", 3))]}
            )
            validate_coding_brief(coding_brief, max_files_editable=int(self.config.get("max_files_editable", 3)))
            jira_comment_draft = build_jira_comment_draft(manager_output, coding_brief)
            next_status = RunStatus.CODING.value
        else:
            jira_comment_draft = build_jira_comment_draft(manager_output, None)
            next_status = RunStatus.WAITING_FOR_INFO.value

        updates: GraphState = {
            "status": next_status,
            "jira_ticket": ticket,
            "manager_pack": manager_pack.model_dump(mode="json"),
            "manager_output": manager_output.model_dump(mode="json"),
            "manager_decision_meta": {
                "mode": mode,
                "team_profile": team_profile,
                "confidence": round(confidence, 4),
                "confidence_reason": confidence_reason,
                "min_confidence": min_confidence,
                "autonomy_gate_triggered": autonomy_gate_triggered,
                "acceptance_check_triggered": acceptance_check_triggered,
            },
            "mode": mode,
            "team_profile": team_profile,
            "coding_brief": coding_brief.model_dump(mode="json") if coding_brief else None,
            "jira_comment_draft": jira_comment_draft.model_dump(mode="json"),
            "timeline": timeline,
        }
        category_key = (
            self.policy_registry.selector.category_key if self.policy_registry else "team_profile|ticket_type|risk_tier"
        )
        updates = self._apply_updates_with_event(
            state,
            updates,
            step="manager",
            event_type="decision",
            payload={
                "decision": manager_output.decision.value,
                "model_mode": manager_meta.mode,
                "model_reason": manager_meta.reason,
                "risk_tier": manager_output.risk_tier.value,
                "selected_policy_id": selected_policy_id,
                "explored": explored,
                "category_estimate": category_estimate,
                "category_actual": category_from_manager_output(
                    manager_output.model_dump(mode="json"),
                    ticket_payload=model_input,
                    category_key=category_key,
                ),
                "runtime_ms": policy_metrics.get("runtime_ms"),
                "cost_proxy": policy_metrics.get("cost_proxy"),
                "mode": mode,
                "team_profile": team_profile,
                "confidence": round(confidence, 4),
                "confidence_reason": confidence_reason,
                "min_confidence": min_confidence,
                "autonomy_gate_triggered": autonomy_gate_triggered,
            },
        )
        return updates

    def _run_manager_policy(
        self,
        ticket_id: str,
        run_id: str,
        model_input: dict[str, Any],
        evidence: list[dict[str, Any]],
        repo_candidates: list[str],
    ) -> tuple[ManagerOutput, ManagerRunMeta, str | None, bool, str | None, dict[str, Any]]:
        operate_enabled = bool(self.config.get("operate", {}).get("enabled", True))
        if not operate_enabled:
            raise RuntimeError(
                "Manager runtime is policy-driven. Enable `operate.enabled=true` to run manager decisions."
            )
        if not self.policy_registry or not self.policy_executor:
            raise RuntimeError(
                "Manager runtime requires configured manager policies at "
                f"{self.config_path.parent / 'manager_policies.yaml'}."
            )

        selector_live = bool(self.config.get("operate", {}).get("selector_enabled_live", True))
        if not self.policy_registry.selector.enabled_live:
            selector_live = False
        category_key = self.policy_registry.selector.category_key
        category_estimate = estimate_category(model_input, category_key=category_key)

        if selector_live and self.policy_selector:
            selection = self.policy_selector.choose_policy(category_estimate)
            selected_policy_id = selection.policy_id
            explored = selection.explored
            epsilon = selection.epsilon
        else:
            selected_policy_id = self.policy_registry.selector.default_policy_id
            explored = False
            epsilon = self.policy_registry.selector.epsilon

        result = self.policy_executor.run_policy(
            policy_id=selected_policy_id,
            ticket_payload=model_input,
            evidence=evidence,
            repo_file_candidates=repo_candidates,
        )
        manager_output = ManagerOutput.model_validate(result.output)
        manager_meta = ManagerRunMeta(
            mode=result.meta.get("mode", selected_policy_id),
            reason=result.meta.get("reason"),
        )
        category_actual = category_from_manager_output(
            result.output,
            ticket_payload=model_input,
            category_key=category_key,
        )
        self.store.log_live_policy_decision(
            ticket_id=ticket_id,
            run_id=run_id or None,
            team_profile=str(model_input.get("team_profile", "")).strip() or None,
            category_estimate=category_estimate,
            category_actual=category_actual,
            selected_policy_id=selected_policy_id,
            explored=explored,
            epsilon=epsilon,
            runtime_ms=int(result.metrics.get("runtime_ms", 0)),
            cost_proxy=float(result.metrics.get("cost_proxy", 0.0)),
        )
        return (
            manager_output,
            manager_meta,
            selected_policy_id,
            explored,
            category_estimate,
            result.metrics,
        )

    def _node_ask_for_info(self, state: GraphState) -> GraphState:
        draft = state.get("jira_comment_draft", {})
        body = str(draft.get("body", "Need additional info"))
        notify_emails = self._notification_emails(state)
        comms_plan, comms_meta = self._build_human_loop_comms_plan(
            state=state,
            scenario="ask_for_info",
            default_jira_body=body,
            notify_emails=notify_emails,
        )

        result = self.jira.add_comment(state["ticket_id"], comms_plan["jira_comment_body"])
        self.jira.transition_status(state["ticket_id"], "WAITING_FOR_INFO")

        self._ensure_tool("finalizer", "confluence_draft")
        try:
            kb_result = self.confluence.create_draft(comms_plan["confluence_title"], comms_plan["confluence_body"])
            confluence_draft = ConfluenceDraft(
                title=comms_plan["confluence_title"],
                body=comms_plan["confluence_body"],
                draft_id=str(kb_result.get("draft_id", "")),
            )
        except Exception as exc:  # noqa: BLE001
            fallback = MockConfluenceClient()
            kb_result = fallback.create_draft(comms_plan["confluence_title"], comms_plan["confluence_body"])
            confluence_draft = ConfluenceDraft(
                title=str(kb_result.get("title", comms_plan["confluence_title"])),
                body=str(kb_result.get("body", comms_plan["confluence_body"]))
                + f"\n\n[Confluence fallback reason: {exc}]",
                draft_id=str(kb_result.get("draft_id", "")),
            )

        self._ensure_tool("finalizer", "calendar")
        duration_minutes = int(comms_plan.get("meeting_duration_minutes", 30))
        try:
            slots = self.calendar.find_free_slots(notify_emails, duration_minutes=duration_minutes, max_slots=3)
            calendar_proposal = self.calendar.propose_slots(
                slots,
                duration_minutes=duration_minutes,
                timezone_name=self.config.get("timezone", "UTC"),
            )
        except Exception as exc:  # noqa: BLE001
            fallback = MockCalendarClient()
            slots = fallback.find_free_slots(notify_emails, duration_minutes=duration_minutes, max_slots=3)
            calendar_proposal = fallback.propose_slots(slots, duration_minutes=duration_minutes, timezone_name="UTC")
            calendar_proposal.ics = (calendar_proposal.ics or "") + f"\n# calendar_fallback_reason={exc}"

        email_body = (
            f"{comms_plan['email_body']}\n\n"
            f"Suggested sync objective: {comms_plan['meeting_objective']}\n"
            "Suggested slots:\n- " + "\n- ".join(calendar_proposal.slots)
        )
        self._ensure_tool("finalizer", "email")
        try:
            email_draft = self.email.create_draft(notify_emails, comms_plan["email_subject"], email_body)
        except Exception as exc:  # noqa: BLE001
            email_draft = EmailDraftArtifact(
                to=notify_emails,
                subject=comms_plan["email_subject"],
                body=email_body + f"\n\n[Fallback draft mode due to error: {exc}]",
                provider_draft_id=None,
            )

        updates: GraphState = {
            "status": RunStatus.WAITING_FOR_INFO.value,
            "jira_comment_id": str(result.get("comment_id")),
            "jira_comment_draft": {"body": comms_plan["jira_comment_body"]},
            "confluence_draft": confluence_draft.model_dump(mode="json"),
            "calendar_proposal": CalendarProposal.model_validate(calendar_proposal).model_dump(mode="json"),
            "email_draft": EmailDraftArtifact.model_validate(email_draft).model_dump(mode="json"),
        }
        updates = self._apply_updates_with_event(
            state,
            updates,
            step="ask_for_info",
            event_type="artifact",
            payload={
                "artifact": "human_loop_comms",
                "comment_id": result.get("comment_id"),
                "llm_mode": comms_meta.get("mode"),
                "llm_reason": comms_meta.get("reason"),
                "llm_provider": comms_meta.get("provider"),
                "llm_model": comms_meta.get("model"),
                "tavily_hits": comms_meta.get("tavily_hits", 0),
                "meeting_slots": len(calendar_proposal.slots),
            },
        )
        return updates

    def _node_engineer(self, state: GraphState) -> GraphState:
        brief = CodingBrief.model_validate(state.get("coding_brief") or {})
        self._ensure_tool("engineer", "file_read")
        files = read_target_files(self.repo_root, brief.suspected_files)
        engineer_pack = EngineerPack(
            coding_brief=brief,
            file_texts=files,
            token_budget=int(self.config.get("token_budgets", {}).get("engineer_pack", 3500)),
        )
        self._ensure_tool("engineer", "gemini_generate")
        patch_artifact, meta = self.engineer.run_engineer(engineer_pack)
        validate_patch_artifact(patch_artifact)
        updates: GraphState = {
            "status": RunStatus.CODING.value,
            "engineer_pack": engineer_pack.model_dump(mode="json"),
            "patch_artifact": patch_artifact.model_dump(mode="json"),
        }
        updates = self._apply_updates_with_event(
            state,
            updates,
            step="engineer",
            event_type="artifact",
            payload={
                "artifact": "patch",
                "mode": meta.get("mode"),
                "reason": meta.get("reason"),
            },
        )
        return updates

    def _node_auditor_prepare(self, state: GraphState) -> GraphState:
        brief = CodingBrief.model_validate(state.get("coding_brief") or {})
        patch = state.get("patch_artifact") or {}
        patch_artifact = PatchArtifact.model_validate(patch)
        timeline = list(state.get("timeline", []))

        try:
            validate_patch_artifact(patch_artifact)
            timeline = self._append_event(
                {"timeline": timeline},
                step="auditor_prepare",
                event_type="guard",
                payload={"check": "patch_artifact_schema", "result": "pass"},
            )
        except Exception as exc:  # noqa: BLE001
            return self._apply_updates_with_event(
                state,
                {"status": RunStatus.FAILED.value, "errors": [str(exc)], "timeline": timeline},
                step="auditor_prepare",
                event_type="guard",
                payload={"check": "patch_artifact_schema", "result": "fail", "error": str(exc)},
            )

        self._ensure_tool("auditor", "patch_scope_verify")
        try:
            changed_files = verify_patch_scope(patch_artifact.diff, brief.suspected_files)
            timeline = self._append_event(
                {"timeline": timeline},
                step="auditor_prepare",
                event_type="guard",
                payload={"check": "patch_scope_verify", "result": "pass", "changed_files": changed_files},
            )
        except Exception as exc:  # noqa: BLE001
            return self._apply_updates_with_event(
                state,
                {"status": RunStatus.FAILED.value, "errors": [str(exc)], "timeline": timeline},
                step="auditor_prepare",
                event_type="guard",
                payload={"check": "patch_scope_verify", "result": "fail", "error": str(exc)},
            )

        self._ensure_tool("auditor", "risk_verify")
        try:
            apply_high_risk_strict_checks(brief.risk_tier, patch_artifact.diff)
            timeline = self._append_event(
                {"timeline": timeline},
                step="auditor_prepare",
                event_type="guard",
                payload={"check": "high_risk_strict_checks", "result": "pass", "risk_tier": brief.risk_tier.value},
            )
        except Exception as exc:  # noqa: BLE001
            return self._apply_updates_with_event(
                state,
                {"status": RunStatus.FAILED.value, "errors": [str(exc)], "timeline": timeline},
                step="auditor_prepare",
                event_type="guard",
                payload={"check": "high_risk_strict_checks", "result": "fail", "error": str(exc)},
            )

        risk_policy = self.config.get("risk_tiers", {}).get(brief.risk_tier.value, "standard")
        auditor_pack = AuditorPack(
            diff=patch_artifact.diff,
            test_output="Tests were not executed in prototype mode.",
            risk_summary=f"risk_tier={brief.risk_tier.value}; policy={risk_policy}; changed_files={changed_files}",
            changed_files=changed_files,
        )

        pre_interrupt: GraphState = {
            "status": RunStatus.AWAITING_APPROVAL.value,
            "patch_artifact": patch_artifact.model_dump(mode="json") | {"changed_files": changed_files},
            "auditor_pack": auditor_pack.model_dump(mode="json"),
            "timeline": timeline,
        }
        return self._apply_updates_with_event(
            state,
            pre_interrupt,
            step="auditor_prepare",
            event_type="decision",
            payload={
                "message": "Patch validated. Awaiting approval.",
                "changed_files": changed_files,
            },
        )

    def _node_auditor_gate(self, state: GraphState) -> GraphState:
        auditor_pack = AuditorPack.model_validate(state.get("auditor_pack") or {})
        approval_payload = interrupt(
            {
                "ticket_id": state["ticket_id"],
                "status": RunStatus.AWAITING_APPROVAL.value,
                "changed_files": auditor_pack.changed_files,
                "risk_summary": auditor_pack.risk_summary,
            }
        )

        approval = ApprovalRequest.model_validate(approval_payload)
        record = ApprovalRecord(
            reviewer=approval.reviewer,
            approved=approval.approved,
            comments=approval.comments,
        )
        final_status = RunStatus.APPROVED.value if approval.approved else RunStatus.REJECTED.value
        updates: GraphState = {
            "status": final_status,
            "approval": record.model_dump(mode="json"),
        }
        return self._apply_updates_with_event(
            state,
            updates,
            step="auditor_gate",
            event_type="approval",
            payload={"reviewer": approval.reviewer, "approved": approval.approved},
        )

    def _node_finalizer(self, state: GraphState) -> GraphState:
        brief = CodingBrief.model_validate(state.get("coding_brief") or {})
        patch = state.get("patch_artifact") or {}
        pr_title = f"[{state['ticket_id']}] {brief.summary}"
        pr_body = (
            f"Automated prototype PR for ticket {state['ticket_id']}.\n\n"
            f"Hypothesis: {brief.hypothesis}\n\n"
            f"Acceptance criteria:\n- " + "\n- ".join(brief.acceptance_criteria)
        )
        self._ensure_tool("finalizer", "bitbucket_create_pr")
        pr_result = self.bitbucket.create_pr(pr_title, pr_body, str(patch.get("diff", "")))
        pr_artifact = PullRequestArtifact(
            pr_number=int(pr_result["pr_number"]),
            url=str(pr_result["url"]),
            title=str(pr_result["title"]),
            body=str(pr_result["body"]),
        )

        self._ensure_tool("finalizer", "jira_comment")
        jira_comment = self.jira.add_comment(
            state["ticket_id"],
            f"Patch approved by {state.get('approval', {}).get('reviewer', 'reviewer')}.\nPR: {pr_artifact.url}",
        )
        self.jira.transition_status(state["ticket_id"], "IN_REVIEW")

        updates: GraphState = {
            "status": RunStatus.FINALIZING.value,
            "pr_artifact": pr_artifact.model_dump(mode="json"),
            "jira_comment_id": str(jira_comment.get("comment_id")),
        }
        updates = self._apply_updates_with_event(
            state,
            updates,
            step="finalizer",
            event_type="artifact",
            payload={"artifact": "pr", "pr_url": pr_artifact.url},
        )
        return updates

    def _node_kb_and_comms(self, state: GraphState) -> GraphState:
        brief = CodingBrief.model_validate(state.get("coding_brief") or {})
        pr = state.get("pr_artifact") or {}
        mode = str(state.get("mode", "")).strip().lower() or "incident"
        escalation_suffix = (
            " Customer escalation mode: include customer-facing summary and clear ETA commitment."
            if mode == "customer_escalation"
            else ""
        )

        self._ensure_tool("finalizer", "confluence_draft")
        default_kb_draft = build_confluence_draft(
            ticket_id=state["ticket_id"],
            context={
                "summary": brief.summary,
                "root_cause": brief.hypothesis,
                "fix": f"Proposed patch in PR {pr.get('url')}",
                "verification": "Run target regression checks before merge.",
                "prevention": f"Capture lessons in tests and monitoring.{escalation_suffix}",
            },
        )
        notify_emails = self._notification_emails(state)
        comms_plan, comms_meta = self._build_human_loop_comms_plan(
            state=state,
            scenario="post_patch_handoff",
            default_jira_body="",
            notify_emails=notify_emails,
            default_confluence_title=default_kb_draft.title,
            default_confluence_body=default_kb_draft.body,
            default_email_subject=f"[{state['ticket_id']}] PR ready for review",
            default_email_body=(
                f"Ticket: {state['ticket_id']}\n"
                f"PR: {pr.get('url')}\n"
                f"Mode: {mode}\n"
                f"Risk tier: {brief.risk_tier.value}\n"
                "Please review and reply in Jira with feedback or approval notes."
            ),
        )
        try:
            kb_result = self.confluence.create_draft(comms_plan["confluence_title"], comms_plan["confluence_body"])
            kb_draft = ConfluenceDraft(
                title=comms_plan["confluence_title"],
                body=comms_plan["confluence_body"],
                draft_id=str(kb_result.get("draft_id", "")),
            )
        except Exception as exc:  # noqa: BLE001
            fallback = MockConfluenceClient()
            kb_result = fallback.create_draft(comms_plan["confluence_title"], comms_plan["confluence_body"])
            kb_draft = ConfluenceDraft(
                title=str(kb_result.get("title", comms_plan["confluence_title"])),
                body=str(kb_result.get("body", comms_plan["confluence_body"]))
                + f"\n\n[Confluence fallback reason: {exc}]",
                draft_id=str(kb_result.get("draft_id", "")),
            )

        self._ensure_tool("finalizer", "calendar")
        duration_minutes = int(comms_plan.get("meeting_duration_minutes", 30))
        try:
            slots = self.calendar.find_free_slots(notify_emails, duration_minutes=duration_minutes, max_slots=3)
            calendar_proposal = self.calendar.propose_slots(
                slots,
                duration_minutes=duration_minutes,
                timezone_name=self.config.get("timezone", "UTC"),
            )
        except Exception as exc:  # noqa: BLE001
            fallback = MockCalendarClient()
            slots = fallback.find_free_slots(notify_emails, duration_minutes=duration_minutes, max_slots=3)
            calendar_proposal = fallback.propose_slots(
                slots,
                duration_minutes=duration_minutes,
                timezone_name="UTC",
            )
            calendar_proposal.ics = (calendar_proposal.ics or "") + f"\n# calendar_fallback_reason={exc}"

        self._ensure_tool("finalizer", "email")
        subject_prefix = "[ESCALATION] " if mode == "customer_escalation" else ""
        subject = f"{subject_prefix}{comms_plan['email_subject']}"
        body = (
            f"{comms_plan['email_body']}\n\n"
            f"Suggested review objective: {comms_plan['meeting_objective']}\n"
            "Suggested review slots:\n- " + "\n- ".join(calendar_proposal.slots)
        )
        try:
            email_draft = self.email.create_draft(notify_emails, subject, body)
        except Exception as exc:  # noqa: BLE001
            email_draft = EmailDraftArtifact(
                to=notify_emails,
                subject=subject,
                body=body + f"\n\n[Fallback draft mode due to error: {exc}]",
                provider_draft_id=None,
            )

        updates: GraphState = {
            "status": RunStatus.COMPLETED.value,
            "confluence_draft": kb_draft.model_dump(mode="json"),
            "calendar_proposal": CalendarProposal.model_validate(calendar_proposal).model_dump(mode="json"),
            "email_draft": EmailDraftArtifact.model_validate(email_draft).model_dump(mode="json"),
        }
        updates = self._apply_updates_with_event(
            state,
            updates,
            step="kb_and_comms",
            event_type="artifact",
            payload={
                "artifact": "knowledge_and_comms",
                "slots": calendar_proposal.slots,
                "llm_mode": comms_meta.get("mode"),
                "llm_reason": comms_meta.get("reason"),
                "llm_provider": comms_meta.get("provider"),
                "llm_model": comms_meta.get("model"),
                "tavily_hits": comms_meta.get("tavily_hits", 0),
            },
        )
        return updates

    def _node_rejected(self, state: GraphState) -> GraphState:
        reviewer = state.get("approval", {}).get("reviewer", "reviewer")
        comments = state.get("approval", {}).get("comments", "")
        notify_emails = self._notification_emails(state)
        default_body = f"Patch was not approved by {reviewer}. Comments: {comments}"
        comms_plan, comms_meta = self._build_human_loop_comms_plan(
            state=state,
            scenario="rejected_feedback",
            default_jira_body=default_body,
            notify_emails=notify_emails,
            review_comments=comments,
        )
        result = self.jira.add_comment(
            state["ticket_id"],
            comms_plan["jira_comment_body"],
        )
        self._ensure_tool("finalizer", "calendar")
        duration_minutes = int(comms_plan.get("meeting_duration_minutes", 30))
        try:
            slots = self.calendar.find_free_slots(notify_emails, duration_minutes=duration_minutes, max_slots=3)
            calendar_proposal = self.calendar.propose_slots(
                slots,
                duration_minutes=duration_minutes,
                timezone_name=self.config.get("timezone", "UTC"),
            )
        except Exception as exc:  # noqa: BLE001
            fallback = MockCalendarClient()
            slots = fallback.find_free_slots(notify_emails, duration_minutes=duration_minutes, max_slots=3)
            calendar_proposal = fallback.propose_slots(
                slots,
                duration_minutes=duration_minutes,
                timezone_name="UTC",
            )
            calendar_proposal.ics = (calendar_proposal.ics or "") + f"\n# calendar_fallback_reason={exc}"

        self._ensure_tool("finalizer", "email")
        email_body = (
            f"{comms_plan['email_body']}\n\n"
            f"Meeting objective: {comms_plan['meeting_objective']}\n"
            "Suggested slots:\n- " + "\n- ".join(calendar_proposal.slots)
        )
        try:
            email_draft = self.email.create_draft(notify_emails, comms_plan["email_subject"], email_body)
        except Exception as exc:  # noqa: BLE001
            email_draft = EmailDraftArtifact(
                to=notify_emails,
                subject=comms_plan["email_subject"],
                body=email_body + f"\n\n[Fallback draft mode due to error: {exc}]",
                provider_draft_id=None,
            )

        self._ensure_tool("finalizer", "confluence_draft")
        try:
            kb_result = self.confluence.create_draft(comms_plan["confluence_title"], comms_plan["confluence_body"])
            confluence_draft = ConfluenceDraft(
                title=comms_plan["confluence_title"],
                body=comms_plan["confluence_body"],
                draft_id=str(kb_result.get("draft_id", "")),
            )
        except Exception as exc:  # noqa: BLE001
            fallback = MockConfluenceClient()
            kb_result = fallback.create_draft(comms_plan["confluence_title"], comms_plan["confluence_body"])
            confluence_draft = ConfluenceDraft(
                title=str(kb_result.get("title", comms_plan["confluence_title"])),
                body=str(kb_result.get("body", comms_plan["confluence_body"]))
                + f"\n\n[Confluence fallback reason: {exc}]",
                draft_id=str(kb_result.get("draft_id", "")),
            )
        updates: GraphState = {
            "status": RunStatus.REJECTED.value,
            "jira_comment_id": str(result.get("comment_id")),
            "confluence_draft": confluence_draft.model_dump(mode="json"),
            "calendar_proposal": CalendarProposal.model_validate(calendar_proposal).model_dump(mode="json"),
            "email_draft": EmailDraftArtifact.model_validate(email_draft).model_dump(mode="json"),
        }
        updates = self._apply_updates_with_event(
            state,
            updates,
            step="rejected",
            event_type="artifact",
            payload={
                "message": "workflow rejected by reviewer",
                "llm_mode": comms_meta.get("mode"),
                "llm_reason": comms_meta.get("reason"),
                "llm_provider": comms_meta.get("provider"),
                "llm_model": comms_meta.get("model"),
                "tavily_hits": comms_meta.get("tavily_hits", 0),
            },
        )
        return updates

    def _route_after_manager(self, state: GraphState) -> str:
        if state.get("status") == RunStatus.FAILED.value:
            return "FAILED"
        manager_output = state.get("manager_output") or {}
        decision = str(manager_output.get("decision", "ASK_FOR_INFO"))
        return "READY_TO_PATCH" if decision == "READY_TO_PATCH" else "ASK_FOR_INFO"

    def _route_after_auditor_prepare(self, state: GraphState) -> str:
        return "FAILED" if str(state.get("status", "")) == RunStatus.FAILED.value else "READY"

    def _route_after_auditor(self, state: GraphState) -> str:
        status = str(state.get("status", "FAILED"))
        if status == RunStatus.APPROVED.value:
            return "APPROVED"
        if status == RunStatus.REJECTED.value:
            return "REJECTED"
        return "FAILED"

    def _apply_updates_with_event(
        self,
        state: GraphState,
        updates: GraphState,
        step: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> GraphState:
        timeline = list(updates.get("timeline", state.get("timeline", [])))
        timeline.append(
            {
                "ts": utc_iso(),
                "step": step,
                "event_type": event_type,
                "payload": payload,
            }
        )
        errors = list(state.get("errors", []))
        if updates.get("errors"):
            errors.extend(updates["errors"])
        out: GraphState = dict(state)
        out.update(updates)
        out["timeline"] = timeline
        out["errors"] = errors
        out["updated_at"] = utc_iso()
        if "created_at" not in out:
            out["created_at"] = utc_iso()
        return out

    def _append_event(
        self,
        state: GraphState,
        step: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        timeline = list(state.get("timeline", []))
        timeline.append(
            {
                "ts": utc_iso(),
                "step": step,
                "event_type": event_type,
                "payload": payload,
            }
        )
        return timeline

    def _notification_emails(self, state: GraphState) -> list[str]:
        notify = os.getenv("NOTIFY_EMAILS", "").strip()
        from_env = [item.strip() for item in notify.split(",") if item.strip()] if notify else []
        reporter = state.get("jira_payload", {}).get("user", {}).get("emailAddress")
        emails = []
        if isinstance(reporter, str) and reporter.strip():
            emails.append(reporter.strip())
        for address in from_env:
            if address not in emails:
                emails.append(address)
        if not emails:
            emails = ["engineering@example.com"]
        return emails

    def _build_human_loop_comms_plan(
        self,
        state: GraphState,
        scenario: str,
        default_jira_body: str,
        notify_emails: list[str],
        review_comments: str | None = None,
        default_confluence_title: str | None = None,
        default_confluence_body: str | None = None,
        default_email_subject: str | None = None,
        default_email_body: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        ticket_id = str(state.get("ticket_id", "")).strip()
        jira_ticket = state.get("jira_ticket", {})
        summary = str(jira_ticket.get("summary", "")).strip() or ticket_id
        description = str(jira_ticket.get("description", "")).strip()
        mode = str(state.get("mode", "")).strip().lower() or "incident"
        risk_tier = str((state.get("manager_output") or {}).get("risk_tier", "")).strip().lower() or "unknown"
        manager_output = state.get("manager_output") or {}
        raw_questions = manager_output.get("questions_needed", [])
        questions = [str(item).strip() for item in raw_questions if str(item).strip()] if isinstance(raw_questions, list) else []
        reviewer_notes = (review_comments or "").strip()
        pr_url = str((state.get("pr_artifact") or {}).get("url", "")).strip()

        web_hits: list[dict[str, str]] = []
        if self.config.get("enable_tavily", True):
            query_candidates = [summary]
            query_candidates.extend(questions[:2])
            if reviewer_notes:
                query_candidates.append(reviewer_notes[:180])
            deduped_queries: list[str] = []
            for query in query_candidates:
                cleaned = str(query).strip()
                if not cleaned or cleaned in deduped_queries:
                    continue
                deduped_queries.append(cleaned)
            try:
                self._ensure_tool("manager", "tavily_search")
                for query in deduped_queries[:2]:
                    results = self.tavily.search(query, max_results=2)
                    for item in results:
                        snippet = str(item.get("snippet", "")).strip()
                        title = str(item.get("title", "")).strip()
                        url = str(item.get("url", "")).strip()
                        if snippet or title:
                            web_hits.append(
                                {
                                    "query": query,
                                    "title": title[:200],
                                    "url": url[:500],
                                    "snippet": snippet[:500],
                                }
                            )
            except Exception:
                web_hits = []

        scenario_title = {
            "ask_for_info": "Discovery loop: additional ticket details required",
            "rejected_feedback": "Rework loop: reviewer requested changes",
            "post_patch_handoff": "Handoff loop: patch ready for validation and communication",
        }.get(scenario, "Human loop follow-up")

        default_email_subject = default_email_subject or f"[{ticket_id}] Action required: additional details needed"
        default_email_body = default_email_body or (
            f"Ticket: {ticket_id}\n"
            f"Summary: {summary}\n"
            f"Mode: {mode}\n"
            f"Risk tier: {risk_tier}\n"
            "Please help unblock this ticket by sharing missing details in Jira.\n"
            + (
                "Required details:\n- " + "\n- ".join(questions)
                if questions
                else "Required details: steps to reproduce, expected/actual behavior, environment, and logs."
            )
        )
        default_confluence_title = default_confluence_title or f"[Draft] Human loop notes for {ticket_id}"
        default_confluence_body = default_confluence_body or (
            "## Current status\n"
            f"{scenario_title}\n\n"
            "## Ticket summary\n"
            f"{summary}\n\n"
            "## Missing details\n"
            + (
                "\n".join(f"- {question}" for question in questions)
                if questions
                else "- Need reproduction details, logs, and environment information."
            )
            + "\n\n## Reviewer notes\n"
            + (reviewer_notes or "No reviewer notes.")
            + "\n\n## Web context (Tavily)\n"
            + (
                "\n".join(
                    f"- {item.get('title', 'Context')}: {item.get('snippet', '')} ({item.get('url', '')})"
                    for item in web_hits[:4]
                )
                if web_hits
                else "- No external context captured."
            )
        )

        plan: dict[str, Any] = {
            "jira_comment_body": str(default_jira_body).strip() or "Additional information is required.",
            "email_subject": default_email_subject,
            "email_body": default_email_body,
            "meeting_objective": "Align on missing context, unblock decision-making, and confirm next owner/actions.",
            "meeting_duration_minutes": 30,
            "confluence_title": default_confluence_title,
            "confluence_body": default_confluence_body,
        }
        meta: dict[str, Any] = {
            "mode": "deterministic",
            "reason": "llm_comms_disabled",
            "provider": None,
            "model": None,
            "tavily_hits": len(web_hits),
        }

        llm_cfg = self.config.get("llm_comms", {})
        enabled_raw = llm_cfg.get("enabled", True)
        enabled = str(enabled_raw).strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return plan, meta

        llm_payload = {
            "scenario": scenario,
            "ticket_id": ticket_id,
            "summary": summary,
            "description": description,
            "mode": mode,
            "risk_tier": risk_tier,
            "notify_emails": notify_emails,
            "questions_needed": questions,
            "review_comments": reviewer_notes,
            "pr_url": pr_url,
            "web_context": web_hits,
            "defaults": plan,
        }
        try:
            llm_out, llm_meta = self._run_comms_llm(llm_payload)
            for key in (
                "jira_comment_body",
                "email_subject",
                "email_body",
                "meeting_objective",
                "confluence_title",
                "confluence_body",
            ):
                value = str(llm_out.get(key, "")).strip()
                if value:
                    plan[key] = value
            duration_raw = llm_out.get("meeting_duration_minutes", plan["meeting_duration_minutes"])
            try:
                duration = int(duration_raw)
            except Exception:
                duration = int(plan["meeting_duration_minutes"])
            plan["meeting_duration_minutes"] = max(15, min(duration, 90))
            meta = {
                "mode": "llm",
                "reason": f"provider={llm_meta.get('provider')};model={llm_meta.get('model')}",
                "provider": llm_meta.get("provider"),
                "model": llm_meta.get("model"),
                "tavily_hits": len(web_hits),
            }
        except Exception as exc:  # noqa: BLE001
            meta = {
                "mode": "deterministic_fallback",
                "reason": str(exc),
                "provider": None,
                "model": None,
                "tavily_hits": len(web_hits),
            }
        return plan, meta

    def _run_comms_llm(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        llm_cfg = self.config.get("llm_comms", {})
        provider = str(llm_cfg.get("provider", os.getenv("COMMS_LLM_PROVIDER", "ollama"))).strip().lower() or "ollama"
        model = self._resolve_comms_model(provider)
        if not model:
            raise RuntimeError(f"missing comms model for provider={provider}")
        temperature = float(llm_cfg.get("temperature", os.getenv("COMMS_LLM_TEMPERATURE", "0.1")))
        max_tokens = int(llm_cfg.get("max_tokens", os.getenv("COMMS_LLM_MAX_TOKENS", "900")))
        client = self._comms_provider_client(provider=provider, model=model)
        response = client.chat(
            LLMRequest(
                system=(
                    "You are a communications copilot for incident and ticket operations.\n"
                    "Return ONLY one JSON object with no markdown and no extra text.\n"
                    "Schema:\n"
                    "{\n"
                    '  "jira_comment_body": string,\n'
                    '  "email_subject": string,\n'
                    '  "email_body": string,\n'
                    '  "meeting_objective": string,\n'
                    '  "meeting_duration_minutes": number,\n'
                    '  "confluence_title": string,\n'
                    '  "confluence_body": string\n'
                    "}\n"
                    "Rules:\n"
                    "- keep content concise and actionable.\n"
                    "- preserve factual details from provided ticket context.\n"
                    "- do not invent ticket IDs or system states.\n"
                ),
                user=json.dumps(payload, ensure_ascii=False),
                temperature=temperature,
                max_tokens=max_tokens,
                expect_json=True,
            ),
            model=model,
        )
        parsed = json.loads(self._extract_first_json(response.text))
        if not isinstance(parsed, dict):
            raise ValueError("comms llm output must be a JSON object")
        return (
            parsed,
            {
                "provider": response.provider,
                "model": response.model,
                "latency_ms": response.latency_ms,
            },
        )

    def _comms_provider_client(self, provider: str, model: str):
        if provider == "ollama":
            base_url = os.getenv("COMMS_OLLAMA_BASE_URL", "").strip() or os.getenv("OLLAMA_BASE_URL", "").strip() or None
            return OllamaClient(default_model=model, base_url=base_url)

        if provider == "openrouter":
            api_key = os.getenv("COMMS_OPENROUTER_API_KEY", "").strip() or os.getenv("OPENROUTER_API_KEY", "").strip()
            base_url = (
                os.getenv("COMMS_OPENROUTER_BASE_URL", "").strip()
                or os.getenv("OPENROUTER_BASE_URL", "").strip()
                or None
            )
            return OpenRouterClient(api_key=api_key, default_model=model, base_url=base_url)

        if provider == "groq":
            api_key = os.getenv("COMMS_GROQ_API_KEY", "").strip() or os.getenv("GROQ_API_KEY", "").strip()
            base_url = os.getenv("COMMS_GROQ_BASE_URL", "").strip() or os.getenv("GROQ_BASE_URL", "").strip() or None
            return GroqClient(api_key=api_key, default_model=model, base_url=base_url)

        if provider == "gemini":
            api_key = os.getenv("COMMS_GEMINI_API_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
            base_url = os.getenv("COMMS_GEMINI_BASE_URL", "").strip() or os.getenv("GEMINI_API_BASE", "").strip() or None
            return GeminiDirectClient(api_key=api_key, default_model=model, base_url=base_url)

        raise RuntimeError(f"unsupported comms llm provider: {provider}")

    def _resolve_comms_model(self, provider: str) -> str:
        env_map = {
            "ollama": ("COMMS_OLLAMA_MODEL", "MANAGER_OLLAMA_MODEL"),
            "openrouter": ("COMMS_OPENROUTER_MODEL", "MANAGER_OPENROUTER_MODEL", "OPENROUTER_MODEL"),
            "groq": ("COMMS_GROQ_MODEL", "MANAGER_GROQ_MODEL", "JUDGE_GROQ_MODEL", "GROQ_MODEL"),
            "gemini": ("COMMS_GEMINI_MODEL", "JUDGE_GEMINI_MODEL", "ENGINEER_GEMINI_MODEL", "GEMINI_MODEL"),
        }.get(provider, ())
        for env_name in env_map:
            value = os.getenv(env_name, "").strip()
            if value:
                return value
        defaults = {
            "ollama": "qwen2.5:3b-instruct",
            "openrouter": "openai/gpt-4o-mini",
            "groq": "llama-3.3-70b-versatile",
            "gemini": "gemini-2.5-flash",
        }
        return defaults.get(provider, "")

    def _extract_first_json(self, text: str) -> str:
        start = text.find("{")
        if start < 0:
            raise ValueError("no JSON object found in LLM response")
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
        raise ValueError("incomplete JSON object in LLM response")

    def _derive_mode_and_team(
        self,
        jira_payload: dict[str, Any],
        ticket: dict[str, Any],
        labels: list[str],
        summary: str,
        description: str,
    ) -> tuple[str, str]:
        lower_labels = [str(item).strip().lower() for item in labels if str(item).strip()]
        fields = jira_payload.get("issue", {}).get("fields", {})
        explicit_mode = str(fields.get("mode", "")).strip().lower()
        explicit_team = str(fields.get("team_profile", "")).strip().lower()
        text = f"{summary}\n{description}".lower()

        mode = explicit_mode
        if mode not in {"incident", "feature", "customer_escalation"}:
            if "mode:incident" in lower_labels or any(token in text for token in ["incident", "outage", "sev", "p1"]):
                mode = "incident"
            elif "mode:customer_escalation" in lower_labels or any(
                token in text for token in ["customer escalation", "vip customer", "escalated account"]
            ):
                mode = "customer_escalation"
            elif "mode:feature" in lower_labels or any(token in text for token in ["feature request", "enhancement"]):
                mode = "feature"
            else:
                mode = "incident"

        team_profile = explicit_team
        if team_profile not in {"platform", "backend", "frontend", "ml_data"}:
            label_to_team = {
                "team:platform": "platform",
                "team:backend": "backend",
                "team:frontend": "frontend",
                "team:ml_data": "ml_data",
                "team:ml-data": "ml_data",
            }
            for key, value in label_to_team.items():
                if key in lower_labels:
                    team_profile = value
                    break
            if team_profile not in {"platform", "backend", "frontend", "ml_data"}:
                if any(token in text for token in ["dataset", "model", "training", "inference", "feature store"]):
                    team_profile = "ml_data"
                elif any(token in text for token in ["react", "ui", "frontend", "css", "browser"]):
                    team_profile = "frontend"
                elif any(token in text for token in ["api", "endpoint", "service", "database", "sql"]):
                    team_profile = "backend"
                else:
                    team_profile = "platform"
        return mode, team_profile

    def _apply_mode_behavior(self, manager_output: ManagerOutput, mode: str) -> ManagerOutput:
        output = manager_output
        if mode == "incident" and output.risk_tier == RiskTier.LOW:
            output = output.model_copy(update={"risk_tier": RiskTier.MEDIUM})
        if mode == "customer_escalation" and output.risk_tier == RiskTier.LOW:
            output = output.model_copy(update={"risk_tier": RiskTier.MEDIUM})
        return output

    def _estimate_manager_confidence(
        self,
        manager_output: ManagerOutput,
        model_mode: str,
        evidence_count: int,
        mode: str,
    ) -> tuple[float, str]:
        confidence = 0.45
        reasons: list[str] = []
        mode_lower = model_mode.lower()
        if mode_lower.startswith("sota_api"):
            confidence += 0.16
            reasons.append("api_model")
        else:
            confidence += 0.08
            reasons.append("non_standard_api_mode")

        evidence_boost = min(0.20, max(0.0, evidence_count * 0.03))
        confidence += evidence_boost
        reasons.append(f"evidence={evidence_count}")

        if manager_output.decision == DecisionType.READY_TO_PATCH:
            confidence += 0.12
            reasons.append("ready_to_patch")
        else:
            confidence -= 0.06
            reasons.append("needs_more_info")

        if manager_output.error_signature != "NONE_PROVIDED":
            confidence += 0.04
            reasons.append("error_signature_present")

        if manager_output.risk_tier == RiskTier.HIGH:
            confidence -= 0.04
            reasons.append("high_risk_penalty")

        if mode == "incident":
            confidence += 0.03
            reasons.append("incident_priority")
        elif mode == "customer_escalation":
            confidence += 0.02
            reasons.append("customer_escalation_priority")

        bounded = max(0.05, min(0.99, confidence))
        return bounded, ";".join(reasons)

    def _force_ask_for_info(self, manager_output: ManagerOutput, reason_question: str) -> ManagerOutput:
        questions = list(manager_output.questions_needed)
        if reason_question not in questions:
            questions.append(reason_question)
        return manager_output.model_copy(
            update={
                "decision": DecisionType.ASK_FOR_INFO,
                "questions_needed": questions[:6],
            }
        )

    def _should_force_ready_for_seeded_ticket(self, labels: list[str]) -> bool:
        enabled = bool(self.config.get("demo", {}).get("force_ready_for_seeded_tickets", False))
        if not enabled:
            return False
        normalized = {str(item).strip().lower() for item in labels if str(item).strip()}
        return any(
            marker in normalized
            for marker in {
                "poc-real",
                "poc-real-demo",
                "poc_seed_real",
            }
        )

    def _promote_seeded_ticket_decision(
        self,
        manager_output: ManagerOutput,
        repo_candidates: list[str],
        max_files_editable: int,
    ) -> ManagerOutput | None:
        if manager_output.decision != DecisionType.ASK_FOR_INFO:
            return None

        existing_files = list(manager_output.coding_brief.suspected_files)
        merged_files: list[str] = []
        for candidate in existing_files + repo_candidates:
            cleaned = str(candidate).strip()
            if cleaned and cleaned not in merged_files:
                merged_files.append(cleaned)
        if not merged_files:
            return None

        acceptance = [item.strip() for item in manager_output.coding_brief.acceptance_criteria if item.strip()]
        defaults = [
            "Fix is scoped to suspected files and reproducer passes.",
            "Regression checks cover the incident path and no new failures are introduced.",
        ]
        for default_item in defaults:
            if len(acceptance) >= 2:
                break
            if default_item not in acceptance:
                acceptance.append(default_item)

        hypothesis = manager_output.coding_brief.hypothesis.strip() or "Scoped fix based on seeded ticket evidence."
        promoted_brief = ManagerCodingBrief(
            suspected_files=merged_files[: max(1, max_files_editable)],
            hypothesis=hypothesis,
            acceptance_criteria=acceptance[:6],
        )
        return manager_output.model_copy(
            update={
                "decision": DecisionType.READY_TO_PATCH,
                "questions_needed": [],
                "coding_brief": promoted_brief,
            }
        )

    def _ensure_tool(self, role: str, tool_name: str) -> None:
        allowed = self.config.get("allowed_tools_by_role", {}).get(role, [])
        if tool_name not in allowed:
            raise PermissionError(f"tool '{tool_name}' is not allowed for role '{role}'")

    def _default_repo_candidates(self) -> list[str]:
        candidates: list[str] = []
        for extension in ("*.py", "*.ts", "*.js", "*.md"):
            for path in self.repo_root.rglob(extension):
                rel = str(path.relative_to(self.repo_root))
                if ".git/" in rel or rel.startswith(".venv/"):
                    continue
                candidates.append(rel)
                if len(candidates) >= 5:
                    return candidates
        return candidates

    def _filter_existing_files(self, candidates: list[str]) -> list[str]:
        max_bytes = int(self.config.get("manager_file_max_bytes", 200000))
        safe: list[str] = []
        for rel in candidates:
            cleaned = rel.strip()
            if not cleaned:
                continue
            full = (self.repo_root / cleaned).resolve()
            if not full.exists() or not full.is_file():
                continue
            if self.repo_root.resolve() not in full.parents:
                continue
            try:
                if full.stat().st_size > max_bytes:
                    continue
            except Exception:
                continue
            if cleaned not in safe:
                safe.append(cleaned)
        return safe


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    output = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(output.get(key), dict):
            output[key] = _deep_merge(output[key], value)  # type: ignore[arg-type]
        else:
            output[key] = value
    return output
