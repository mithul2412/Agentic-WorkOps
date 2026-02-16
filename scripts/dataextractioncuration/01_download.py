#!/usr/bin/env python3
"""
Download open-source issue datasets into parquet files for Sprint 1 curation.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

try:
    from datasets import get_dataset_config_names, load_dataset, load_dataset_builder
except Exception as exc:  # pragma: no cover - import-time dependency failure
    raise SystemExit(
        "Missing dependency: `datasets`. Install with `pip install datasets pandas pyarrow`."
    ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download datasets for issue curation.")
    parser.add_argument(
        "--out_dir",
        type=Path,
        default=Path("data/raw"),
        help="Directory where parquet files are written.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed used for deterministic sampling.",
    )
    parser.add_argument(
        "--max_hank_rows",
        type=int,
        default=200_000,
        help="Maximum total rows to keep for hank issues and comments each.",
    )
    parser.add_argument(
        "--include_swebench",
        action="store_true",
        help="Include SWE-bench download. If omitted, only hankzhwang/issues is downloaded.",
    )
    return parser.parse_args()


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _to_parquet(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path, index=False)
    except Exception as exc:
        raise RuntimeError(
            "Failed writing parquet. Ensure `pyarrow` is installed: pip install pyarrow"
        ) from exc


def _split_names(dataset_name: str, config_name: Optional[str]) -> list[str]:
    builder = load_dataset_builder(dataset_name, config_name)
    names = list(builder.info.splits.keys())
    if not names:
        raise RuntimeError(f"No splits found for dataset={dataset_name}, config={config_name}")
    return names


def _ordered_splits(split_names: list[str]) -> list[str]:
    preferred_common = [s for s in ("train", "validation", "dev", "test") if s in split_names]
    remaining = [s for s in split_names if s not in preferred_common]
    remaining_sorted = sorted(remaining, key=lambda s: (0 if "jira" in s.lower() else 1, s))
    return preferred_common + remaining_sorted


def _split_cap_plan(split_names: list[str], max_rows: Optional[int]) -> dict[str, Optional[int]]:
    if max_rows is None:
        return {name: None for name in split_names}
    if not split_names:
        return {}

    jira_splits = [s for s in split_names if "jira" in s.lower()]
    other_splits = [s for s in split_names if s not in jira_splits]

    caps: dict[str, int] = {}
    if jira_splits and other_splits:
        jira_budget = max(1, int(max_rows * 0.7))
        other_budget = max(1, max_rows - jira_budget)
        jira_cap = max(1, math.ceil(jira_budget / len(jira_splits)))
        other_cap = max(1, math.ceil(other_budget / len(other_splits)))
        for split_name in jira_splits:
            caps[split_name] = jira_cap
        for split_name in other_splits:
            caps[split_name] = other_cap
    else:
        cap = max(1, math.ceil(max_rows / len(split_names)))
        for split_name in split_names:
            caps[split_name] = cap
    return caps


def _load_config_rows(
    dataset_name: str,
    config_name: Optional[str],
    max_rows: Optional[int],
    seed: int,
) -> pd.DataFrame:
    split_names = _ordered_splits(_split_names(dataset_name, config_name))
    cap_plan = _split_cap_plan(split_names, max_rows=max_rows)
    frames: list[pd.DataFrame] = []

    for split_name in split_names:
        split_cap = cap_plan.get(split_name)
        split_spec = split_name if split_cap is None else f"{split_name}[:{split_cap}]"
        try:
            split_ds = load_dataset(dataset_name, config_name, split=split_spec)
            frame = split_ds.to_pandas()
            frame["_dataset_name"] = dataset_name
            frame["_config_name"] = config_name if config_name is not None else "default"
            frame["_split"] = split_name
            frames.append(frame)
        except Exception as exc:
            print(
                f"[download] skipped split={split_name} config={config_name}: {exc}",
                file=sys.stderr,
            )

    merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if max_rows is not None and len(merged) > max_rows:
        merged = merged.sample(n=max_rows, random_state=seed).reset_index(drop=True)
    return merged


def _prioritize_configs(configs: Iterable[str], keyword: str) -> list[str]:
    matches = [cfg for cfg in configs if keyword in cfg.lower()]
    if not matches:
        return []
    return sorted(matches, key=lambda cfg: (0 if "jira" in cfg.lower() else 1, cfg))


def download_swebench(out_dir: Path) -> None:
    dataset_name = "SWE-bench/SWE-bench"
    swebench = load_dataset(dataset_name)
    frames: list[pd.DataFrame] = []

    for split_name, split_ds in swebench.items():
        frame = split_ds.to_pandas()
        frame["_dataset_name"] = dataset_name
        frame["_config_name"] = "default"
        frame["_split"] = split_name
        frames.append(frame)

    merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    out_path = out_dir / "swebench.parquet"
    _to_parquet(merged, out_path)
    print(f"[download] wrote {len(merged):,} rows to {out_path}")


def download_hank(out_dir: Path, max_hank_rows: int, seed: int) -> None:
    dataset_name = "hankzhwang/issues"
    configs = get_dataset_config_names(dataset_name)
    if not configs:
        raise RuntimeError(f"No configurations found for dataset {dataset_name}")

    issue_configs = _prioritize_configs(configs, "issue")
    comment_configs = _prioritize_configs(configs, "comment")

    if not issue_configs:
        issue_configs = sorted(configs, key=lambda cfg: (0 if "jira" in cfg.lower() else 1, cfg))

    issue_frames: list[pd.DataFrame] = []
    comment_frames: list[pd.DataFrame] = []

    issue_cap_per_cfg = max(1, max_hank_rows // max(1, len(issue_configs)))
    for cfg in issue_configs:
        try:
            frame = _load_config_rows(dataset_name, cfg, issue_cap_per_cfg, seed=seed)
            issue_frames.append(frame)
            print(f"[download] loaded issue config={cfg} rows={len(frame):,}")
        except Exception as exc:
            print(f"[download] skipped issue config={cfg}: {exc}", file=sys.stderr)

    if comment_configs:
        comment_cap_per_cfg = max(1, max_hank_rows // max(1, len(comment_configs)))
        for cfg in comment_configs:
            try:
                frame = _load_config_rows(dataset_name, cfg, comment_cap_per_cfg, seed=seed)
                comment_frames.append(frame)
                print(f"[download] loaded comment config={cfg} rows={len(frame):,}")
            except Exception as exc:
                print(f"[download] skipped comment config={cfg}: {exc}", file=sys.stderr)

    issue_df = pd.concat(issue_frames, ignore_index=True) if issue_frames else pd.DataFrame()
    comment_df = pd.concat(comment_frames, ignore_index=True) if comment_frames else pd.DataFrame()

    if len(issue_df) > max_hank_rows:
        issue_df = issue_df.sample(n=max_hank_rows, random_state=seed).reset_index(drop=True)
    if len(comment_df) > max_hank_rows:
        comment_df = comment_df.sample(n=max_hank_rows, random_state=seed).reset_index(drop=True)

    _to_parquet(issue_df, out_dir / "hank_issues.parquet")
    _to_parquet(comment_df, out_dir / "hank_comments.parquet")

    print(f"[download] wrote {len(issue_df):,} rows to {out_dir / 'hank_issues.parquet'}")
    print(f"[download] wrote {len(comment_df):,} rows to {out_dir / 'hank_comments.parquet'}")


def main() -> None:
    args = parse_args()
    random.seed(args.seed)
    _ensure_dir(args.out_dir)

    if args.include_swebench:
        download_swebench(args.out_dir)
    else:
        print("[download] skipping SWE-bench (pass --include_swebench to include)")

    download_hank(args.out_dir, args.max_hank_rows, args.seed)
    print("[download] done")


if __name__ == "__main__":
    main()
