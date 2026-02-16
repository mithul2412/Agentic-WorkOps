# Data Extraction + Curation (Sprint 1)

This folder contains the 4-script curation pipeline for the 3k weak-labeled pilot.

## LLM Output Stages (Reference)

- Stage A: Webhook Intake (no LLM)
- Stage B: Manager/Triage (mandatory LLM output #1)
- Stage C: Context Bridge (deterministic templates in v1, no LLM required)
- Stage D: Engineer Patch (mandatory LLM output #2 only for `READY_TO_PATCH`)
- Stage E: Auditor + Approval Pause (no LLM)
- Stage F: Finalizer (no LLM required; optional drafting LLMs can be added later)

## Dependencies

```bash
pip install -U datasets pandas pyarrow
```

## Run Order

From project root:

```bash
python3 scripts/dataextractioncuration/01_download.py \
  --out_dir data/raw \
  --seed 42 \
  --max_hank_rows 200000 \
  --include_swebench
```

```bash
python3 scripts/dataextractioncuration/02_normalize_filter.py \
  --raw_dir data/raw \
  --out_file data/interim/canonical_issues.parquet \
  --language en \
  --max_comments 5
```

```bash
python3 scripts/dataextractioncuration/03_weak_label.py \
  --in_file data/interim/canonical_issues.parquet \
  --out_file data/curated/triage_curated_v1_3k.parquet \
  --target_rows 3000 \
  --seed 42 \
  --ticket_type_targets bug=0.70,feature_update=0.20,feature_insert=0.10
```

```bash
python3 scripts/dataextractioncuration/04_make_review_pack.py \
  --in_file data/curated/triage_curated_v1_3k.parquet \
  --sample_size 300 \
  --out_dir data/review \
  --ticket_type_targets bug=0.70,feature_update=0.20,feature_insert=0.10
```

## Expected Artifacts

- `data/raw/swebench.parquet`
- `data/raw/hank_issues.parquet`
- `data/raw/hank_comments.parquet`
- `data/interim/canonical_issues.parquet`
- `data/curated/triage_curated_v1_3k.parquet`
- `data/review/review_sample_300.csv`
- `data/review/label_report.json`
- `data/review/distribution_report.csv`
