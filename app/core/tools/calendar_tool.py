"""
Calendar tool.

Design-doc non-negotiable rule: "The AI must never fabricate ... availability
... — All facts come from ... live tool calls". This module is the single
source of truth for availability; the LLM only ever sees what this module
returns and cannot invent slots.

Modes:
- Mock mode (default, no credentials needed): generates real slot candidates
  (Mon-Sat, 9am-5pm, hourly) for the next N business days and checks them
  against the `appointments` table in PostgreSQL so double-booking is
  impossible even in mock mode.
- Google Calendar mode (session 7): active whenever
  GOOGLE_CALENDAR_CREDENTIALS_JSON is set. Uses a service account (the
  documented pattern for a backend service that owns one shared clinic
  calendar, as opposed to per-user OAuth) via google-api-python-client:
    - check_availability() calls the Calendar API's freebusy.query for the
      configured calendar (GOOGLE_CALENDAR_ID, default "primary") over the
      requested window, subtracts busy periods from the same Mon-Sat
      9am-5pm hourly grid the mock mode uses, and also subtracts anything
      already in our own `appointments` table (covers the gap between "we
      wrote to Postgres" and "Google's freebusy index catches up").
    - book() creates a real Calendar event via events().insert() and stores
      its Google-assigned event id in appointments.calendar_event_id (same
      column mock mode uses for its `mock-evt-...` placeholder — one schema,
      either mode).
  google-api-python-client + google-auth are only imported lazily, inside
  _load_google_service(), so this module still imports fine in mock mode
  without those packages installed (see requirements.txt).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from ..database import Database

BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR = 17  # last bookable slot starts at 16:00
WORKING_DAYS = {0, 1, 2, 3, 4, 5}  # Mon-Sat (0=Mon ... 5=Sat), Sunday closed
SLOT_DURATION_MINUTES = 60

GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
GOOGLE_CALENDAR_TIMEZONE = os.environ.get("GOOGLE_CALENDAR_TIMEZONE", "UTC")


class SlotUnavailableError(Exception):
    pass


class CalendarConfigError(Exception):
    """Google mode is selected but the client library / credentials aren't
    usable — distinct from SlotUnavailableError (a user-facing "pick another
    time") because this is a deployment/config problem the orchestrator
    should escalate to a human rather than ask the user to retry."""


def _slot_grid(days_ahead: int, from_date: datetime | None) -> list[datetime]:
    """The single source of truth for what an hourly slot grid looks like
    (Mon-Sat, business hours). Shared by mock and Google modes so 'what
    counts as a bookable slot' can never drift between them."""
    start = (from_date or datetime.now()).replace(minute=0, second=0, microsecond=0)
    slots: list[datetime] = []
    day_cursor = start
    days_checked = 0
    while days_checked < days_ahead:
        if day_cursor.weekday() in WORKING_DAYS:
            for hour in range(BUSINESS_START_HOUR, BUSINESS_END_HOUR):
                slot = day_cursor.replace(hour=hour, minute=0, second=0, microsecond=0)
                if slot > start:
                    slots.append(slot)
            days_checked += 1
        day_cursor = day_cursor + timedelta(days=1)
    return slots


class CalendarTool:
    def __init__(self, db: Database):
        self.db = db
        self.mode = "google" if os.environ.get("GOOGLE_CALENDAR_CREDENTIALS_JSON") else "mock"
        self._google_service = None  # lazy-loaded googleapiclient Resource

    def check_availability(self, days_ahead: int = 7, from_date: datetime | None = None) -> list[str]:
        """Returns a list of ISO datetime strings for open hourly slots."""
        if self.mode == "google":
            return self._google_check_availability(days_ahead, from_date)
        return self._mock_check_availability(days_ahead, from_date)

    def book(self, lead_id: str, slot_datetime: str) -> dict:
        """
        Books a slot. Raises SlotUnavailableError if the slot is not actually
        open (already booked, outside business hours, etc.) — the caller
        (orchestrator) must never present this as a success to the user.

        Validation (parse, business hours, our own double-booking check) is
        shared across both modes: our `appointments` table is always the
        final tie-breaker before writing anywhere, mock or Google, since two
        concurrent requests could otherwise both look free to Google's API
        at the same instant.
        """
        slot_dt = self._validate_slot(slot_datetime)

        if self.mode == "google":
            calendar_event_id = self._google_book_event(slot_dt)
        else:
            calendar_event_id = f"mock-evt-{slot_datetime}"

        appointment_id = self.db.create_appointment(lead_id, slot_datetime, calendar_event_id)
        return {
            "appointment_id": appointment_id,
            "calendar_event_id": calendar_event_id,
            "slot_datetime": slot_datetime,
            "status": "confirmed",
        }

    def _validate_slot(self, slot_datetime: str) -> datetime:
        try:
            slot_dt = datetime.strptime(slot_datetime, "%Y-%m-%dT%H:%M:%S")
        except ValueError as e:
            raise SlotUnavailableError(f"Malformed slot datetime: {slot_datetime}") from e

        if slot_dt.weekday() not in WORKING_DAYS:
            raise SlotUnavailableError("Clinic is closed on Sundays.")
        if not (BUSINESS_START_HOUR <= slot_dt.hour < BUSINESS_END_HOUR):
            raise SlotUnavailableError("Requested time is outside business hours (9 AM - 5 PM).")
        if self.db.is_slot_booked(slot_datetime):
            raise SlotUnavailableError("That slot was just booked by someone else. Please pick another.")
        return slot_dt

    # ---- Mock implementation ------------------------------------------

    def _mock_check_availability(self, days_ahead: int, from_date: datetime | None) -> list[str]:
        open_slots = []
        for slot in _slot_grid(days_ahead, from_date):
            slot_str = slot.strftime("%Y-%m-%dT%H:%M:%S")
            if not self.db.is_slot_booked(slot_str):
                open_slots.append(slot_str)
        return open_slots

    # ---- Google Calendar implementation (session 7) ---------------------

    def _load_google_service(self):
        """Lazily builds an authenticated Calendar API client from a service
        account. Cached on self after the first call. Raises
        CalendarConfigError (never a raw ImportError/google exception) so
        the orchestrator can catch one type and escalate to a human rather
        than surface a stack trace to the end user.

        GOOGLE_CALENDAR_CREDENTIALS_JSON holds the *contents* of a service
        account key file (not a path) — the standard way to inject a
        credential into a container/PaaS env var without mounting a file.
        """
        if self._google_service is not None:
            return self._google_service
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as e:
            raise CalendarConfigError(
                "GOOGLE_CALENDAR_CREDENTIALS_JSON is set but google-api-python-client / "
                "google-auth aren't installed — see requirements.txt."
            ) from e

        raw = os.environ["GOOGLE_CALENDAR_CREDENTIALS_JSON"]
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CalendarConfigError(
                "GOOGLE_CALENDAR_CREDENTIALS_JSON is not valid JSON — it must contain the "
                "full contents of a Google service-account key file, not a file path."
            ) from e

        try:
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=GOOGLE_CALENDAR_SCOPES
            )
            self._google_service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        except Exception as e:  # noqa: BLE001 - normalize any google-auth/API error uniformly
            raise CalendarConfigError(f"Failed to authenticate with Google Calendar: {e}") from e
        return self._google_service

    def _google_busy_periods(self, window_start: datetime, window_end: datetime) -> list[tuple[datetime, datetime]]:
        service = self._load_google_service()
        body = {
            "timeMin": window_start.isoformat() + "Z" if window_start.tzinfo is None else window_start.isoformat(),
            "timeMax": window_end.isoformat() + "Z" if window_end.tzinfo is None else window_end.isoformat(),
            "timeZone": GOOGLE_CALENDAR_TIMEZONE,
            "items": [{"id": GOOGLE_CALENDAR_ID}],
        }
        try:
            resp = service.freebusy().query(body=body).execute()
        except Exception as e:  # noqa: BLE001 - surfaces any googleapiclient.errors.HttpError uniformly
            raise CalendarConfigError(f"Google Calendar freebusy query failed: {e}") from e

        busy_raw = resp.get("calendars", {}).get(GOOGLE_CALENDAR_ID, {}).get("busy", [])
        periods = []
        for b in busy_raw:
            busy_start = datetime.fromisoformat(b["start"].replace("Z", "+00:00")).replace(tzinfo=None)
            busy_end = datetime.fromisoformat(b["end"].replace("Z", "+00:00")).replace(tzinfo=None)
            periods.append((busy_start, busy_end))
        return periods

    def _google_check_availability(self, days_ahead: int, from_date: datetime | None) -> list[str]:
        candidates = _slot_grid(days_ahead, from_date)
        if not candidates:
            return []
        window_start = candidates[0]
        window_end = candidates[-1] + timedelta(minutes=SLOT_DURATION_MINUTES)
        busy_periods = self._google_busy_periods(window_start, window_end)

        open_slots = []
        for slot in candidates:
            slot_end = slot + timedelta(minutes=SLOT_DURATION_MINUTES)
            overlaps_busy = any(slot < b_end and slot_end > b_start for b_start, b_end in busy_periods)
            if overlaps_busy:
                continue
            slot_str = slot.strftime("%Y-%m-%dT%H:%M:%S")
            # Belt-and-suspenders: also exclude anything our own DB thinks is
            # booked, in case Google's freebusy index hasn't caught up yet
            # with a booking we just wrote.
            if self.db.is_slot_booked(slot_str):
                continue
            open_slots.append(slot_str)
        return open_slots

    def _google_book_event(self, slot_dt: datetime) -> str:
        service = self._load_google_service()
        slot_end = slot_dt + timedelta(minutes=SLOT_DURATION_MINUTES)
        event = {
            "summary": "Clinic appointment",
            "start": {"dateTime": slot_dt.isoformat(), "timeZone": GOOGLE_CALENDAR_TIMEZONE},
            "end": {"dateTime": slot_end.isoformat(), "timeZone": GOOGLE_CALENDAR_TIMEZONE},
        }
        try:
            created = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        except Exception as e:  # noqa: BLE001 - surfaces any googleapiclient.errors.HttpError uniformly
            raise CalendarConfigError(f"Failed to create Google Calendar event: {e}") from e
        return created["id"]
