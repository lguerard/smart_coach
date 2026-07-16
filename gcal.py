#!/usr/bin/env python3
"""Google Calendar sync for tonight's adapted session.

Near-verbatim port of garmin-coach/gcal.py -- same OAuth/Calendar API
plumbing, isolated from training.py's pure logic and llm.py/notify.py.
Config lives under ~/.config/smart_sport/ instead of
~/.config/garmin-coach/ so the two projects can run in parallel during
the migration period without clobbering each other's tokens.
"""

import datetime as dt
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
# One shared OAuth Desktop app client for the whole deployment (the
# admin registers it once in Google Cloud Console); the token is
# per-user since each person consents to their own calendar access.
CLIENT_SECRET_FILE = (
    Path.home() / ".config/smart_sport/calendar_client_secret.json"
)
CONFIG_DIR = Path.home() / ".config/smart_sport"

WINDOW_BEFORE_MIN = 30
WINDOW_AFTER_MIN = 90


def get_calendar_service(username: str):
    """Return an authenticated Calendar API service for one user.

    Uses cached OAuth tokens (refreshed silently if expired, one file
    per username so family members don't share calendar access);
    falls back to interactive browser consent on first run.

    Parameters:
        username (str): The smart_sport account requesting access --
            keys the cached token file.

    Returns:
        googleapiclient.discovery.Resource: Authenticated service.

    Raises:
        RuntimeError: No usable token and no terminal to consent in.
    """
    token_file = CONFIG_DIR / f"calendar_token_{username}.json"
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(
            str(token_file), SCOPES
        )
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not sys.stdin.isatty():
            raise RuntimeError(
                f"Calendar token missing/expired for {username!r}. Run "
                "`python run_coach.py` once in a terminal to grant "
                "Calendar access."
            )
        if not CLIENT_SECRET_FILE.exists():
            raise RuntimeError(
                f"Missing {CLIENT_SECRET_FILE}. Create an OAuth "
                "Desktop app client in Google Cloud Console and "
                "download it there (see README)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CLIENT_SECRET_FILE), SCOPES
        )
        creds = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def resolve_calendar_id(service, name: str) -> str:
    """Find a calendar's id by its display name.

    Parameters:
        service: Authenticated Calendar API service.
        name (str): Calendar display name (``summary``) to match.

    Returns:
        str: Calendar id.

    Raises:
        RuntimeError: No calendar with that name is on the account.
    """
    page_token = None
    while True:
        result = service.calendarList().list(
            pageToken=page_token
        ).execute()
        for entry in result.get("items", []):
            if entry.get("summary") == name:
                return entry["id"]
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    raise RuntimeError(f"No calendar named {name!r} on this account.")


def _search_window(day: dt.date, template: dict) -> tuple[str, str]:
    """Compute the [timeMin, timeMax) RFC3339 search window.

    Parameters:
        day (date): Day to search.
        template (dict): Weekday template with a ``"start"`` HH:MM.

    Returns:
        tuple[str, str]: (timeMin, timeMax) in local time, RFC3339.
    """
    hour, minute = (int(part) for part in template["start"].split(":"))
    start = dt.datetime.combine(
        day, dt.time(hour, minute)
    ).astimezone()
    window_start = start - dt.timedelta(minutes=WINDOW_BEFORE_MIN)
    window_end = start + dt.timedelta(minutes=WINDOW_AFTER_MIN)
    return window_start.isoformat(), window_end.isoformat()


def find_matching_event(
    service, calendar_id: str, day: dt.date, template: dict,
):
    """Find tonight's session event by time window, not title.

    Parameters:
        service: Authenticated Calendar API service.
        calendar_id (str): Target calendar id.
        day (date): Day to search.
        template (dict): Weekday template.

    Returns:
        dict | None: The first matching event, or ``None``.
    """
    time_min, time_max = _search_window(day, template)
    result = service.events().list(
        calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
        singleEvents=True, orderBy="startTime",
    ).execute()
    items = result.get("items", [])
    return items[0] if items else None


def upsert_session_event(
    service, calendar_id: str, day: dt.date, template: dict,
    description: str,
) -> str:
    """Update tonight's event description, or create it if missing.

    Parameters:
        service: Authenticated Calendar API service.
        calendar_id (str): Target calendar id.
        day (date): Day of the session.
        template (dict): The user's weekday template
            (``training.schedule_for_user``): title/start/duration.
        description (str): New event description (adapted workout).

    Returns:
        str: The event id that was updated or created.
    """
    existing = find_matching_event(service, calendar_id, day, template)
    if existing:
        service.events().patch(
            calendarId=calendar_id, eventId=existing["id"],
            body={"description": description},
        ).execute()
        return existing["id"]

    hour, minute = (int(part) for part in template["start"].split(":"))
    start = dt.datetime.combine(
        day, dt.time(hour, minute)
    ).astimezone()
    end = start + dt.timedelta(minutes=template["duration_min"])
    created = service.events().insert(
        calendarId=calendar_id,
        body={
            "summary": template["title"], "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        },
    ).execute()
    return created["id"]


if __name__ == "__main__":
    window = _search_window(
        dt.date(2026, 7, 13), {"start": "20:00", "duration_min": 30},
    )
    start = dt.datetime.fromisoformat(window[0])
    end = dt.datetime.fromisoformat(window[1])
    assert (end - start).total_seconds() / 60 == (
        WINDOW_BEFORE_MIN + WINDOW_AFTER_MIN
    )
    print("gcal.py: all checks passed (no live Calendar call made)")
