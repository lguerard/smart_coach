"""Garmin Connect API ingestion: sessions, sleep, HRV, readiness.

Exercise sessions and sleep come straight from Garmin's API (same
unofficial ``garminconnect`` client garmin-coach used) instead of the
Health Connect export -- Garmin's HC writer mislabels activity types
and never syncs per-session HR series, while its own API has both.
All other record types (steps, weight, nutrition, ...) still flow in
from the Health Connect export via parse_health_connect.

HRV, training readiness and body battery have NO Health Connect
equivalent at all (Garmin never syncs them there) -- these were
dropped entirely in the HC-only era (see training.py's original
docstring) and are now pulled straight from the API into their own
tables (``garmin_hrv``, ``garmin_training_readiness``,
``garmin_body_battery``), feeding real votes in
training.compute_status instead of the activity-load proxy alone.

Exercise/sleep rows land in the same tables the HC parser used to
fill (``exercise_sessions``, ``sleep_sessions``, ``sleep_stages``,
plus ``exercise_hr_samples`` and ``exercise_route_points`` for
activities with a GPS track), keyed by ``garmin-*`` uuids, so every
consumer (metrics, training, dashboard) is untouched. A Garmin row
overlapping an existing HC-era row is skipped rather than
duplicated, so switching sources mid-history is safe.

``push_workout_for_session`` closes the loop the other way: it
builds a Garmin workout from tonight's coach-computed session
(reps/duration per exercise) and pushes it to the watch via
``upload_workout`` + ``schedule_workout``, replacing yesterday's
pushed template so the library doesn't grow one workout per day
forever. Ceiling (ponytail): per-step exercise labels are sent as a
best-effort ``description`` extra field with no confirmed on-watch
display -- verify against a real account and adjust
``_EXERCISE_LABEL_FR``/the step-building helpers if labels don't
show correctly. Treadmill incline has no confirmed Garmin workout
target field either, so it's carried in the workout's overall
description text, not enforced on-device.

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

from garminconnect.workout import (
    ExecutableStep,
    FitnessEquipmentWorkout,
    StepType,
    TargetType,
    WorkoutSegment,
    create_interval_step,
    create_repeat_group,
)

from garminconnect import Garmin

TOKEN_ROOT = Path(os.environ.get("GARMIN_TOKEN_DIR", "data/garmin-tokens"))
LOOKBACK_DAYS = int(os.environ.get("GARMIN_LOOKBACK_DAYS", "30"))

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

# training.session_values() key -> French exercise label, for pushed
# workout step descriptions. Keys ending in "_sec" are time-based
# (seconds), everything else (except "rounds"/"duration_min", never
# turned into a step) is rep-based.
_EXERCISE_LABEL_FR = {
    "squats": "squats", "lunges_per_leg": "fentes avant/jambe",
    "wall_sit_sec": "chaise contre mur", "calf_raises": "mollets debout",
    "glute_bridge": "pont fessier", "pushups": "pompes", "dips": "dips",
    "superman": "superman", "plank_sec": "planche",
    "reverse_lunges_per_leg": "fentes arriere/jambe",
    "side_plank_sec": "gainage lateral/cote",
    "mountain_climbers": "mountain climbers",
    "jumping_jacks": "jumping jacks",
}
_NON_STEP_KEYS = {"rounds", "duration_min"}

# training.session_values() key -> (Garmin exercise category, exercise
# name), from garminconnect's own exercises.py catalog -- this is what
# drives the on-watch exercise name + muscle diagram; the free-text
# description field pushed alongside it (see _circuit_steps) is not
# rendered on-device at all, which is why steps showed as a bare
# "Allez"/"Go" prompt before this. "" for exercise_name means only the
# category name shows -- no closer bodyweight-only catalog match
# exists for that movement.
_EXERCISE_CATALOG = {
    "squats": ("SQUAT", "SQUAT"),
    "lunges_per_leg": ("LUNGE", "LUNGE"),
    "reverse_lunges_per_leg": ("LUNGE", ""),
    "wall_sit_sec": ("SQUAT", "BODY_WEIGHT_WALL_SQUAT"),
    "calf_raises": ("CALF_RAISE", "CALF_RAISE"),
    "glute_bridge": ("HIP_RAISE", "HIP_RAISE"),
    "pushups": ("PUSH_UP", "PUSH_UP"),
    "dips": ("TRICEPS_EXTENSION", "BODY_WEIGHT_DIP"),
    "superman": ("HYPEREXTENSION", "SUPERMAN_FROM_FLOOR"),
    "plank_sec": ("PLANK", "PLANK"),
    "side_plank_sec": ("PLANK", "SIDE_PLANK"),
    "mountain_climbers": ("PLANK", "MOUNTAIN_CLIMBER"),
    "jumping_jacks": ("CARDIO", "JUMPING_JACKS"),
}


def get_client(username: str) -> Garmin:
    """Return a logged-in Garmin client for one account.

    Uses cached OAuth tokens; falls back to interactive login on
    first run (or after token expiry, ~1 year).

    Parameters:
        username (str): smart_coach account name -- keys the token
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
    # garminconnect's own login() tries several strategies to dodge
    # Garmin's IP rate limiting, but its last one (the SSO "widget"
    # flow) doesn't recognize the page Garmin now serves for its
    # (as of 2025, effectively mandatory) email MFA -- title "GARMIN
    # Authentication Application" instead of the "Enter MFA code for
    # login" title it expects -- and throws instead of prompting.
    # garmin-auth wraps the same login but handles that MFA page
    # correctly, then hands the tokens to garminconnect's own
    # Garmin.login(tokenstore=...), so they land in token_dir in the
    # same format the cached-token path above already reads.
    from garmin_auth import GarminAuth

    email = input(f"Garmin email for {username}: ")
    password = getpass.getpass("Garmin password: ")
    auth = GarminAuth(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Garmin MFA code (check your email): "),
        token_dir=token_dir,
    )
    return auth.login()


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
        conn (sqlite3.Connection): smart_coach db connection.
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


def _ingest_activity_details(
    conn: sqlite3.Connection, user_id: int, client: Garmin,
    activity_id: int, uuid: str,
) -> tuple[int, int]:
    """Fetch one activity's HR series + GPS route into their tables.

    Skipped entirely if HR samples already exist for this uuid -- one
    detail call per activity, once ever. Route points are absent
    (0) for indoor/strength sessions with no GPS track, which is the
    common case for this project's bodyweight-circuit session types.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        activity_id (int): Garmin activityId.
        uuid (str): Matching exercise_sessions uuid.

    Returns:
        tuple[int, int]: (HR samples inserted, route points inserted).
    """
    if conn.execute(
        "SELECT 1 FROM exercise_hr_samples WHERE exercise_uuid = ? "
        "LIMIT 1", (uuid,),
    ).fetchone():
        return 0, 0
    details = client.get_activity_details(activity_id)
    index = {
        d["key"]: d["metricsIndex"]
        for d in details.get("metricDescriptors") or []
    }
    hr_i, ts_i = index.get("directHeartRate"), index.get("directTimestamp")
    lat_i, lon_i = index.get("directLatitude"), index.get("directLongitude")
    alt_i = index.get("directElevation")

    hr_samples, route_points = [], []
    for point in details.get("activityDetailMetrics") or []:
        values = point.get("metrics") or []
        if (
            hr_i is not None and ts_i is not None
            and max(hr_i, ts_i) < len(values)
            and values[hr_i] and values[ts_i]
        ):
            hr_samples.append((
                uuid, user_id,
                _epoch_ms_utc(values[ts_i]).isoformat(),
                int(values[hr_i]),
            ))
        if (
            lat_i is not None and lon_i is not None and ts_i is not None
            and max(lat_i, lon_i, ts_i) < len(values)
            and values[lat_i] and values[lon_i] and values[ts_i]
        ):
            route_points.append((
                uuid, user_id,
                _epoch_ms_utc(values[ts_i]).isoformat(),
                values[lat_i], values[lon_i],
                values[alt_i] if alt_i is not None and alt_i < len(values)
                else None,
            ))
    conn.executemany(
        "INSERT OR IGNORE INTO exercise_hr_samples VALUES (?, ?, ?, ?)",
        hr_samples,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO exercise_route_points VALUES "
        "(?, ?, ?, ?, ?, ?)",
        route_points,
    )
    return len(hr_samples), len(route_points)


def upsert_activities(
    conn: sqlite3.Connection, user_id: int, client: Garmin,
    tz: ZoneInfo, days: int,
) -> tuple[int, int, int]:
    """Fetch recent Garmin activities into exercise_sessions.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        tz (ZoneInfo): User timezone for local_date day boundaries.
        days (int): Trailing window length.

    Returns:
        tuple[int, int, int]: (sessions upserted, HR samples
        inserted, route points inserted).
    """
    today = dt.date.today()
    activities = client.get_activities_by_date(
        (today - dt.timedelta(days=days)).isoformat(), today.isoformat()
    )
    session_count = hr_count = route_count = 0
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
        hr_added, route_added = _ingest_activity_details(
            conn, user_id, client, act["activityId"], uuid,
        )
        hr_count += hr_added
        route_count += route_added
    return session_count, hr_count, route_count


def upsert_sleep(
    conn: sqlite3.Connection, user_id: int, client: Garmin,
    tz: ZoneInfo, days: int,
) -> tuple[int, int]:
    """Fetch recent Garmin sleep into sleep_sessions/sleep_stages.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
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
        # Garmin's own nightly summary rides along in the same payload
        # as the stages -- its sleep score is the real one (what the
        # watch and Connect show), and avgSleepHRV/avgSpO2/respiration
        # have no equivalent in any other source we ingest.
        scores = dto.get("sleepScores") or {}
        overall = scores.get("overall") or {}
        conn.execute(
            "INSERT INTO sleep_sessions (uuid, user_id, start_utc, "
            "end_utc, local_date, title, notes, sleep_score, "
            "avg_sleep_hrv, avg_spo2, avg_respiration, "
            "lowest_respiration, highest_respiration) VALUES "
            "(?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?) ON "
            "CONFLICT(uuid) DO UPDATE "
            "SET start_utc = excluded.start_utc, "
            "end_utc = excluded.end_utc, "
            "local_date = excluded.local_date, "
            "sleep_score = excluded.sleep_score, "
            "avg_sleep_hrv = excluded.avg_sleep_hrv, "
            "avg_spo2 = excluded.avg_spo2, "
            "avg_respiration = excluded.avg_respiration, "
            "lowest_respiration = excluded.lowest_respiration, "
            "highest_respiration = excluded.highest_respiration",
            (uuid, user_id, start_utc, end_utc,
             start.astimezone(tz).date().isoformat(),
             overall.get("value"), dto.get("avgSleepHRV"),
             dto.get("avgSpO2"), dto.get("averageRespirationValue"),
             dto.get("lowestRespirationValue"),
             dto.get("highestRespirationValue")),
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


def upsert_hrv(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch today's HRV summary into garmin_hrv.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if a summary was upserted, 0 if Garmin has none yet
        (common right after waking -- HRV needs a full night's sleep
        to compute).
    """
    data = client.get_hrv_data(date) or {}
    summary = data.get("hrvSummary") or {}
    if summary.get("lastNightAvg") is None:
        return 0
    conn.execute(
        "INSERT INTO garmin_hrv (user_id, local_date, last_night_avg, "
        "weekly_avg, status) VALUES (?, ?, ?, ?, ?) ON CONFLICT"
        "(user_id, local_date) DO UPDATE SET "
        "last_night_avg = excluded.last_night_avg, "
        "weekly_avg = excluded.weekly_avg, status = excluded.status",
        (
            user_id, date, summary.get("lastNightAvg"),
            summary.get("weeklyAvg"), summary.get("status"),
        ),
    )
    return 1


def upsert_training_readiness(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch today's training readiness into garmin_training_readiness.

    Garmin's own aggregate of HRV, sleep, ACWR and stress history --
    the most-recent snapshot wins if the endpoint returns several
    (e.g. one per wake-up event).

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if a snapshot was upserted, 0 if none yet.
    """
    snapshots = client.get_training_readiness(date) or []
    if not snapshots:
        return 0
    latest = max(snapshots, key=lambda s: s.get("timestamp") or "")
    if latest.get("score") is None:
        return 0
    conn.execute(
        "INSERT INTO garmin_training_readiness (user_id, local_date, "
        "score, level, feedback_long) VALUES (?, ?, ?, ?, ?) ON "
        "CONFLICT(user_id, local_date) DO UPDATE SET "
        "score = excluded.score, level = excluded.level, "
        "feedback_long = excluded.feedback_long",
        (
            user_id, date, latest.get("score"), latest.get("level"),
            latest.get("feedbackLong"),
        ),
    )
    return 1


def upsert_body_battery(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch today's body battery summary into garmin_body_battery.

    A running energy-level gauge (charged overnight, drained during
    the day), not a morning score -- dashboard/LLM context only, not
    a training.compute_status vote.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if a summary was upserted, 0 if none yet.
    """
    entries = client.get_body_battery(date) or []
    if not entries:
        return 0
    entry = entries[0]
    conn.execute(
        "INSERT INTO garmin_body_battery (user_id, local_date, "
        "charged, drained, highest, lowest) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, local_date) DO UPDATE SET "
        "charged = excluded.charged, drained = excluded.drained, "
        "highest = excluded.highest, lowest = excluded.lowest",
        (
            user_id, date, entry.get("charged"), entry.get("drained"),
            _body_battery_extreme(entry, max),
            _body_battery_extreme(entry, min),
        ),
    )
    return 1


def _body_battery_extreme(entry: dict, pick) -> int | None:
    """Highest/lowest level from a body-battery entry's samples.

    Parameters:
        entry (dict): One ``get_body_battery`` list entry.
        pick (Callable): ``max`` or ``min``.

    Returns:
        int | None: The picked level, or ``None`` if no samples.
    """
    levels = [
        point[1] for point in entry.get("bodyBatteryValuesArray") or []
        if len(point) > 1 and point[1] is not None
    ]
    return pick(levels) if levels else None


def upsert_stress(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch today's all-day stress summary into garmin_stress.

    Context only, deliberately NOT a training.compute_status vote:
    Garmin's own training_readiness score already factors stress
    history into its aggregate (see
    TrainingReadiness.stress_history_factor_percent), so a separate
    stress vote would double-count the same underlying signal and
    bias the daily status toward red on days that are already
    reflected in the readiness vote.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if a summary was upserted, 0 if none yet.
    """
    data = client.get_all_day_stress(date) or {}
    avg, high = data.get("avgStressLevel"), data.get("maxStressLevel")
    if avg is None and high is None:
        return 0
    conn.execute(
        "INSERT INTO garmin_stress (user_id, local_date, avg_level, "
        "max_level) VALUES (?, ?, ?, ?) ON CONFLICT(user_id, "
        "local_date) DO UPDATE SET avg_level = excluded.avg_level, "
        "max_level = excluded.max_level",
        (user_id, date, avg, high),
    )
    return 1
def upsert_garmin_hydration(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch one day's Garmin Connect hydration total into
    garmin_hydration.

    Exists specifically because Health Connect's hydration is
    unreliable for the same day this project cares about: MyFitnessPal
    writes the day's water as one record covering the whole day, and
    it reaches the phone's Health Connect export a full day later
    than its meals do -- confirmed structural, not an off day, across
    two real exports four days apart. Garmin's API has no export
    snapshot in between: asking for a completed day's total returns
    Garmin's current value for that calendar date, not whatever was
    true when some earlier job ran. Logging water via the Garmin
    Connect app or a watch widget sidesteps the lag entirely, at the
    cost of actually needing to log it there.

    Undocumented endpoint (usersummary-service hydration/daily) with
    no field names modelled anywhere in the client library -- unlike
    daily_summary/sleep, ``valueInML``/``goalInML`` here are a
    best-effort guess, not verified against real account data. If
    this keeps upserting 0, the guess is wrong; check a raw response
    and correct the keys below.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if a total was upserted, 0 if Garmin has none for
        this date.
    """
    data = client.get_hydration_data(date) or {}
    volume_ml = data.get("valueInML")
    if volume_ml is None:
        return 0
    conn.execute(
        "INSERT INTO garmin_hydration (user_id, local_date, "
        "volume_ml) VALUES (?, ?, ?) ON CONFLICT(user_id, local_date) "
        "DO UPDATE SET volume_ml = excluded.volume_ml",
        (user_id, date, volume_ml),
    )
    return 1




def upsert_body_composition(
    conn: sqlite3.Connection, user_id: int, client: Garmin,
    tz: ZoneInfo, days: int,
) -> int:
    """Fetch Garmin weigh-ins into weight/body_fat/lean_body_mass.

    These tables are also filled from the Health Connect export, and
    Garmin is the better source for them: the scale syncs to Garmin
    first and only reaches Health Connect afterwards, where readings
    arrive erratically -- a real account had daily weigh-ins showing
    up there about weekly, so the coach kept calling a plateau on a
    fortnight-old figure. Both sources land in the same tables under
    their own uuids; metrics.latest_body_comp takes the most recent
    reading, which is the Garmin one whenever the two disagree on
    freshness.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        tz (ZoneInfo): User timezone for local_date day boundaries.
        days (int): Trailing window length.

    Returns:
        int: Weigh-ins upserted.
    """
    today = dt.date.today()
    data = client.get_body_composition(
        (today - dt.timedelta(days=days)).isoformat(), today.isoformat(),
    ) or {}
    # Undocumented endpoint: the per-weigh-in list has carried a
    # couple of names, and masses come back in grams.
    entries = (
        data.get("dateWeightList")
        or data.get("dailyWeightSummaries")
        or []
    )
    count = 0
    for entry in entries:
        grams = entry.get("weight")
        timestamp_ms = entry.get("date") or entry.get("timestampGMT")
        if grams is None or not timestamp_ms:
            continue
        measured = _epoch_ms_utc(int(timestamp_ms))
        local_date = measured.astimezone(tz).date().isoformat()
        time_utc = measured.isoformat()
        uuid = f"garmin-weight-{entry.get('samplePk') or timestamp_ms}"
        conn.execute(
            "INSERT INTO weight (uuid, user_id, time_utc, local_date, kg) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(uuid) DO UPDATE SET "
            "time_utc = excluded.time_utc, "
            "local_date = excluded.local_date, kg = excluded.kg",
            (uuid, user_id, time_utc, local_date, grams / 1000),
        )
        count += 1
        if entry.get("bodyFat") is not None:
            conn.execute(
                "INSERT INTO body_fat (uuid, user_id, time_utc, "
                "local_date, percentage) VALUES (?, ?, ?, ?, ?) ON "
                "CONFLICT(uuid) DO UPDATE SET "
                "time_utc = excluded.time_utc, "
                "local_date = excluded.local_date, "
                "percentage = excluded.percentage",
                (uuid, user_id, time_utc, local_date, entry["bodyFat"]),
            )
        if entry.get("muscleMass") is not None:
            conn.execute(
                "INSERT INTO lean_body_mass (uuid, user_id, time_utc, "
                "local_date, kg) VALUES (?, ?, ?, ?, ?) ON CONFLICT"
                "(uuid) DO UPDATE SET time_utc = excluded.time_utc, "
                "local_date = excluded.local_date, kg = excluded.kg",
                (uuid, user_id, time_utc, local_date,
                 entry["muscleMass"] / 1000),
            )
    return count


def upsert_daily_summary(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch Garmin's whole-day rollup into garmin_daily_summary.

    The one endpoint here whose field names the client library models
    explicitly (garminconnect.typed.DailyStats), so these are verified
    rather than guessed like the wellness endpoints below.

    Worth having even though Health Connect carries most of the same
    numbers: HC only syncs overnight, so its steps/resting HR run a
    day behind, while this has today's. metrics.daily_wellness prefers
    HC where it has the day and falls back to this.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if a summary was upserted, 0 if Garmin has none yet.
    """
    data = client.get_stats(date) or {}
    if data.get("totalSteps") is None and data.get("restingHeartRate") is None:
        return 0
    conn.execute(
        "INSERT INTO garmin_daily_summary (user_id, local_date, "
        "total_steps, daily_step_goal, total_distance_m, resting_hr, "
        "min_hr, max_hr, total_kcal, active_kcal, bmr_kcal, "
        "moderate_intensity_min, vigorous_intensity_min, "
        "floors_ascended, active_seconds, sedentary_seconds, "
        "highly_active_seconds) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON "
        "CONFLICT(user_id, local_date) DO UPDATE SET "
        "total_steps = excluded.total_steps, "
        "daily_step_goal = excluded.daily_step_goal, "
        "total_distance_m = excluded.total_distance_m, "
        "resting_hr = excluded.resting_hr, min_hr = excluded.min_hr, "
        "max_hr = excluded.max_hr, total_kcal = excluded.total_kcal, "
        "active_kcal = excluded.active_kcal, "
        "bmr_kcal = excluded.bmr_kcal, "
        "moderate_intensity_min = excluded.moderate_intensity_min, "
        "vigorous_intensity_min = excluded.vigorous_intensity_min, "
        "floors_ascended = excluded.floors_ascended, "
        "active_seconds = excluded.active_seconds, "
        "sedentary_seconds = excluded.sedentary_seconds, "
        "highly_active_seconds = excluded.highly_active_seconds",
        (
            user_id, date, data.get("totalSteps"),
            data.get("dailyStepGoal"), data.get("totalDistanceMeters"),
            data.get("restingHeartRate"), data.get("minHeartRate"),
            data.get("maxHeartRate"), data.get("totalKilocalories"),
            data.get("activeKilocalories"), data.get("bmrKilocalories"),
            data.get("moderateIntensityMinutes"),
            data.get("vigorousIntensityMinutes"),
            data.get("floorsAscended"), data.get("activeSeconds"),
            data.get("sedentarySeconds"),
            data.get("highlyActiveSeconds"),
        ),
    )
    return 1


def upsert_respiration(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch today's respiration summary into garmin_respiration.

    Context only, like body battery/stress. get_respiration_data rides
    an undocumented endpoint -- unverified against a real account, so
    field names are a best-effort guess; a wrong name just leaves that
    column NULL rather than failing the whole ingest.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if a summary was upserted, 0 if none yet.
    """
    data = client.get_respiration_data(date) or {}
    avg_waking = data.get("avgWakingRespirationValue")
    avg_sleep = data.get("avgSleepRespirationValue")
    highest = data.get("highestRespirationValue")
    lowest = data.get("lowestRespirationValue")
    if all(v is None for v in (avg_waking, avg_sleep, highest, lowest)):
        return 0
    conn.execute(
        "INSERT INTO garmin_respiration (user_id, local_date, "
        "avg_waking, avg_sleep, highest, lowest) VALUES "
        "(?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, local_date) DO "
        "UPDATE SET avg_waking = excluded.avg_waking, "
        "avg_sleep = excluded.avg_sleep, highest = excluded.highest, "
        "lowest = excluded.lowest",
        (user_id, date, avg_waking, avg_sleep, highest, lowest),
    )
    return 1


def upsert_spo2(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch today's pulse ox (SpO2) summary into garmin_spo2.

    Context only. Not every watch has a pulse ox sensor -- absent
    entirely for those accounts, which is not an error here.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if a summary was upserted, 0 if none yet.
    """
    data = client.get_spo2_data(date) or {}
    average = data.get("averageSpO2")
    lowest = data.get("lowestSpO2")
    if average is None and lowest is None:
        return 0
    conn.execute(
        "INSERT INTO garmin_spo2 (user_id, local_date, average, "
        "lowest) VALUES (?, ?, ?, ?) ON CONFLICT(user_id, local_date) "
        "DO UPDATE SET average = excluded.average, "
        "lowest = excluded.lowest",
        (user_id, date, average, lowest),
    )
    return 1


def upsert_intensity_minutes(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch today's intensity minutes into garmin_intensity_minutes.

    Context only -- rides an undocumented endpoint, field names are a
    best-effort guess (see upsert_respiration's caveat).

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if a summary was upserted, 0 if none yet.
    """
    data = client.get_intensity_minutes_data(date) or {}
    moderate = data.get("moderateIntensityMinutes")
    vigorous = data.get("vigorousIntensityMinutes")
    goal = data.get("intensityMinutesGoal") or data.get("weeklyGoal")
    if moderate is None and vigorous is None:
        return 0
    conn.execute(
        "INSERT INTO garmin_intensity_minutes (user_id, local_date, "
        "moderate_min, vigorous_min, weekly_goal_min) VALUES "
        "(?, ?, ?, ?, ?) ON CONFLICT(user_id, local_date) DO UPDATE "
        "SET moderate_min = excluded.moderate_min, "
        "vigorous_min = excluded.vigorous_min, "
        "weekly_goal_min = excluded.weekly_goal_min",
        (user_id, date, moderate, vigorous, goal),
    )
    return 1


def upsert_vo2max(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch today's VO2max estimate into garmin_vo2max.

    Context only. get_max_metrics has no stable typed schema even per
    the client library's own docs -- it bundles per-sport breakdowns
    under keys like "generic"/"cycling" that vary by device/account,
    and may come back as a bare dict or a one-item list depending on
    account. Best-effort: only the "generic" (running/overall)
    estimate is stored.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if an estimate was upserted, 0 if none yet.
    """
    data = client.get_max_metrics(date) or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    generic = data.get("generic") or {}
    value = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
    fitness_age = generic.get("fitnessAge")
    if value is None and fitness_age is None:
        return 0
    conn.execute(
        "INSERT INTO garmin_vo2max (user_id, local_date, value, "
        "fitness_age) VALUES (?, ?, ?, ?) ON CONFLICT(user_id, "
        "local_date) DO UPDATE SET value = excluded.value, "
        "fitness_age = excluded.fitness_age",
        (user_id, date, value, fitness_age),
    )
    return 1


# get_earned_badges() has no typed wrapper in garminconnect and no
# test fixture upstream either -- field names below are the
# commonly-documented ones (badgeId/badgeKey, badgeName,
# badgeEarnedDate) but UNVERIFIED against a live account. Failure
# mode if wrong is graceful (badge silently skipped, not a crash or
# a wrong value shown), but verify against a real account and adjust
# these keys if badges don't show up on the Trophees page.
def _badge_key(badge: dict) -> str | None:
    raw = (
        badge.get("badgeId") or badge.get("badgeKey")
        or badge.get("badgeUuid")
    )
    return str(raw) if raw is not None else None


def _badge_earned_date(badge: dict) -> str | None:
    raw = (
        badge.get("badgeEarnedDate") or badge.get("earnedDate")
        or badge.get("badgeAcquiredDate")
    )
    if not raw:
        return None
    return raw.split("T")[0].split(" ")[0]


def upsert_badges(
    conn: sqlite3.Connection, user_id: int, client: Garmin,
) -> int:
    """Fetch earned Garmin Connect badges into garmin_badges.

    Surfaced as achievements (achievements.py's ``_garmin_badge_items``)
    instead of a separate section -- one unified Trophees page.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.

    Returns:
        int: Number of badges upserted (skips entries missing an id
        or an earned date -- see the field-name caveat above).
    """
    badges = client.get_earned_badges() or []
    rows = []
    for badge in badges:
        key = _badge_key(badge)
        earned = _badge_earned_date(badge)
        name = badge.get("badgeName")
        if not key or not earned or not name:
            continue
        rows.append((user_id, key, name, earned))
    conn.executemany(
        "INSERT INTO garmin_badges (user_id, badge_key, name, "
        "earned_date) VALUES (?, ?, ?, ?) ON CONFLICT(user_id, "
        "badge_key) DO UPDATE SET name = excluded.name, "
        "earned_date = excluded.earned_date",
        rows,
    )
    return len(rows)


# get_menstrual_data_for_date has no typed wrapper AND no test
# fixture upstream (same as badges, but higher stakes -- sensitive
# personal health data). Only ever fetched if the user opts in
# (settings.track_menstrual_cycle) -- never otherwise. Parsing is
# maximally defensive: if the guessed field name is wrong, the day is
# silently skipped, never a wrong/fabricated phase shown. Surfaced as
# LLM context only (metrics.garmin_wellness), never a hard training
# or nutrition rule -- verify against a real account and adjust
# _cycle_phase's candidate keys if this never populates.
def _cycle_phase(data: dict) -> str | None:
    return (
        data.get("cyclePhaseType") or data.get("phase")
        or data.get("dayType")
    )


def upsert_menstrual_cycle(
    conn: sqlite3.Connection, user_id: int, client: Garmin, date: str,
) -> int:
    """Fetch one day's menstrual cycle phase, opt-in only.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        date (str): ISO local date to fetch.

    Returns:
        int: 1 if a phase was upserted, 0 if none/unrecognized.
    """
    data = client.get_menstrual_data_for_date(date) or {}
    phase = _cycle_phase(data)
    if not phase:
        return 0
    conn.execute(
        "INSERT INTO garmin_menstrual_cycle (user_id, local_date, "
        "phase) VALUES (?, ?, ?) ON CONFLICT(user_id, local_date) DO "
        "UPDATE SET phase = excluded.phase",
        (user_id, date, phase),
    )
    return 1


def fetch_and_upsert(
    conn: sqlite3.Connection, user_id: int, username: str,
    days: int = LOOKBACK_DAYS,
) -> dict[str, int]:
    """Pull one user's Garmin activities, sleep and wellness into the db.

    HRV/training-readiness/body-battery/stress/respiration/SpO2/
    intensity-minutes/VO2max are fetched for today's local date only
    (not backfilled over ``days``) -- they're inputs for today's
    coaching run, not historical training data. Hydration is the one
    exception fetched for both today AND yesterday: unlike those,
    someone reads yesterday's completed total back the next morning
    (progress.intake_for_date), and Garmin's API returns a completed
    day's true value on demand rather than an ingest-time snapshot,
    so there is no staleness cost to asking for both.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        username (str): Account name (keys the Garmin token dir).
        days (int): Trailing fetch window for activities/sleep
            (GARMIN_LOOKBACK_DAYS env, default 30 -- raise it once
            for a first-run backfill).

    Returns:
        dict[str, int]: Row counts per table, also logged to
        ``ingest_runs`` for the dashboard's status page.
    """
    import db

    client = get_client(username)
    tz = ZoneInfo(
        db.get_setting(conn, user_id, "timezone") or "Europe/Paris"
    )
    sessions, hr_samples, route_points = upsert_activities(
        conn, user_id, client, tz, days,
    )
    sleep_sessions, sleep_stages = upsert_sleep(
        conn, user_id, client, tz, days,
    )
    weigh_ins = upsert_body_composition(conn, user_id, client, tz, days)
    today = dt.datetime.now(tz).date().isoformat()
    yesterday = (
        dt.datetime.now(tz).date() - dt.timedelta(days=1)
    ).isoformat()
    counts = {
        "exercise_sessions": sessions,
        "exercise_hr_samples": hr_samples,
        "exercise_route_points": route_points,
        "sleep_sessions": sleep_sessions,
        "sleep_stages": sleep_stages,
        "body_composition": weigh_ins,
        "hrv": upsert_hrv(conn, user_id, client, today),
        "training_readiness": upsert_training_readiness(
            conn, user_id, client, today,
        ),
        "body_battery": upsert_body_battery(conn, user_id, client, today),
        "stress": upsert_stress(conn, user_id, client, today),
        "daily_summary": upsert_daily_summary(conn, user_id, client, today),
        "respiration": upsert_respiration(conn, user_id, client, today),
        "spo2": upsert_spo2(conn, user_id, client, today),
        "intensity_minutes": upsert_intensity_minutes(
            conn, user_id, client, today,
        ),
        "vo2max": upsert_vo2max(conn, user_id, client, today),
        "hydration": (
            upsert_garmin_hydration(conn, user_id, client, today)
            + upsert_garmin_hydration(conn, user_id, client, yesterday)
        ),
        "badges": upsert_badges(conn, user_id, client),
        "menstrual_cycle": (
            upsert_menstrual_cycle(conn, user_id, client, today)
            if db.get_setting(conn, user_id, "track_menstrual_cycle") == "1"
            else 0
        ),
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


def _treadmill_workout(level: int, values: dict) -> FitnessEquipmentWorkout:
    """Build a single-step indoor-cardio workout for tonight's treadmill
    session.

    Parameters:
        level (int): Tonight's treadmill level (name only).
        values (dict): ``training.treadmill_values()`` output.

    Returns:
        FitnessEquipmentWorkout: Ready to upload via
        ``client.upload_workout``.
    """
    # sportTypeId 17 ("walking" per WalkingWorkout's own default) is
    # wrong on Garmin's actual backend -- verified against a real
    # account, it renders the pushed step as a pool-swimming step
    # (distance target + pool-size field) instead of a treadmill walk.
    # 6/cardio_training is the same, already-verified sportType the
    # circuit workouts use.
    cardio_sport_type = {
        "sportTypeId": 6, "sportTypeKey": "cardio_training", "displayOrder": 6,
    }
    duration_s = values["duration_min"] * 60
    segment = WorkoutSegment(
        segmentOrder=1,
        sportType=cardio_sport_type,
        workoutSteps=[create_interval_step(duration_s, step_order=1)],
    )
    return FitnessEquipmentWorkout(
        workoutName=f"Smart Coach - Tapis niveau {level}",
        estimatedDurationInSecs=int(duration_s),
        description=(
            f"{values['speed_kmh']} km/h, inclinaison "
            f"{values['incline_pct']}%, {values['duration_min']} min"
        ),
        workoutSegments=[segment],
        sportType=cardio_sport_type,
    )


def _circuit_steps(values: dict) -> list[ExecutableStep]:
    """One step per exercise in ``values`` (reps- or time-based).

    Parameters:
        values (dict): A circuit session_values() output (lower_body/
            upper_body/calisthenics) -- every key but ``rounds`` and
            ``duration_min`` becomes one step.

    Returns:
        list[ExecutableStep]: Ordered steps, one per exercise.
    """
    steps = []
    for order, (key, value) in enumerate(
        (k, v) for k, v in values.items() if k not in _NON_STEP_KEYS
    ):
        is_time = key.endswith("_sec")
        # Garmin's end-condition type IDs (2 = time, 10 = reps) -- hardcoded
        # rather than read off garminconnect.workout.ConditionType, since
        # older installed versions of that library don't define REPS yet.
        condition = (
            {
                "conditionTypeId": 2,
                "conditionTypeKey": "time", "displayOrder": 2,
                "displayable": True,
            } if is_time else {
                "conditionTypeId": 10,
                "conditionTypeKey": "reps", "displayOrder": 10,
                "displayable": True,
            }
        )
        category, exercise_name = _EXERCISE_CATALOG.get(key, (None, None))
        extra = (
            {"category": category, "exerciseName": exercise_name}
            if category else {}
        )
        steps.append(ExecutableStep(
            stepOrder=order + 1,
            stepType={
                "stepTypeId": StepType.INTERVAL,
                "stepTypeKey": "interval", "displayOrder": 3,
            },
            endCondition=condition,
            endConditionValue=float(value),
            targetType={
                "workoutTargetTypeId": TargetType.NO_TARGET,
                "workoutTargetTypeKey": "no.target", "displayOrder": 1,
            },
            description=_EXERCISE_LABEL_FR.get(key, key),
            **extra,
        ))
    return steps


def _circuit_workout(
    session_type: str, level: int, values: dict,
) -> FitnessEquipmentWorkout:
    """Build a repeat-group bodyweight-circuit workout.

    Parameters:
        session_type (str): ``lower_body``, ``upper_body`` or
            ``calisthenics``.
        level (int): Tonight's level (name only).
        values (dict): ``training.session_values()`` output.

    Returns:
        FitnessEquipmentWorkout: Ready to upload.
    """
    import training

    group = create_repeat_group(
        values["rounds"], _circuit_steps(values), step_order=1,
    )
    segment = WorkoutSegment(
        segmentOrder=1,
        # 5/strength_training, not 6/cardio_training -- Garmin only
        # shows the per-step exercise name + muscle diagram on watches
        # for segments typed as strength training.
        sportType={
            "sportTypeId": 5, "sportTypeKey": "strength_training",
            "displayOrder": 5,
        },
        workoutSteps=[group],
    )
    label = training.SESSION_LABEL_FR[session_type]
    description = ", ".join(
        f"{_EXERCISE_LABEL_FR.get(key, key)} {value}"
        f"{'s' if key.endswith('_sec') else ''}"
        for key, value in values.items() if key not in _NON_STEP_KEYS
    )
    return FitnessEquipmentWorkout(
        workoutName=f"Smart Coach - {label} niveau {level}",
        estimatedDurationInSecs=values["duration_min"] * 60,
        description=f"{values['rounds']} tours - {description}",
        workoutSegments=[segment],
        # Match the segment's sportType above -- FitnessEquipmentWorkout
        # defaults to cardio_training otherwise.
        sportType={
            "sportTypeId": 5, "sportTypeKey": "strength_training",
            "displayOrder": 5,
        },
    )


def build_workout(session_type: str, level: int, values: dict):
    """Build a typed Garmin workout from tonight's coach-computed session.

    Parameters:
        session_type (str): One of ``training.SESSION_LABEL_FR``'s keys.
        level (int): Tonight's level (name only, doesn't affect
            enforced targets).
        values (dict): ``training.session_values()`` output.

    Returns:
        FitnessEquipmentWorkout: Ready to upload via
        ``client.upload_workout(workout.to_dict())``.
    """
    if session_type == "treadmill":
        return _treadmill_workout(level, values)
    return _circuit_workout(session_type, level, values)


def push_workout_for_session(
    conn: sqlite3.Connection, user_id: int, client: Garmin,
    session_type: str, level: int, values: dict, date: str,
) -> str:
    """Push tonight's session to the watch, replacing yesterday's.

    Deletes the previously pushed workout template first (best-effort
    -- a manually-deleted template on Garmin's side isn't an error
    here), so the workout library doesn't accumulate one entry per
    day forever.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        client (Garmin): Authenticated client.
        session_type (str): One of ``training.SESSION_LABEL_FR``'s keys.
        level (int): Tonight's level.
        values (dict): ``training.session_values()`` output.
        date (str): ISO local date to schedule the workout on.

    Returns:
        str: The new Garmin workout id.

    Raises:
        RuntimeError: The upload response carried no workout id.
    """
    old = conn.execute(
        "SELECT workout_id FROM garmin_workout_pushes WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if old:
        try:
            client.delete_workout(old["workout_id"])
        except Exception:
            pass

    workout = build_workout(session_type, level, values)
    result = client.upload_workout(workout.to_dict())
    workout_id = result.get("workoutId") if isinstance(result, dict) else None
    if not workout_id:
        raise RuntimeError(
            f"Garmin upload_workout returned no workoutId: {result!r}"
        )
    client.schedule_workout(workout_id, date)
    conn.execute(
        "INSERT INTO garmin_workout_pushes (user_id, workout_id, "
        "local_date) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE "
        "SET workout_id = excluded.workout_id, "
        "local_date = excluded.local_date",
        (user_id, str(workout_id), date),
    )
    conn.commit()
    return str(workout_id)


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
            # Only activity 101 (the outdoor-ish canned one) carries a
            # GPS track -- 102 (Muscu) has none, the common case for
            # this project's indoor bodyweight sessions.
            if activity_id != 101:
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
            return {
                "metricDescriptors": [
                    {"key": "directTimestamp", "metricsIndex": 0},
                    {"key": "directHeartRate", "metricsIndex": 1},
                    {"key": "directLatitude", "metricsIndex": 2},
                    {"key": "directLongitude", "metricsIndex": 3},
                    {"key": "directElevation", "metricsIndex": 4},
                ],
                "activityDetailMetrics": [
                    {"metrics": [1784311200000, 120.0, 45.75, 4.85, 200.0]},
                    {"metrics": [1784311260000, 150.0, 45.76, 4.86, 205.0]},
                    {"metrics": [1784311320000, None, None, None, None]},
                ],
            }

        def get_all_day_stress(self, date: str) -> dict:
            if date != "2026-07-16":
                return {}
            return {"avgStressLevel": 32, "maxStressLevel": 68}

        def get_hydration_data(self, date: str) -> dict:
            if date != "2026-07-16":
                return {}
            return {"valueInML": 1850.0, "goalInML": 3000.0}

        def get_earned_badges(self) -> list:
            return [
                {
                    "badgeId": 42, "badgeName": "5K Runner",
                    "badgeEarnedDate": "2026-07-10T08:00:00.0",
                },
                # Missing a name -> must be skipped, not crash.
                {"badgeId": 43, "badgeEarnedDate": "2026-07-11T08:00:00.0"},
            ]

        def get_menstrual_data_for_date(self, date: str) -> dict:
            if date != "2026-07-16":
                return {}
            return {"cyclePhaseType": "LUTEAL"}

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

        def get_hrv_data(self, date: str) -> dict:
            if date != "2026-07-16":
                return {}
            return {
                "hrvSummary": {
                    "lastNightAvg": 55.0, "weeklyAvg": 58.0,
                    "status": "BALANCED",
                },
            }

        def get_training_readiness(self, date: str) -> list:
            if date != "2026-07-16":
                return []
            return [{
                "timestamp": "2026-07-16T06:00:00.0", "score": 62,
                "level": "MODERATE", "feedbackLong": "note",
            }]

        def get_body_composition(self, start: str, end: str) -> dict:
            return {
                "dateWeightList": [
                    {
                        "samplePk": 7001, "date": 1784176200000,
                        "weight": 88700.0, "bodyFat": 24.5,
                        "muscleMass": 61200.0,
                    },
                    # Garmin returns weightless rows for days the
                    # scale only measured impedance -- must be skipped.
                    {"samplePk": 7002, "date": 1784262600000,
                     "weight": None},
                ],
            }

        def get_body_battery(self, date: str) -> list:
            if date != "2026-07-16":
                return []
            return [{
                "date": "2026-07-16", "charged": 80, "drained": 45,
                "bodyBatteryValuesArray": [
                    [1784260800000, 90], [1784304000000, 30],
                    [1784332800000, None],
                ],
            }]

    tmp = Path(tempfile.mkdtemp()) / "smart_coach.db"
    conn = db.connect(tmp)
    db.init_db(conn)
    uid = db.create_user(conn, "test", "password1234")
    tz = ZoneInfo("Europe/Paris")
    fake = _FakeGarmin()

    # Freeze the fetch window around the canned data's dates.
    real_date = dt.date

    sessions, hr, route = upsert_activities(conn, uid, fake, tz, 3650)
    assert sessions == 2, sessions
    assert hr == 4, hr  # 2 activities x 2 valid samples (None dropped)
    assert route == 2, route  # only activity 101 carries a GPS track
    route_rows = conn.execute(
        "SELECT * FROM exercise_route_points WHERE exercise_uuid = "
        "'garmin-101' ORDER BY epoch_utc",
    ).fetchall()
    assert len(route_rows) == 2, route_rows
    assert route_rows[0]["latitude"] == 45.75, dict(route_rows[0])
    assert route_rows[0]["altitude_m"] == 200.0, dict(route_rows[0])
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM exercise_route_points WHERE "
        "exercise_uuid = 'garmin-102'",
    ).fetchone()["n"] == 0  # no GPS track for the indoor session
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
    sessions2, hr2, route2 = upsert_activities(conn, uid, fake, tz, 3650)
    assert sessions2 == 2 and hr2 == 0 and route2 == 0, (
        sessions2, hr2, route2,
    )
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

    dup_sessions, _, _ = upsert_activities(
        conn, uid, _OverlapGarmin(), tz, 3650,
    )
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

    # HRV / training readiness / body battery: today's row upserted,
    # a day with no Garmin data yet yields 0 and no row.
    other_uid = db.create_user(conn, "other", "password1234")
    assert upsert_hrv(conn, uid, fake, "2026-07-01") == 0
    assert upsert_hrv(conn, uid, fake, "2026-07-16") == 1
    hrv_row = conn.execute(
        "SELECT * FROM garmin_hrv WHERE user_id = ? AND "
        "local_date = '2026-07-16'", (uid,),
    ).fetchone()
    assert hrv_row["last_night_avg"] == 55.0, dict(hrv_row)
    assert hrv_row["status"] == "BALANCED"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM garmin_hrv WHERE user_id = ?",
        (other_uid,),
    ).fetchone()["n"] == 0  # isolated

    assert upsert_training_readiness(conn, uid, fake, "2026-07-01") == 0
    assert upsert_training_readiness(conn, uid, fake, "2026-07-16") == 1
    tr_row = conn.execute(
        "SELECT * FROM garmin_training_readiness WHERE user_id = ? AND "
        "local_date = '2026-07-16'", (uid,),
    ).fetchone()
    assert tr_row["score"] == 62, dict(tr_row)
    assert tr_row["level"] == "MODERATE", dict(tr_row)

    assert upsert_body_battery(conn, uid, fake, "2026-07-01") == 0
    assert upsert_body_battery(conn, uid, fake, "2026-07-16") == 1
    bb_row = conn.execute(
        "SELECT * FROM garmin_body_battery WHERE user_id = ? AND "
        "local_date = '2026-07-16'", (uid,),
    ).fetchone()
    assert bb_row["charged"] == 80 and bb_row["drained"] == 45, dict(bb_row)
    assert bb_row["highest"] == 90 and bb_row["lowest"] == 30, dict(bb_row)

    assert upsert_stress(conn, uid, fake, "2026-07-01") == 0
    assert upsert_stress(conn, uid, fake, "2026-07-16") == 1
    stress_row = conn.execute(
        "SELECT * FROM garmin_stress WHERE user_id = ? AND "
        "local_date = '2026-07-16'", (uid,),
    ).fetchone()
    assert stress_row["avg_level"] == 32, dict(stress_row)
    assert stress_row["max_level"] == 68, dict(stress_row)

    assert upsert_garmin_hydration(conn, uid, fake, "2026-07-01") == 0
    assert upsert_garmin_hydration(conn, uid, fake, "2026-07-16") == 1
    hyd_row = conn.execute(
        "SELECT volume_ml FROM garmin_hydration WHERE user_id = ? AND "
        "local_date = '2026-07-16'", (uid,),
    ).fetchone()
    assert hyd_row["volume_ml"] == 1850.0, dict(hyd_row)
    # Re-run: idempotent upsert, no duplicate row.
    assert upsert_garmin_hydration(conn, uid, fake, "2026-07-16") == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM garmin_hydration WHERE user_id = ?",
        (uid,),
    ).fetchone()["n"] == 1

    # Body composition: grams -> kg, weightless row skipped, and the
    # re-run stays idempotent (same uuid, no second weigh-in).
    assert upsert_body_composition(conn, uid, fake, tz, 3650) == 1
    weight_row = conn.execute(
        "SELECT * FROM weight WHERE user_id = ?", (uid,),
    ).fetchone()
    assert weight_row["uuid"] == "garmin-weight-7001", dict(weight_row)
    assert weight_row["kg"] == 88.7, dict(weight_row)
    assert weight_row["local_date"] == "2026-07-16", dict(weight_row)
    assert conn.execute(
        "SELECT percentage FROM body_fat WHERE user_id = ?", (uid,),
    ).fetchone()["percentage"] == 24.5
    assert conn.execute(
        "SELECT kg FROM lean_body_mass WHERE user_id = ?", (uid,),
    ).fetchone()["kg"] == 61.2
    assert upsert_body_composition(conn, uid, fake, tz, 3650) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM weight WHERE user_id = ?", (uid,),
    ).fetchone()["n"] == 1

    # A stale Health Connect weigh-in must not outrank the Garmin one:
    # metrics.latest_body_comp is what the coach reads, and picking
    # the older row is exactly the "plateau" bug this fetch fixes.
    conn.execute(
        "INSERT INTO weight (uuid, user_id, time_utc, local_date, kg) "
        "VALUES ('hc-old-weight', ?, '2026-07-01T07:00:00+00:00', "
        "'2026-07-01', 90.0)", (uid,),
    )
    assert metrics.latest_body_comp(
        conn, uid, "2026-07-31",
    )["weight_kg"] == 88.7

    # Badges: nameless entry skipped, not crashed on; idempotent re-run.
    assert upsert_badges(conn, uid, fake) == 1
    badge_row = conn.execute(
        "SELECT * FROM garmin_badges WHERE user_id = ?", (uid,),
    ).fetchone()
    assert badge_row["badge_key"] == "42", dict(badge_row)
    assert badge_row["name"] == "5K Runner", dict(badge_row)
    assert badge_row["earned_date"] == "2026-07-10", dict(badge_row)
    assert upsert_badges(conn, uid, fake) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM garmin_badges WHERE user_id = ?",
        (uid,),
    ).fetchone()["n"] == 1

    # Menstrual cycle: opt-in only, never called unless the caller
    # gates it -- direct unit test of the ingestion function itself.
    assert upsert_menstrual_cycle(conn, uid, fake, "2026-07-01") == 0
    assert upsert_menstrual_cycle(conn, uid, fake, "2026-07-16") == 1
    cycle_row = conn.execute(
        "SELECT phase FROM garmin_menstrual_cycle WHERE user_id = ? "
        "AND local_date = '2026-07-16'", (uid,),
    ).fetchone()
    assert cycle_row["phase"] == "LUTEAL", dict(cycle_row)

    # Re-run: idempotent upsert, no duplicate row.
    assert upsert_hrv(conn, uid, fake, "2026-07-16") == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM garmin_hrv WHERE user_id = ?", (uid,),
    ).fetchone()["n"] == 1

    # --- build_workout / push_workout_for_session ---
    import training

    td_values = training.treadmill_values(5)
    treadmill_workout = build_workout("treadmill", 5, td_values).to_dict()
    assert treadmill_workout["workoutName"] == "Smart Coach - Tapis niveau 5"
    assert (
        treadmill_workout["estimatedDurationInSecs"]
        == td_values["duration_min"] * 60
    )
    assert "km/h" in treadmill_workout["description"]

    lb_values = training.session_values("lower_body", 4)
    circuit_workout = build_workout("lower_body", 4, lb_values).to_dict()
    repeat_group = circuit_workout["workoutSegments"][0]["workoutSteps"][0]
    assert repeat_group["numberOfIterations"] == lb_values["rounds"]
    # One step per exercise key (squats, lunges, wall_sit, calf_raises,
    # glute_bridge) -- rounds/duration_min never become steps.
    assert len(repeat_group["workoutSteps"]) == 5, repeat_group

    class _FakeWorkoutGarmin:
        """Records upload/schedule/delete calls -- no live push made."""

        def __init__(self):
            self.uploaded, self.deleted, self.scheduled = [], [], []
            self._next_id = 1000

        def upload_workout(self, workout_json):
            self._next_id += 1
            self.uploaded.append(workout_json)
            return {"workoutId": self._next_id}

        def schedule_workout(self, workout_id, date_str):
            self.scheduled.append((workout_id, date_str))
            return {}

        def delete_workout(self, workout_id):
            self.deleted.append(workout_id)

    fake_watch = _FakeWorkoutGarmin()
    wid1 = push_workout_for_session(
        conn, uid, fake_watch, "lower_body", 4, lb_values, "2026-07-16",
    )
    assert wid1 == "1001", wid1
    assert fake_watch.deleted == []  # nothing to clean up on first push
    assert fake_watch.scheduled == [(1001, "2026-07-16")]
    push_row = conn.execute(
        "SELECT * FROM garmin_workout_pushes WHERE user_id = ?", (uid,),
    ).fetchone()
    assert push_row["workout_id"] == "1001"

    # Next day: yesterday's template gets deleted before the new one
    # is uploaded+scheduled -- one row per user, not one per day.
    wid2 = push_workout_for_session(
        conn, uid, fake_watch, "treadmill", 6, training.treadmill_values(6),
        "2026-07-17",
    )
    assert wid2 == "1002", wid2
    assert fake_watch.deleted == ["1001"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM garmin_workout_pushes WHERE "
        "user_id = ?", (uid,),
    ).fetchone()["n"] == 1
    push_row2 = conn.execute(
        "SELECT workout_id FROM garmin_workout_pushes WHERE user_id = ?",
        (uid,),
    ).fetchone()
    assert push_row2["workout_id"] == "1002"

    print("garmin_api.py: all checks passed (no live Garmin call made)")
