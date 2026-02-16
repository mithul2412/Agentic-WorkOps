from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run repeatable POC simulation against the API service.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Backend API base URL.")
    parser.add_argument(
        "--tasks-path",
        default="/Users/myth/Documents/VSCode/Codetor/samples/tasks.json",
        help="Path to simulation task JSON file.",
    )
    parser.add_argument(
        "--output-path",
        default="/Users/myth/Documents/VSCode/Codetor/storage/poc_report.json",
        help="Where to write report JSON.",
    )
    parser.add_argument("--reviewer", default="poc.reviewer", help="Reviewer identity used for auto-approvals.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds.")
    parser.add_argument("--max-tasks", type=int, default=30, help="Maximum tasks to execute from tasks file.")
    return parser.parse_args()


def _request_json(method: str, url: str, timeout: float, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.request(
        method=method,
        url=url,
        json=payload,
        timeout=timeout,
        headers={"Content-Type": "application/json"},
    )
    response.raise_for_status()
    if not response.text.strip():
        return {}
    return response.json()


def _build_webhook_payload(task: dict[str, Any]) -> dict[str, Any]:
    labels = task.get("labels", [])
    if not isinstance(labels, list):
        labels = []
    mode = str(task.get("mode", "incident")).strip()
    team_profile = str(task.get("team_profile", "platform")).strip()
    if f"mode:{mode}" not in labels:
        labels.append(f"mode:{mode}")
    if f"team:{team_profile}" not in labels:
        labels.append(f"team:{team_profile}")
    return {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": str(task.get("ticket_id", task.get("task_id", "POC-UNKNOWN"))),
            "fields": {
                "project": {"key": str(task.get("project", "POC"))},
                "summary": str(task.get("title", "Synthetic POC task")),
                "description": str(task.get("description", "")),
                "labels": labels,
                "mode": mode,
                "team_profile": team_profile,
            },
        },
        "user": {
            "displayName": "POC Simulator",
            "emailAddress": "poc-sim@example.com",
        },
    }


def _add_handoff_story_events(base_url: str, ticket_id: str, timeout: float, include_reopen: bool) -> list[str]:
    created: list[str] = []
    baseline_events = [
        ("TEAM_INVOLVED", {"team": "triage"}),
        ("MEETING_NOTES_ADDED", {"note": "Simulated handoff notes captured."}),
        ("QA_REQUESTED", {"scope": "targeted regression suite"}),
        ("CONFLUENCE_UPDATED", {"status": "draft_ready"}),
    ]
    for kind, payload in baseline_events:
        try:
            _request_json(
                "POST",
                f"{base_url}/ticket/{ticket_id}/story-events",
                timeout,
                {
                    "kind": kind,
                    "source": "MANUAL",
                    "actor": "poc-simulator",
                    "team": "workflow",
                    "payload": payload,
                },
            )
            created.append(kind)
        except Exception:
            continue

    if include_reopen:
        for kind in ("REGRESSION_FOUND", "REOPENED"):
            try:
                _request_json(
                    "POST",
                    f"{base_url}/ticket/{ticket_id}/story-events",
                    timeout,
                    {
                        "kind": kind,
                        "source": "MANUAL",
                        "actor": "poc-simulator",
                        "team": "qa",
                        "payload": {"reason": "Synthetic regression/reopen signal"},
                    },
                )
                created.append(kind)
            except Exception:
                continue

    return created


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    tasks_raw = json.loads(Path(args.tasks_path).read_text(encoding="utf-8"))
    tasks = tasks_raw.get("tasks", tasks_raw) if isinstance(tasks_raw, dict) else tasks_raw
    if not isinstance(tasks, list):
        raise ValueError("tasks file must contain a list or {'tasks': [...]} object")
    tasks = [task for task in tasks if isinstance(task, dict)][: max(1, args.max_tasks)]

    run_results: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    errors: list[str] = []

    for idx, task in enumerate(tasks, start=1):
        ticket_id = str(task.get("ticket_id", task.get("task_id", f"TASK-{idx}")))
        entry: dict[str, Any] = {
            "ticket_id": ticket_id,
            "mode": task.get("mode"),
            "team_profile": task.get("team_profile"),
            "status_before_approval": None,
            "status_after_approval": None,
            "story_events": [],
        }
        try:
            webhook_payload = _build_webhook_payload(task)
            start_resp = _request_json("POST", f"{base_url}/webhook/jira", args.timeout, webhook_payload)
            entry["run_id"] = start_resp.get("run_id")
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"webhook_failed: {exc}"
            errors.append(f"{ticket_id}: {exc}")
            run_results.append(entry)
            continue

        try:
            status_resp = _request_json("GET", f"{base_url}/status/{ticket_id}", args.timeout)
            current_status = str(status_resp.get("status", "UNKNOWN"))
            entry["status_before_approval"] = current_status
            if current_status == "AWAITING_APPROVAL":
                _request_json(
                    "POST",
                    f"{base_url}/approve",
                    args.timeout,
                    {
                        "ticket_id": ticket_id,
                        "reviewer": args.reviewer,
                        "approved": True,
                        "comments": "POC auto-approval for simulation.",
                    },
                )
                status_resp = _request_json("GET", f"{base_url}/status/{ticket_id}", args.timeout)
                current_status = str(status_resp.get("status", current_status))
            entry["status_after_approval"] = current_status
            status_counts[current_status] = status_counts.get(current_status, 0) + 1
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"status_or_approval_failed: {exc}"
            errors.append(f"{ticket_id}: {exc}")

        try:
            include_reopen = idx % 7 == 0 or idx % 9 == 0
            entry["story_events"] = _add_handoff_story_events(
                base_url=base_url,
                ticket_id=ticket_id,
                timeout=args.timeout,
                include_reopen=include_reopen,
            )
        except Exception as exc:  # noqa: BLE001
            entry["story_event_error"] = str(exc)

        run_results.append(entry)

    ab_response: dict[str, Any] | None = None
    judge_response: dict[str, Any] | None = None
    selector_response: dict[str, Any] | None = None
    metrics_response: dict[str, Any] | None = None

    try:
        ab_response = _request_json(
            "POST",
            f"{base_url}/operate/ab_run",
            args.timeout,
            {
                "source": "tasks_json",
                "tasks_path": str(args.tasks_path),
                "policy_a_id": "manager_ollama_qwen25_sft_v1",
                "policy_b_id": "manager_ollama_gemma2_local_v1",
                "max_tasks": len(tasks),
                "seed": 42,
            },
        )
        ab_run_id = str(ab_response.get("ab_run_id", "")).strip()
        if ab_run_id:
            judge_response = _request_json(
                "POST",
                f"{base_url}/operate/judge",
                args.timeout,
                {
                    "ab_run_id": ab_run_id,
                    "judge_policy_id": "judge_groq_v1",
                    "category_key": "team_profile|ticket_type|risk_tier",
                },
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"operate_pipeline: {exc}")

    try:
        selector_response = _request_json("GET", f"{base_url}/operate/selector?min_samples=1", args.timeout)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"selector_fetch: {exc}")

    try:
        metrics_response = _request_json("GET", f"{base_url}/metrics/summary", args.timeout)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"metrics_fetch: {exc}")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "tasks_path": str(args.tasks_path),
        "scenario_count": len(tasks),
        "status_counts": status_counts,
        "runs": run_results,
        "operate_ab_run": ab_response,
        "operate_judge": judge_response,
        "selector": selector_response,
        "metrics_summary": metrics_response,
        "errors": errors,
    }

    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote POC report: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
