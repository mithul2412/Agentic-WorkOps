from __future__ import annotations

import re


def estimate_category(ticket_payload: dict, category_key: str = "ticket_type|risk_tier") -> str:
    team_profile = estimate_team_profile(ticket_payload)
    ticket_type = estimate_ticket_type(ticket_payload)
    risk_tier = estimate_risk_tier(ticket_payload)
    return _build_category(
        team_profile=team_profile,
        ticket_type=ticket_type,
        risk_tier=risk_tier,
        category_key=category_key,
    )


def estimate_team_profile(ticket_payload: dict) -> str:
    explicit = str(ticket_payload.get("team_profile", "")).strip().lower()
    if explicit in {"platform", "backend", "frontend", "ml_data"}:
        return explicit

    labels = ticket_payload.get("labels", [])
    if isinstance(labels, list):
        lower_labels = [str(item).strip().lower() for item in labels if str(item).strip()]
        for token, resolved in {
            "team:platform": "platform",
            "team:backend": "backend",
            "team:frontend": "frontend",
            "team:ml_data": "ml_data",
            "team:ml-data": "ml_data",
        }.items():
            if token in lower_labels:
                return resolved

    text = _joined_text(ticket_payload)
    if any(term in text for term in ["pipeline", "feature store", "model", "dataset", "training", "inference"]):
        return "ml_data"
    if any(term in text for term in ["react", "ui", "frontend", "css", "browser"]):
        return "frontend"
    if any(term in text for term in ["api", "endpoint", "service", "database", "sql"]):
        return "backend"
    return "platform"


def estimate_ticket_type(ticket_payload: dict) -> str:
    text = _joined_text(ticket_payload)
    title = str(ticket_payload.get("title", "")).lower()
    bug_terms = ["bug", "error", "exception", "traceback", "crash", "fails", "broken", "500", "timeout"]
    insert_terms = ["new feature", "feature request", "add support", "introduce", "enable", "allow users"]
    update_terms = ["update", "improve", "enhance", "migrate", "refactor", "optimize", "maintenance"]

    bug_score = sum(term in text for term in bug_terms)
    insert_score = sum(term in text for term in insert_terms)
    update_score = sum(term in text for term in update_terms)
    if bug_score >= insert_score and bug_score >= update_score and bug_score > 0:
        return "bug"
    if insert_score > bug_score and insert_score >= update_score:
        return "feature_insert"
    if update_score > 0:
        return "feature_update"
    if re.search(r"^\s*(add|enable|implement|introduce|create|support|allow)\b", title, flags=re.I):
        return "feature_insert"
    return "feature_update"


def estimate_risk_tier(ticket_payload: dict) -> str:
    text = _joined_text(ticket_payload)
    high_terms = [
        "security",
        "authentication",
        "authorization",
        "auth",
        "billing",
        "payment",
        "token leak",
        "data loss",
        "privacy",
    ]
    medium_terms = ["performance", "latency", "timeout", "regression", "memory leak", "slow"]
    if any(term in text for term in high_terms):
        return "high"
    if any(term in text for term in medium_terms):
        return "medium"
    return "low"


def category_from_manager_output(
    manager_output: dict,
    ticket_payload: dict | None = None,
    category_key: str = "ticket_type|risk_tier",
) -> str:
    team_profile = str(manager_output.get("team_profile", "")).strip().lower()
    if team_profile not in {"platform", "backend", "frontend", "ml_data"}:
        team_profile = estimate_team_profile(ticket_payload or {})
    ticket_type = str(manager_output.get("ticket_type", "feature_update")).strip() or "feature_update"
    risk_tier = str(manager_output.get("risk_tier", "low")).strip() or "low"
    return _build_category(
        team_profile=team_profile,
        ticket_type=ticket_type,
        risk_tier=risk_tier,
        category_key=category_key,
    )


def _joined_text(ticket_payload: dict) -> str:
    comments = ticket_payload.get("comments", [])
    if not isinstance(comments, list):
        comments = []
    base = [
        str(ticket_payload.get("title", "")),
        str(ticket_payload.get("description", "")),
        " ".join(str(item) for item in comments),
    ]
    return " ".join(base).lower()


def _build_category(
    team_profile: str,
    ticket_type: str,
    risk_tier: str,
    category_key: str,
) -> str:
    mapping = {
        "team_profile": team_profile,
        "ticket_type": ticket_type,
        "risk_tier": risk_tier,
    }
    tokens = [token.strip() for token in str(category_key).split("|") if token.strip()]
    if not tokens:
        tokens = ["ticket_type", "risk_tier"]
    parts: list[str] = []
    for token in tokens:
        parts.append(mapping.get(token, "unknown"))
    return "|".join(parts)
