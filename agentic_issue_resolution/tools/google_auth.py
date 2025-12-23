from __future__ import annotations

from pathlib import Path
from typing import Sequence
import json


def get_google_credentials(
    scopes: Sequence[str],
    client_secret_file: str,
    token_file: str,
):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Google API dependencies missing. Install google-api-python-client/google-auth/google-auth-oauthlib."
        ) from exc

    required_scopes = _normalize_scopes(scopes)
    token_path = Path(token_file)
    creds = None
    if token_path.exists():
        token_scopes = _token_file_scopes(token_path)
        if set(required_scopes).issubset(token_scopes):
            creds = Credentials.from_authorized_user_file(str(token_path), scopes=required_scopes)

    if creds and creds.valid and _has_required_scopes(creds, required_scopes):
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if creds.valid and _has_required_scopes(creds, required_scopes):
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, scopes=required_scopes)
    try:
        creds = flow.run_local_server(port=0)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Google OAuth flow failed: {exc}") from exc

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    if not _has_required_scopes(creds, required_scopes):
        raise RuntimeError("Google token is missing required scopes after OAuth flow.")
    return creds


def _normalize_scopes(scopes: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    for scope in scopes:
        text = str(scope).strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _has_required_scopes(creds, required_scopes: Sequence[str]) -> bool:
    if not required_scopes:
        return True
    try:
        return bool(creds.has_scopes(required_scopes))
    except Exception:  # noqa: BLE001
        granted = set(getattr(creds, "scopes", []) or [])
        return set(required_scopes).issubset(granted)


def _token_file_scopes(token_path: Path) -> set[str]:
    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    scopes = payload.get("scopes")
    if isinstance(scopes, list):
        return {str(item).strip() for item in scopes if str(item).strip()}
    scope = payload.get("scope")
    if isinstance(scope, str):
        return {item.strip() for item in scope.split(" ") if item.strip()}
    return set()
