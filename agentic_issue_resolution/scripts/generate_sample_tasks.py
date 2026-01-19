from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PACKAGE_ROOT / "samples" / "tasks.json"

DEFAULT_MODES = ["incident", "feature", "customer_escalation"]
DEFAULT_TEAMS = ["platform", "backend", "frontend", "ml_data"]
DEFAULT_TICKET_TYPES = ["bug", "feature_update", "feature_insert"]
DEFAULT_RISKS = ["high", "medium", "low"]


TEAM_FILE_CANDIDATES = {
    "platform": [
        "agentic_issue_resolution/graph/workflow.py",
        "agentic_issue_resolution/storage/sqlite_store.py",
        "agentic_issue_resolution/app/main.py",
    ],
    "backend": [
        "agentic_issue_resolution/tools/jira.py",
        "agentic_issue_resolution/tools/bitbucket.py",
        "agentic_issue_resolution/tools/confluence.py",
    ],
    "frontend": [
        "agentic_issue_resolution/ui/src/pages/DashboardPage.tsx",
        "agentic_issue_resolution/ui/src/pages/TicketStoryPage.tsx",
        "agentic_issue_resolution/ui/src/pages/TicketsPage.tsx",
    ],
    "ml_data": [
        "agentic_issue_resolution/operate/categorizer.py",
        "agentic_issue_resolution/operate/policy_executor.py",
        "agentic_issue_resolution/operate/judge.py",
    ],
}


MODE_SUMMARY = {
    "incident": "Production incident with customer-visible impact and strict SLA pressure.",
    "feature": "Feature delivery request with measurable acceptance and rollout controls.",
    "customer_escalation": "Escalated customer issue requiring engineering fix plus clear communication artifacts.",
}

TEAM_SUMMARY = {
    "platform": "Reliability guardrails, orchestration behavior, and cross-service runtime consistency.",
    "backend": "API/data contract behavior, integration reliability, and safe migration/rollback handling.",
    "frontend": "Workflow UX quality, status visibility, and operator productivity in dashboard/story views.",
    "ml_data": "Policy selection quality, model-eval reliability, and decision telemetry correctness.",
}

RISK_SUMMARY = {
    "high": "High risk: strict guardrails, rollback clarity, and explicit reviewer evidence required.",
    "medium": "Medium risk: scoped change, targeted regression checks, and clear rollout notes required.",
    "low": "Low risk: focused patch with lightweight verification and handoff completeness.",
}

DOMAIN_PROFILES = [
    {
        "domain": "Checkout Payments",
        "service": "checkout-api",
        "symptom": "coupon apply intermittently returns HTTP 500",
        "kpi": "checkout_success_rate",
        "impact": "abandonment spike for paid users in US region",
        "repro": "apply fixed-value coupon on cart with mixed subscription items",
        "root_hint": "legacy coupon parser path not handling null tax code",
    },
    {
        "domain": "Authentication",
        "service": "identity-gateway",
        "symptom": "SSO callback loops for a subset of enterprise tenants",
        "kpi": "login_success_rate",
        "impact": "customer admins blocked from dashboard access",
        "repro": "login with Okta on tenant using enforced MFA",
        "root_hint": "state nonce validation mismatch after recent middleware update",
    },
    {
        "domain": "Notification Delivery",
        "service": "notify-worker",
        "symptom": "email queue lag exceeds 20 minutes during bursts",
        "kpi": "notification_delivery_latency",
        "impact": "customers miss critical workflow alerts",
        "repro": "trigger high-volume bulk assignment updates",
        "root_hint": "retry backoff policy saturates worker concurrency",
    },
    {
        "domain": "Search and Discovery",
        "service": "catalog-search",
        "symptom": "top query returns stale ranking for newly published items",
        "kpi": "search_ctr",
        "impact": "higher bounce rate on high-intent product pages",
        "repro": "search for newly launched SKU families",
        "root_hint": "feature refresh job skipped index write after cache warm restart",
    },
    {
        "domain": "Billing and Invoicing",
        "service": "billing-ledger",
        "symptom": "invoice PDF generation fails for annual contracts",
        "kpi": "invoice_generation_success_rate",
        "impact": "finance ops manual intervention load increases",
        "repro": "generate invoice after prorated plan upgrade",
        "root_hint": "currency formatter path mismatches locale fallback",
    },
    {
        "domain": "Customer Support Integrations",
        "service": "support-sync",
        "symptom": "Jira ticket updates fail to sync to CRM timeline",
        "kpi": "external_sync_success_rate",
        "impact": "support handoff quality drops across shifts",
        "repro": "update priority and assignee on escalated issues",
        "root_hint": "webhook signature verification not aligned with new secret rotation",
    },
    {
        "domain": "Feature Flag Platform",
        "service": "flag-evaluator",
        "symptom": "flag targeting returns wrong cohort for EU traffic",
        "kpi": "targeting_accuracy",
        "impact": "partial rollout reaches unintended customers",
        "repro": "evaluate staged rollout with geo + plan predicates",
        "root_hint": "predicate merge order changed in policy refactor",
    },
    {
        "domain": "Analytics Pipeline",
        "service": "events-ingestion",
        "symptom": "daily active users metric under-counted after schema bump",
        "kpi": "analytics_data_freshness",
        "impact": "product decisions made on stale/incorrect dashboards",
        "repro": "ingest events from mobile app version with new payload field",
        "root_hint": "parser drops events when optional nested object is absent",
    },
    {
        "domain": "Release Automation",
        "service": "deploy-orchestrator",
        "symptom": "canary rollback trigger delayed past guardrail threshold",
        "kpi": "rollback_latency",
        "impact": "longer blast radius during unstable deploys",
        "repro": "deploy release train with failing health checks in one region",
        "root_hint": "alarm subscription mismatch after infra module consolidation",
    },
    {
        "domain": "Team Collaboration",
        "service": "comment-service",
        "symptom": "threaded comments occasionally duplicate in UI timeline",
        "kpi": "collaboration_event_integrity",
        "impact": "triage confusion and handoff rework",
        "repro": "add concurrent comments while status transitions",
        "root_hint": "idempotency key not persisted under retry path",
    },
]


@dataclass(frozen=True)
class TaskShape:
    mode: str
    team_profile: str
    ticket_type: str
    risk_tier: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate comprehensive sample tasks for simulation and A/B runs.")
    parser.add_argument(
        "--output-path",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Output JSON path.",
    )
    parser.add_argument("--project-key", default="POC", help="Project key used in generated tickets.")
    parser.add_argument("--ticket-prefix", default="POC", help="Ticket prefix used in generated ticket_id values.")
    parser.add_argument("--start-index", type=int, default=201, help="Starting numeric suffix for ticket IDs.")
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=0,
        help="Optional cap on generated tasks (0 = full Cartesian set).",
    )
    return parser.parse_args()


def _all_shapes() -> list[TaskShape]:
    rows: list[TaskShape] = []
    for mode, team, ticket_type, risk in itertools.product(
        DEFAULT_MODES,
        DEFAULT_TEAMS,
        DEFAULT_TICKET_TYPES,
        DEFAULT_RISKS,
    ):
        rows.append(
            TaskShape(
                mode=mode,
                team_profile=team,
                ticket_type=ticket_type,
                risk_tier=risk,
            )
        )
    return rows


def _slug(value: str) -> str:
    return (
        value.strip().lower().replace(" ", "-").replace("/", "-").replace("&", "and").replace("_", "-")
    )


def _pick_domain(shape: TaskShape, idx: int) -> dict:
    fingerprint = f"{shape.mode}|{shape.team_profile}|{shape.ticket_type}|{shape.risk_tier}|{idx}"
    score = sum(ord(ch) for ch in fingerprint)
    return DOMAIN_PROFILES[score % len(DOMAIN_PROFILES)]


def _build_acceptance(shape: TaskShape, domain: dict) -> list[str]:
    base = [
        f"Fix addresses '{domain['symptom']}' without broadening scope beyond the targeted files.",
        f"Regression checks cover '{domain['repro']}' and guardrail metric '{domain['kpi']}' remains healthy.",
    ]
    if shape.mode == "incident":
        base.append("Incident mode: include rollback trigger and incident timeline note in final handoff.")
    elif shape.mode == "feature":
        base.append("Feature mode: include rollout plan, owner, and success metric checkpoint.")
    else:
        base.append("Customer escalation mode: include customer-safe summary and committed next update time.")

    if shape.risk_tier == "high":
        base.append("High-risk gate: explicitly list blast radius and mitigation before approval.")
    return base


def _task_payload(shape: TaskShape, idx: int, project_key: str, ticket_prefix: str, ticket_number: int) -> dict:
    mode = shape.mode
    team = shape.team_profile
    ticket_type = shape.ticket_type
    risk = shape.risk_tier
    domain = _pick_domain(shape, idx)
    title = (
        f"{domain['domain']}: {team} {mode} {ticket_type.replace('_', ' ')} case #{idx:03d}"
    )
    ticket_id = f"{ticket_prefix}-{ticket_number}"
    files = TEAM_FILE_CANDIDATES[team]
    acceptance = _build_acceptance(shape, domain)
    description_lines = [
        MODE_SUMMARY[mode],
        f"Business domain: {domain['domain']} ({domain['service']}).",
        f"Team profile: {team}. {TEAM_SUMMARY[team]}",
        f"Ticket type: {ticket_type}.",
        f"Risk tier: {risk}. {RISK_SUMMARY[risk]}",
        f"Observed symptom: {domain['symptom']}.",
        f"Business impact: {domain['impact']}.",
        f"Reproduction path: {domain['repro']}.",
        f"Root-cause hint: {domain['root_hint']}.",
        "Goal: validate triage-to-fix quality, guardrail enforcement, and comms artifacts quality.",
        "Acceptance criteria:",
    ]
    for i, criterion in enumerate(acceptance, start=1):
        description_lines.append(f"{i}) {criterion}")
    description = "\n".join(description_lines)
    domain_label = f"domain:{_slug(domain['domain'])}"

    return {
        "task_id": f"task_{idx:03d}_{team}_{mode}_{ticket_type}_{risk}",
        "ticket_id": ticket_id,
        "project": project_key,
        "title": title,
        "description": description,
        "comments": [
            f"Mode marker: {mode}",
            f"Team marker: {team}",
            f"Risk marker: {risk}",
            f"Domain marker: {domain['domain']}",
            f"Primary KPI to monitor after rollout: {domain['kpi']}",
            "Include at least one explicit rollback trigger in reviewer packet.",
        ],
        "source": "tasks_json",
        "mode": mode,
        "team_profile": team,
        "ticket_type": ticket_type,
        "risk_tier": risk,
        "labels": [
            "poc-simulation",
            "poc-real-story",
            f"mode:{mode}",
            f"team:{team}",
            f"ticket_type:{ticket_type}",
            f"risk:{risk}",
            domain_label,
        ],
        "repo_file_candidates": files,
        "evidence": [
            {
                "source": "jira",
                "id": ticket_id,
                "title": f"{domain['domain']} incident summary",
                "snippet": (
                    f"Signal: {domain['symptom']}; impact: {domain['impact']}; "
                    f"mode={mode}; team={team}; risk={risk}."
                ),
            },
            {
                "source": "code",
                "id": files[0],
                "title": f"{team} primary implementation candidate",
                "snippet": f"Suspected ownership area for {domain['service']} behavior.",
            },
            {
                "source": "bitbucket",
                "id": f"{domain['service']}-{idx:03d}",
                "title": f"{domain['service']} recent change reference",
                "snippet": "Recent change candidate that may have introduced the issue.",
            },
        ],
    }


def main() -> int:
    args = parse_args()
    shapes = _all_shapes()
    if args.max_tasks > 0:
        shapes = shapes[: args.max_tasks]

    tasks = []
    for idx, shape in enumerate(shapes, start=1):
        ticket_number = args.start_index + idx - 1
        tasks.append(
            _task_payload(
                shape=shape,
                idx=idx,
                project_key=args.project_key,
                ticket_prefix=args.ticket_prefix,
                ticket_number=ticket_number,
            )
        )

    out = {
        "meta": {
            "generator": "generate_sample_tasks.py",
            "task_count": len(tasks),
            "modes": DEFAULT_MODES,
            "teams": DEFAULT_TEAMS,
            "ticket_types": DEFAULT_TICKET_TYPES,
            "risk_tiers": DEFAULT_RISKS,
        },
        "tasks": tasks,
    }

    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {len(tasks)} tasks to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
