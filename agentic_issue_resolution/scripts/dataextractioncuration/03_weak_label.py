#!/usr/bin/env python3
"""
Apply weak-supervision labels to canonical issue rows without LLM intervention.
"""

from __future__ import annotations

import argparse
import math
import random
import re
from pathlib import Path
from typing import Any

import pandas as pd


RAW_TYPE_MAP = {
    "bug": "bug",
    "defect": "bug",
    "incident": "bug",
    "failure": "bug",
    "type/bug": "bug",
    "c-bug": "bug",
    "type: bug fix": "bug",
    "new feature": "feature_insert",
    "feature request": "feature_insert",
    "story": "feature_insert",
    "epic": "feature_insert",
    "type/feature": "feature_insert",
    "feature_insert": "feature_insert",
    "improvement": "feature_update",
    "enhancement": "feature_update",
    "c-enhancement": "feature_update",
    "task": "feature_update",
    "change request": "feature_update",
    "refactor": "feature_update",
    "maintenance": "feature_update",
    "documentation": "feature_update",
    "type: maintenance": "feature_update",
    "type: documentation": "feature_update",
    "feature_update": "feature_update",
}

BUG_TERMS = [
    "bug",
    "error",
    "exception",
    "traceback",
    "fail",
    "fails",
    "failing",
    "crash",
    "regression",
    "broken",
]
FEATURE_INSERT_TERMS = [
    "new feature",
    "feature request",
    "add support",
    "introduce",
    "enable",
    "allow users",
    "create endpoint",
    "implement",
    "proposal",
    "support ",
]
FEATURE_UPDATE_TERMS = [
    "update",
    "improve",
    "enhance",
    "migrate",
    "change behavior",
    "refactor",
    "optimize",
    "cleanup",
    "bump",
    "deprecate",
    "maintenance",
    "documentation",
    "tech debt",
]

TICKET_TYPE_ORDER = ["bug", "feature_update", "feature_insert"]

DEFAULT_TICKET_TYPE_TARGETS = {
    "bug": 0.70,
    "feature_update": 0.20,
    "feature_insert": 0.10,
}

INSERT_TITLE_RE = re.compile(r"^\s*(add|enable|implement|introduce|create|support|allow)\b", flags=re.I)
UPDATE_TITLE_RE = re.compile(
    r"^\s*(update|improve|enhance|refactor|migrate|replace|cleanup|bump|deprecate)\b",
    flags=re.I,
)
EXPLICIT_BUG_SIGNAL_RE = re.compile(
    r"\b(traceback|stack trace|exception|error|segfault|crash|assertionerror|keyerror|typeerror|fails?|failing|broken)\b",
    flags=re.I,
)

HIGH_RISK_TERMS = [
    "security",
    "auth bypass",
    "authentication",
    "authorization",
    "billing",
    "payment",
    "data loss",
    "data corruption",
    "outage",
    "privacy",
    "token leak",
    "schema migration",
]
MEDIUM_RISK_TERMS = [
    "performance",
    "latency",
    "timeout",
    "deadlock",
    "race condition",
    "regression",
    "memory leak",
    "slow",
]

ERROR_SIGNATURE_RE = re.compile(
    r"(?im)^(.*(?:traceback|exception|error|assertionerror|keyerror|typeerror|http\s+\d{3}).*)$"
)
FILE_PATH_RE = re.compile(
    r"\b[\w./-]+\.(?:py|js|ts|tsx|java|go|rb|php|cpp|c|cs|yaml|yml|json|toml|md)\b"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply weak labels to canonical issues.")
    parser.add_argument(
        "--in_file",
        type=Path,
        default=Path("data/interim/canonical_issues.parquet"),
        help="Input canonical parquet file.",
    )
    parser.add_argument(
        "--out_file",
        type=Path,
        default=Path("data/curated/triage_curated_v1_3k.parquet"),
        help="Output labeled parquet file.",
    )
    parser.add_argument(
        "--target_rows",
        type=int,
        default=3000,
        help="Target row count after sampling.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for deterministic sampling.",
    )
    parser.add_argument(
        "--ticket_type_targets",
        type=str,
        default="bug=0.70,feature_update=0.20,feature_insert=0.10",
        help="Ticket type target mix as comma-separated ratios, e.g. bug=0.7,feature_update=0.2,feature_insert=0.1",
    )
    return parser.parse_args()


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def _normalize_text_list(value: Any) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, (list, tuple)):
        return [_normalize_text(item) for item in value if _normalize_text(item)]
    if hasattr(value, "tolist"):
        try:
            as_list = value.tolist()
            if isinstance(as_list, list):
                return [_normalize_text(item) for item in as_list if _normalize_text(item)]
        except Exception:
            return []
    return []


def _full_text(row: pd.Series) -> str:
    comments = _normalize_text_list(row.get("comments", []))
    parts = [_normalize_text(row.get("title")), _normalize_text(row.get("description"))] + [
        _normalize_text(c) for c in comments
    ]
    return " ".join(p for p in parts if p).lower()


def _count_keyword_hits(text: str, terms: list[str]) -> int:
    count = 0
    for term in terms:
        if term in text:
            count += 1
    return count


def _parse_ticket_type_targets(value: str) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for chunk in value.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(f"Invalid --ticket_type_targets item: {piece}")
        key, raw_ratio = piece.split("=", 1)
        key = key.strip()
        if key not in TICKET_TYPE_ORDER:
            raise ValueError(f"Unknown ticket type target key: {key}")
        try:
            parsed[key] = float(raw_ratio.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid ratio for {key}: {raw_ratio}") from exc

    merged = DEFAULT_TICKET_TYPE_TARGETS.copy()
    merged.update(parsed)
    total = sum(merged.values())
    if total <= 0:
        raise ValueError("Sum of ticket type target ratios must be > 0.")
    return {key: merged[key] / total for key in TICKET_TYPE_ORDER}


def _target_counts(total: int, targets: dict[str, float]) -> dict[str, int]:
    if total <= 0:
        return {ticket_type: 0 for ticket_type in TICKET_TYPE_ORDER}

    exact = {ticket_type: targets[ticket_type] * total for ticket_type in TICKET_TYPE_ORDER}
    counts = {ticket_type: int(math.floor(exact[ticket_type])) for ticket_type in TICKET_TYPE_ORDER}
    remaining = total - sum(counts.values())
    if remaining > 0:
        order = sorted(
            TICKET_TYPE_ORDER,
            key=lambda ticket_type: exact[ticket_type] - counts[ticket_type],
            reverse=True,
        )
        for idx in range(remaining):
            counts[order[idx % len(order)]] += 1
    return counts


def _label_ticket_type(raw_type: str, title: str, text: str, trace: list[str]) -> str:
    raw_lower = raw_type.lower()
    for key, value in RAW_TYPE_MAP.items():
        if key in raw_lower:
            trace.append(f"raw_type_map:{key}->{value}")
            return value

    score_bug = _count_keyword_hits(text, BUG_TERMS)
    score_insert = _count_keyword_hits(text, FEATURE_INSERT_TERMS)
    score_update = _count_keyword_hits(text, FEATURE_UPDATE_TERMS)
    trace.append(f"type_scores:bug={score_bug},insert={score_insert},update={score_update}")
    explicit_bug_signal = bool(EXPLICIT_BUG_SIGNAL_RE.search(text))

    if score_bug == 0 and score_insert == 0 and score_update == 0:
        if INSERT_TITLE_RE.search(title):
            trace.append("type_fallback:title_insert_verb")
            return "feature_insert"
        if UPDATE_TITLE_RE.search(title):
            trace.append("type_fallback:title_update_verb")
            return "feature_update"
        if explicit_bug_signal:
            trace.append("type_fallback:explicit_bug_signal")
            return "bug"
        trace.append("type_fallback:default_feature_update")
        return "feature_update"

    if explicit_bug_signal and score_bug >= score_insert and score_bug >= score_update:
        trace.append("type_pick:explicit_bug_signal")
        return "bug"
    if score_insert > score_bug and score_insert >= score_update:
        trace.append("type_pick:feature_insert")
        return "feature_insert"
    if score_update > score_bug and score_update >= score_insert:
        trace.append("type_pick:feature_update")
        return "feature_update"
    if score_bug > score_insert and score_bug >= score_update:
        trace.append("type_pick:bug")
        return "bug"

    if score_bug == score_update and score_bug > score_insert:
        chosen = "bug" if explicit_bug_signal else "feature_update"
        trace.append(f"type_tie:bug_update->{chosen}")
        return chosen
    if score_insert == score_update and score_insert > score_bug:
        trace.append("type_tie:insert_update->feature_update")
        return "feature_update"
    if score_bug == score_insert and score_bug > score_update:
        chosen = "bug" if explicit_bug_signal else "feature_insert"
        trace.append(f"type_tie:bug_insert->{chosen}")
        return chosen

    trace.append("type_fallback:default_feature_update")
    return "feature_update"


def _label_risk_tier(text: str, trace: list[str]) -> str:
    if any(term in text for term in HIGH_RISK_TERMS):
        trace.append("risk:high_term_match")
        return "high"
    if any(term in text for term in MEDIUM_RISK_TERMS):
        trace.append("risk:medium_term_match")
        return "medium"
    trace.append("risk:default_low")
    return "low"


def _extract_evidence_flags(text: str) -> dict[str, bool]:
    has_repro = bool(
        re.search(r"\b(steps to reproduce|repro(?:duce)?|how to reproduce|1\.\s+.+2\.)\b", text, flags=re.I)
    )
    has_stacktrace_or_logs = bool(
        re.search(r"\b(traceback|stack trace|exception|error:|fatal|segfault|log excerpt)\b", text, flags=re.I)
    )
    has_expected_vs_actual = bool(
        re.search(r"\b(expected).{0,80}(actual)\b", text, flags=re.I)
        or re.search(r"\b(should).{0,80}(but)\b", text, flags=re.I)
    )
    has_env_version = bool(
        re.search(r"\b(version|os|python|node|java|docker|kubernetes|browser)\b", text, flags=re.I)
    )
    has_scope_hint = bool(
        FILE_PATH_RE.search(text)
        or re.search(r"\b(module|service|component|endpoint|controller|handler|repository)\b", text, flags=re.I)
    )
    return {
        "has_repro_steps": has_repro,
        "has_stacktrace_or_logs": has_stacktrace_or_logs,
        "has_expected_vs_actual": has_expected_vs_actual,
        "has_env_version": has_env_version,
        "has_scope_hint": has_scope_hint,
    }


def _label_decision(flags: dict[str, bool], trace: list[str]) -> tuple[str, int]:
    score = (
        2 * int(flags["has_repro_steps"])
        + 2 * int(flags["has_stacktrace_or_logs"])
        + int(flags["has_expected_vs_actual"])
        + int(flags["has_env_version"])
        + int(flags["has_scope_hint"])
    )
    ready = (
        score >= 4
        and (flags["has_repro_steps"] or flags["has_stacktrace_or_logs"])
        and flags["has_expected_vs_actual"]
    )
    trace.append(f"evidence_score:{score}")
    if ready:
        trace.append("decision:READY_TO_PATCH")
        return "READY_TO_PATCH", score
    trace.append("decision:ASK_FOR_INFO")
    return "ASK_FOR_INFO", score


def _summary_seed(title: str, description: str) -> str:
    if title.strip():
        return title.strip()[:240]
    return description.strip()[:240] if description.strip() else "No clear summary provided."


def _error_signature_seed(text: str) -> str:
    match = ERROR_SIGNATURE_RE.search(text)
    if match:
        return match.group(1).strip()[:300]
    return "NONE_PROVIDED"


def _suspected_components_seed(text: str) -> list[str]:
    files = FILE_PATH_RE.findall(text)
    if files:
        unique = list(dict.fromkeys(files))
        return unique[:6]

    candidates: list[str] = []
    keyword_map = {
        "auth": ["auth", "login", "token", "oauth"],
        "billing": ["billing", "payment", "invoice"],
        "database": ["db", "database", "sql", "schema", "migration"],
        "api": ["api", "endpoint", "http", "graphql"],
        "frontend": ["ui", "frontend", "react", "vue", "css"],
        "worker": ["job", "queue", "worker", "cron"],
    }
    for component, terms in keyword_map.items():
        if any(term in text for term in terms):
            candidates.append(component)
    return candidates[:6]


def _questions_needed_seed(flags: dict[str, bool]) -> list[str]:
    questions: list[str] = []
    if not flags["has_repro_steps"]:
        questions.append("Please share exact steps to reproduce this issue from a clean setup.")
    if not flags["has_expected_vs_actual"]:
        questions.append("What is the expected behavior and what is the actual behavior observed?")
    if not flags["has_env_version"]:
        questions.append("Please provide environment details (OS, runtime, and relevant package versions).")
    if not flags["has_stacktrace_or_logs"]:
        questions.append("Can you attach relevant logs or a stack trace from the failing run?")
    if not flags["has_scope_hint"]:
        questions.append("Which module/service/file appears most impacted based on your investigation?")
    return questions[:6]


def _acceptance_criteria_seed(ticket_type: str) -> list[str]:
    if ticket_type == "bug":
        return [
            "The issue reproduces on the current baseline and no longer reproduces after the patch.",
            "A regression test captures the failure mode and passes after the fix.",
            "Adjacent behavior in related components remains unchanged.",
        ]
    if ticket_type == "feature_insert":
        return [
            "The requested behavior is newly available and reachable through the documented path.",
            "Automated tests verify the new behavior and edge cases.",
            "Relevant usage or API documentation is updated if user-facing.",
        ]
    return [
        "The existing behavior is updated according to the request without breaking backward compatibility.",
        "Automated tests cover both updated behavior and unchanged baseline behavior.",
        "Rollout or migration notes are captured when applicable.",
    ]


def _label_confidence(raw_type: str, evidence_score: int, decision: str) -> str:
    raw_type_present = bool(raw_type.strip())
    if decision == "READY_TO_PATCH" and evidence_score >= 5 and raw_type_present:
        return "high"
    if evidence_score >= 3:
        return "medium"
    return "low"


def _dedupe_by_index(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df.loc[~df.index.duplicated(keep="first")]


def _sample_stratified(df: pd.DataFrame, n: int, seed: int, strata: list[str]) -> pd.DataFrame:
    if n >= len(df):
        return df.copy()
    if n <= 0:
        return df.iloc[0:0].copy()

    grouped = df.groupby(strata, dropna=False).size().rename("count").reset_index()
    grouped["quota_float"] = grouped["count"] / grouped["count"].sum() * n
    grouped["quota"] = grouped["quota_float"].apply(math.floor).astype(int)
    grouped.loc[grouped["quota"] == 0, "quota"] = 1

    while grouped["quota"].sum() > n:
        idx = grouped.sort_values(by=["quota", "quota_float"], ascending=False).index[0]
        if grouped.loc[idx, "quota"] > 1:
            grouped.loc[idx, "quota"] -= 1
        else:
            break
    while grouped["quota"].sum() < n:
        grouped["remainder"] = grouped["quota_float"] - grouped["quota"]
        idx = grouped.sort_values(by=["remainder", "count"], ascending=False).index[0]
        grouped.loc[idx, "quota"] += 1

    sampled_parts: list[pd.DataFrame] = []
    rng = random.Random(seed)

    for _, row in grouped.iterrows():
        quota = int(row["quota"])
        mask = pd.Series(True, index=df.index)
        for col in strata:
            mask &= df[col] == row[col]
        subset = df[mask]
        if subset.empty:
            continue
        take = min(quota, len(subset))
        sampled_parts.append(subset.sample(n=take, random_state=rng.randint(1, 10_000_000)))

    sampled = _dedupe_by_index(pd.concat(sampled_parts, ignore_index=False))
    if len(sampled) < n:
        leftover = df.drop(index=sampled.index, errors="ignore")
        if not leftover.empty:
            sampled = _dedupe_by_index(
                pd.concat(
                [
                    sampled,
                    leftover.sample(
                        n=min(n - len(sampled), len(leftover)),
                        random_state=seed,
                    ),
                ],
                ignore_index=False,
            )
            )
    return sampled.sample(n=min(n, len(sampled)), random_state=seed)


def _enforce_high_risk_ratio(sampled: pd.DataFrame, pool: pd.DataFrame, seed: int) -> pd.DataFrame:
    target = len(sampled)
    if target == 0:
        return sampled
    min_high = math.ceil(target * 0.05)
    max_high = math.floor(target * 0.25)

    high_count = int((sampled["risk_tier"] == "high").sum())
    if high_count < min_high:
        need = min_high - high_count
        add_candidates = pool[
            (pool["risk_tier"] == "high") & (~pool.index.isin(sampled.index))
        ]
        remove_candidates = sampled[sampled["risk_tier"] != "high"]
        take = min(need, len(add_candidates), len(remove_candidates))
        if take > 0:
            add = add_candidates.sample(n=take, random_state=seed)
            remove = remove_candidates.sample(n=take, random_state=seed + 1)
            sampled = _dedupe_by_index(
                pd.concat([sampled.drop(index=remove.index), add], ignore_index=False)
            )
    elif high_count > max_high:
        need = high_count - max_high
        add_candidates = pool[
            (pool["risk_tier"] != "high") & (~pool.index.isin(sampled.index))
        ]
        remove_candidates = sampled[sampled["risk_tier"] == "high"]
        take = min(need, len(add_candidates), len(remove_candidates))
        if take > 0:
            add = add_candidates.sample(n=take, random_state=seed + 2)
            remove = remove_candidates.sample(n=take, random_state=seed + 3)
            sampled = _dedupe_by_index(
                pd.concat([sampled.drop(index=remove.index), add], ignore_index=False)
            )
    return sampled


def _enforce_decision_minority(sampled: pd.DataFrame, pool: pd.DataFrame, seed: int) -> pd.DataFrame:
    target = len(sampled)
    if target == 0:
        return sampled
    required = math.ceil(target * 0.30)
    counts = sampled["decision"].value_counts()
    ask = int(counts.get("ASK_FOR_INFO", 0))
    ready = int(counts.get("READY_TO_PATCH", 0))

    if ask < required:
        need = required - ask
        add_candidates = pool[
            (pool["decision"] == "ASK_FOR_INFO") & (~pool.index.isin(sampled.index))
        ]
        remove_candidates = sampled[sampled["decision"] == "READY_TO_PATCH"]
        take = min(need, len(add_candidates), len(remove_candidates))
        if take > 0:
            add = add_candidates.sample(n=take, random_state=seed + 4)
            remove = remove_candidates.sample(n=take, random_state=seed + 5)
            sampled = _dedupe_by_index(
                pd.concat([sampled.drop(index=remove.index), add], ignore_index=False)
            )

    counts = sampled["decision"].value_counts()
    ask = int(counts.get("ASK_FOR_INFO", 0))
    ready = int(counts.get("READY_TO_PATCH", 0))
    if ready < required:
        need = required - ready
        add_candidates = pool[
            (pool["decision"] == "READY_TO_PATCH") & (~pool.index.isin(sampled.index))
        ]
        remove_candidates = sampled[sampled["decision"] == "ASK_FOR_INFO"]
        take = min(need, len(add_candidates), len(remove_candidates))
        if take > 0:
            add = add_candidates.sample(n=take, random_state=seed + 6)
            remove = remove_candidates.sample(n=take, random_state=seed + 7)
            sampled = _dedupe_by_index(
                pd.concat([sampled.drop(index=remove.index), add], ignore_index=False)
            )

    return sampled


def _sample_with_ticket_type_targets(
    df: pd.DataFrame,
    target_rows: int,
    seed: int,
    ticket_type_targets: dict[str, float],
) -> tuple[pd.DataFrame, dict[str, int], dict[str, int]]:
    desired_counts = _target_counts(target_rows, ticket_type_targets)
    shortfalls: dict[str, int] = {}
    sampled_parts: list[pd.DataFrame] = []

    for offset, ticket_type in enumerate(TICKET_TYPE_ORDER):
        pool = df[df["ticket_type"] == ticket_type]
        desired = desired_counts[ticket_type]
        take = min(desired, len(pool))
        if take > 0:
            sampled_parts.append(
                _sample_stratified(
                    pool,
                    take,
                    seed=seed + 100 + offset,
                    strata=["decision", "risk_tier"],
                )
            )
        if take < desired:
            shortfalls[ticket_type] = desired - take

    sampled = (
        _dedupe_by_index(pd.concat(sampled_parts, ignore_index=False))
        if sampled_parts
        else df.iloc[0:0].copy()
    )

    while len(sampled) < target_rows:
        leftover = df.drop(index=sampled.index, errors="ignore")
        if leftover.empty:
            break
        remaining = target_rows - len(sampled)
        capacities = {
            ticket_type: int((leftover["ticket_type"] == ticket_type).sum())
            for ticket_type in TICKET_TYPE_ORDER
        }
        available_types = [ticket_type for ticket_type in TICKET_TYPE_ORDER if capacities[ticket_type] > 0]
        if not available_types:
            break

        weight_total = sum(ticket_type_targets[ticket_type] for ticket_type in available_types)
        effective_targets = {ticket_type: 0.0 for ticket_type in TICKET_TYPE_ORDER}
        for ticket_type in available_types:
            effective_targets[ticket_type] = ticket_type_targets[ticket_type] / weight_total
        allocations = _target_counts(remaining, effective_targets)
        progress = False

        for offset, ticket_type in enumerate(TICKET_TYPE_ORDER):
            if capacities[ticket_type] <= 0:
                continue
            take = min(allocations.get(ticket_type, 0), capacities[ticket_type])
            if take <= 0:
                continue
            candidates = leftover[leftover["ticket_type"] == ticket_type]
            sampled = _dedupe_by_index(
                pd.concat(
                    [
                        sampled,
                        _sample_stratified(
                            candidates,
                            take,
                            seed=seed + 200 + offset,
                            strata=["decision", "risk_tier"],
                        ),
                    ],
                    ignore_index=False,
                )
            )
            progress = True

        if not progress:
            break

    if len(sampled) < target_rows:
        leftover = df.drop(index=sampled.index, errors="ignore")
        if not leftover.empty:
            sampled = _dedupe_by_index(
                pd.concat(
                    [
                        sampled,
                        _sample_stratified(
                            leftover,
                            n=min(target_rows - len(sampled), len(leftover)),
                            seed=seed + 300,
                            strata=["ticket_type", "decision", "risk_tier"],
                        ),
                    ],
                    ignore_index=False,
                )
            )

    if len(sampled) > target_rows:
        sampled = sampled.sample(n=target_rows, random_state=seed + 400)
    return sampled, desired_counts, shortfalls


def _enforce_ticket_type_targets(
    sampled: pd.DataFrame,
    pool: pd.DataFrame,
    ticket_type_targets: dict[str, float],
    seed: int,
) -> pd.DataFrame:
    if sampled.empty:
        return sampled

    desired = _target_counts(len(sampled), ticket_type_targets)
    result = sampled.copy()

    for iteration in range(30):
        counts = result["ticket_type"].value_counts().to_dict()
        deficits = {
            ticket_type: max(0, desired[ticket_type] - int(counts.get(ticket_type, 0)))
            for ticket_type in TICKET_TYPE_ORDER
        }
        excesses = {
            ticket_type: max(0, int(counts.get(ticket_type, 0)) - desired[ticket_type])
            for ticket_type in TICKET_TYPE_ORDER
        }

        if all(value == 0 for value in deficits.values()):
            break
        if all(value == 0 for value in excesses.values()):
            break

        for ticket_type in TICKET_TYPE_ORDER:
            deficit = deficits[ticket_type]
            if deficit <= 0:
                continue

            add_candidates = pool[
                (pool["ticket_type"] == ticket_type) & (~pool.index.isin(result.index))
            ]
            if add_candidates.empty:
                continue

            for over_type in sorted(excesses, key=excesses.get, reverse=True):
                excess = excesses.get(over_type, 0)
                if excess <= 0 or deficit <= 0:
                    continue
                remove_candidates = result[result["ticket_type"] == over_type]
                if remove_candidates.empty:
                    continue

                take = min(deficit, excess, len(add_candidates), len(remove_candidates))
                if take <= 0:
                    continue

                add_rows = add_candidates.sample(
                    n=take,
                    random_state=seed + (iteration * 100) + take + 1,
                )
                remove_rows = remove_candidates.sample(
                    n=take,
                    random_state=seed + (iteration * 100) + take + 2,
                )
                result = _dedupe_by_index(
                    pd.concat(
                        [result.drop(index=remove_rows.index), add_rows],
                        ignore_index=False,
                    )
                )
                deficits[ticket_type] -= take
                excesses[over_type] -= take
                add_candidates = add_candidates.drop(index=add_rows.index)
                deficit = deficits[ticket_type]

    return result


def _target_sample(
    df: pd.DataFrame,
    target_rows: int,
    seed: int,
    ticket_type_targets: dict[str, float],
) -> tuple[pd.DataFrame, dict[str, int], dict[str, int]]:
    if len(df) <= target_rows:
        sampled = df.copy()
        desired_counts = _target_counts(len(sampled), ticket_type_targets)
        return sampled, desired_counts, {}

    sampled, desired_counts, shortfalls = _sample_with_ticket_type_targets(
        df=df,
        target_rows=target_rows,
        seed=seed,
        ticket_type_targets=ticket_type_targets,
    )

    sampled = _enforce_high_risk_ratio(sampled, df, seed=seed + 20)
    sampled = _enforce_decision_minority(sampled, df, seed=seed + 30)
    sampled = _enforce_ticket_type_targets(sampled, df, ticket_type_targets, seed=seed + 40)
    sampled = _enforce_high_risk_ratio(sampled, df, seed=seed + 50)
    sampled = _enforce_decision_minority(sampled, df, seed=seed + 60)
    sampled = _enforce_ticket_type_targets(sampled, df, ticket_type_targets, seed=seed + 70)

    if len(sampled) < target_rows:
        leftover = df.drop(index=sampled.index, errors="ignore")
        if not leftover.empty:
            sampled = _dedupe_by_index(
                pd.concat(
                    [
                        sampled,
                        _sample_stratified(
                            leftover,
                            n=min(target_rows - len(sampled), len(leftover)),
                            seed=seed + 80,
                            strata=["ticket_type", "decision", "risk_tier"],
                        ),
                    ],
                    ignore_index=False,
                )
            )
    if len(sampled) > target_rows:
        sampled = sampled.sample(n=target_rows, random_state=seed + 90)
    return sampled, desired_counts, shortfalls


def main() -> None:
    args = parse_args()
    args.out_file.parent.mkdir(parents=True, exist_ok=True)
    ticket_type_targets = _parse_ticket_type_targets(args.ticket_type_targets)

    df = pd.read_parquet(args.in_file)
    required_cols = {"title", "description", "comments", "raw_type"}
    missing = required_cols - set(df.columns)
    if missing:
        raise SystemExit(f"Missing required columns in input: {sorted(missing)}")

    records: list[dict[str, Any]] = []

    for _, row in df.iterrows():
        trace: list[str] = []
        text = _full_text(row)
        title = _normalize_text(row.get("title", ""))
        raw_type = _normalize_text(row.get("raw_type", ""))

        ticket_type = _label_ticket_type(raw_type, title, text, trace)
        risk_tier = _label_risk_tier(text, trace)
        flags = _extract_evidence_flags(text)
        decision, evidence_score = _label_decision(flags, trace)

        questions_needed_seed = _questions_needed_seed(flags) if decision == "ASK_FOR_INFO" else []
        acceptance_criteria_seed = _acceptance_criteria_seed(ticket_type)
        confidence = _label_confidence(raw_type, evidence_score, decision)

        item = dict(row)
        item.update(
            {
                "decision": decision,
                "ticket_type": ticket_type,
                "risk_tier": risk_tier,
                "summary_seed": _summary_seed(_normalize_text(row.get("title")), _normalize_text(row.get("description"))),
                "error_signature_seed": _error_signature_seed(text),
                "suspected_components_seed": _suspected_components_seed(text),
                "questions_needed_seed": questions_needed_seed,
                "acceptance_criteria_seed": acceptance_criteria_seed,
                "evidence_score": int(evidence_score),
                "label_confidence": confidence,
                "rule_trace": trace,
                "has_repro_steps": flags["has_repro_steps"],
                "has_stacktrace_or_logs": flags["has_stacktrace_or_logs"],
                "has_expected_vs_actual": flags["has_expected_vs_actual"],
                "has_env_version": flags["has_env_version"],
                "has_scope_hint": flags["has_scope_hint"],
            }
        )
        records.append(item)

    labeled = pd.DataFrame.from_records(records)
    sampled, desired_type_counts, shortfalls = _target_sample(
        labeled,
        target_rows=args.target_rows,
        seed=args.seed,
        ticket_type_targets=ticket_type_targets,
    )
    sampled = sampled.reset_index(drop=True)
    sampled.to_parquet(args.out_file, index=False)

    decision_counts = sampled["decision"].value_counts(normalize=True).to_dict()
    high_ratio = float((sampled["risk_tier"] == "high").mean()) if len(sampled) else 0.0
    ticket_type_counts = sampled["ticket_type"].value_counts(normalize=True).to_dict()
    actual_type_counts = sampled["ticket_type"].value_counts().to_dict()

    print(f"[weak_label] input_rows={len(labeled):,}")
    print(f"[weak_label] output_rows={len(sampled):,}")
    print(f"[weak_label] ticket_type_targets={ticket_type_targets}")
    print(f"[weak_label] ticket_type_target_counts={desired_type_counts}")
    print(f"[weak_label] ticket_type_actual_counts={actual_type_counts}")
    print(f"[weak_label] ticket_type_distribution={ticket_type_counts}")
    print(f"[weak_label] ticket_type_shortfalls={shortfalls}")
    print(f"[weak_label] decision_distribution={decision_counts}")
    print(f"[weak_label] high_risk_ratio={high_ratio:.4f}")
    print(f"[weak_label] wrote {args.out_file}")


if __name__ == "__main__":
    main()
