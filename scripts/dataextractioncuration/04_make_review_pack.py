#!/usr/bin/env python3
"""
Create manual validation review artifacts from curated labeled data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import pandas as pd


LIST_COLUMNS = [
    "comments",
    "suspected_components_seed",
    "questions_needed_seed",
    "acceptance_criteria_seed",
    "rule_trace",
]

DEFAULT_TICKET_TYPE_TARGETS = {
    "bug": 0.70,
    "feature_update": 0.20,
    "feature_insert": 0.10,
}
TICKET_TYPE_TOLERANCE = 0.03


def _dedupe_by_index(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df.loc[~df.index.duplicated(keep="first")]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build review sample and quality reports.")
    parser.add_argument(
        "--in_file",
        type=Path,
        default=Path("data/curated/triage_curated_v1_3k.parquet"),
        help="Input curated parquet file.",
    )
    parser.add_argument(
        "--sample_size",
        type=int,
        default=300,
        help="Sample size for manual review CSV.",
    )
    parser.add_argument(
        "--out_dir",
        type=Path,
        default=Path("data/review"),
        help="Output directory for review artifacts.",
    )
    parser.add_argument(
        "--ticket_type_targets",
        type=str,
        default="bug=0.70,feature_update=0.20,feature_insert=0.10",
        help="Ticket type target mix as comma-separated ratios.",
    )
    return parser.parse_args()


def _parse_ticket_type_targets(value: str) -> dict[str, float]:
    parsed = DEFAULT_TICKET_TYPE_TARGETS.copy()
    for chunk in value.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(f"Invalid ticket_type_targets item: {piece}")
        key, raw_ratio = piece.split("=", 1)
        key = key.strip()
        if key not in parsed:
            raise ValueError(f"Unknown ticket type key: {key}")
        parsed[key] = float(raw_ratio.strip())
    total = sum(parsed.values())
    if total <= 0:
        raise ValueError("ticket_type_targets sum must be > 0")
    return {key: parsed[key] / total for key in parsed}


def _serialize_for_csv(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    if hasattr(value, "tolist"):
        try:
            list_value = value.tolist()
            if isinstance(list_value, list):
                return json.dumps(list_value, ensure_ascii=False)
        except Exception:
            pass
    return str(value)


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
    for offset, row in grouped.iterrows():
        mask = pd.Series(True, index=df.index)
        for col in strata:
            mask &= df[col] == row[col]
        subset = df[mask]
        if subset.empty:
            continue
        take = min(int(row["quota"]), len(subset))
        sampled_parts.append(subset.sample(n=take, random_state=seed + offset))

    sampled = _dedupe_by_index(pd.concat(sampled_parts, ignore_index=False))
    if len(sampled) < n:
        leftovers = df.drop(index=sampled.index, errors="ignore")
        if not leftovers.empty:
            sampled = _dedupe_by_index(
                pd.concat(
                    [
                        sampled,
                        leftovers.sample(
                            n=min(n - len(sampled), len(leftovers)),
                            random_state=seed + 99,
                        ),
                    ],
                    ignore_index=False,
                )
            )
    return sampled.sample(n=min(n, len(sampled)), random_state=seed + 199)


def _normalize_text_for_fp(text: str) -> str:
    return re.sub(r"\W+", " ", str(text).lower()).strip()


def _duplicate_rate(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    fp = df.apply(
        lambda row: hashlib.sha1(
            f"{_normalize_text_for_fp(row.get('project', ''))}|{_normalize_text_for_fp(row.get('title', ''))}|"
            f"{_normalize_text_for_fp(row.get('description', ''))[:400]}".encode("utf-8")
        ).hexdigest(),
        axis=1,
    )
    dupes = fp.duplicated().sum()
    return float(dupes / max(1, len(df)))


def _distribution_rows(df: pd.DataFrame, column: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    counts = df[column].value_counts(dropna=False)
    for key, count in counts.items():
        out.append(
            {
                "metric": column,
                "key": str(key),
                "count": int(count),
                "ratio": float(count / max(1, len(df))),
            }
        )
    return out


def _combo_distribution_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    combo = (
        df.groupby(["decision", "ticket_type", "risk_tier"], dropna=False)
        .size()
        .rename("count")
        .reset_index()
    )
    for _, row in combo.iterrows():
        key = f"{row['decision']}|{row['ticket_type']}|{row['risk_tier']}"
        out.append(
            {
                "metric": "decision_ticket_type_risk_tier",
                "key": key,
                "count": int(row["count"]),
                "ratio": float(row["count"] / max(1, len(df))),
            }
        )
    return out


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(args.in_file)
    ticket_type_targets = _parse_ticket_type_targets(args.ticket_type_targets)

    required = {"decision", "ticket_type", "risk_tier", "title", "description"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"Input file missing required columns: {sorted(missing)}")

    review_sample = _sample_stratified(
        df,
        n=min(args.sample_size, len(df)),
        seed=42,
        strata=["decision", "ticket_type", "risk_tier"],
    ).copy()

    for column in LIST_COLUMNS:
        if column in review_sample.columns:
            review_sample[column] = review_sample[column].map(_serialize_for_csv)

    review_csv_path = args.out_dir / "review_sample_300.csv"
    review_sample.to_csv(review_csv_path, index=False)

    total_rows = int(len(df))
    nonempty_coverage = float(
        (
            (df["title"].astype(str).str.strip() != "")
            | (df["description"].astype(str).str.strip() != "")
        ).mean()
    )
    decision_dist = df["decision"].value_counts(normalize=True).to_dict()
    decision_minority = float(min(decision_dist.values()) if decision_dist else 0.0)
    high_ratio = float((df["risk_tier"] == "high").mean())
    dup_rate = _duplicate_rate(df)
    ticket_type_dist = df["ticket_type"].value_counts(normalize=True).to_dict()
    unknown_project_ratio = float((df["project"].astype(str) == "unknown_project").mean())
    issue_id_uniqueness_rate = float(df["issue_id"].astype(str).nunique() / max(1, len(df)))

    bug_target = float(ticket_type_targets.get("bug", DEFAULT_TICKET_TYPE_TARGETS["bug"]))
    feature_update_target = float(
        ticket_type_targets.get("feature_update", DEFAULT_TICKET_TYPE_TARGETS["feature_update"])
    )
    feature_insert_target = float(
        ticket_type_targets.get("feature_insert", DEFAULT_TICKET_TYPE_TARGETS["feature_insert"])
    )

    label_report = {
        "row_count": total_rows,
        "quality_gates": {
            "row_count_3000_plus_minus_5_percent": {
                "actual": total_rows,
                "pass": 2850 <= total_rows <= 3150,
            },
            "nonempty_title_description_coverage_gte_95_percent": {
                "actual": nonempty_coverage,
                "pass": nonempty_coverage >= 0.95,
            },
            "decision_class_minority_gte_30_percent": {
                "actual": decision_minority,
                "pass": decision_minority >= 0.30,
            },
            "risk_tier_high_between_5_and_25_percent": {
                "actual": high_ratio,
                "pass": 0.05 <= high_ratio <= 0.25,
            },
            "duplicate_rate_lt_3_percent": {
                "actual": dup_rate,
                "pass": dup_rate < 0.03,
            },
            "review_sample_size": {
                "actual": int(len(review_sample)),
                "pass": int(len(review_sample)) == min(args.sample_size, total_rows),
            },
            "ticket_type_target_bug_70pct_pm_3pct": {
                "actual": float(ticket_type_dist.get("bug", 0.0)),
                "target": bug_target,
                "tolerance": TICKET_TYPE_TOLERANCE,
                "pass": abs(float(ticket_type_dist.get("bug", 0.0)) - bug_target) <= TICKET_TYPE_TOLERANCE,
            },
            "ticket_type_target_feature_update_20pct_pm_3pct": {
                "actual": float(ticket_type_dist.get("feature_update", 0.0)),
                "target": feature_update_target,
                "tolerance": TICKET_TYPE_TOLERANCE,
                "pass": abs(float(ticket_type_dist.get("feature_update", 0.0)) - feature_update_target)
                <= TICKET_TYPE_TOLERANCE,
            },
            "ticket_type_target_feature_insert_10pct_pm_3pct": {
                "actual": float(ticket_type_dist.get("feature_insert", 0.0)),
                "target": feature_insert_target,
                "tolerance": TICKET_TYPE_TOLERANCE,
                "pass": abs(float(ticket_type_dist.get("feature_insert", 0.0)) - feature_insert_target)
                <= TICKET_TYPE_TOLERANCE,
            },
            "unknown_project_ratio_lte_5pct": {
                "actual": unknown_project_ratio,
                "pass": unknown_project_ratio <= 0.05,
            },
            "issue_id_uniqueness_rate_gte_99pct": {
                "actual": issue_id_uniqueness_rate,
                "pass": issue_id_uniqueness_rate >= 0.99,
            },
        },
        "distributions": {
            "decision": {str(k): float(v) for k, v in decision_dist.items()},
            "ticket_type": {str(k): float(v) for k, v in ticket_type_dist.items()},
            "risk_tier": {
                str(k): float(v)
                for k, v in df["risk_tier"].value_counts(normalize=True).to_dict().items()
            },
        },
    }

    label_report_path = args.out_dir / "label_report.json"
    with label_report_path.open("w", encoding="utf-8") as f:
        json.dump(label_report, f, indent=2, ensure_ascii=False)

    distribution_rows: list[dict[str, Any]] = []
    for metric in ["decision", "ticket_type", "risk_tier"]:
        distribution_rows.extend(_distribution_rows(df, metric))
    distribution_rows.extend(_combo_distribution_rows(df))

    distribution_df = pd.DataFrame(distribution_rows)
    distribution_csv_path = args.out_dir / "distribution_report.csv"
    distribution_df.to_csv(distribution_csv_path, index=False)

    print(f"[review] wrote {review_csv_path}")
    print(f"[review] wrote {label_report_path}")
    print(f"[review] wrote {distribution_csv_path}")


if __name__ == "__main__":
    main()
