from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from models.artifacts import CalendarProposal
from tools.google_auth import get_google_credentials


# Keep a single shared token scope-set across Calendar and Gmail clients
# so one integration does not overwrite token scopes required by the other.
CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


class MockCalendarClient:
    def find_free_slots(
        self,
        attendees: list[str],
        duration_minutes: int = 30,
        max_slots: int = 3,
    ) -> list[str]:
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        return [(now + timedelta(hours=idx + 2)).isoformat() for idx in range(max_slots)]

    def propose_slots(
        self,
        slots: list[str],
        duration_minutes: int = 30,
        timezone_name: str = "UTC",
    ) -> CalendarProposal:
        return CalendarProposal(
            slots=slots,
            duration_minutes=duration_minutes,
            timezone=timezone_name,
            ics=_build_ics(slots, duration_minutes),
        )


class GoogleCalendarClient:
    def __init__(
        self,
        calendar_id: str | None = None,
        timezone_name: str = "UTC",
        client_secret_file: str | None = None,
        token_file: str | None = None,
    ) -> None:
        self.calendar_id = calendar_id or os.getenv("GOOGLE_CALENDAR_ID", "primary")
        self.timezone_name = timezone_name
        self.client_secret_file = client_secret_file or os.getenv("GOOGLE_CLIENT_SECRET_FILE", "")
        self.token_file = token_file or os.getenv("GOOGLE_TOKEN_FILE", "storage/google_token.json")

    def _service(self):
        if not self.client_secret_file:
            raise RuntimeError("GOOGLE_CLIENT_SECRET_FILE is required for real calendar integration")
        creds = get_google_credentials(CALENDAR_SCOPES, self.client_secret_file, self.token_file)
        try:
            from googleapiclient.discovery import build
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("google-api-python-client is required") from exc
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def find_free_slots(
        self,
        attendees: list[str],
        duration_minutes: int = 30,
        max_slots: int = 3,
    ) -> list[str]:
        service = self._service()
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=5)
        items = [{"id": self.calendar_id}] + [{"id": email} for email in attendees if "@" in email]
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": self.timezone_name,
            "items": items,
        }
        result = service.freebusy().query(body=body).execute()
        busy_ranges: list[tuple[datetime, datetime]] = []
        calendars = result.get("calendars", {})
        for calendar_data in calendars.values():
            for busy in calendar_data.get("busy", []):
                busy_start = _parse_dt(busy.get("start"))
                busy_end = _parse_dt(busy.get("end"))
                if busy_start and busy_end:
                    busy_ranges.append((busy_start, busy_end))

        candidates: list[str] = []
        cursor = start.replace(minute=0, second=0, microsecond=0)
        limit = end
        while cursor < limit and len(candidates) < max_slots:
            if 9 <= cursor.hour <= 17:
                slot_end = cursor + timedelta(minutes=duration_minutes)
                if not _overlaps_busy(cursor, slot_end, busy_ranges):
                    candidates.append(cursor.isoformat())
            cursor += timedelta(hours=1)

        if not candidates:
            fallback = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            candidates = [(fallback + timedelta(hours=idx + 1)).isoformat() for idx in range(max_slots)]
        return candidates

    def propose_slots(
        self,
        slots: list[str],
        duration_minutes: int = 30,
        timezone_name: str = "UTC",
    ) -> CalendarProposal:
        return CalendarProposal(
            slots=slots,
            duration_minutes=duration_minutes,
            timezone=timezone_name,
            ics=_build_ics(slots, duration_minutes),
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _overlaps_busy(start: datetime, end: datetime, busy_ranges: list[tuple[datetime, datetime]]) -> bool:
    for busy_start, busy_end in busy_ranges:
        if start < busy_end and end > busy_start:
            return True
    return False


def _build_ics(slots: list[str], duration_minutes: int) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TicketOps//EN",
    ]
    for idx, slot in enumerate(slots):
        start = _parse_dt(slot)
        if not start:
            continue
        end = start + timedelta(minutes=duration_minutes)
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:agentic-slot-{idx}@example.local",
                f"DTSTAMP:{_ics_dt(datetime.now(timezone.utc))}",
                f"DTSTART:{_ics_dt(start)}",
                f"DTEND:{_ics_dt(end)}",
                "SUMMARY:Agentic Incident Review",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return "\n".join(lines)


def _ics_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
