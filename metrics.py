#!/usr/bin/env python3
"""Derive a daily wellness dict from smart_sport's ingested tables.

Replaces garmin-coach's coach.fetch_wellness()/fetch_nutrition(),
which called the Garmin Connect API directly for every value. Most
signals here now come from smart_sport's own Health-Connect-sourced
database instead; HRV/training-readiness/body-battery (see
``garmin_wellness``) are the exception -- they're ingested from the
Garmin API too (ingest/garmin_api.py), just staged through the db
instead of a live call. Two fields (sleep_score, activity_load) are
NEW approximations with no Garmin equivalent at all -- see
training.py for how everything feeds the daily readiness vote.

Multi-user: every function takes ``user_id`` and scopes its queries to
that person's rows only -- this is the actual data-isolation boundary
between accounts sharing one deployment.
"""

import datetime as dt
import sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

import db
import training_load
from ingest.parse_health_connect import EXERCISE_TYPE_LABELS

# Tunable sleep-score weights/target, same "reasonable starting point,
# retune against real mornings" posture as garmin-coach's constants.
SLEEP_TARGET_HOURS = 7.5
SLEEP_MIN_HOURS = 4.0
DEEP_REM_TARGET_PCT = 40.0
ASLEEP_STAGE_TYPES = {2, 4, 5, 6}  # sleeping, light, deep, rem
DEEP_REM_STAGE_TYPES = {5, 6}
AWAKE_STAGE_TYPES = {1, 7}  # awake, awake_in_bed


def local_tz(conn: sqlite3.Connection, user_id: int) -> ZoneInfo:
    """The configured local timezone for day-boundary calculations.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.

    Returns:
        ZoneInfo: Timezone from settings (default Europe/Paris).
    """
    return ZoneInfo(db.get_setting(conn, user_id, "timezone") or "Europe/Paris")


def _local_date_of(iso_utc: str, tz: ZoneInfo) -> str:
    """Local calendar date of a UTC ISO timestamp.

    Parameters:
        iso_utc (str): ISO 8601 UTC timestamp.
        tz (ZoneInfo): Target timezone.

    Returns:
        str: ``YYYY-MM-DD`` in the given timezone.
    """
    return dt.datetime.fromisoformat(iso_utc).astimezone(tz).date().isoformat()


def score_sleep(stages: list[sqlite3.Row]) -> dict:
    """Approximate a sleep score from stage durations.

    No Health Connect / Garmin field gives this directly, so it's
    derived from three tunable components: total asleep duration vs
    target, in-bed efficiency, and deep+REM proportion.

    Parameters:
        stages (list[sqlite3.Row]): Rows with ``stage_type``,
            ``stage_start_utc``, ``stage_end_utc`` for one sleep
            session.

    Returns:
        dict: ``sleep_hours``, ``deep_rem_pct``, ``efficiency_pct``,
        ``sleep_score`` (0-100), or ``{}`` if no stage data at all.
    """
    if not stages:
        return {}

    def minutes(stage_types: set[int]) -> float:
        total = 0.0
        for row in stages:
            if row["stage_type"] not in stage_types:
                continue
            start = dt.datetime.fromisoformat(row["stage_start_utc"])
            end = dt.datetime.fromisoformat(row["stage_end_utc"])
            total += (end - start).total_seconds() / 60
        return total

    asleep_min = minutes(ASLEEP_STAGE_TYPES)
    deep_rem_min = minutes(DEEP_REM_STAGE_TYPES)
    awake_min = minutes(AWAKE_STAGE_TYPES)
    if asleep_min <= 0:
        return {}

    sleep_hours = asleep_min / 60
    duration_score = 100 * min(
        1.0,
        max(0.0, sleep_hours - SLEEP_MIN_HOURS)
        / (SLEEP_TARGET_HOURS - SLEEP_MIN_HOURS),
    )
    efficiency_pct = 100 * asleep_min / (asleep_min + awake_min or 1)
    deep_rem_pct = 100 * deep_rem_min / asleep_min
    deep_rem_score = 100 * min(1.0, deep_rem_pct / DEEP_REM_TARGET_PCT)

    sleep_score = round(
        0.5 * duration_score + 0.3 * efficiency_pct + 0.2 * deep_rem_score
    )
    return {
        "sleep_hours": round(sleep_hours, 1),
        "deep_rem_pct": round(deep_rem_pct, 1),
        "efficiency_pct": round(efficiency_pct, 1),
        "sleep_score": sleep_score,
    }


def sleep_for_date(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Sleep score for the night that ended on ``date`` (wake-up day).

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        date (str): ISO local date, the morning readiness is computed
            for.

    Returns:
        dict: ``score_sleep`` output for the longest session ending
        that local day, or ``{}`` if none.
    """
    tz = local_tz(conn, user_id)
    sessions = conn.execute(
        "SELECT uuid, start_utc, end_utc FROM sleep_sessions "
        "WHERE user_id = ?", (user_id,),
    ).fetchall()
    candidates = [
        row for row in sessions
        if _local_date_of(row["end_utc"], tz) == date
        # Corrupt HC rows (end <= start) are dropped outright: mixing
        # them into max() would compare bool vs timedelta and crash.
        and row["end_utc"] > row["start_utc"]
    ]
    if not candidates:
        return {}
    longest = max(
        candidates,
        key=lambda row: (
            dt.datetime.fromisoformat(row["end_utc"])
            - dt.datetime.fromisoformat(row["start_utc"])
        ),
    )
    stages = conn.execute(
        "SELECT stage_type, stage_start_utc, stage_end_utc "
        "FROM sleep_stages WHERE user_id = ? AND parent_uuid = ?",
        (user_id, longest["uuid"]),
    ).fetchall()
    return score_sleep(stages)


def steps_for_range(
    conn: sqlite3.Connection, user_id: int, end_date: str, days: int,
) -> dict[str, int]:
    """Daily step totals for a trailing window ending on ``end_date``.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        end_date (str): ISO local date, last day of the window.
        days (int): Window length in days (inclusive of end_date).

    Returns:
        dict[str, int]: ``{local_date: total_steps}``, missing days
        simply absent (no HC data that day).
    """
    start_date = (
        dt.date.fromisoformat(end_date) - dt.timedelta(days=days - 1)
    ).isoformat()
    rows = conn.execute(
        "SELECT local_date, SUM(count) AS total FROM steps "
        "WHERE user_id = ? AND local_date BETWEEN ? AND ? "
        "GROUP BY local_date", (user_id, start_date, end_date),
    ).fetchall()
    return {row["local_date"]: row["total"] for row in rows}


def resting_hr_for_date(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> int | None:
    """Resting heart rate reading for a specific local date.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        date (str): ISO local date.

    Returns:
        int | None: bpm, or ``None`` if no reading that day.
    """
    row = conn.execute(
        "SELECT bpm FROM resting_heart_rate WHERE user_id = ? AND "
        "local_date = ? ORDER BY time_utc DESC LIMIT 1", (user_id, date),
    ).fetchone()
    return row["bpm"] if row else None


def activity_load(
    conn: sqlite3.Connection, user_id: int, end_date: str,
) -> dict:
    """Recent training load: last 7 days vs the prior 7 days.

    NEW signal (no Garmin equivalent) feeding training.compute_status
    in place of the dropped training-readiness/body-battery vote:
    a jump in recent exercise minutes with no matching recovery is
    read the same way a red training-readiness score would be.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        end_date (str): ISO local date, last day of the window.

    Returns:
        dict: ``recent_minutes`` (last 7 days), ``previous_minutes``
        (7 days before that), ``recent_avg_rpe`` if any RPE logged.
    """
    end = dt.date.fromisoformat(end_date)
    recent_start = (end - dt.timedelta(days=6)).isoformat()
    previous_start = (end - dt.timedelta(days=13)).isoformat()
    previous_end = (end - dt.timedelta(days=7)).isoformat()

    rows = conn.execute(
        "SELECT local_date, start_utc, end_utc, rpe FROM "
        "exercise_sessions WHERE user_id = ? AND local_date BETWEEN ? "
        "AND ?", (user_id, previous_start, end_date),
    ).fetchall()

    def duration_min(row: sqlite3.Row) -> float:
        start = dt.datetime.fromisoformat(row["start_utc"])
        finish = dt.datetime.fromisoformat(row["end_utc"])
        return max(0.0, (finish - start).total_seconds() / 60)

    recent = [r for r in rows if r["local_date"] >= recent_start]
    previous = [
        r for r in rows
        if previous_start <= r["local_date"] <= previous_end
    ]
    rpes = [r["rpe"] for r in recent if r["rpe"] is not None]
    return {
        "recent_minutes": round(sum(duration_min(r) for r in recent)),
        "previous_minutes": round(
            sum(duration_min(r) for r in previous)
        ),
        "recent_avg_rpe": round(sum(rpes) / len(rpes), 1) if rpes else None,
    }


def nutrition_for_date(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> dict:
    """Sum logged nutrition for a local date.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        date (str): ISO local date.

    Returns:
        dict: Calorie/macro totals, empty if nothing logged that day
        (currently sparse -- the user just started logging).
    """
    row = conn.execute(
        "SELECT SUM(calories) AS calories, SUM(protein_g) AS protein_g, "
        "SUM(carbs_g) AS carbs_g, SUM(fat_g) AS fat_g FROM nutrition "
        "WHERE user_id = ? AND local_date = ?", (user_id, date),
    ).fetchone()
    if row["calories"] is None:
        return {}
    return {
        "calories_kcal": round(row["calories"]),
        "protein_g": round(row["protein_g"] or 0, 1),
        "carbs_g": round(row["carbs_g"] or 0, 1),
        "fat_g": round(row["fat_g"] or 0, 1),
    }


def sum_for_date(
    conn: sqlite3.Connection, user_id: int, table: str, column: str,
    date: str,
) -> Optional[float]:
    """Sum a single-value interval table's column for one local date.

    Shared by hydration/distance/floors/total-calories -- all four are
    HC interval tables with one value column, ingested the same shape.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        table (str): Table name.
        column (str): Column to sum.
        date (str): ISO local date.

    Returns:
        float | None: Sum, or ``None`` if nothing recorded that day.
    """
    row = conn.execute(
        f"SELECT SUM({column}) AS total FROM {table} WHERE user_id = ? "
        "AND local_date = ?", (user_id, date),
    ).fetchone()
    return row["total"]


def latest_body_comp(
    conn: sqlite3.Connection, user_id: int, on_or_before: str,
) -> dict:
    """Most recent weight/body-fat readings up to a date.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        on_or_before (str): ISO local date ceiling.

    Returns:
        dict: ``weight_kg``, ``body_fat_pct`` if known.
    """
    result: dict = {}
    weight = conn.execute(
        "SELECT kg FROM weight WHERE user_id = ? AND local_date <= ? "
        "ORDER BY local_date DESC LIMIT 1", (user_id, on_or_before),
    ).fetchone()
    if weight:
        result["weight_kg"] = round(weight["kg"], 1)
    fat = conn.execute(
        "SELECT percentage FROM body_fat WHERE user_id = ? AND "
        "local_date <= ? ORDER BY local_date DESC LIMIT 1",
        (user_id, on_or_before),
    ).fetchone()
    if fat:
        result["body_fat_pct"] = round(fat["percentage"], 1)
    return result


def garmin_wellness(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Garmin-API-only signals for a date: HRV, readiness, body battery.

    No Health Connect equivalent for any of these (see
    ingest/garmin_api.py) -- absent entirely until the Garmin
    ingestion step has run for this date.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        date (str): ISO local date.

    Returns:
        dict: ``hrv_last_night_avg``, ``hrv_status``,
        ``training_readiness_score``, ``training_readiness_level``,
        ``body_battery_charged``, ``body_battery_drained``,
        ``body_battery_highest``, ``body_battery_lowest``,
        ``stress_avg_level``, ``stress_max_level``,
        ``menstrual_cycle_phase`` (only present if the user opted
        into tracking it) -- keys with no data that day are simply
        absent.
    """
    result: dict = {}
    hrv = conn.execute(
        "SELECT last_night_avg, status FROM garmin_hrv WHERE "
        "user_id = ? AND local_date = ?", (user_id, date),
    ).fetchone()
    if hrv:
        result["hrv_last_night_avg"] = hrv["last_night_avg"]
        result["hrv_status"] = hrv["status"]
    readiness = conn.execute(
        "SELECT score, level FROM garmin_training_readiness WHERE "
        "user_id = ? AND local_date = ?", (user_id, date),
    ).fetchone()
    if readiness:
        result["training_readiness_score"] = readiness["score"]
        result["training_readiness_level"] = readiness["level"]
    battery = conn.execute(
        "SELECT charged, drained, highest, lowest FROM "
        "garmin_body_battery WHERE user_id = ? AND local_date = ?",
        (user_id, date),
    ).fetchone()
    if battery:
        result["body_battery_charged"] = battery["charged"]
        result["body_battery_drained"] = battery["drained"]
        result["body_battery_highest"] = battery["highest"]
        result["body_battery_lowest"] = battery["lowest"]
    stress = conn.execute(
        "SELECT avg_level, max_level FROM garmin_stress WHERE "
        "user_id = ? AND local_date = ?", (user_id, date),
    ).fetchone()
    if stress:
        result["stress_avg_level"] = stress["avg_level"]
        result["stress_max_level"] = stress["max_level"]
    cycle = conn.execute(
        "SELECT phase FROM garmin_menstrual_cycle WHERE user_id = ? "
        "AND local_date = ?", (user_id, date),
    ).fetchone()
    if cycle and cycle["phase"]:
        result["menstrual_cycle_phase"] = cycle["phase"]
    return result


def daily_wellness(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Full daily wellness dict for a date, in garmin-coach's shape.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today, in the run's timezone).

    Returns:
        dict: Merged sleep/steps/RHR/activity-load/body-comp signals,
        used by training.compute_status and the LLM payload.
    """
    steps_window = steps_for_range(conn, user_id, date, days=7)
    wellness: dict = {
        **sleep_for_date(conn, user_id, date),
        "resting_hr": resting_hr_for_date(conn, user_id, date),
        "steps_today": steps_window.get(date),
        "steps_last_7_days": [
            steps_window.get(
                (
                    dt.date.fromisoformat(date) - dt.timedelta(days=n)
                ).isoformat()
            )
            for n in range(6, -1, -1)
        ],
        "step_goal": int(
            db.get_setting(conn, user_id, "step_goal") or 0
        ) or None,
        **activity_load(conn, user_id, date),
        **latest_body_comp(conn, user_id, date),
        **garmin_wellness(conn, user_id, date),
    }
    hydration = sum_for_date(conn, user_id, "hydration", "volume_ml", date)
    distance = sum_for_date(conn, user_id, "distance", "meters", date)
    floors = sum_for_date(conn, user_id, "floors_climbed", "floors", date)
    total_cal = sum_for_date(
        conn, user_id, "total_calories_burned", "kcal", date,
    )
    wellness["hydration_ml_today"] = (
        round(hydration) if hydration is not None else None
    )
    wellness["distance_km_today"] = (
        round(distance / 1000, 1) if distance is not None else None
    )
    wellness["floors_climbed_today"] = (
        round(floors) if floors is not None else None
    )
    wellness["total_calories_burned_today"] = (
        round(total_cal) if total_cal is not None else None
    )
    hydration_target = db.get_setting(
        conn, user_id, "hydration_target_ml_per_kg",
    )
    if hydration_target and wellness.get("weight_kg"):
        wellness["hydration_target_ml"] = round(
            wellness["weight_kg"] * float(hydration_target)
        )
    return {k: v for k, v in wellness.items() if v is not None}


def _session_duration_min(row: sqlite3.Row) -> int:
    """Whole minutes between a session row's start/end timestamps."""
    start = dt.datetime.fromisoformat(row["start_utc"])
    end = dt.datetime.fromisoformat(row["end_utc"])
    return max(0, round((end - start).total_seconds() / 60))


# Standard 5-zone %-of-max-HR model (Z1 <60%, Z2 60-70%, ... Z5 >=90%).
HR_ZONE_BOUNDS_PCT = (0.6, 0.7, 0.8, 0.9)


def estimated_max_hr(conn: sqlite3.Connection, user_id: int) -> int | None:
    """Age-based max-HR estimate (220-age), or None if age isn't set.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.

    Returns:
        int | None: Estimated max HR, or ``None`` (age_years unset).
    """
    age = db.get_setting(conn, user_id, "age_years")
    try:
        return 220 - int(age)
    except (TypeError, ValueError):
        return None


def hr_zone_pct(
    conn: sqlite3.Connection, user_id: int, uuid: str, max_hr: int,
) -> list[float]:
    """Time-in-zone breakdown for one exercise session's HR samples.

    ponytail: each sample's weight is the time gap to the NEXT sample
    (last sample reuses the previous gap) -- correct for roughly-even
    sampling (this project's source data), a coarser approximation if
    the watch samples very unevenly.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        uuid (str): exercise_sessions uuid.
        max_hr (int): Estimated or known max HR (see
            ``estimated_max_hr``).

    Returns:
        list[float]: 5 percentages (Z1..Z5) summing to ~100, or an
        empty list if this session has no HR samples.
    """
    rows = conn.execute(
        "SELECT epoch_utc, bpm FROM exercise_hr_samples WHERE "
        "user_id = ? AND exercise_uuid = ? ORDER BY epoch_utc",
        (user_id, uuid),
    ).fetchall()
    if not rows or not max_hr:
        return []
    times = [dt.datetime.fromisoformat(r["epoch_utc"]) for r in rows]
    zone_seconds = [0.0] * 5
    for i, row in enumerate(rows):
        if i + 1 < len(rows):
            gap = (times[i + 1] - times[i]).total_seconds()
        elif i > 0:
            gap = (times[i] - times[i - 1]).total_seconds()
        else:
            gap = 0.0
        ratio = row["bpm"] / max_hr
        zone = sum(1 for bound in HR_ZONE_BOUNDS_PCT if ratio >= bound)
        zone_seconds[zone] += max(gap, 0.0)
    total = sum(zone_seconds)
    if total <= 0:
        return []
    return [round(100 * s / total, 1) for s in zone_seconds]


def all_route_polylines(
    conn: sqlite3.Connection, user_id: int,
) -> dict[str, list[list[float]]]:
    """Every exercise session's GPS track, across all history.

    Keyed by session uuid (not just a bare list) so callers can join
    each route back to its session's details -- e.g. clicking a route
    on the Sessions map to show that session's info.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.

    Returns:
        dict[str, list[[lat, lon]]]: ``{exercise_uuid: points}`` for
        every session with a GPS track, points in chronological
        order. Sessions with none (the common case for this
        project's bodyweight-circuit types) are simply absent.
    """
    rows = conn.execute(
        "SELECT exercise_uuid, latitude, longitude FROM "
        "exercise_route_points WHERE user_id = ? ORDER BY "
        "exercise_uuid, epoch_utc", (user_id,),
    ).fetchall()
    polylines: dict[str, list[list[float]]] = {}
    for row in rows:
        polylines.setdefault(row["exercise_uuid"], []).append(
            [row["latitude"], row["longitude"]]
        )
    return polylines


def history_snapshot(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> dict:
    """Last-7-days context for the LLM payload.

    Surfaces the per-activity Garmin detail (HR, effort, duration,
    calories) that the daily aggregates flatten away, plus the
    status history and fitness/fatigue trend the coach needs to
    speak to consistency instead of just today's snapshot.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today).

    Returns:
        dict: ``activities_last_7_days`` (date, label, duration_min,
        rpe, avg_hr, max_hr, kcal -- absent keys mean no data),
        ``statuses_last_7_days`` (date, status),
        ``adherence_last_7_days`` (date, planned, done, duration_min
        -- ends yesterday: today's session hasn't happened yet), and
        ``training_load`` ({date, ctl, atl, tsb} or ``{}``).
    """
    start = (
        dt.date.fromisoformat(date) - dt.timedelta(days=6)
    ).isoformat()
    yesterday = (
        dt.date.fromisoformat(date) - dt.timedelta(days=1)
    ).isoformat()

    sessions = conn.execute(
        "SELECT uuid, local_date, start_utc, end_utc, exercise_type, "
        "label_override, rpe FROM exercise_sessions WHERE user_id = ? "
        "AND local_date BETWEEN ? AND ? ORDER BY start_utc",
        (user_id, start, date),
    ).fetchall()

    hr_by_uuid: dict[str, sqlite3.Row] = {}
    if sessions:
        marks = ",".join("?" * len(sessions))
        hr_by_uuid = {
            row["exercise_uuid"]: row
            for row in conn.execute(
                f"SELECT exercise_uuid, AVG(bpm) AS avg_hr, "
                f"MAX(bpm) AS max_hr FROM exercise_hr_samples "
                f"WHERE user_id = ? AND exercise_uuid IN ({marks}) "
                f"GROUP BY exercise_uuid",
                (user_id, *[s["uuid"] for s in sessions]),
            ).fetchall()
        }

    activities = []
    for row in sessions:
        item: dict = {
            "date": row["local_date"],
            "label": row["label_override"] or EXERCISE_TYPE_LABELS.get(
                row["exercise_type"], "other"
            ),
            "duration_min": _session_duration_min(row),
        }
        if row["rpe"] is not None:
            item["rpe"] = row["rpe"]
        hr = hr_by_uuid.get(row["uuid"])
        if hr:
            item["avg_hr"] = round(hr["avg_hr"])
            item["max_hr"] = hr["max_hr"]
        kcal = conn.execute(
            "SELECT SUM(kcal) AS kcal FROM active_calories WHERE "
            "user_id = ? AND start_utc < ? AND end_utc > ?",
            (user_id, row["end_utc"], row["start_utc"]),
        ).fetchone()["kcal"]
        if kcal is not None:
            item["kcal"] = round(kcal)
        activities.append(item)

    # Latest coach_log row per date wins (same dedup posture as
    # achievements' streak logic -- not imported: achievements
    # imports metrics, importing back would cycle).
    planned = conn.execute(
        "SELECT local_date, status, session_type FROM coach_log "
        "WHERE id IN (SELECT MAX(id) FROM coach_log WHERE user_id = ? "
        "AND local_date BETWEEN ? AND ? GROUP BY local_date) "
        "ORDER BY local_date", (user_id, start, date),
    ).fetchall()

    statuses = [
        {"date": row["local_date"], "status": row["status"]}
        for row in planned if row["status"] is not None
    ]

    done_min: dict[str, int] = {}
    for row in sessions:
        done_min[row["local_date"]] = (
            done_min.get(row["local_date"], 0)
            + _session_duration_min(row)
        )
    adherence = [
        {
            "date": row["local_date"],
            "planned": row["session_type"],
            "done": row["local_date"] in done_min,
            "duration_min": done_min.get(row["local_date"], 0),
        }
        for row in planned
        if row["session_type"] is not None
        and row["local_date"] <= yesterday
    ]

    load = training_load.latest_training_load(conn, user_id)
    return {
        "activities_last_7_days": activities,
        "statuses_last_7_days": statuses,
        "adherence_last_7_days": adherence,
        "training_load": {
            "date": load["local_date"], "ctl": round(load["ctl"], 1),
            "atl": round(load["atl"], 1), "tsb": round(load["tsb"], 1),
        } if load else {},
    }


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "smart_sport.db"
    conn = db.connect(tmp)
    db.init_db(conn)
    uid = db.create_user(conn, "test", "password1234")
    other_uid = db.create_user(conn, "other", "password1234")

    conn.execute(
        "INSERT INTO sleep_sessions VALUES "
        "('s1', ?, '2026-07-12T22:00:00+00:00', "
        "'2026-07-13T06:00:00+00:00', '2026-07-12', NULL, NULL)", (uid,),
    )
    stages = [
        ("s1", uid, "2026-07-12T22:00:00+00:00", "2026-07-12T22:30:00+00:00", 1),
        ("s1", uid, "2026-07-12T22:30:00+00:00", "2026-07-13T00:30:00+00:00", 4),
        ("s1", uid, "2026-07-13T00:30:00+00:00", "2026-07-13T02:00:00+00:00", 5),
        ("s1", uid, "2026-07-13T02:00:00+00:00", "2026-07-13T05:30:00+00:00", 6),
    ]
    conn.executemany(
        "INSERT INTO sleep_stages VALUES (?, ?, ?, ?, ?)", stages
    )
    conn.execute(
        "INSERT INTO resting_heart_rate VALUES "
        "('r1', ?, '2026-07-13T06:00:00+00:00', '2026-07-13', 54)", (uid,),
    )
    conn.execute(
        "INSERT INTO steps VALUES "
        "('st1', ?, '2026-07-13T08:00:00+00:00', "
        "'2026-07-13T08:10:00+00:00', '2026-07-13', 4000)", (uid,),
    )
    # Another user's data, same date -- must never leak into uid's results.
    conn.execute(
        "INSERT INTO steps VALUES "
        "('st1-other', ?, '2026-07-13T08:00:00+00:00', "
        "'2026-07-13T08:10:00+00:00', '2026-07-13', 99999)", (other_uid,),
    )
    conn.execute(
        "INSERT INTO exercise_sessions VALUES ('e1', ?, "
        "'2026-07-12T18:00:00+00:00', '2026-07-12T18:30:00+00:00', "
        "'2026-07-12', 70, 'Muscu', NULL, 6.0, NULL)", (uid,),
    )
    conn.execute(
        "INSERT INTO hydration VALUES ('h1', ?, "
        "'2026-07-13T08:00:00+00:00', '2026-07-13T08:01:00+00:00', "
        "'2026-07-13', 500)", (uid,),
    )
    conn.execute(
        "INSERT INTO total_calories_burned VALUES ('tc1', ?, "
        "'2026-07-13T00:00:00+00:00', '2026-07-13T23:59:00+00:00', "
        "'2026-07-13', 2400)", (uid,),
    )
    conn.execute(
        "INSERT INTO weight VALUES ('w1', ?, '2026-07-13T07:00:00+00:00', "
        "'2026-07-13', 80.0)", (uid,),
    )
    conn.execute(
        "INSERT INTO garmin_hrv VALUES (?, '2026-07-13', 55.0, 58.0, "
        "'BALANCED')", (uid,),
    )
    conn.execute(
        "INSERT INTO garmin_training_readiness VALUES (?, '2026-07-13', "
        "62, 'MODERATE', 'note')", (uid,),
    )
    conn.execute(
        "INSERT INTO garmin_body_battery VALUES (?, '2026-07-13', 80, "
        "45, 90, 30)", (uid,),
    )
    conn.execute(
        "INSERT INTO garmin_stress VALUES (?, '2026-07-13', 32, 68)",
        (uid,),
    )
    conn.execute(
        "INSERT INTO garmin_menstrual_cycle VALUES (?, '2026-07-13', "
        "'LUTEAL')", (uid,),
    )
    conn.commit()

    # 2026-07-13 06:00 UTC = 08:00 Europe/Paris (CEST), so this
    # session's local wake-up date is 2026-07-13, not 07-12.
    sleep = sleep_for_date(conn, uid, "2026-07-13")
    assert sleep["sleep_hours"] == 7.0, sleep
    assert 0 < sleep["sleep_score"] <= 100

    assert resting_hr_for_date(conn, uid, "2026-07-13") == 54
    assert resting_hr_for_date(conn, uid, "2026-07-01") is None

    load = activity_load(conn, uid, "2026-07-13")
    assert load["recent_minutes"] == 30
    assert load["recent_avg_rpe"] == 6.0

    wellness = daily_wellness(conn, uid, "2026-07-13")
    assert wellness["sleep_score"] == sleep["sleep_score"]
    assert wellness["resting_hr"] == 54
    assert wellness["steps_today"] == 4000  # not 99999 from the other user
    assert wellness["hydration_ml_today"] == 500
    assert wellness["total_calories_burned_today"] == 2400
    assert wellness["hydration_target_ml"] == round(80.0 * 35)
    assert "distance_km_today" not in wellness  # none logged -> omitted
    assert nutrition_for_date(conn, uid, "2026-07-13") == {}

    # Garmin-API-only signals (no HC equivalent): merged into wellness.
    assert wellness["hrv_status"] == "BALANCED"
    assert wellness["hrv_last_night_avg"] == 55.0
    assert wellness["training_readiness_score"] == 62
    assert wellness["body_battery_charged"] == 80
    assert wellness["body_battery_lowest"] == 30
    assert wellness["stress_avg_level"] == 32
    assert wellness["stress_max_level"] == 68
    assert wellness["menstrual_cycle_phase"] == "LUTEAL"
    assert garmin_wellness(conn, uid, "2026-07-01") == {}  # no data that day

    # Data isolation: the other user sees none of uid's data.
    other_wellness = daily_wellness(conn, other_uid, "2026-07-13")
    assert other_wellness.get("steps_today") == 99999
    assert "sleep_score" not in other_wellness
    assert "hrv_status" not in other_wellness
    assert "menstrual_cycle_phase" not in other_wellness

    # --- history_snapshot ---
    # HR samples + overlapping active-calories for e1 (07-12 18:00-30).
    conn.executemany(
        "INSERT INTO exercise_hr_samples VALUES (?, ?, ?, ?)",
        [("e1", uid, "2026-07-12T18:05:00+00:00", 120),
         ("e1", uid, "2026-07-12T18:15:00+00:00", 140),
         ("e1", uid, "2026-07-12T18:25:00+00:00", 160)],
    )
    conn.execute(
        "INSERT INTO active_calories VALUES ('ac1', ?, "
        "'2026-07-12T18:00:00+00:00', '2026-07-12T18:30:00+00:00', "
        "'2026-07-12', 250)", (uid,),
    )
    # Non-overlapping burn the same day must NOT count for e1.
    conn.execute(
        "INSERT INTO active_calories VALUES ('ac2', ?, "
        "'2026-07-12T10:00:00+00:00', '2026-07-12T10:30:00+00:00', "
        "'2026-07-12', 99)", (uid,),
    )
    # Session with a manual label, older than the 7-day window.
    conn.execute(
        "INSERT INTO exercise_sessions VALUES ('e-old', ?, "
        "'2026-07-01T18:00:00+00:00', '2026-07-01T18:30:00+00:00', "
        "'2026-07-01', 70, NULL, NULL, NULL, 'tapis')", (uid,),
    )
    # Other user's session in-window -- must never leak.
    conn.execute(
        "INSERT INTO exercise_sessions VALUES ('e-other', ?, "
        "'2026-07-12T18:00:00+00:00', '2026-07-12T19:00:00+00:00', "
        "'2026-07-12', 70, NULL, NULL, NULL, NULL)", (other_uid,),
    )
    # coach_log: planned+done (07-12, has e1), planned+skipped (07-11).
    conn.executemany(
        "INSERT INTO coach_log (user_id, created_at, local_date, "
        "status, session_type, level, message) VALUES (?, ?, ?, ?, ?, "
        "?, ?)",
        [(uid, "2026-07-11T06:00:00+00:00", "2026-07-11", "green",
          "treadmill", 4, "m"),
         (uid, "2026-07-12T06:00:00+00:00", "2026-07-12", "yellow",
          "upper_body", 3, "m")],
    )
    conn.execute(
        "INSERT INTO training_load VALUES (?, '2026-07-12', 30.0, "
        "10.123, 20.456, -10.333)", (uid,),
    )
    conn.commit()

    # HR zone breakdown for e1's samples (120/140/160 bpm, 600s gaps
    # each): with max_hr 190, zones land Z2/Z3/Z4 respectively.
    assert estimated_max_hr(conn, uid) is None  # age_years unset
    db.set_setting(conn, uid, "age_years", "30")
    assert estimated_max_hr(conn, uid) == 190
    zones = hr_zone_pct(conn, uid, "e1", 190)
    assert zones == [0.0, 33.3, 33.3, 33.3, 0.0], zones
    assert hr_zone_pct(conn, uid, "no-such-uuid", 190) == []

    # All-history route polylines: e1 has a track, e-other (other
    # user) must never leak into uid's polylines.
    conn.executemany(
        "INSERT INTO exercise_route_points VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("e1", uid, "2026-07-12T18:05:00+00:00", 45.75, 4.85, 200.0),
            ("e1", uid, "2026-07-12T18:15:00+00:00", 45.76, 4.86, None),
            ("e-old", uid, "2026-07-01T18:05:00+00:00", 45.70, 4.80, None),
            ("e-other", other_uid, "2026-07-12T18:05:00+00:00", 1.0, 1.0, None),
        ],
    )
    conn.commit()
    polylines = all_route_polylines(conn, uid)
    assert len(polylines) == 2, polylines  # e1 + e-old, e-other excluded
    assert polylines["e1"] == [[45.75, 4.85], [45.76, 4.86]], polylines
    assert all_route_polylines(conn, other_uid) == {"e-other": [[1.0, 1.0]]}

    snap = history_snapshot(conn, uid, "2026-07-13")
    acts = snap["activities_last_7_days"]
    assert len(acts) == 1, acts  # e-old out of window, e-other scoped
    assert acts[0]["duration_min"] == 30 and acts[0]["rpe"] == 6.0
    assert acts[0]["avg_hr"] == 140 and acts[0]["max_hr"] == 160
    assert acts[0]["kcal"] == 250, acts  # ac2 doesn't overlap e1
    assert snap["statuses_last_7_days"] == [
        {"date": "2026-07-11", "status": "green"},
        {"date": "2026-07-12", "status": "yellow"},
    ]
    adherence = snap["adherence_last_7_days"]
    assert adherence == [
        {"date": "2026-07-11", "planned": "treadmill", "done": False,
         "duration_min": 0},
        {"date": "2026-07-12", "planned": "upper_body", "done": True,
         "duration_min": 30},
    ], adherence
    assert snap["training_load"] == {
        "date": "2026-07-12", "ctl": 10.1, "atl": 20.5, "tsb": -10.3,
    }
    # label_override wins when the old session is in range.
    old_snap = history_snapshot(conn, uid, "2026-07-02")
    assert old_snap["activities_last_7_days"][0]["label"] == "tapis"
    # Empty DB degrades to empty containers.
    empty = history_snapshot(conn, other_uid, "2026-01-01")
    assert empty == {
        "activities_last_7_days": [], "statuses_last_7_days": [],
        "adherence_last_7_days": [], "training_load": {},
    }, empty

    print("metrics.py: all checks passed")
