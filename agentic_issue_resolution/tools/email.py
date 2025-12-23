from __future__ import annotations

import base64
import os
from email.message import EmailMessage
from typing import Any

from agentic_issue_resolution.models.artifacts import EmailDraftArtifact
from agentic_issue_resolution.tools.google_auth import get_google_credentials


# Keep a single shared token scope-set across Gmail and Calendar clients
# so one integration does not overwrite token scopes required by the other.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
]


class MockEmailClient:
    def create_draft(self, to: list[str], subject: str, body: str) -> EmailDraftArtifact:
        return EmailDraftArtifact(
            to=to,
            subject=subject,
            body=body,
            provider_draft_id="mock-draft-1",
        )


class GoogleGmailClient:
    def __init__(
        self,
        client_secret_file: str | None = None,
        token_file: str | None = None,
    ) -> None:
        self.client_secret_file = client_secret_file or os.getenv("GOOGLE_CLIENT_SECRET_FILE", "")
        self.token_file = token_file or os.getenv("GOOGLE_TOKEN_FILE", "storage/google_token.json")

    def _service(self):
        if not self.client_secret_file:
            raise RuntimeError("GOOGLE_CLIENT_SECRET_FILE is required for Gmail integration")
        creds = get_google_credentials(GMAIL_SCOPES, self.client_secret_file, self.token_file)
        try:
            from googleapiclient.discovery import build
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("google-api-python-client is required") from exc
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def create_draft(self, to: list[str], subject: str, body: str) -> EmailDraftArtifact:
        service = self._service()
        message = EmailMessage()
        message["To"] = ", ".join(to)
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        draft = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        return EmailDraftArtifact(
            to=to,
            subject=subject,
            body=body,
            provider_draft_id=str(draft.get("id")),
        )
