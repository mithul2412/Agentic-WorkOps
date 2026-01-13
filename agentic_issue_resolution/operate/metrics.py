from __future__ import annotations

import math
from typing import Any


def estimate_tokens_from_text(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def compute_cost_proxy(
    input_text: str,
    output_text: str,
    input_cost_coef: float,
    output_cost_coef: float,
) -> float:
    input_tokens = estimate_tokens_from_text(input_text)
    output_tokens = estimate_tokens_from_text(output_text)
    # Coefficients are interpreted as cost per 1k tokens.
    cost = (input_tokens * input_cost_coef + output_tokens * output_cost_coef) / 1000.0
    return round(cost, 8)


def summarize_policy_metrics(metric_rows: list[dict[str, Any]]) -> dict[str, float]:
    if not metric_rows:
        return {
            "schema_pass_rate": 0.0,
            "tool_correctness": 0.0,
            "violations": 0.0,
            "attempts": 0.0,
            "runtime_ms": 0.0,
            "cost_proxy": 0.0,
        }
    count = max(1, len(metric_rows))
    return {
        "schema_pass_rate": _avg(metric_rows, "schema_pass_rate", count),
        "tool_correctness": _avg(metric_rows, "tool_correctness", count),
        "violations": _avg(metric_rows, "violations", count),
        "attempts": _avg(metric_rows, "attempts", count),
        "runtime_ms": _avg(metric_rows, "runtime_ms", count),
        "cost_proxy": _avg(metric_rows, "cost_proxy", count),
    }


def _avg(rows: list[dict[str, Any]], key: str, count: int) -> float:
    total = 0.0
    for row in rows:
        value = row.get(key, 0.0)
        try:
            total += float(value)
        except Exception:  # noqa: BLE001
            total += 0.0
    return round(total / count, 6)
