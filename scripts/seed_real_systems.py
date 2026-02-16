from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT
ENV_FILE = PACKAGE_ROOT / ".env"
DEFAULT_MANIFEST = PACKAGE_ROOT / "storage" / "seed_manifest.json"

SEED_LABEL = "poc_seed_real"
BITBUCKET_MARKER_PATH = "poc_seed/real_seed_marker.json"


@dataclass
class Scenario:
    mode: str
    team_profile: str
    ticket_type: str
    risk_tier: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed real Jira/Bitbucket/Confluence systems for POC testing.")
    parser.add_argument("--project-key", default="SCRUM", help="Jira project key.")
    parser.add_argument("--count", type=int, default=12, help="Number of Jira issues to ensure.")
    parser.add_argument(
        "--manifest-path",
        default=str(DEFAULT_MANIFEST),
        help="Output manifest JSON path.",
    )
    parser.add_argument("--seed-label", default=SEED_LABEL, help="Jira label used to detect existing seeded issues.")
    parser.add_argument("--confluence-drafts", type=int, default=3, help="Number of demo Confluence drafts to ensure.")
    return parser.parse_args()


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        parsed = os.path.expandvars(value.strip())
        if (parsed.startswith('"') and parsed.endswith('"')) or (parsed.startswith("'") and parsed.endswith("'")):
            parsed = parsed[1:-1]
        os.environ[key] = parsed


def _require_env(*keys: str) -> None:
    missing = [key for key in keys if not os.getenv(key, "").strip()]
    if missing:
        raise RuntimeError(f"missing required env vars: {', '.join(missing)}")


def _jira_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()
    headers = kwargs.pop("headers", {}) or {}
    headers = {"Accept": "application/json", **headers}
    response = requests.request(
        method=method,
        url=f"{base}{path}",
        auth=(email, token),
        headers=headers,
        timeout=45,
        **kwargs,
    )
    response.raise_for_status()
    if not response.text.strip():
        return {}
    return response.json()


def _confluence_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    base = os.getenv("CONFLUENCE_BASE_URL", "").rstrip("/")
    email = os.getenv("CONFLUENCE_EMAIL", "").strip()
    token = os.getenv("CONFLUENCE_API_TOKEN", "").strip()
    headers = kwargs.pop("headers", {}) or {}
    headers = {"Accept": "application/json", **headers}
    response = requests.request(
        method=method,
        url=f"{base}{path}",
        auth=(email, token),
        headers=headers,
        timeout=45,
        **kwargs,
    )
    response.raise_for_status()
    if not response.text.strip():
        return {}
    return response.json()


def _bitbucket_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    api_base = os.getenv("BITBUCKET_API_BASE", "https://api.bitbucket.org/2.0").rstrip("/")
    workspace = os.getenv("BITBUCKET_WORKSPACE", "").strip()
    repo_slug = os.getenv("BITBUCKET_REPO_SLUG", "").strip()
    token = os.getenv("BITBUCKET_API_TOKEN", "").strip()
    username = os.getenv("BITBUCKET_USERNAME", "").strip()
    app_password = os.getenv("BITBUCKET_APP_PASSWORD", "").strip()

    headers = kwargs.pop("headers", {}) or {}
    headers = {"Accept": "application/json", **headers}
    auth = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif username and app_password:
        auth = (username, app_password)
    else:
        raise RuntimeError(
            "Bitbucket auth not configured. Set BITBUCKET_API_TOKEN or BITBUCKET_USERNAME + BITBUCKET_APP_PASSWORD."
        )

    final_path = path.format(workspace=workspace, repo_slug=repo_slug)
    response = requests.request(
        method=method,
        url=f"{api_base}{final_path}",
        headers=headers,
        auth=auth,
        timeout=45,
        **kwargs,
    )
    response.raise_for_status()
    if not response.text.strip():
        return {}
    return response.json()


def _adf_from_lines(lines: list[str]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        content.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        )
    return {"type": "doc", "version": 1, "content": content}


def _build_scenarios(count: int) -> list[Scenario]:
    modes = ["incident", "feature", "customer_escalation"]
    teams = ["platform", "backend", "frontend", "ml_data"]
    ticket_types = ["bug", "feature_insert", "feature_update"]
    risk_tiers = ["high", "medium", "low"]
    combinations: list[Scenario] = []
    for mode in modes:
        for team_profile in teams:
            for ticket_type in ticket_types:
                for risk_tier in risk_tiers:
                    combinations.append(
                        Scenario(
                            mode=mode,
                            team_profile=team_profile,
                            ticket_type=ticket_type,
                            risk_tier=risk_tier,
                        )
                    )
    if count <= len(combinations):
        return combinations[:count]
    scenarios: list[Scenario] = []
    for idx in range(count):
        scenarios.append(combinations[idx % len(combinations)])
    return scenarios


def _jira_search_seeded_issues(project_key: str, seed_label: str) -> list[str]:
    query = f'project = {project_key} AND labels = "{seed_label}" ORDER BY created DESC'
    start_at = 0
    page_size = 100
    keys: list[str] = []
    while True:
        payload = _jira_request(
            "GET",
            "/rest/api/3/search/jql",
            params={"jql": query, "maxResults": page_size, "startAt": start_at, "fields": "summary,labels"},
        )
        rows = payload.get("issues", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list) or not rows:
            break
        for item in rows:
            if isinstance(item, dict) and item.get("key"):
                keys.append(str(item.get("key", "")).strip())
        if len(rows) < page_size:
            break
        start_at += len(rows)
    # preserve order but drop duplicates
    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def _create_jira_issue(project_key: str, scenario: Scenario, index: int, seed_label: str) -> str:
    summary = (
        f"[POC REAL {index:02d}] {scenario.team_profile} {scenario.mode} "
        f"{scenario.ticket_type.replace('_', ' ')}"
    )
    description_lines = [
        "POC seeded real issue for end-to-end workflow validation.",
        f"Mode: {scenario.mode}",
        f"Team profile: {scenario.team_profile}",
        f"Ticket type: {scenario.ticket_type}",
        f"Risk tier: {scenario.risk_tier}",
        "Steps to reproduce: run smoke script against this ticket in a clean environment.",
        "Expected behavior: workflow reaches COMPLETED and creates all external artifacts.",
        "Actual behavior: pending validation.",
        "Impacted scope: workflow manager/auditor/finalizer paths.",
    ]
    labels = [
        seed_label,
        "poc-real",
        f"mode:{scenario.mode}",
        f"team:{scenario.team_profile}",
        f"ticket_type:{scenario.ticket_type}",
        f"risk:{scenario.risk_tier}",
    ]

    issue_type_candidates = ["Task", "Story", "Bug"]
    last_error: Exception | None = None
    for issue_type in issue_type_candidates:
        try:
            payload = _jira_request(
                "POST",
                "/rest/api/3/issue",
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={
                    "fields": {
                        "project": {"key": project_key},
                        "summary": summary,
                        "description": _adf_from_lines(description_lines),
                        "issuetype": {"name": issue_type},
                        "labels": labels,
                    }
                },
            )
            key = str(payload.get("key", "")).strip()
            if key:
                return key
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    raise RuntimeError(f"failed to create Jira issue after issuetype fallbacks: {last_error}")


def _resolve_confluence_space_key() -> str:
    explicit = os.getenv("CONFLUENCE_SPACE_KEY", "").strip()
    if explicit:
        return explicit
    payload = _confluence_request("GET", "/rest/api/space", params={"limit": 1})
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    if isinstance(rows, list) and rows:
        first = rows[0]
        if isinstance(first, dict) and first.get("key"):
            return str(first["key"]).strip()
    raise RuntimeError("unable to resolve Confluence space key")


def _ensure_confluence_drafts(count: int) -> list[dict[str, Any]]:
    existing_payload = _confluence_request(
        "GET",
        "/rest/api/content/search",
        params={"cql": 'type=page and title ~ "POC Seed Real"', "limit": max(10, count * 2)},
    )
    existing_rows = existing_payload.get("results", []) if isinstance(existing_payload, dict) else []
    existing: list[dict[str, Any]] = []
    if isinstance(existing_rows, list):
        for row in existing_rows:
            if not isinstance(row, dict):
                continue
            existing.append(
                {
                    "draft_id": str(row.get("id", "")).strip(),
                    "title": str(row.get("title", "")).strip(),
                    "existing": True,
                }
            )
            if len(existing) >= count:
                break
    if len(existing) >= count:
        return existing[:count]

    to_create = count - len(existing)
    space_key = _resolve_confluence_space_key()
    created: list[dict[str, Any]] = []
    templates = [
        "Incident Mode Runbook",
        "Feature Mode Handoff Checklist",
        "Customer Escalation Playbook",
    ]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    for idx in range(to_create):
        title = f"POC Seed Real - {templates[idx % len(templates)]} - {now} #{idx + 1}"
        body = (
            "This is a seeded draft page for real integration testing.\n\n"
            f"Created at: {now}\n"
            "Purpose: validate Confluence write path from finalizer.\n"
            "Sections: Symptoms, Root cause, Fix, Verification, Prevention.\n"
        )
        storage_value = "<pre>" + body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</pre>"
        payload = _confluence_request(
            "POST",
            "/rest/api/content",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={
                "type": "page",
                "title": title,
                "status": "draft",
                "space": {"key": space_key},
                "body": {"storage": {"value": storage_value, "representation": "storage"}},
            },
        )
        created.append(
            {
                "draft_id": str(payload.get("id", "")).strip(),
                "title": str(payload.get("title", title)).strip(),
                "existing": False,
            }
        )
    return existing + created


def _ensure_bitbucket_seed_commit(seed_issue_keys: list[str]) -> dict[str, Any]:
    source_branch = os.getenv("BITBUCKET_SOURCE_BRANCH", "").strip()
    destination_branch = os.getenv("BITBUCKET_DESTINATION_BRANCH", "").strip()
    if not source_branch:
        raise RuntimeError("BITBUCKET_SOURCE_BRANCH is required")
    if not destination_branch:
        raise RuntimeError("BITBUCKET_DESTINATION_BRANCH is required")

    # Skip commit if marker file already exists on source branch.
    try:
        _bitbucket_request(
            "GET",
            f"/repositories/{{workspace}}/{{repo_slug}}/src/{source_branch}/{BITBUCKET_MARKER_PATH}",
        )
        return {
            "source_branch": source_branch,
            "destination_branch": destination_branch,
            "marker_path": BITBUCKET_MARKER_PATH,
            "created_commit": False,
            "reason": "marker already exists",
        }
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status not in {404, 400}:
            raise

    marker_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed_issue_keys": seed_issue_keys[:10],
        "purpose": "ensure source branch is ahead for PR creation in real POC runs",
    }
    content = json.dumps(marker_payload, indent=2) + "\n"
    response = _bitbucket_request(
        "POST",
        "/repositories/{workspace}/{repo_slug}/src",
        data={
            "branch": source_branch,
            "message": "POC real seed commit for workflow validation",
        },
        files={
            BITBUCKET_MARKER_PATH: (
                Path(BITBUCKET_MARKER_PATH).name,
                content.encode("utf-8"),
                "application/json",
            )
        },
    )
    commit_hash = str(response.get("hash", "")).strip()
    if not commit_hash and isinstance(response.get("commit"), dict):
        commit_hash = str(response["commit"].get("hash", "")).strip()
    return {
        "source_branch": source_branch,
        "destination_branch": destination_branch,
        "marker_path": BITBUCKET_MARKER_PATH,
        "created_commit": True,
        "commit_hash": commit_hash,
    }


def main() -> int:
    args = parse_args()
    _load_env(ENV_FILE)
    _require_env(
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "BITBUCKET_WORKSPACE",
        "BITBUCKET_REPO_SLUG",
        "CONFLUENCE_BASE_URL",
        "CONFLUENCE_EMAIL",
        "CONFLUENCE_API_TOKEN",
    )

    requested_count = max(1, int(args.count))
    existing_keys = _jira_search_seeded_issues(args.project_key, args.seed_label)
    scenarios = _build_scenarios(requested_count)

    created_keys: list[str] = []
    if len(existing_keys) < requested_count:
        start_idx = len(existing_keys)
        for idx in range(start_idx, requested_count):
            key = _create_jira_issue(
                project_key=args.project_key,
                scenario=scenarios[idx],
                index=idx + 1,
                seed_label=args.seed_label,
            )
            created_keys.append(key)
            existing_keys.append(key)

    confluence_rows = _ensure_confluence_drafts(count=max(1, args.confluence_drafts))
    bitbucket_result = _ensure_bitbucket_seed_commit(existing_keys)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_key": args.project_key,
        "seed_label": args.seed_label,
        "jira": {
            "requested_count": requested_count,
            "reused_issue_keys": existing_keys[: requested_count - len(created_keys)],
            "created_issue_keys": created_keys,
            "all_issue_keys": existing_keys[:requested_count],
        },
        "bitbucket": bitbucket_result,
        "confluence": {
            "drafts": confluence_rows,
        },
    }

    manifest_path = Path(args.manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote seed manifest: {manifest_path}")
    print(f"jira issues ready: {len(manifest['jira']['all_issue_keys'])}")
    print(f"confluence drafts tracked: {len(confluence_rows)}")
    print(f"bitbucket commit created: {bitbucket_result.get('created_commit')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
