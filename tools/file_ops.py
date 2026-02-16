from __future__ import annotations

from pathlib import Path
from typing import Any


def _resolve_safe_path(repo_root: Path, candidate: str) -> Path:
    target = (repo_root / candidate).resolve()
    root = repo_root.resolve()
    if root not in target.parents and target != root:
        raise ValueError(f"path '{candidate}' is outside repo root")
    return target


def read_target_files(repo_root: Path, file_paths: list[str], max_bytes: int = 200_000) -> dict[str, str]:
    output: dict[str, str] = {}
    for rel_path in file_paths:
        safe = _resolve_safe_path(repo_root, rel_path)
        if not safe.exists():
            raise FileNotFoundError(f"target file does not exist: {rel_path}")
        if not safe.is_file():
            raise ValueError(f"target path is not a file: {rel_path}")
        content = safe.read_text(encoding="utf-8")
        if len(content.encode("utf-8")) > max_bytes:
            raise ValueError(f"file exceeds max_bytes guardrail: {rel_path}")
        output[rel_path] = content
    return output


def apply_patch_optional(diff: str, enabled: bool = False) -> dict[str, Any]:
    if not enabled:
        return {
            "applied": False,
            "reason": "Patch application is disabled in prototype configuration.",
        }
    # Left as optional by design in this prototype.
    return {
        "applied": False,
        "reason": "Patch apply routine is intentionally not enabled in this build.",
        "diff_size": len(diff),
    }
