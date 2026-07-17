"""Garmin Connect API ingestion: exercise sessions and sleep.

These two domains come straight from Garmin's API (same unofficial
``garminconnect`` client garmin-coach used) instead of the Health
Connect export -- Garmin's HC writer mislabels activity types and
never syncs per-session HR series, while its own API has both. All
other record types (steps, weight, nutrition, ...) still flow in
from the Health Connect export via parse_health_connect.

Rows land in the same tables the HC parser used to fill
(``exercise_sessions``, ``sleep_sessions``, ``sleep_stages``, plus
``exercise_hr_samples``), keyed by ``garmin-*`` uuids, so every
consumer (metrics, training, dashboard) is untouched. A Garmin row
overlapping an existing HC-era row is skipped rather than
duplicated, so switching sources mid-history is safe.

First run per user is interactive (Garmin email/password + MFA
prompt); tokens persist ~1 year under GARMIN_TOKEN_DIR/<username>.
"""

import datetime as dt
import getpass
import os
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from garminconnect import Garmin

TOKEN_ROOT = Path(os.environ.get("GARMIN_TOKEN_DIR", "data/garmin-tokens"))
LOOKBACK_DAYS = int(os.environ.get("GARMIN_LOOKBACK_DAYS", "14"))

# Garmin activityType.typeKey -> Health Connect exercise_type int
# (see EXERCISE_TYPE_LABELS in parse_health_connect). Downstream
# consumers keep reading the HC ints they always did; unmapped keys
# fall back to 0 ("other_workout") with the Garmin activity name
# preserved in title.
GARMIN_TYPE_TO_HC = {
    "treadmill_running": 57, "running": 56, "trail_running": 56,
    "street_running": 56, "track_running": 56, "walking": 79,
    "casual_walking": 79, "speed_walking": 79, "hiking": 37,
    "strength_training": 70, "indoor_cardio": 36, "hiit": 36,
    "cycling": 8, "road_biking": 8, "mountain_biking": 8, "gravel_cycling": 8,
    "indoor_cycling": 9, "virtual_ride": 9,
    "lap_swimming": 74, "open_water_swimming": 73,
    "yoga": 83, "pilates": 48, "elliptical": 25,
    "indoor_rowing": 54, "rowing": 53, "stair_climbing": 68,
    "breathwork": 33,
}

# Garmin sleepLevels activityLevel -> HC stage_type int
# (0 deep, 1 light, 2 REM, 3 awake -> see SLEEP_STAGE_LABELS).
SLEEP_LEVEL_TO_HC_STAGE = {0: 5, 1: 4, 2: 6, 3: 1}


def get_client(username: str) -> Garmin:
    """Return a logged-in Garmin client for one account.

    Uses cached OAuth tokens; falls back to interactive login on
    first run (or after token expiry, ~1 year).

    Parameters:
        username (str): smart_sport account name -- keys the token
            directory, one Garmin login per account.

    Returns:
        Garmin: Authenticated Garmin Connect client.
    """
    token_dir = str(TOKEN_ROOT / username)
    try:
        client = Garmin()
        client.login(token_dir)
        return client
    except Exception:
        if not sys.stdin.isatty():
            raise RuntimeError(
                f"Garmin tokens missing/expired for '{username}'. Run "
                "once in a terminal: python -c \"from ingest import "
                f"garmin_api; garmin_api.get_client('{username}')\""
            )
    # ponytail: login(tokenstore) both authenticates (MFA prompt on
    # stdin) and persists tokens to token_dir (garminconnect >= 0.3)
    email = input(f"Garmin email for {username}: ")
    password = getpass.getpass("Garmin password: ")
    client = Garmin(email=email, password=password)
    client.login(token_dir)
    return client


def _parse_gmt(value: str) -> dt.datetime:
    """Parse Garmin's GMT timestamp strings to aware UTC datetimes.

    Handles both ``2026-07-16 18:03:11`` (activities) and
    ``2026-07-16T22:00:00.0`` (sleepLevels).

    Parameters:
        value (str): Garmin GMT timestamp string.

    Returns:
        datetime.datetime: Timezone-aware UTC datetime.
    """
    return dt.datetime.fromisoformat(
        value.replace(" ", "T").split(".")[0]
    ).replace(tzinfo=dt.timezone.utc)


def _epoch_ms_utc(ms: int) -> dt.datetime:
    """Aware UTC datetime from epoch milliseconds.

    Parameters:
        ms (int): Epoch milliseconds.

    Returns:
        datetime.datetime: Timezone-aware UTC datetime.
    """
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


def _overlaps_other(
    conn: sqlite3.Connection, user_id: int, table: str,
    start_utc: str, end_utc: str, uuid: str,
) -> bool:
    """True if another session (different uuid) overlaps this window.

    Guards the HC-to-Garmin transition: history ingested from Health
    Connect keeps its HC uuids (and any label_override), so the same
    physical session fetched from Garmin must be skipped, not
    inserted alongside it.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        table (str): ``exercise_sessions`` or ``sleep_sessions``.
        start_utc (str): ISO UTC window start.
        end_utc (str): ISO UTC window end.
        uuid (str): The candidate row's own uuid (excluded).

    Returns:
        bool: Whether a different overlapping row already exists.
    """
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE user_id = ? AND uuid != ? "
        f"AND start_utc < ? AND end_utc > ? LIMIT 1",
        (user_id, uuid, end_utc, start_utc),
    ).fetchone() is not None


def _hc_exercise_type(type_key: str | None, title: str | None) -> int:
    """Map a Garmin activity to a Health Connect exercise_type int.

    Custom watch profiles (renamed copies of built-in activities)
    keep the parent typeKey, so the profile name is the only way to
    tell them apart (same heuristic garmin-coach used).

    Parameters:
        type_key (str | None): Garmin ``activityType.typeKey``.
        title (str | None): Garmin ``activityName``.

    Returns:
        int: HC exercise_type (0 = other_workout fallback).
    """
    name = (title or "").lower()
    if "calisth" in name:
        return 13
    if "tapis" in name:
        return 57
    return GARMIN_TYPE_TO_HC.get(type_key or "", 0)


def _ingest_hr_samples(
    conn: sqlite3.Connection, user_id: int, client: Garmin,
    activity_id: int, uuid: str,
) -> int:
    """Fetch one activity's HR time series into exercise_hr_samples.

    Skipped (0) if samples already exist for this uuid -- one detail
    call per activity, once ever.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        activity_id (int): Garmin activityId.
        uuid (str): Matching exercise_sessions uuid.

    Returns:
        int: Number of HR samples inserted.
    """
    if conn.execute(
        "SELECT 1 FROM exercise_hr_samples WHERE exercise_uuid = ? "
        "LIMIT 1", (uuid,),
    ).fetchone():
        return 0
    details = client.get_activity_details(activity_id)
    index = {
        d["key"]: d["metricsIndex"]
        for d in details.get("metricDescriptors") or []
    }
    hr_i = index.get("directHeartRate")
    ts_i = index.get("directTimestamp")
    if hr_i is None or ts_i is None:
        return 0
    samples = []
    for point in details.get("activityDetailMetrics") or []:
        values = point.get("metrics") or []
        if max(hr_i, ts_i) < len(values) and values[hr_i] and values[ts_i]:
            samples.append((
                uuid, user_id,
                _epoch_ms_utc(values[ts_i]).isoformat(),
                int(values[hr_i]),
            ))
    conn.executemany(
        "INSERT OR IGNORE INTO exercise_hr_samples VALUES (?, ?, ?, ?)",
        samples,
    )
    return len(samples)


def upsert_activities(
    conn: sqlite3.Connection, user_id: int, client: Garmin,
    tz: ZoneInfo, days: int,
) -> tuple[int, int]:
    """Fetch recent Garmin activities into exercise_sessions.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        tz (ZoneInfo): User timezone for local_date day boundaries.
        days (int): Trailing window length.

    Returns:
        tuple[int, int]: (sessions upserted, HR samples inserted).
    """
    today = dt.date.today()
    activities = client.get_activities_by_date(
        (today - dt.timedelta(days=days)).isoformat(), today.isoformat()
    )
    session_count = hr_count = 0
    for act in activities:
        start = _parse_gmt(act["startTimeGMT"])
        end = start + dt.timedelta(seconds=act.get("duration") or 0)
        uuid = f"garmin-{act['activityId']}"
        start_utc, end_utc = start.isoformat(), end.isoformat()
        if _overlaps_other(
            conn, user_id, "exercise_sessions", start_utc, end_utc, uuid,
        ):
            continue
        row = (
            uuid, start_utc, end_utc,
            start.astimezone(tz).date().isoformat(),
            _hc_exercise_type(
                (act.get("activityType") or {}).get("typeKey"),
                act.get("activityName"),
            ),
            act.get("activityName"), act.get("description"),
        )
        conn.execute(
            "INSERT INTO exercise_sessions (uuid, user_id, start_utc, "
            "end_utc, local_date, exercise_type, title, notes) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(uuid) DO UPDATE SET "
            "start_utc = excluded.start_utc, end_utc = excluded.end_utc, "
            "local_date = excluded.local_date, "
            "exercise_type = excluded.exercise_type, "
            "title = excluded.title, notes = excluded.notes",
            (row[0], user_id, *row[1:]),
        )
        session_count += 1
        hr_count += _ingest_hr_samples(
            conn, user_id, client, act["activityId"], uuid,
        )
    return session_count, hr_count


def upsert_sleep(
    conn: sqlite3.Connection, user_id: int, client: Garmin,
    tz: ZoneInfo, days: int,
) -> tuple[int, int]:
    """Fetch recent Garmin sleep into sleep_sessions/sleep_stages.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        tz (ZoneInfo): User timezone for local_date day boundaries.
        days (int): Trailing window length (one API call per day).

    Returns:
        tuple[int, int]: (sessions upserted, stage rows inserted).
    """
    today = dt.date.today()
    session_count = stage_count = 0
    for back in range(days + 1):
        data = client.get_sleep_data(
            (today - dt.timedelta(days=back)).isoformat()
        ) or {}
        dto = data.get("dailySleepDTO") or {}
        start_ms = dto.get("sleepStartTimestampGMT")
        end_ms = dto.get("sleepEndTimestampGMT")
        if not start_ms or not end_ms:
            continue
        start, end = _epoch_ms_utc(start_ms), _epoch_ms_utc(end_ms)
        uuid = f"garmin-sleep-{dto.get('id') or start_ms}"
        start_utc, end_utc = start.isoformat(), end.isoformat()
        if _overlaps_other(
            conn, user_id, "sleep_sessions", start_utc, end_utc, uuid,
        ):
            continue
        conn.execute(
            "INSERT INTO sleep_sessions (uuid, user_id, start_utc, "
            "end_utc, local_date, title, notes) VALUES "
            "(?, ?, ?, ?, ?, NULL, NULL) ON CONFLICT(uuid) DO UPDATE "
            "SET start_utc = excluded.start_utc, "
            "end_utc = excluded.end_utc, "
            "local_date = excluded.local_date",
            (uuid, user_id, start_utc, end_utc,
             start.astimezone(tz).date().isoformat()),
        )
        session_count += 1
        stages = []
        for level in data.get("sleepLevels") or []:
            stage = SLEEP_LEVEL_TO_HC_STAGE.get(
                int(level.get("activityLevel", -1))
            )
            if stage is None:
                continue
            stages.append((
                uuid, user_id,
                _parse_gmt(level["startGMT"]).isoformat(),
                _parse_gmt(level["endGMT"]).isoformat(), stage,
            ))
        conn.executemany(
            "INSERT OR IGNORE INTO sleep_stages VALUES (?, ?, ?, ?, ?)",
            stages,
        )
        stage_count += len(stages)
    return session_count, stage_count


def fetch_and_upsert(
    conn: sqlite3.Connection, user_id: int, username: str,
    days: int = LOOKBACK_DAYS,
) -> dict[str, int]:
    """Pull one user's Garmin activities + sleep into the db.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        username (str): Account name (keys the Garmin token dir).
        days (int): Trailing fetch window (GARMIN_LOOKBACK_DAYS env,
            default 14 -- raise it once for a first-run backfill).

    Returns:
        dict[str, int]: Row counts per table, also logged to
        ``ingest_runs`` for the dashboard's status page.
    """
    import db

    client = get_client(username)
    tz = ZoneInfo(
        db.get_setting(conn, user_id, "timezone") or "Europe/Paris"
    )
    sessions, hr_samples = upsert_activities(
        conn, user_id, client, tz, days,
    )
    sleep_sessions, sleep_stages = upsert_sleep(
        conn, user_id, client, tz, days,
    )
    counts = {
        "exercise_sessions": sessions,
        "exercise_hr_samples": hr_samples,
        "sleep_sessions": sleep_sessions,
        "sleep_stages": sleep_stages,
    }
    ran_at = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO ingest_runs (user_id, ran_at, table_name, row_count) "
        "VALUES (?, ?, ?, ?)",
        [(user_id, ran_at, f"garmin:{table}", count)
         for table, count in counts.items()],
    )
    conn.commit()
    return counts


if __name__ == "__main__":
    import tempfile

    sys.path.insert(0, str(Path(__file__).parent.parent))
    import db

    class _FakeGarmin:
        """Canned-payload stand-in -- no live Garmin call is made."""

        def get_activities_by_date(self, start: str, end: str) -> list:
            return [
                {
                    "activityId": 101,
                    "activityName": "Tapis du soir",
                    "activityType": {"typeKey": "indoor_cardio"},
                    "startTimeGMT": "2026-07-15 18:00:00",
                    "duration": 1800.0,
                    "description": "note",
                },
                {
                    "activityId": 102,
                    "activityName": "Muscu",
                    "activityType": {"typeKey": "strength_training"},
                    "startTimeGMT": "2026-07-14 18:00:00.0",
                    "duration": 2400.0,
                },
            ]

        def get_activity_details(self, activity_id: int) -> dict:
            return {
                "metricDescriptors": [
                    {"key": "directTimestamp", "metricsIndex": 0},
                    {"key": "directHeartRate", "metricsIndex": 1},
                ],
                "activityDetailMetrics": [
                    {"metrics": [1784311200000, 120.0]},
                    {"metrics": [1784311260000, 150.0]},
                    {"metrics": [1784311320000, None]},
                ],
            }

        def get_sleep_data(self, date: str) -> dict:
            if date != "2026-07-15":
                return {"dailySleepDTO": {}}
            return {
                "dailySleepDTO": {
                    "id": 555,
                    "sleepStartTimestampGMT": 1784239200000,
                    "sleepEndTimestampGMT": 1784268000000,
                },
                "sleepLevels": [
                    {"startGMT": "2026-07-14T22:00:00.0",
                     "endGMT": "2026-07-15T00:00:00.0",
                     "activityLevel": 1.0},
                    {"startGMT": "2026-07-15T00:00:00.0",
                     "endGMT": "2026-07-15T02:00:00.0",
                     "activityLevel": 0.0},
                    {"startGMT": "2026-07-15T02:00:00.0",
                     "endGMT": "2026-07-15T05:30:00.0",
                     "activityLevel": 2.0},
                    {"startGMT": "2026-07-15T05:30:00.0",
                     "endGMT": "2026-07-15T06:00:00.0",
                     "activityLevel": 3.0},
                ],
            }

    tmp = Path(tempfile.mkdtemp()) / "smart_sport.db"
    conn = db.connect(tmp)
    db.init_db(conn)
    uid = db.create_user(conn, "test", "password1234")
    tz = ZoneInfo("Europe/Paris")
    fake = _FakeGarmin()

    # Freeze the fetch window around the canned data's dates.
    real_date = dt.date

    sessions, hr = upsert_activities(conn, uid, fake, tz, 3650)
    assert sessions == 2, sessions
    assert hr == 4, hr  # 2 activities x 2 valid samples (None dropped)
    row = conn.execute(
        "SELECT * FROM exercise_sessions WHERE uuid = 'garmin-101'",
    ).fetchone()
    # "tapis" in the title overrides the generic indoor_cardio typeKey
    assert row["exercise_type"] == 57, row["exercise_type"]
    assert row["end_utc"] == "2026-07-15T18:30:00+00:00", row["end_utc"]
    assert row["local_date"] == "2026-07-15", row["local_date"]
    muscu = conn.execute(
        "SELECT exercise_type FROM exercise_sessions WHERE "
        "uuid = 'garmin-102'",
    ).fetchone()
    assert muscu["exercise_type"] == 70, muscu["exercise_type"]

    # Re-run: idempotent (upsert, HR details not re-fetched), and a
    # manual label_override must survive.
    conn.execute(
        "UPDATE exercise_sessions SET label_override = 'perso' "
        "WHERE uuid = 'garmin-101'",
    )
    sessions2, hr2 = upsert_activities(conn, uid, fake, tz, 3650)
    assert sessions2 == 2 and hr2 == 0, (sessions2, hr2)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM exercise_sessions",
    ).fetchone()["n"] == 2
    assert conn.execute(
        "SELECT label_override FROM exercise_sessions WHERE "
        "uuid = 'garmin-101'",
    ).fetchone()["label_override"] == "perso"

    # An HC-era row overlapping the same physical session blocks the
    # Garmin duplicate.
    conn.execute(
        "INSERT INTO exercise_sessions (uuid, user_id, start_utc, "
        "end_utc, local_date) VALUES ('hc-old', ?, "
        "'2026-07-13T18:00:00+00:00', '2026-07-13T19:00:00+00:00', "
        "'2026-07-13')", (uid,),
    )
    conn.execute("DELETE FROM exercise_sessions WHERE uuid = 'garmin-101'")
    conn.execute(
        "UPDATE exercise_sessions SET start_utc = "
        "'2026-07-13T18:10:00+00:00', end_utc = "
        "'2026-07-13T18:50:00+00:00' WHERE uuid = 'garmin-102'",
    )
    # garmin-102 now overlaps hc-old -> next run must NOT reinsert
    # garmin-101's window as a conflict, but must skip re-touching
    # anything overlapping hc-old... simplest observable: force the
    # fake's 102 window onto hc-old's and count rows stays stable.
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM exercise_sessions",
    ).fetchone()["n"]

    class _OverlapGarmin(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list:
            return [{
                "activityId": 999,
                "activityName": "Dup",
                "activityType": {"typeKey": "running"},
                "startTimeGMT": "2026-07-13 18:05:00",
                "duration": 3000.0,
            }]

    dup_sessions, _ = upsert_activities(conn, uid, _OverlapGarmin(), tz, 3650)
    assert dup_sessions == 0, dup_sessions
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM exercise_sessions",
    ).fetchone()["n"] == before

    # Sleep: one real night in the window, stages mapped to HC ints.
    sleep_sessions, sleep_stages = upsert_sleep(
        conn, uid, fake, tz, (dt.date.today() - real_date(2026, 7, 14)).days,
    )
    assert sleep_sessions == 1, sleep_sessions
    assert sleep_stages == 4, sleep_stages
    srow = conn.execute(
        "SELECT * FROM sleep_sessions WHERE uuid = 'garmin-sleep-555'",
    ).fetchone()
    assert srow is not None
    stage_types = sorted(
        r["stage_type"] for r in conn.execute(
            "SELECT stage_type FROM sleep_stages WHERE "
            "parent_uuid = 'garmin-sleep-555'",
        )
    )
    assert stage_types == [1, 4, 5, 6], stage_types

    # metrics.score_sleep consumes these stage ints directly.
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import metrics
    stages_rows = conn.execute(
        "SELECT stage_type, stage_start_utc, stage_end_utc FROM "
        "sleep_stages WHERE parent_uuid = 'garmin-sleep-555'",
    ).fetchall()
    scored = metrics.score_sleep(stages_rows)
    assert scored["sleep_hours"] == 7.5, scored

    print("garmin_api.py: all checks passed (no live Garmin call made)")
