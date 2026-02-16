from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT
ENV_FILE = PACKAGE_ROOT / ".env"
DEFAULT_TASKS_PATH = PACKAGE_ROOT / "samples" / "tasks.json"
DEFAULT_MANIFEST_PATH = PACKAGE_ROOT / "storage" / "seed_manifest.json"
DEFAULT_REPORT_PATH = PACKAGE_ROOT / "storage" / "real_connected_report.json"
SEED_SCRIPT_PATH = PACKAGE_ROOT / "scripts" / "seed_real_systems.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a full real connected demo across Jira/Bitbucket/Confluence/Google.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Backend API base URL.")
    parser.add_argument("--project-key", default="SCRUM", help="Jira project key to seed/use.")
    parser.add_argument("--seed-count", type=int, default=24, help="How many Jira issues to ensure.")
    parser.add_argument("--confluence-drafts", type=int, default=6, help="How many Confluence drafts to ensure.")
    parser.add_argument("--process-count", type=int, default=20, help="How many seeded tickets to execute through workflow.")
    parser.add_argument("--operate-max-tasks", type=int, default=12, help="Max tasks used for operate A/B run.")
    parser.add_argument("--manifest-path", default=str(DEFAULT_MANIFEST_PATH), help="Seed manifest path.")
    parser.add_argument("--tasks-path", default=str(DEFAULT_TASKS_PATH), help="Task library path.")
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT_PATH), help="Output report path.")
    parser.add_argument("--reviewer", default="demo.approver", help="Reviewer name for approvals.")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout seconds.")
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="Skip seed_real_systems step and use existing manifest.",
    )
    return parser.parse_args()


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        parsed = os.path.expandvars(value.strip())
        if (parsed.startswith('"') and parsed.endswith('"')) or (parsed.startswith("'") and parsed.endswith("'")):
            parsed = parsed[1:-1]
        os.environ[key] = parsed


def _request_json(method: str, url: str, timeout: float, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.request(
        method=method,
        url=url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    if not response.text.strip():
        return {}
    return response.json()


def _jira_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()
    headers = kwargs.pop("headers", {}) or {}
    headers = {"Accept": "application/json", **headers}
    response = requests.request(
        method=method,
        url=f"{base}{path}",
        auth=(email, token),
        headers=headers,
        timeout=45,
        **kwargs,
    )
    response.raise_for_status()
    if not response.text.strip():
        return {}
    return response.json()


def _adf_from_text(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    content: list[dict[str, Any]] = []
    for line in lines:
        content.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
    return {"type": "doc", "version": 1, "content": content}


def _sync_jira_issue_with_task(ticket_id: str, task: dict[str, Any]) -> None:
    title = str(task.get("title", f"POC task for {ticket_id}")).strip()[:240]
    description = str(task.get("description", "")).strip()
    comments = task.get("comments", [])
    if isinstance(comments, list) and comments:
        description = description + "\n\n" + "\n".join(f"- {str(item).strip()}" for item in comments if str(item).strip())
    if "Acceptance criteria:" not in description:
        description += (
            "\n\nAcceptance criteria:\n"
            "1) scoped patch only\n"
            "2) regression coverage\n"
            "3) rollout and rollback notes\n"
        )

    labels = task.get("labels", [])
    if not isinstance(labels, list):
        labels = []
    mode = str(task.get("mode", "incident")).strip()
    team = str(task.get("team_profile", "platform")).strip()
    risk = str(task.get("risk_tier", "medium")).strip()
    ticket_type = str(task.get("ticket_type", "bug")).strip()
    required_labels = {
        "poc-real-demo",
        f"mode:{mode}",
        f"team:{team}",
        f"risk:{risk}",
        f"ticket_type:{ticket_type}",
    }
    for label in sorted(required_labels):
        if label not in labels:
            labels.append(label)

    _jira_request(
        "PUT",
        f"/rest/api/3/issue/{ticket_id}",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={
            "fields": {
                "summary": title,
                "description": _adf_from_text(description),
                "labels": labels,
            }
        },
    )


def _webhook_payload(ticket_id: str, task: dict[str, Any]) -> dict[str, Any]:
    labels = task.get("labels", [])
    if not isinstance(labels, list):
        labels = []
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": ticket_id,
            "fields": {
                "summary": str(task.get("title", f"POC task for {ticket_id}")),
                "description": str(task.get("description", "")),
                "labels": labels,
                "mode": str(task.get("mode", "incident")),
                "team_profile": str(task.get("team_profile", "platform")),
            },
        },
        "user": {"displayName": "POC Demo Runner", "emailAddress": os.getenv("JIRA_EMAIL", "").strip()},
    }


def _run_seed(args: argparse.Namespace) -> None:
    cmd = [
        sys.executable,
        str(SEED_SCRIPT_PATH),
        "--project-key",
        args.project_key,
        "--count",
        str(args.seed_count),
        "--confluence-drafts",
        str(args.confluence_drafts),
        "--manifest-path",
        args.manifest_path,
    ]
    subprocess.run(cmd, check=True)


def _extract_manager_decision(replay_payload: dict[str, Any]) -> dict[str, Any]:
    timeline = replay_payload.get("timeline", [])
    if not isinstance(timeline, list):
        return {}
    for event in reversed(timeline):
        if not isinstance(event, dict):
            continue
        if event.get("step") == "manager" and event.get("event_type") == "decision":
            payload = event.get("payload", {})
            if isinstance(payload, dict):
                return {
                    "decision": payload.get("decision"),
                    "model_mode": payload.get("model_mode"),
                    "model_reason": payload.get("model_reason"),
                    "selected_policy_id": payload.get("selected_policy_id"),
                    "confidence": payload.get("confidence"),
                }
    return {}


def main() -> int:
    args = parse_args()
    _load_env(ENV_FILE)

    if not args.skip_seed:
        _run_seed(args)

    manifest = json.loads(Path(args.manifest_path).read_text(encoding="utf-8"))
    ticket_ids = manifest.get("jira", {}).get("all_issue_keys", [])
    if not isinstance(ticket_ids, list) or not ticket_ids:
        raise RuntimeError(f"seed manifest has no jira keys: {args.manifest_path}")
    ticket_ids = [str(item).strip() for item in ticket_ids if str(item).strip()]
    ticket_ids = ticket_ids[: max(1, min(args.process_count, len(ticket_ids)))]

    task_payload = json.loads(Path(args.tasks_path).read_text(encoding="utf-8"))
    tasks = task_payload.get("tasks", task_payload) if isinstance(task_payload, dict) else task_payload
    if not isinstance(tasks, list) or not tasks:
        raise RuntimeError(f"invalid tasks payload: {args.tasks_path}")
    tasks = [task for task in tasks if isinstance(task, dict)]
    if not tasks:
        raise RuntimeError(f"no valid tasks found: {args.tasks_path}")

    base_url = args.base_url.rstrip("/")
    run_rows: list[dict[str, Any]] = []
    counts = {"completed": 0, "awaiting_approval": 0, "waiting_for_info": 0, "failed": 0}

    for idx, ticket_id in enumerate(ticket_ids):
        task = tasks[idx % len(tasks)]
        row: dict[str, Any] = {
            "ticket_id": ticket_id,
            "task_id": task.get("task_id"),
            "mode": task.get("mode"),
            "team_profile": task.get("team_profile"),
        }
        try:
            _sync_jira_issue_with_task(ticket_id, task)
            start = _request_json(
                "POST",
                f"{base_url}/webhook/jira",
                args.timeout,
                _webhook_payload(ticket_id, task),
            )
            row["webhook_status"] = start.get("status")
            row["run_id"] = start.get("run_id")

            if start.get("status") == "AWAITING_APPROVAL":
                approve = _request_json(
                    "POST",
                    f"{base_url}/approve",
                    args.timeout,
                    {
                        "ticket_id": ticket_id,
                        "reviewer": args.reviewer,
                        "approved": True,
                        "comments": "bulk demo approval",
                    },
                )
                row["approve_status"] = approve.get("status")
            elif start.get("status") == "WAITING_FOR_INFO":
                # Retry once with enriched details to reduce ASK_FOR_INFO churn.
                enriched = dict(task)
                enriched["description"] = (
                    str(task.get("description", ""))
                    + "\n\nExtra evidence:\n"
                    + "- stack trace points to checkout coupon parser\n"
                    + "- regression started after latest deploy\n"
                    + "- blast radius includes premium customers"
                )
                _sync_jira_issue_with_task(ticket_id, enriched)
                retry = _request_json(
                    "POST",
                    f"{base_url}/webhook/jira",
                    args.timeout,
                    _webhook_payload(ticket_id, enriched),
                )
                row["retry_webhook_status"] = retry.get("status")
                row["retry_run_id"] = retry.get("run_id")
                if retry.get("status") == "AWAITING_APPROVAL":
                    approve = _request_json(
                        "POST",
                        f"{base_url}/approve",
                        args.timeout,
                        {
                            "ticket_id": ticket_id,
                            "reviewer": args.reviewer,
                            "approved": True,
                            "comments": "bulk demo approval after retry",
                        },
                    )
                    row["approve_status"] = approve.get("status")

            status = _request_json("GET", f"{base_url}/status/{ticket_id}", args.timeout)
            final_status = str(status.get("status", "UNKNOWN"))
            row["final_status"] = final_status

            story = _request_json("GET", f"{base_url}/ticket/{ticket_id}/story", args.timeout)
            artifacts = story.get("artifacts", {}) if isinstance(story, dict) else {}
            row["artifacts"] = {
                "pr_url_present": bool(artifacts.get("pr_url")),
                "confluence_draft_present": bool(artifacts.get("confluence_draft")),
                "calendar_slots_count": len(artifacts.get("calendar_slots", []))
                if isinstance(artifacts.get("calendar_slots"), list)
                else 0,
                "email_subject_present": bool(artifacts.get("email_subject")),
            }
            replay = _request_json("GET", f"{base_url}/replay/{ticket_id}", args.timeout)
            row["manager_decision"] = _extract_manager_decision(replay)

            if final_status == "COMPLETED":
                counts["completed"] += 1
            elif final_status == "AWAITING_APPROVAL":
                counts["awaiting_approval"] += 1
            elif final_status == "WAITING_FOR_INFO":
                counts["waiting_for_info"] += 1
            elif final_status == "FAILED":
                counts["failed"] += 1
        except Exception as exc:  # noqa: BLE001
            row["error"] = str(exc)
            counts["failed"] += 1
        run_rows.append(row)

    operate_rows: dict[str, Any] = {}
    operate_timeout = max(args.timeout, 180.0)
    try:
        ab = _request_json(
            "POST",
            f"{base_url}/operate/ab_run",
            operate_timeout,
            {
                "source": "tasks_json",
                "tasks_path": args.tasks_path,
                "policy_a_id": "manager_ollama_qwen25_sft_v1",
                "policy_b_id": "manager_ollama_gemma2_local_v1",
                "max_tasks": min(max(1, args.operate_max_tasks), len(tasks)),
                "seed": 42,
            },
        )
        operate_rows["ab_run"] = ab
        ab_run_id = str(ab.get("ab_run_id", "")).strip()
        if ab_run_id:
            operate_rows["judge"] = _request_json(
                "POST",
                f"{base_url}/operate/judge",
                operate_timeout,
                {
                    "ab_run_id": ab_run_id,
                    "judge_policy_id": "judge_groq_v1",
                    "category_key": "team_profile|ticket_type|risk_tier",
                },
            )
    except Exception as exc:  # noqa: BLE001
        operate_rows["error"] = str(exc)

    metrics: dict[str, Any] = {}
    for endpoint in [
        "/metrics/summary",
        "/metrics/model_ops",
        "/operate/selector?min_samples=1",
        "/operate/live_decisions?limit=50",
        "/integrations/status",
    ]:
        key = endpoint.replace("/", "_")
        try:
            metrics[key] = _request_json("GET", f"{base_url}{endpoint}", args.timeout)
        except Exception as exc:  # noqa: BLE001
            metrics[key] = {"error": str(exc)}

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "manifest_path": args.manifest_path,
        "tasks_path": args.tasks_path,
        "seed_count_requested": args.seed_count,
        "process_count_requested": args.process_count,
        "summary": counts,
        "runs": run_rows,
        "operate": operate_rows,
        "metrics": metrics,
    }

    out_path = Path(args.report_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote connected demo report: {out_path}")
    print(f"completed={counts['completed']} waiting_for_info={counts['waiting_for_info']} failed={counts['failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
