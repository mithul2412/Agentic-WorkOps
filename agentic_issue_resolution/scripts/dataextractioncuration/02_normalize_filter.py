#!/usr/bin/env python3
"""
Normalize raw issue datasets into one canonical schema with English filtering.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any, Optional

import pandas as pd


CANONICAL_COLUMNS = [
    "source",
    "project",
    "issue_id",
    "title",
    "description",
    "comments",
    "raw_type",
    "raw_priority",
    "created_at",
    "updated_at",
    "language",
    "text_len",
]

COMMON_ENGLISH_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "when",
    "from",
    "error",
    "issue",
    "not",
    "are",
    "can",
    "will",
    "should",
    "fix",
    "fails",
}

HTML_TAG_RE = re.compile(r"<[^>]+>")
MULTISPACE_RE = re.compile(r"\s+")

RAW_TYPE_BUG_CUES = {
    "bug",
    "type/bug",
    "c-bug",
    "regression",
    "broken",
    "type: bug fix",
    "defect",
    "failure",
}
RAW_TYPE_FEATURE_INSERT_CUES = {
    "feature",
    "type/feature",
    "new feature",
    "feature request",
    "proposal",
    "add support",
}
RAW_TYPE_FEATURE_UPDATE_CUES = {
    "enhancement",
    "c-enhancement",
    "maintenance",
    "cleanup",
    "refactor",
    "documentation",
    "docs",
    "type: maintenance",
    "type: documentation",
    "chore",
    "tech debt",
    "improvement",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize and filter issue datasets.")
    parser.add_argument(
        "--raw_dir",
        type=Path,
        default=Path("data/raw"),
        help="Directory containing raw parquet files.",
    )
    parser.add_argument(
        "--out_file",
        type=Path,
        default=Path("data/interim/canonical_issues.parquet"),
        help="Output parquet path for canonical issues.",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="en",
        help="Language filter. Use 'en' or 'all'.",
    )
    parser.add_argument(
        "--max_comments",
        type=int,
        default=5,
        help="Max comments to keep per issue after chronological sort.",
    )
    return parser.parse_args()


def _pick_column(columns: list[str], candidates: list[str]) -> Optional[str]:
    normalized = {col.lower(): col for col in columns}
    for candidate in candidates:
        if candidate.lower() in normalized:
            return normalized[candidate.lower()]
    return None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value)
    text = html.unescape(text)
    text = HTML_TAG_RE.sub(" ", text)
    text = MULTISPACE_RE.sub(" ", text).strip()
    return text


def _to_list_of_text(value: Any) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, list):
        return [_clean_text(v) for v in value if _clean_text(v)]
    if isinstance(value, tuple):
        return [_clean_text(v) for v in value if _clean_text(v)]
    cleaned = _clean_text(value)
    return [cleaned] if cleaned else []


def _normalize_issue_id(value: Any) -> str:
    text = _clean_text(value)
    if re.fullmatch(r"\d+\.0", text):
        return text[:-2]
    return text


def _composite_issue_key(split_value: Any, issue_id_value: Any) -> str:
    split = _clean_text(split_value)
    issue_id = _normalize_issue_id(issue_id_value)
    if split:
        return f"{split}::{issue_id}"
    return issue_id


def _derive_project_from_split(split_value: Any) -> str:
    split = _clean_text(split_value)
    if not split:
        return "unknown_project"
    parts = split.split("__")
    if len(parts) >= 3:
        return f"{parts[1]}/{parts[2]}"
    if len(parts) >= 2:
        return parts[1]
    return "unknown_project"


def _safe_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    raw = str(value).strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _collect_metadata_type_strings(metadata_value: Any) -> list[str]:
    meta = _safe_json_dict(metadata_value)
    if not meta:
        return []

    out: list[str] = []
    for key in ["type", "issue_type", "issuetype", "kind", "state_reason", "_provider"]:
        value = meta.get(key)
        if isinstance(value, (str, int, float)):
            out.append(str(value))

    labels = meta.get("labels")
    if isinstance(labels, list):
        for label in labels[:50]:
            if isinstance(label, dict):
                for key in ("name", "description"):
                    value = label.get(key)
                    if isinstance(value, (str, int, float)):
                        out.append(str(value))
            elif isinstance(label, (str, int, float)):
                out.append(str(label))

    raw_fields = meta.get("raw_fields")
    if isinstance(raw_fields, dict):
        for key in [
            "issuetype",
            "issue_type",
            "type",
            "kind",
            "priority",
            "severity",
            "status",
            "summary",
            "description",
        ]:
            value = raw_fields.get(key)
            if isinstance(value, (str, int, float)):
                out.append(str(value))
            elif isinstance(value, dict):
                name_value = value.get("name")
                if isinstance(name_value, (str, int, float)):
                    out.append(str(name_value))
    return out


def _infer_raw_type(raw_type_value: Any, metadata_value: Any) -> str:
    raw = _clean_text(raw_type_value).lower()
    if raw:
        if any(cue in raw for cue in RAW_TYPE_BUG_CUES):
            return "bug"
        if any(cue in raw for cue in RAW_TYPE_FEATURE_INSERT_CUES):
            return "feature_insert"
        if any(cue in raw for cue in RAW_TYPE_FEATURE_UPDATE_CUES):
            return "feature_update"
        return raw

    metadata_text = " ".join(_collect_metadata_type_strings(metadata_value)).lower()
    if not metadata_text:
        return ""

    bug_score = sum(cue in metadata_text for cue in RAW_TYPE_BUG_CUES)
    feature_insert_score = sum(cue in metadata_text for cue in RAW_TYPE_FEATURE_INSERT_CUES)
    feature_update_score = sum(cue in metadata_text for cue in RAW_TYPE_FEATURE_UPDATE_CUES)
    scores = {
        "bug": bug_score,
        "feature_insert": feature_insert_score,
        "feature_update": feature_update_score,
    }
    best_type = max(scores, key=scores.get)
    return best_type if scores[best_type] > 0 else ""


def _probable_english(text: str) -> bool:
    if not text:
        return False
    ascii_ratio = sum(ch.isascii() for ch in text) / max(1, len(text))
    tokens = re.findall(r"[A-Za-z]{2,}", text.lower())
    if len(tokens) < 6:
        return ascii_ratio >= 0.90
    common_hits = sum(token in COMMON_ENGLISH_WORDS for token in tokens[:120])
    return ascii_ratio >= 0.85 and common_hits >= 2


def _infer_language(explicit_value: Any, text_blob: str) -> str:
    explicit = _clean_text(explicit_value).lower()
    if explicit.startswith("en"):
        return "en"
    if explicit and explicit not in {"unknown", "null", "none"}:
        return explicit
    return "en" if _probable_english(text_blob) else "other"


def _build_comment_lookup(comments_df: pd.DataFrame, max_comments: int) -> dict[str, list[str]]:
    if comments_df.empty:
        return {}

    issue_id_col = _pick_column(
        list(comments_df.columns),
        ["issue_id", "issueid", "issue_key", "key", "ticket_id", "id", "number"],
    )
    text_col = _pick_column(
        list(comments_df.columns),
        ["comment", "body", "text", "content", "message", "details"],
    )
    created_col = _pick_column(
        list(comments_df.columns),
        ["created_at", "created", "timestamp", "time", "updated_at", "date"],
    )
    split_col = _pick_column(list(comments_df.columns), ["_split", "split", "dataset_split"])

    if issue_id_col is None or text_col is None:
        return {}

    working = comments_df[[issue_id_col, text_col] + ([created_col] if created_col else [])].copy()
    if split_col:
        working[split_col] = comments_df[split_col]
        working["__join_key"] = working.apply(
            lambda row: _composite_issue_key(row.get(split_col, ""), row.get(issue_id_col, "")),
            axis=1,
        )
    else:
        working["__join_key"] = working[issue_id_col].map(_normalize_issue_id)
    working[text_col] = working[text_col].map(_clean_text)
    working = working[working[text_col] != ""]

    if created_col:
        working = working.sort_values(by=["__join_key", created_col], kind="mergesort")
    else:
        working = working.sort_values(by=["__join_key"], kind="mergesort")

    lookup: dict[str, list[str]] = {}
    for join_key, group in working.groupby("__join_key", sort=False):
        comments = group[text_col].tolist()
        lookup[join_key] = comments[:max_comments]
    return lookup


def _normalize_swebench(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    issue_id_col = _pick_column(list(df.columns), ["instance_id", "id", "problem_id"])
    project_col = _pick_column(list(df.columns), ["repo", "repository", "project"])
    problem_col = _pick_column(
        list(df.columns),
        ["problem_statement", "description", "problem", "statement", "body"],
    )
    hints_col = _pick_column(list(df.columns), ["hints_text", "hints", "comments"])
    created_col = _pick_column(list(df.columns), ["created_at", "created", "timestamp"])
    updated_col = _pick_column(list(df.columns), ["updated_at", "updated"])

    records: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        description = _clean_text(row.get(problem_col, "")) if problem_col else ""
        title = description.split(". ")[0].strip() if description else ""
        title = title[:200] if title else f"swebench issue {idx}"
        comments = _to_list_of_text(row.get(hints_col, "")) if hints_col else []
        text_blob = " ".join([title, description] + comments)

        records.append(
            {
                "source": "swebench",
                "project": _clean_text(row.get(project_col, "")) if project_col else "unknown_project",
                "issue_id": _clean_text(row.get(issue_id_col, "")) if issue_id_col else f"swebench_{idx}",
                "title": title,
                "description": description,
                "comments": comments,
                "raw_type": "bug",
                "raw_priority": "",
                "created_at": _clean_text(row.get(created_col, "")) if created_col else "",
                "updated_at": _clean_text(row.get(updated_col, "")) if updated_col else "",
                "language": _infer_language("en", text_blob),
                "text_len": len(text_blob),
            }
        )

    out = pd.DataFrame.from_records(records)
    return out[CANONICAL_COLUMNS]


def _normalize_hank(issues_df: pd.DataFrame, comments_df: pd.DataFrame, max_comments: int) -> pd.DataFrame:
    if issues_df.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    comment_lookup = _build_comment_lookup(comments_df, max_comments=max_comments)
    columns = list(issues_df.columns)

    issue_id_col = _pick_column(columns, ["issue_id", "issueid", "issue_key", "key", "id", "number", "ticket_id"])
    title_col = _pick_column(columns, ["title", "summary", "subject", "name"])
    description_col = _pick_column(columns, ["description", "body", "content", "details", "text"])
    project_col = _pick_column(columns, ["project", "project_key", "repository", "repo", "repo_name", "namespace"])
    type_col = _pick_column(columns, ["issue_type", "type", "kind", "category"])
    priority_col = _pick_column(columns, ["priority", "severity", "prio", "importance"])
    created_col = _pick_column(columns, ["created_at", "created", "timestamp", "date"])
    updated_col = _pick_column(columns, ["updated_at", "updated", "last_modified"])
    language_col = _pick_column(columns, ["language", "lang"])
    split_col = _pick_column(columns, ["_split", "split", "dataset_split"])
    metadata_col = _pick_column(columns, ["metadata", "raw_fields", "extra", "payload"])

    records: list[dict[str, Any]] = []
    for idx, row in issues_df.iterrows():
        split_value = _clean_text(row.get(split_col, "")) if split_col else ""
        issue_id_raw = _normalize_issue_id(row.get(issue_id_col, "")) if issue_id_col else ""
        if not issue_id_raw:
            issue_id_raw = f"hank_{idx}"
        issue_id = _composite_issue_key(split_value, issue_id_raw)

        title = _clean_text(row.get(title_col, "")) if title_col else ""
        description = _clean_text(row.get(description_col, "")) if description_col else ""
        comments = comment_lookup.get(issue_id, [])
        text_blob = " ".join([title, description] + comments)
        language = _infer_language(row.get(language_col, ""), text_blob) if language_col else _infer_language("", text_blob)
        project_value = _clean_text(row.get(project_col, "")) if project_col else ""
        if not project_value:
            project_value = _derive_project_from_split(split_value)

        metadata_value = row.get(metadata_col, "") if metadata_col else ""

        records.append(
            {
                "source": "hankzhwang/issues",
                "project": project_value if project_value else "unknown_project",
                "issue_id": issue_id,
                "title": title,
                "description": description,
                "comments": comments,
                "raw_type": _infer_raw_type(row.get(type_col, "") if type_col else "", metadata_value),
                "raw_priority": _clean_text(row.get(priority_col, "")) if priority_col else "",
                "created_at": _clean_text(row.get(created_col, "")) if created_col else "",
                "updated_at": _clean_text(row.get(updated_col, "")) if updated_col else "",
                "language": language,
                "text_len": len(text_blob),
            }
        )

    out = pd.DataFrame.from_records(records)
    return out[CANONICAL_COLUMNS]


def _fingerprint(project: str, title: str, description: str) -> str:
    norm_title = re.sub(r"\W+", " ", title.lower()).strip()
    norm_description = re.sub(r"\W+", " ", description.lower()).strip()
    key = f"{project}|{norm_title}|{norm_description[:400]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def main() -> None:
    args = parse_args()
    args.out_file.parent.mkdir(parents=True, exist_ok=True)

    swebench_path = args.raw_dir / "swebench.parquet"
    hank_issues_path = args.raw_dir / "hank_issues.parquet"
    hank_comments_path = args.raw_dir / "hank_comments.parquet"

    frames: list[pd.DataFrame] = []

    if swebench_path.exists():
        swebench_df = pd.read_parquet(swebench_path)
        frames.append(_normalize_swebench(swebench_df))
        print(f"[normalize] loaded SWE-bench rows={len(swebench_df):,}")
    else:
        print(f"[normalize] SWE-bench file missing at {swebench_path}, skipping")

    if hank_issues_path.exists():
        hank_issues_df = pd.read_parquet(hank_issues_path)
        hank_comments_df = pd.read_parquet(hank_comments_path) if hank_comments_path.exists() else pd.DataFrame()
        frames.append(_normalize_hank(hank_issues_df, hank_comments_df, max_comments=args.max_comments))
        print(
            f"[normalize] loaded hank issues rows={len(hank_issues_df):,} comments rows={len(hank_comments_df):,}"
        )
    else:
        print(f"[normalize] hank issues file missing at {hank_issues_path}, skipping")

    if not frames:
        raise SystemExit("No input parquet files were found to normalize.")

    canonical = pd.concat(frames, ignore_index=True)
    canonical["comments"] = canonical["comments"].map(lambda x: x if isinstance(x, list) else [])

    before_nonempty = len(canonical)
    canonical = canonical[
        (canonical["title"].astype(str).str.strip() != "")
        | (canonical["description"].astype(str).str.strip() != "")
    ].copy()
    removed_empty = before_nonempty - len(canonical)

    if args.language.lower() != "all":
        language_value = args.language.lower()
        canonical = canonical[canonical["language"].astype(str).str.lower() == language_value].copy()

    before_dedupe = len(canonical)
    canonical["__fp"] = canonical.apply(
        lambda row: _fingerprint(str(row["project"]), str(row["title"]), str(row["description"])),
        axis=1,
    )
    canonical = canonical.drop_duplicates(subset=["__fp"]).drop(columns=["__fp"]).reset_index(drop=True)
    duplicate_rate = 0.0 if before_dedupe == 0 else (before_dedupe - len(canonical)) / before_dedupe

    canonical["text_len"] = canonical.apply(
        lambda row: len(" ".join([row["title"], row["description"]] + row["comments"])),
        axis=1,
    )
    canonical = canonical[CANONICAL_COLUMNS]
    canonical.to_parquet(args.out_file, index=False)

    nonempty_coverage = 0.0
    if len(canonical):
        nonempty_coverage = (
            (
                (canonical["title"].astype(str).str.strip() != "")
                | (canonical["description"].astype(str).str.strip() != "")
            ).mean()
        )

    print(f"[normalize] removed empty rows={removed_empty:,}")
    print(f"[normalize] duplicate_rate={duplicate_rate:.4f}")
    print(f"[normalize] nonempty_title_or_description_coverage={nonempty_coverage:.4f}")
    print(f"[normalize] wrote rows={len(canonical):,} to {args.out_file}")


if __name__ == "__main__":
    main()
