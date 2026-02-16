from __future__ import annotations

from typing import Any

from models.artifacts import (
    CodingBrief,
    ConfluenceDraft,
    EvidenceItem,
    JiraCommentDraft,
    ManagerOutput,
)


def build_coding_brief(
    ticket_id: str,
    manager_output: ManagerOutput,
    evidence: list[EvidenceItem],
    max_files_editable: int,
    repo_file_candidates: list[str],
) -> CodingBrief:
    target_files: list[str] = []
    for file_path in manager_output.coding_brief.suspected_files:
        if file_path and file_path not in target_files:
            target_files.append(file_path)
    for item in evidence:
        if item.source.value == "code":
            file_path = str(item.id).strip()
            if file_path and file_path not in target_files:
                target_files.append(file_path)
    if not target_files and repo_file_candidates:
        target_files.append(repo_file_candidates[0])

    target_files = target_files[:max_files_editable]

    constraints = [
        "Engineer agent is file-locked and must edit only files listed in suspected_files.",
        "Output must be a valid unified diff patch.",
    ]
    if manager_output.risk_tier.value == "high":
        constraints.append("High-risk issue: include extra safety checks and avoid placeholder TODO/FIXME changes.")

    return CodingBrief(
        ticket_id=ticket_id,
        ticket_type=manager_output.ticket_type,
        risk_tier=manager_output.risk_tier,
        summary=manager_output.summary,
        error_signature=manager_output.error_signature,
        suspected_files=target_files,
        hypothesis=manager_output.coding_brief.hypothesis,
        acceptance_criteria=manager_output.coding_brief.acceptance_criteria,
        constraints=constraints,
        evidence=evidence,
    )


def build_jira_comment_draft(
    manager_output: ManagerOutput,
    coding_brief: CodingBrief | None = None,
) -> JiraCommentDraft:
    if manager_output.decision.value == "ASK_FOR_INFO":
        questions = "\n".join(f"- {question}" for question in manager_output.questions_needed)
        body = (
            "Triage update: additional information is required before patch generation.\n"
            f"Summary: {manager_output.summary}\n"
            "Please provide:\n"
            f"{questions}"
        )
        return JiraCommentDraft(body=body)

    files = ", ".join(coding_brief.suspected_files) if coding_brief else "TBD"
    body = (
        "Triage update: enough evidence gathered, moving to patch generation.\n"
        f"Summary: {manager_output.summary}\n"
        f"Target files: {files}\n"
        f"Hypothesis: {manager_output.coding_brief.hypothesis}"
    )
    return JiraCommentDraft(body=body)


def build_confluence_draft(ticket_id: str, context: dict[str, Any]) -> ConfluenceDraft:
    summary = context.get("summary", "No summary")
    root_cause = context.get("root_cause", "Pending confirmation after merge validation.")
    fix = context.get("fix", "See linked PR for patch details.")
    verification = context.get("verification", "Run regression suite and targeted scenario checks.")
    prevention = context.get("prevention", "Add monitoring and tests for this failure signature.")
    body = (
        "## Symptoms\n"
        f"{summary}\n\n"
        "## Root cause\n"
        f"{root_cause}\n\n"
        "## Fix\n"
        f"{fix}\n\n"
        "## Verification\n"
        f"{verification}\n\n"
        "## Prevention\n"
        f"{prevention}\n"
    )
    return ConfluenceDraft(
        title=f"[Draft] KB Update for {ticket_id}",
        body=body,
    )
