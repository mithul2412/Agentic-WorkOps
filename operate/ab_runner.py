from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from models.operate import (
    OperateABRunRequest,
    OperateABRunResponse,
    PolicyMetricsSummary,
)
from operate.categorizer import category_from_manager_output, estimate_category
from operate.metrics import summarize_policy_metrics
from operate.policy_executor import ManagerPolicyExecutor
from operate.policy_registry import PolicyRegistry
from storage.sqlite_store import SQLiteRunStore


class OperateABRunner:
    def __init__(
        self,
        store: SQLiteRunStore,
        registry: PolicyRegistry,
        executor: ManagerPolicyExecutor,
    ) -> None:
        self.store = store
        self.registry = registry
        self.executor = executor

    def run(self, request: OperateABRunRequest) -> OperateABRunResponse:
        tasks = self._load_tasks(request)
        if request.max_tasks is not None:
            tasks = tasks[: max(0, request.max_tasks)]
        random.Random(request.seed).shuffle(tasks)
        total = len(tasks)

        ab_run_id = self.store.create_ab_run(
            source=request.source,
            policy_a_id=request.policy_a_id,
            policy_b_id=request.policy_b_id,
            total_tasks=total,
        )
        metrics_a_rows: list[dict[str, Any]] = []
        metrics_b_rows: list[dict[str, Any]] = []

        completed = 0
        category_key = self.registry.selector.category_key
        try:
            for task in tasks:
                task_id = str(task.get("task_id", f"task_{completed + 1}")).strip()
                ticket_payload = self._task_to_ticket_payload(task)
                evidence = task.get("evidence", [])
                if not isinstance(evidence, list):
                    evidence = []
                category_estimate = estimate_category(ticket_payload, category_key=category_key)
                repo_candidates = task.get("repo_file_candidates", [])
                if not isinstance(repo_candidates, list):
                    repo_candidates = []

                result_a = self.executor.run_policy(
                    policy_id=request.policy_a_id,
                    ticket_payload=ticket_payload,
                    evidence=evidence,
                    repo_file_candidates=repo_candidates,
                )
                result_b = self.executor.run_policy(
                    policy_id=request.policy_b_id,
                    ticket_payload=ticket_payload,
                    evidence=evidence,
                    repo_file_candidates=repo_candidates,
                )
                category_actual = category_from_manager_output(
                    result_a.output,
                    ticket_payload=ticket_payload,
                    category_key=category_key,
                )
                self.store.add_ab_item(
                    ab_run_id=ab_run_id,
                    task_id=task_id,
                    ticket_id=_opt_str(ticket_payload.get("ticket_id")),
                    category_estimate=category_estimate,
                    category_actual=category_actual,
                    task_context=task,
                    output_a=result_a.output,
                    output_b=result_b.output,
                    metrics_a=result_a.metrics,
                    metrics_b=result_b.metrics,
                )
                metrics_a_rows.append(result_a.metrics)
                metrics_b_rows.append(result_b.metrics)
                completed += 1
                self.store.update_ab_run_progress(ab_run_id, completed_tasks=completed, status="RUNNING")
        except Exception:
            self.store.update_ab_run_progress(ab_run_id, completed_tasks=completed, status="FAILED")
            raise

        self.store.complete_ab_run(ab_run_id, completed_tasks=completed)
        summary = {
            request.policy_a_id: PolicyMetricsSummary.model_validate(summarize_policy_metrics(metrics_a_rows)),
            request.policy_b_id: PolicyMetricsSummary.model_validate(summarize_policy_metrics(metrics_b_rows)),
        }
        return OperateABRunResponse(
            ab_run_id=ab_run_id,
            total_tasks=total,
            completed_tasks=completed,
            run_status="COMPLETED",
            summary_by_policy=summary,
        )

    def _load_tasks(self, request: OperateABRunRequest) -> list[dict[str, Any]]:
        if request.source == "tasks_json":
            raw = json.loads(Path(request.tasks_path or "").read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("tasks"), list):
                tasks = raw["tasks"]
            elif isinstance(raw, list):
                tasks = raw
            else:
                raise ValueError("tasks file must be a list or {\"tasks\": [...]} object")
            return [task for task in tasks if isinstance(task, dict)]

        tasks: list[dict[str, Any]] = []
        for idx, ticket_id in enumerate(request.ticket_ids):
            state = self.store.get_state(ticket_id)
            if not state:
                continue
            tasks.append(
                {
                    "task_id": f"saved_{idx + 1}_{ticket_id}",
                    "ticket_id": ticket_id,
                    "jira_payload": state.jira_payload,
                    "source": "saved_jira",
                }
            )
        return tasks

    def _task_to_ticket_payload(self, task: dict[str, Any]) -> dict[str, Any]:
        if isinstance(task.get("ticket_payload"), dict):
            payload = dict(task["ticket_payload"])
            payload.setdefault("ticket_id", str(task.get("ticket_id", task.get("task_id", ""))))
            return payload

        jira_payload = task.get("jira_payload")
        if isinstance(jira_payload, dict):
            issue = jira_payload.get("issue", {})
            fields = issue.get("fields", {})
            labels = fields.get("labels", [])
            if not isinstance(labels, list):
                labels = []
            return {
                "ticket_id": str(issue.get("key", issue.get("id", task.get("task_id", "")))),
                "project": str(fields.get("project", {}).get("key", "jira/project")),
                "title": str(fields.get("summary", task.get("title", ""))),
                "description": str(fields.get("description", task.get("description", ""))),
                "comments": [],
                "labels": labels,
                "mode": str(task.get("mode", "incident")),
                "team_profile": str(task.get("team_profile", "platform")),
                "source": "jira",
            }

        return {
            "ticket_id": str(task.get("ticket_id", task.get("task_id", ""))),
            "project": str(task.get("project", "sim/project")),
            "title": str(task.get("title", "")),
            "description": str(task.get("description", "")),
            "comments": task.get("comments", []) if isinstance(task.get("comments"), list) else [],
            "labels": task.get("labels", []) if isinstance(task.get("labels"), list) else [],
            "mode": str(task.get("mode", "incident")),
            "team_profile": str(task.get("team_profile", "platform")),
            "source": str(task.get("source", "tasks_json")),
        }


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None
