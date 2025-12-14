from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_issue_resolution.models.state import WorkflowState, utc_now


class SQLiteRunStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    ticket_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS story_events (
                    event_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    actor TEXT,
                    team TEXT,
                    payload_json TEXT NOT NULL,
                    deleted_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operate_ab_runs (
                    ab_run_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    policy_a_id TEXT NOT NULL,
                    policy_b_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_tasks INTEGER NOT NULL DEFAULT 0,
                    completed_tasks INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operate_ab_items (
                    item_id TEXT PRIMARY KEY,
                    ab_run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    ticket_id TEXT,
                    category_estimate TEXT,
                    category_actual TEXT,
                    task_context_json TEXT NOT NULL,
                    output_a_json TEXT NOT NULL,
                    output_b_json TEXT NOT NULL,
                    metrics_a_json TEXT NOT NULL,
                    metrics_b_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operate_judgments (
                    judgment_id TEXT PRIMARY KEY,
                    ab_run_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    category TEXT,
                    preference TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    rationale TEXT,
                    judge_policy_id TEXT,
                    selector_updated INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operate_selector_stats (
                    category TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    ties INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    win_rate REAL NOT NULL DEFAULT 0.0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (category, policy_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS live_policy_decisions (
                    decision_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    run_id TEXT,
                    team_profile TEXT,
                    category_estimate TEXT,
                    category_actual TEXT,
                    selected_policy_id TEXT,
                    explored INTEGER NOT NULL DEFAULT 0,
                    epsilon REAL NOT NULL DEFAULT 0.0,
                    runtime_ms INTEGER NOT NULL DEFAULT 0,
                    cost_proxy REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "operate_judgments", "selector_updated", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "live_policy_decisions", "team_profile", "TEXT")
            conn.commit()

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, column_def: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column_name in existing:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")

    def upsert_state(self, state: WorkflowState) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (ticket_id, run_id, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.ticket_id,
                    state.run_id,
                    state.model_dump_json(),
                    utc_now().isoformat(),
                ),
            )
            conn.commit()

    def get_state(self, ticket_id: str) -> WorkflowState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM workflow_runs WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        if not row:
            return None
        return WorkflowState.model_validate_json(row["state_json"])

    def list_states(self, limit: int = 100, offset: int = 0) -> list[WorkflowState]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT state_json
                FROM workflow_runs
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [WorkflowState.model_validate_json(row["state_json"]) for row in rows]

    # ----------------------------
    # Story Events
    # ----------------------------
    def create_story_event(
        self,
        ticket_id: str,
        kind: str,
        source: str,
        actor: str | None,
        team: str | None,
        payload: dict[str, Any],
        ts: str | None = None,
    ) -> dict[str, Any]:
        event_id = f"evt_{uuid4().hex[:12]}"
        created_ts = ts or utc_now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO story_events (
                    event_id, ticket_id, ts, kind, source, actor, team, payload_json, deleted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    event_id,
                    ticket_id,
                    created_ts,
                    kind,
                    source,
                    actor,
                    team,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            conn.commit()
        return {
            "event_id": event_id,
            "ticket_id": ticket_id,
            "ts": created_ts,
            "kind": kind,
            "source": source,
            "actor": actor,
            "team": team,
            "payload": payload,
            "deleted": False,
        }

    def list_story_events(self, ticket_id: str, include_deleted: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT event_id, ticket_id, ts, kind, source, actor, team, payload_json, deleted_at
            FROM story_events
            WHERE ticket_id = ?
        """
        params: list[Any] = [ticket_id]
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        query += " ORDER BY ts ASC"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_story_event(row) for row in rows]

    def update_story_event(
        self,
        event_id: str,
        kind: str | None = None,
        actor: str | None = None,
        team: str | None = None,
        payload: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        values: list[Any] = []
        if kind is not None:
            fields.append("kind = ?")
            values.append(kind)
        if actor is not None:
            fields.append("actor = ?")
            values.append(actor)
        if team is not None:
            fields.append("team = ?")
            values.append(team)
        if payload is not None:
            fields.append("payload_json = ?")
            values.append(json.dumps(payload, ensure_ascii=False))
        if ts is not None:
            fields.append("ts = ?")
            values.append(ts)
        if not fields:
            return self.get_story_event(event_id)
        values.append(event_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE story_events SET {', '.join(fields)} WHERE event_id = ?",
                tuple(values),
            )
            conn.commit()
        return self.get_story_event(event_id)

    def soft_delete_story_event(self, event_id: str) -> bool:
        deleted_at = utc_now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE story_events SET deleted_at = ? WHERE event_id = ?",
                (deleted_at, event_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def get_story_event(self, event_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT event_id, ticket_id, ts, kind, source, actor, team, payload_json, deleted_at
                FROM story_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if not row:
            return None
        return self._row_to_story_event(row)

    # ----------------------------
    # Operate A/B Runs
    # ----------------------------
    def create_ab_run(
        self,
        source: str,
        policy_a_id: str,
        policy_b_id: str,
        total_tasks: int,
    ) -> str:
        ab_run_id = f"abr_{uuid4().hex[:12]}"
        created_at = utc_now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operate_ab_runs (
                    ab_run_id, source, policy_a_id, policy_b_id, status,
                    total_tasks, completed_tasks, created_at, finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL)
                """,
                (
                    ab_run_id,
                    source,
                    policy_a_id,
                    policy_b_id,
                    "RUNNING",
                    total_tasks,
                    created_at,
                ),
            )
            conn.commit()
        return ab_run_id

    def update_ab_run_progress(self, ab_run_id: str, completed_tasks: int, status: str = "RUNNING") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE operate_ab_runs
                SET completed_tasks = ?, status = ?
                WHERE ab_run_id = ?
                """,
                (completed_tasks, status, ab_run_id),
            )
            conn.commit()

    def complete_ab_run(self, ab_run_id: str, completed_tasks: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE operate_ab_runs
                SET completed_tasks = ?, status = ?, finished_at = ?
                WHERE ab_run_id = ?
                """,
                (completed_tasks, "COMPLETED", utc_now().isoformat(), ab_run_id),
            )
            conn.commit()

    def get_ab_run(self, ab_run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT ab_run_id, source, policy_a_id, policy_b_id, status,
                       total_tasks, completed_tasks, created_at, finished_at
                FROM operate_ab_runs
                WHERE ab_run_id = ?
                """,
                (ab_run_id,),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def list_ab_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ab_run_id, source, policy_a_id, policy_b_id, status,
                       total_tasks, completed_tasks, created_at, finished_at
                FROM operate_ab_runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_ab_item(
        self,
        ab_run_id: str,
        task_id: str,
        ticket_id: str | None,
        category_estimate: str | None,
        category_actual: str | None,
        task_context: dict[str, Any],
        output_a: dict[str, Any],
        output_b: dict[str, Any],
        metrics_a: dict[str, Any],
        metrics_b: dict[str, Any],
    ) -> str:
        item_id = f"abi_{uuid4().hex[:12]}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operate_ab_items (
                    item_id, ab_run_id, task_id, ticket_id, category_estimate, category_actual,
                    task_context_json, output_a_json, output_b_json, metrics_a_json, metrics_b_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    ab_run_id,
                    task_id,
                    ticket_id,
                    category_estimate,
                    category_actual,
                    json.dumps(task_context, ensure_ascii=False),
                    json.dumps(output_a, ensure_ascii=False),
                    json.dumps(output_b, ensure_ascii=False),
                    json.dumps(metrics_a, ensure_ascii=False),
                    json.dumps(metrics_b, ensure_ascii=False),
                    utc_now().isoformat(),
                ),
            )
            conn.commit()
        return item_id

    def list_ab_items(self, ab_run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT item_id, ab_run_id, task_id, ticket_id, category_estimate, category_actual,
                       task_context_json, output_a_json, output_b_json, metrics_a_json, metrics_b_json, created_at
                FROM operate_ab_items
                WHERE ab_run_id = ?
                ORDER BY created_at ASC
                """,
                (ab_run_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "item_id": row["item_id"],
                    "ab_run_id": row["ab_run_id"],
                    "task_id": row["task_id"],
                    "ticket_id": row["ticket_id"],
                    "category_estimate": row["category_estimate"],
                    "category_actual": row["category_actual"],
                    "task_context": json.loads(row["task_context_json"]),
                    "output_a": json.loads(row["output_a_json"]),
                    "output_b": json.loads(row["output_b_json"]),
                    "metrics_a": json.loads(row["metrics_a_json"]),
                    "metrics_b": json.loads(row["metrics_b_json"]),
                    "created_at": row["created_at"],
                }
            )
        return out

    # ----------------------------
    # Operate Judging + Selector Stats
    # ----------------------------
    def add_judgment(
        self,
        ab_run_id: str,
        item_id: str,
        task_id: str,
        category: str | None,
        preference: str,
        confidence: float,
        rationale: str,
        judge_policy_id: str,
        selector_updated: bool = True,
    ) -> str:
        judgment_id = f"jud_{uuid4().hex[:12]}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operate_judgments (
                    judgment_id, ab_run_id, item_id, task_id, category, preference,
                    confidence, rationale, judge_policy_id, selector_updated, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    judgment_id,
                    ab_run_id,
                    item_id,
                    task_id,
                    category,
                    preference,
                    confidence,
                    rationale,
                    judge_policy_id,
                    1 if selector_updated else 0,
                    utc_now().isoformat(),
                ),
            )
            conn.commit()
        return judgment_id

    def list_judgments(self, ab_run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT judgment_id, ab_run_id, item_id, task_id, category, preference,
                       confidence, rationale, judge_policy_id, selector_updated, created_at
                FROM operate_judgments
                WHERE ab_run_id = ?
                ORDER BY created_at ASC
                """,
                (ab_run_id,),
            ).fetchall()
        out = [dict(row) for row in rows]
        for row in out:
            row["selector_updated"] = bool(row.get("selector_updated", 0))
        return out

    def upsert_selector_stat(
        self,
        category: str,
        policy_id: str,
        wins_delta: int = 0,
        losses_delta: int = 0,
        ties_delta: int = 0,
    ) -> None:
        now = utc_now().isoformat()
        total_delta = wins_delta + losses_delta + ties_delta
        initial_win_rate = (wins_delta / total_delta) if total_delta > 0 else 0.0
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operate_selector_stats (
                    category, policy_id, wins, losses, ties, total, win_rate, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(category, policy_id) DO UPDATE SET
                    wins = operate_selector_stats.wins + excluded.wins,
                    losses = operate_selector_stats.losses + excluded.losses,
                    ties = operate_selector_stats.ties + excluded.ties,
                    total = operate_selector_stats.total + excluded.total,
                    win_rate = CAST(operate_selector_stats.wins + excluded.wins AS REAL)
                               / CASE WHEN (operate_selector_stats.total + excluded.total) <= 0 THEN 1
                                      ELSE (operate_selector_stats.total + excluded.total) END,
                    updated_at = excluded.updated_at
                """,
                (
                    category,
                    policy_id,
                    wins_delta,
                    losses_delta,
                    ties_delta,
                    total_delta,
                    initial_win_rate,
                    now,
                ),
            )
            conn.commit()

    def get_selector_stats(self, min_samples: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT category, policy_id, wins, losses, ties, total, win_rate, updated_at
                FROM operate_selector_stats
                WHERE total >= ?
                ORDER BY category ASC, win_rate DESC, total DESC
                """,
                (min_samples,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_selector_stats_for_category(self, category: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT category, policy_id, wins, losses, ties, total, win_rate, updated_at
                FROM operate_selector_stats
                WHERE category = ?
                ORDER BY win_rate DESC, total DESC
                """,
                (category,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------
    # Live policy decisions
    # ----------------------------
    def log_live_policy_decision(
        self,
        ticket_id: str,
        run_id: str | None,
        team_profile: str | None,
        category_estimate: str | None,
        category_actual: str | None,
        selected_policy_id: str | None,
        explored: bool,
        epsilon: float,
        runtime_ms: int,
        cost_proxy: float,
    ) -> str:
        decision_id = f"lpd_{uuid4().hex[:12]}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_policy_decisions (
                    decision_id, ticket_id, run_id, team_profile, category_estimate, category_actual,
                    selected_policy_id, explored, epsilon, runtime_ms, cost_proxy, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    ticket_id,
                    run_id,
                    team_profile,
                    category_estimate,
                    category_actual,
                    selected_policy_id,
                    1 if explored else 0,
                    epsilon,
                    runtime_ms,
                    cost_proxy,
                    utc_now().isoformat(),
                ),
            )
            conn.commit()
        return decision_id

    def list_live_policy_decisions(self, ticket_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        query = """
            SELECT decision_id, ticket_id, run_id, team_profile, category_estimate, category_actual,
                   selected_policy_id, explored, epsilon, runtime_ms, cost_proxy, created_at
            FROM live_policy_decisions
        """
        params: list[Any] = []
        if ticket_id:
            query += " WHERE ticket_id = ?"
            params.append(ticket_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        out = [dict(row) for row in rows]
        for item in out:
            item["explored"] = bool(item["explored"])
        return out

    def _row_to_story_event(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "ticket_id": row["ticket_id"],
            "ts": row["ts"],
            "kind": row["kind"],
            "source": row["source"],
            "actor": row["actor"],
            "team": row["team"],
            "payload": json.loads(row["payload_json"]) if row["payload_json"] else {},
            "deleted": row["deleted_at"] is not None,
            "deleted_at": row["deleted_at"],
        }
