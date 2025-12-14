from __future__ import annotations

import re

from agentic_issue_resolution.models.artifacts import CodingBrief, PatchArtifact, RiskTier


DIFF_HEADER_RE = re.compile(r"^---\s+")
DIFF_PLUS_RE = re.compile(r"^\+\+\+\s+")
DIFF_FILE_RE = re.compile(r"^\+\+\+\s+(?:b/)?(.+)$")


def validate_coding_brief(brief: CodingBrief, max_files_editable: int) -> None:
    if len(brief.suspected_files) > max_files_editable:
        raise ValueError(
            f"coding brief exceeds max files editable ({len(brief.suspected_files)} > {max_files_editable})"
        )


def validate_patch_artifact(patch: PatchArtifact) -> None:
    lines = patch.diff.splitlines()
    if not any(DIFF_HEADER_RE.match(line) for line in lines):
        raise ValueError("patch must include '---' diff headers")
    if not any(DIFF_PLUS_RE.match(line) for line in lines):
        raise ValueError("patch must include '+++' diff headers")


def extract_changed_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        match = DIFF_FILE_RE.match(line)
        if not match:
            continue
        path = match.group(1).strip()
        if path == "/dev/null":
            continue
        if path.startswith("a/"):
            path = path[2:]
        if path.startswith("b/"):
            path = path[2:]
        if path not in files:
            files.append(path)
    return files


def verify_patch_scope(diff: str, allowed_files: list[str]) -> list[str]:
    changed = extract_changed_files(diff)
    allowed_set = {path.strip() for path in allowed_files if path.strip()}
    unexpected = [path for path in changed if path not in allowed_set]
    if unexpected:
        raise ValueError(f"patch scope violation; changed files outside brief: {unexpected}")
    return changed


def apply_high_risk_strict_checks(risk_tier: RiskTier, diff: str) -> None:
    if risk_tier != RiskTier.HIGH:
        return

    lowered = diff.lower()
    if "todo" in lowered:
        raise ValueError("high-risk patch cannot include TODO placeholders")
    if "fixme" in lowered:
        raise ValueError("high-risk patch cannot include FIXME placeholders")

    if "--- /dev/null" in diff or "+++ /dev/null" in diff:
        raise ValueError("high-risk patch cannot create or delete files in prototype strict mode")

    changed_lines = [
        line
        for line in diff.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++")
        and not line.startswith("---")
    ]
    if len(changed_lines) < 2:
        raise ValueError("high-risk patch must include at least two concrete changed lines")
