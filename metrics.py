#!/usr/bin/env python3
"""Derive a daily wellness dict from smart_sport's ingested tables.

Replaces garmin-coach's coach.fetch_wellness()/fetch_nutrition(),
which called the Garmin Connect API directly. Every value here comes
from smart_sport's own Health-Connect-sourced database instead, and
two fields (sleep_score, activity_load) are NEW approximations that
have no Garmin equivalent to copy -- see training.py for how they
feed the daily readiness vote.

Multi-user: every function takes ``user_id`` and scopes its queries to
that person's rows only -- this is the actual data-isolation boundary
between accounts sharing one deployment.
"""

import datetime as dt
import sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

import db

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

    # Data isolation: the other user sees none of uid's data.
    other_wellness = daily_wellness(conn, other_uid, "2026-07-13")
    assert other_wellness.get("steps_today") == 99999
    assert "sleep_score" not in other_wellness

    print("metrics.py: all checks passed")
