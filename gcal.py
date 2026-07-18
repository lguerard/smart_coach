#!/usr/bin/env python3
"""Google Calendar sync for tonight's adapted session.

Near-verbatim port of garmin-coach/gcal.py -- same OAuth/Calendar API
plumbing, isolated from training.py's pure logic and llm.py/notify.py.
Config lives under ~/.config/smart_coach/ instead of
~/.config/garmin-coach/ so the two projects can run in parallel during
the migration period without clobbering each other's tokens.
"""

import datetime as dt
import sys
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
# One shared OAuth Desktop app client for the whole deployment (the
# admin registers it once in Google Cloud Console); the token is
# per-user since each person consents to their own calendar access.
CLIENT_SECRET_FILE = (
    Path.home() / ".config/smart_coach/calendar_client_secret.json"
)
CONFIG_DIR = Path.home() / ".config/smart_coach"

WINDOW_BEFORE_MIN = 30
WINDOW_AFTER_MIN = 90


def get_calendar_service(username: str):
    """Return an authenticated Calendar API service for one user.

    Uses cached OAuth tokens (refreshed silently if expired, one file
    per username so family members don't share calendar access);
    falls back to interactive browser consent on first run.

    Parameters:
        username (str): The smart_coach account requesting access --
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


def get_or_create_calendar_id(service, name: str) -> str:
    """Find a calendar by display name, creating it if missing.

    Lets tonight's session live in its own calendar (e.g. "Sport")
    instead of the account's primary one, with no manual setup in
    the Google Calendar web UI -- the calendar is provisioned the
    first time this account pushes a session.

    Parameters:
        service: Authenticated Calendar API service.
        name (str): Calendar display name to find or create.

    Returns:
        str: Calendar id (existing, or newly created).
    """
    try:
        return resolve_calendar_id(service, name)
    except RuntimeError:
        created = service.calendars().insert(
            body={"summary": name}
        ).execute()
        return created["id"]


# How far into the evening find_available_start will look for a free
# slot before giving up and keeping the original (conflicting) time.
RESCHEDULE_DAY_END_HOUR = 22
RESCHEDULE_STEP_MIN = 15


def _busy_intervals(
    service, calendar_id: str, day: dt.date,
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Busy blocks on ``calendar_id`` for one day, via freebusy.

    Parameters:
        service: Authenticated Calendar API service.
        calendar_id (str): Calendar to check (e.g. "primary").
        day (date): Day to check.

    Returns:
        list[tuple[datetime, datetime]]: Busy (start, end) pairs.
    """
    day_start = dt.datetime.combine(day, dt.time(0, 0)).astimezone()
    day_end = dt.datetime.combine(
        day, dt.time(RESCHEDULE_DAY_END_HOUR, 0)
    ).astimezone()
    result = service.freebusy().query(body={
        "timeMin": day_start.isoformat(), "timeMax": day_end.isoformat(),
        "items": [{"id": calendar_id}],
    }).execute()
    busy = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    return [
        (
            dt.datetime.fromisoformat(b["start"]),
            dt.datetime.fromisoformat(b["end"]),
        )
        for b in busy
    ]


def find_available_start(
    service, calendar_id: str, day: dt.date, template: dict,
    duration_min: int,
) -> tuple[str, bool]:
    """Tonight's session start time, moved later if it conflicts.

    Real life comes first: if the planned slot overlaps something
    already on ``calendar_id`` (a real meeting, travel, ...), look for
    the next free slot of ``duration_min`` before
    ``RESCHEDULE_DAY_END_HOUR``. If none exists, keep the original
    time rather than pushing the session somewhere unreasonable (e.g.
    the middle of the night) -- a packed day beats a silently-wrong
    schedule.

    Parameters:
        service: Authenticated Calendar API service.
        calendar_id (str): Calendar checked for conflicts (the user's
            ``busy_calendar_name`` setting, or "primary").
        day (date): Day of the session.
        template (dict): Weekday template with a ``"start"`` HH:MM.
        duration_min (int): Today's computed session length.

    Returns:
        tuple[str, bool]: ``(start HH:MM, moved)`` -- ``moved`` is
        true only when a different, free slot was found.
    """
    hour, minute = (int(part) for part in template["start"].split(":"))
    planned_start = dt.datetime.combine(day, dt.time(hour, minute)).astimezone()
    planned_end = planned_start + dt.timedelta(minutes=duration_min)
    busy = _busy_intervals(service, calendar_id, day)

    def overlaps(start: dt.datetime, end: dt.datetime) -> bool:
        return any(start < b_end and end > b_start for b_start, b_end in busy)

    if not overlaps(planned_start, planned_end):
        return template["start"], False

    day_end_limit = dt.datetime.combine(
        day, dt.time(RESCHEDULE_DAY_END_HOUR, 0)
    ).astimezone()
    candidate = planned_start
    step = dt.timedelta(minutes=RESCHEDULE_STEP_MIN)
    while candidate + dt.timedelta(minutes=duration_min) <= day_end_limit:
        candidate_end = candidate + dt.timedelta(minutes=duration_min)
        if not overlaps(candidate, candidate_end):
            return candidate.strftime("%H:%M"), True
        candidate += step
    return template["start"], False  # no free slot -- keep the original


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
    description: str, duration_min: Optional[int] = None,
) -> str:
    """Update tonight's event description, or create it if missing.

    Parameters:
        service: Authenticated Calendar API service.
        calendar_id (str): Target calendar id.
        day (date): Day of the session.
        template (dict): The user's weekday template
            (``training.schedule_for_user``): title/start/duration.
        description (str): New event description (adapted workout).
        duration_min (int | None): Today's computed session length;
            when set, the event's end time is moved to start +
            duration so a longer prescription shows as a longer
            calendar block. ``None`` keeps the event's own times.

    Returns:
        str: The event id that was updated or created.
    """
    existing = find_matching_event(service, calendar_id, day, template)
    if existing:
        body: dict = {"description": description}
        event_start = existing.get("start", {}).get("dateTime")
        if duration_min and event_start:
            end = dt.datetime.fromisoformat(event_start) + dt.timedelta(
                minutes=duration_min,
            )
            body["end"] = {"dateTime": end.isoformat()}
        service.events().patch(
            calendarId=calendar_id, eventId=existing["id"], body=body,
        ).execute()
        return existing["id"]

    hour, minute = (int(part) for part in template["start"].split(":"))
    start = dt.datetime.combine(
        day, dt.time(hour, minute)
    ).astimezone()
    end = start + dt.timedelta(
        minutes=duration_min or template["duration_min"],
    )
    created = service.events().insert(
        calendarId=calendar_id,
        body={
            "summary": template["title"], "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        },
    ).execute()
    return created["id"]


def push_description(
    username: str, calendar_name: str, day: dt.date, template: dict,
    description: str, duration_min: Optional[int] = None,
) -> str:
    """Authenticate, resolve (or create) the calendar, and upsert the
    day's event.

    Shared by the daily cron and the dashboard's level editor. No
    error handling here on purpose: callers wrap failures into their
    own user-facing note.

    Parameters:
        username (str): Account whose token file to use.
        calendar_name (str): Calendar display name (user setting) --
            created automatically if it doesn't exist yet, so a
            dedicated calendar (e.g. "Sport") needs no manual setup.
        day (date): Day of the session.
        template (dict): The user's weekday template.
        description (str): New event description.

    Returns:
        str: The event id that was updated or created.
    """
    service = get_calendar_service(username)
    calendar_id = get_or_create_calendar_id(service, calendar_name)
    return upsert_session_event(
        service, calendar_id, day, template, description, duration_min,
    )


if __name__ == "__main__":
    window = _search_window(
        dt.date(2026, 7, 13), {"start": "20:00", "duration_min": 30},
    )
    start = dt.datetime.fromisoformat(window[0])
    end = dt.datetime.fromisoformat(window[1])
    assert (end - start).total_seconds() / 60 == (
        WINDOW_BEFORE_MIN + WINDOW_AFTER_MIN
    )

    # find_available_start: no conflict -> keeps the planned time.
    day = dt.date(2026, 7, 13)
    template = {"start": "20:00", "duration_min": 30}

    class _FakeFreebusy:
        def __init__(self, busy):
            self._busy = busy

        def query(self, body):
            return self

        def execute(self):
            return {"calendars": {"cal-1": {"busy": self._busy}}}

    class _FakeService:
        def __init__(self, busy):
            self._freebusy = _FakeFreebusy(busy)

        def freebusy(self):
            return self._freebusy

    def _iso(hour: int, minute: int = 0) -> str:
        return dt.datetime.combine(
            day, dt.time(hour, minute)
        ).astimezone().isoformat()

    free_service = _FakeService([])
    assert find_available_start(
        free_service, "cal-1", day, template, 30,
    ) == ("20:00", False)

    # A meeting exactly over the planned slot -> pushed to the next
    # free 30-min slot after it ends.
    busy_service = _FakeService(
        [{"start": _iso(20, 0), "end": _iso(20, 45)}]
    )
    new_start, moved = find_available_start(
        busy_service, "cal-1", day, template, 30,
    )
    assert moved is True
    assert new_start == "20:45", new_start

    # Booked solid until the day-end limit -> no slot, original kept.
    packed_service = _FakeService(
        [{"start": _iso(0, 0), "end": _iso(RESCHEDULE_DAY_END_HOUR, 0)}]
    )
    kept_start, kept_moved = find_available_start(
        packed_service, "cal-1", day, template, 30,
    )
    assert kept_moved is False
    assert kept_start == "20:00"

    # get_or_create_calendar_id: found by name -> no creation call;
    # not found -> auto-created, id returned.
    class _FakeCalendarList:
        def __init__(self, entries):
            self._entries = entries

        def list(self, pageToken=None):
            return self

        def execute(self):
            return {"items": self._entries}

    class _FakeCalendars:
        def __init__(self):
            self.created = []

        def insert(self, body):
            self._pending = body
            return self

        def execute(self):
            self.created.append(self._pending["summary"])
            return {"id": f"created-{self._pending['summary']}"}

    class _FakeCalendarService:
        def __init__(self, entries):
            self._calendar_list = _FakeCalendarList(entries)
            self.calendars_api = _FakeCalendars()

        def calendarList(self):
            return self._calendar_list

        def calendars(self):
            return self.calendars_api

    found_service = _FakeCalendarService(
        [{"summary": "Sport", "id": "existing-sport-id"}]
    )
    assert get_or_create_calendar_id(found_service, "Sport") == (
        "existing-sport-id"
    )
    assert found_service.calendars_api.created == []  # never created

    missing_service = _FakeCalendarService([])
    assert get_or_create_calendar_id(missing_service, "Sport") == (
        "created-Sport"
    )
    assert missing_service.calendars_api.created == ["Sport"]

    print("gcal.py: all checks passed (no live Calendar call made)")
