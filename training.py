#!/usr/bin/env python3
"""Daily-adaptive training levels.

Ported from garmin-coach/training.py: same per-session-type level
(0-10), same green/yellow/red daily status, same level -> concrete
workout numbers. Changes from the original:

1. State lives in smart_sport's db (``levels`` table) instead of a
   flat JSON file.
2. Resting-HR baseline is computed directly from smart_sport's own
   ingested history (no more manual rolling-window bookkeeping -- the
   full history is already in the db), plus a NEW activity-load vote
   with no garmin-coach equivalent.
3. HRV and training-readiness are back as real votes (they were
   dropped in the Health-Connect-only era -- see metrics.py's
   ``garmin_wellness`` -- now pulled straight from the Garmin API via
   ingest/garmin_api.py). Body battery and stress have no vote: body
   battery is a running energy gauge, not a morning score, and a
   separate stress vote would double-count what training-readiness's
   own aggregate already factors in -- both are dashboard/LLM context
   only (metrics.daily_wellness).
"""

import datetime as dt
import json
import sqlite3
from typing import Optional

import db

LEVEL_MIN, LEVEL_MAX = 0, 10

SESSION_LABEL_FR = {
    "treadmill": "Tapis",
    "lower_body": "Muscu bas du corps",
    "upper_body": "Muscu haut du corps + gainage",
    "calisthenics": "Calisthenie",
}

STATUS_LABEL_FR = {
    "green": "vert - progression",
    "yellow": "jaune - maintien",
    "red": "rouge - seance allegee",
}

# Tunable thresholds -- reasonable starting points, same posture as
# garmin-coach's original constants: retune against real mornings.
SLEEP_SCORE_GOOD, SLEEP_SCORE_POOR = 75, 60
RHR_SPIKE_RED_MIN = 5  # bpm above rolling personal baseline
RHR_BASELINE_DAYS = 14
ACTIVITY_LOAD_SPIKE_RATIO = 1.5  # recent-7d vs previous-7d minutes
ACTIVITY_LOAD_HIGH_RPE = 7.0
TRAINING_READINESS_GOOD, TRAINING_READINESS_POOR = 75, 50  # 0-100 scale
# TSB (yesterday's CTL - ATL): very negative means fatigue has been
# accumulating for a while -- unlike a single red day, TSB is already
# a smoothed signal (42d/7d windows), so one critical reading is
# trustworthy enough to force a deload immediately, no streak needed.
TSB_DELOAD_THRESHOLD = -20.0


def get_level(
    conn: sqlite3.Connection, user_id: int, session_type: str,
) -> int:
    """Current level for a session type (0 if never set).

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        session_type (str): One of ``SESSION_LABEL_FR``'s
            values.

    Returns:
        int: Current level.
    """
    row = conn.execute(
        "SELECT level FROM levels WHERE user_id = ? AND session_type = ?",
        (user_id, session_type),
    ).fetchone()
    return row["level"] if row else 0


def set_level(
    conn: sqlite3.Connection, user_id: int, session_type: str, level: int,
) -> None:
    """Persist a session type's new level.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        session_type (str): Session type key.
        level (int): New level.
    """
    conn.execute(
        "INSERT INTO levels (user_id, session_type, level) VALUES "
        "(?, ?, ?) ON CONFLICT(user_id, session_type) DO UPDATE SET "
        "level = excluded.level",
        (user_id, session_type, level),
    )
    conn.commit()


def get_red_streak(
    conn: sqlite3.Connection, user_id: int, session_type: str,
) -> int:
    """Current consecutive-red count for a session type (0 if unset)."""
    row = conn.execute(
        "SELECT red_streak FROM levels WHERE user_id = ? AND "
        "session_type = ?", (user_id, session_type),
    ).fetchone()
    return row["red_streak"] if row else 0


def set_red_streak(
    conn: sqlite3.Connection, user_id: int, session_type: str, streak: int,
) -> None:
    """Persist a session type's consecutive-red count."""
    conn.execute(
        "INSERT INTO levels (user_id, session_type, level, red_streak) "
        "VALUES (?, ?, 0, ?) ON CONFLICT(user_id, session_type) DO "
        "UPDATE SET red_streak = excluded.red_streak",
        (user_id, session_type, streak),
    )
    conn.commit()


def get_deload_until(
    conn: sqlite3.Connection, user_id: int, session_type: str,
) -> Optional[str]:
    """ISO date a session type's active deload window ends, if any."""
    row = conn.execute(
        "SELECT deload_until FROM levels WHERE user_id = ? AND "
        "session_type = ?", (user_id, session_type),
    ).fetchone()
    return row["deload_until"] if row else None


def set_deload_until(
    conn: sqlite3.Connection, user_id: int, session_type: str,
    date: Optional[str],
) -> None:
    """Persist (or clear, with ``None``) a session type's deload window."""
    conn.execute(
        "INSERT INTO levels (user_id, session_type, level, deload_until) "
        "VALUES (?, ?, 0, ?) ON CONFLICT(user_id, session_type) DO "
        "UPDATE SET deload_until = excluded.deload_until",
        (user_id, session_type, date),
    )
    conn.commit()


# Deload guardrail: the daily +-1 adjustment has no memory beyond
# yesterday, so a bad stretch can grind on indefinitely one small step
# at a time. 3 reds in a row instead forces a bigger cut and a
# no-increase window, resetting the streak.
RED_STREAK_THRESHOLD = 3
DELOAD_DAYS = 7
DELOAD_LEVEL_CUT = 2


def _trigger_deload(
    conn: sqlite3.Connection, user_id: int, session_type: str, level: int,
    date: str, trigger: str,
) -> dict:
    """Force the level cut + deload window, shared by both triggers.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        session_type (str): Session type key.
        level (int): Current level, before the cut.
        date (str): ISO local date (today) -- deload window start.
        trigger (str): ``"red_streak"`` or ``"tsb"``, logged to
            ``deload_events`` for the Progress page's history.

    Returns:
        dict: ``level``, ``in_deload=True``, ``deload_triggered=True``,
        ``trigger``.
    """
    new_level = max(level - DELOAD_LEVEL_CUT, LEVEL_MIN)
    ends_at = (
        dt.date.fromisoformat(date) + dt.timedelta(days=DELOAD_DAYS)
    ).isoformat()
    set_level(conn, user_id, session_type, new_level)
    set_red_streak(conn, user_id, session_type, 0)
    set_deload_until(conn, user_id, session_type, ends_at)
    conn.execute(
        "INSERT INTO deload_events (user_id, session_type, triggered_at, "
        "ends_at, trigger) VALUES (?, ?, ?, ?, ?)",
        (user_id, session_type, date, ends_at, trigger),
    )
    conn.commit()
    return {
        "level": new_level, "in_deload": True, "deload_triggered": True,
        "trigger": trigger,
    }


def apply_deload_guardrail(
    conn: sqlite3.Connection, user_id: int, session_type: str, status: str,
    date: str, tsb: Optional[float] = None,
) -> dict:
    """Adjust today's level, applying the deload guardrail on top of
    the normal +-1 rule.

    Two independent triggers force a deload: 3 reds in a row (the
    original guardrail), or TSB dropping below
    ``TSB_DELOAD_THRESHOLD`` -- the latter fires on a single reading
    (no streak needed), since TSB is already a smoothed 42d/7d signal
    that can miss the red-streak counter entirely (e.g. red/yellow/
    red never reaches 3 in a row while fatigue keeps climbing).

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        session_type (str): Session type key.
        status (str): Today's ``compute_status`` result.
        date (str): ISO local date (today).
        tsb (float | None): Yesterday's Training Stress Balance
            (``training_load.latest_training_load``), if computed yet.

    Returns:
        dict: ``level`` (new level, already persisted),
        ``in_deload`` (a deload window is active today),
        ``deload_triggered`` (this call is what triggered it), and
        ``trigger`` (``"red_streak"`` or ``"tsb"``) only present when
        ``deload_triggered`` is true.
    """
    level = get_level(conn, user_id, session_type)
    deload_until = get_deload_until(conn, user_id, session_type)
    in_deload = deload_until is not None and date <= deload_until

    if (
        not in_deload and tsb is not None
        and tsb <= TSB_DELOAD_THRESHOLD
    ):
        return _trigger_deload(conn, user_id, session_type, level, date, "tsb")

    if status == "red":
        streak = get_red_streak(conn, user_id, session_type) + 1
        if streak >= RED_STREAK_THRESHOLD:
            return _trigger_deload(
                conn, user_id, session_type, level, date, "red_streak",
            )
        set_red_streak(conn, user_id, session_type, streak)
        new_level = adjust_level(level, status)
        set_level(conn, user_id, session_type, new_level)
        return {
            "level": new_level, "in_deload": in_deload,
            "deload_triggered": False,
        }

    set_red_streak(conn, user_id, session_type, 0)
    if in_deload:
        # Hold the level through the deload window regardless of a
        # green/yellow day -- the point is a forced lighter week, not
        # a one-day pause.
        return {
            "level": level, "in_deload": True, "deload_triggered": False,
        }
    if deload_until is not None:
        # Window just ended: clear it so next time starts fresh.
        set_deload_until(conn, user_id, session_type, None)
    new_level = adjust_level(level, status)
    set_level(conn, user_id, session_type, new_level)
    return {
        "level": new_level, "in_deload": False, "deload_triggered": False,
    }


def rhr_baseline(
    conn: sqlite3.Connection, user_id: int, date: str,
    days: int = RHR_BASELINE_DAYS,
) -> Optional[float]:
    """Mean resting HR over the ``days`` before ``date``.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today), excluded from the window.
        days (int): Window length.

    Returns:
        float | None: Rolling average, or ``None`` if fewer than 3
        readings are on record in that window.
    """
    start = (
        dt.date.fromisoformat(date) - dt.timedelta(days=days)
    ).isoformat()
    end = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT bpm FROM resting_heart_rate WHERE user_id = ? AND "
        "local_date BETWEEN ? AND ?", (user_id, start, end),
    ).fetchall()
    if len(rows) < 3:
        return None
    return sum(r["bpm"] for r in rows) / len(rows)


def _sleep_vote(wellness: dict) -> Optional[str]:
    score = wellness.get("sleep_score")
    if score is None:
        return None
    if score >= SLEEP_SCORE_GOOD:
        return "green"
    if score < SLEEP_SCORE_POOR:
        return "red"
    return "yellow"


def _activity_load_vote(wellness: dict) -> Optional[str]:
    recent = wellness.get("recent_minutes")
    previous = wellness.get("previous_minutes")
    if recent is None or previous is None:
        return None
    high_rpe = (wellness.get("recent_avg_rpe") or 0) >= ACTIVITY_LOAD_HIGH_RPE
    if previous == 0:
        return "yellow" if recent > 0 and high_rpe else None
    ratio = recent / previous
    if ratio >= ACTIVITY_LOAD_SPIKE_RATIO:
        return "red" if high_rpe else "yellow"
    return "green"


def _resting_hr_vote(
    wellness: dict, baseline_rhr: Optional[float],
) -> Optional[str]:
    resting_hr = wellness.get("resting_hr")
    if resting_hr is None or baseline_rhr is None:
        return None
    spike = resting_hr - baseline_rhr
    return "red" if spike >= RHR_SPIKE_RED_MIN else "green"


_HRV_STATUS_VOTE = {"BALANCED": "green", "UNBALANCED": "yellow", "LOW": "red"}


def _hrv_vote(wellness: dict) -> Optional[str]:
    return _HRV_STATUS_VOTE.get(wellness.get("hrv_status"))


def _training_readiness_vote(wellness: dict) -> Optional[str]:
    score = wellness.get("training_readiness_score")
    if score is None:
        return None
    if score >= TRAINING_READINESS_GOOD:
        return "green"
    if score < TRAINING_READINESS_POOR:
        return "red"
    return "yellow"


def compute_status(wellness: dict, baseline_rhr: Optional[float]) -> str:
    """Combine wellness signals into a green/yellow/red daily status.

    Parameters:
        wellness (dict): Today's wellness metrics (as produced by
            ``metrics.daily_wellness``).
        baseline_rhr (float | None): Rolling personal resting-HR
            baseline (``training.rhr_baseline``).

    Returns:
        str: ``"green"``, ``"yellow"``, or ``"red"``. No data at all
        falls back to ``"yellow"`` (maintain level). Any red signal
        wins over green/yellow; green requires all available votes
        to be green.
    """
    votes = [
        vote for vote in (
            _sleep_vote(wellness),
            _activity_load_vote(wellness),
            _resting_hr_vote(wellness, baseline_rhr),
            _hrv_vote(wellness),
            _training_readiness_vote(wellness),
        )
        if vote is not None
    ]
    if not votes:
        return "yellow"
    if "red" in votes:
        return "red"
    if all(v == "green" for v in votes):
        return "green"
    return "yellow"


def adjust_level(
    level: int, status: str, lo: int = LEVEL_MIN, hi: int = LEVEL_MAX,
) -> int:
    """Apply the day's status to a session type's current level.

    Parameters:
        level (int): Current level for the session type.
        status (str): ``"green"``, ``"yellow"``, or ``"red"``.
        lo (int): Floor.
        hi (int): Ceiling.

    Returns:
        int: New level. Green +1 (capped), yellow unchanged,
        red -1 (floored).
    """
    if status == "green":
        return min(level + 1, hi)
    if status == "red":
        return max(level - 1, lo)
    return level


def schedule_for_user(
    conn: sqlite3.Connection, user_id: int,
) -> dict[int, dict]:
    """The user's weekly plan: weekday (Monday=0) -> session template.

    Each template carries ``session_type`` (or ``None`` for a day
    outside the leveling system), ``title``, ``start`` (HH:MM) and
    ``duration_min`` -- the same shape as ``db.DEFAULT_SCHEDULE``.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.

    Returns:
        dict[int, dict]: One template per weekday; a corrupt or
        missing setting falls back to the default week.
    """
    raw = db.get_setting(conn, user_id, "schedule")
    try:
        schedule = json.loads(raw) if raw else db.DEFAULT_SCHEDULE
    except json.JSONDecodeError:
        schedule = db.DEFAULT_SCHEDULE
    return {
        weekday: {**db.DEFAULT_SCHEDULE[str(weekday)],
                  **schedule.get(str(weekday), {})}
        for weekday in range(7)
    }


def session_type_for_weekday(
    conn: sqlite3.Connection, user_id: int, weekday: int,
) -> Optional[str]:
    """The user's session type for an ISO weekday (Monday=0).

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        weekday (int): ``date.weekday()`` result.

    Returns:
        str | None: Session type key, or ``None`` for a day outside
        the leveling system.
    """
    return schedule_for_user(conn, user_id)[weekday].get("session_type")


# Density-first philosophy: a higher level first packs more work into
# the same slot (speed, reps, rounds); only once intensity is maxed
# does the session get LONGER, and never past the user's cap.
DEFAULT_SESSION_CAP_MIN = 30
SESSION_CAP_FLOOR_MIN, SESSION_CAP_CEIL_MIN = 10, 240


def session_cap_min(conn: sqlite3.Connection, user_id: int) -> int:
    """The user's max session duration in minutes (Settings).

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.

    Returns:
        int: Cap clamped to a sane range; default 30.
    """
    raw = db.get_setting(conn, user_id, "session_cap_min")
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        cap = DEFAULT_SESSION_CAP_MIN
    return max(SESSION_CAP_FLOOR_MIN, min(SESSION_CAP_CEIL_MIN, cap))


def treadmill_values(
    level: int, cap_min: int = DEFAULT_SESSION_CAP_MIN,
) -> dict:
    """Level -> treadmill workout values.

    Speed climbs until level 8 caps it at 7.0 km/h; levels above that
    extend the walk instead (+4 min per level), up to ``cap_min``.
    """
    return {
        "speed_kmh": min(round(5.5 + level * 0.2, 1), 7.0),
        "incline_pct": 12,
        "duration_min": min(20 + 4 * max(0, level - 7), cap_min),
    }


def _circuit_duration_min(rounds: int, cap_min: int) -> int:
    """Estimated circuit duration: ~6 min/round + warm-up, capped."""
    return min(12 + 6 * rounds, cap_min)


def lower_body_values(
    level: int, cap_min: int = DEFAULT_SESSION_CAP_MIN,
) -> dict:
    """Level -> lower-body bodyweight circuit values."""
    rounds = 3 if level <= 3 else 4 if level <= 7 else 5
    return {
        "squats": 12 + level,
        "lunges_per_leg": 10 + level,
        "wall_sit_sec": 30 + level * 4,
        "calf_raises": 15 + level,
        "glute_bridge": 15 + level,
        "rounds": rounds,
        "duration_min": _circuit_duration_min(rounds, cap_min),
    }


def upper_body_values(
    level: int, cap_min: int = DEFAULT_SESSION_CAP_MIN,
) -> dict:
    """Level -> upper-body + core circuit values."""
    rounds = 3 if level <= 3 else 4
    return {
        "pushups": 8 + level,
        "dips": 10 + level,
        "superman": 12 + level,
        "plank_sec": 20 + level * 4,
        "rounds": rounds,
        "duration_min": _circuit_duration_min(rounds, cap_min),
    }


def calisthenics_values(
    level: int, cap_min: int = DEFAULT_SESSION_CAP_MIN,
) -> dict:
    """Level -> full-body calisthenics circuit values."""
    rounds = 3 if level <= 3 else 4 if level <= 7 else 5
    return {
        "squats": 15 + level,
        "pushups": 10 + level,
        "reverse_lunges_per_leg": 10 + level,
        "side_plank_sec": 15 + level * 2,
        "mountain_climbers": 20 + level * 2,
        "jumping_jacks": 20 + level * 2,
        "rounds": rounds,
        "duration_min": _circuit_duration_min(rounds, cap_min),
    }


SESSION_VALUE_FUNCS = {
    "treadmill": treadmill_values,
    "lower_body": lower_body_values,
    "upper_body": upper_body_values,
    "calisthenics": calisthenics_values,
}


def session_values(
    session_type: str, level: int,
    cap_min: int = DEFAULT_SESSION_CAP_MIN,
) -> dict:
    """Dispatch to the value-mapping function for a session type."""
    return SESSION_VALUE_FUNCS[session_type](level, cap_min)


def format_description_fr(
    session_type: str, level: int, values: dict, status: str,
) -> str:
    """Render the French calendar-event description body."""
    label = SESSION_LABEL_FR[session_type]
    status_label = STATUS_LABEL_FR[status]

    if session_type == "treadmill":
        body = (
            f"{values['speed_kmh']} km/h, marche, inclinaison "
            f"{values['incline_pct']}%, {values['duration_min']} min "
            "continu"
        )
    elif session_type == "lower_body":
        body = (
            f"{values['rounds']} tours (~{values['duration_min']} "
            f"min) - squats {values['squats']}, "
            f"fentes avant {values['lunges_per_leg']}/jambe, chaise "
            f"contre mur {values['wall_sit_sec']}s, mollets debout "
            f"{values['calf_raises']}, pont fessier "
            f"{values['glute_bridge']}"
        )
    elif session_type == "upper_body":
        body = (
            f"{values['rounds']} tours (~{values['duration_min']} "
            f"min) - pompes {values['pushups']}, "
            f"dips {values['dips']}, superman {values['superman']}, "
            f"planche {values['plank_sec']}s"
        )
    else:  # calisthenics
        body = (
            f"{values['rounds']} tours (~{values['duration_min']} "
            f"min) - squats {values['squats']}, "
            f"pompes {values['pushups']}, fentes arriere "
            f"{values['reverse_lunges_per_leg']}/jambe, gainage "
            f"lateral {values['side_plank_sec']}s/cote, mountain "
            f"climbers {values['mountain_climbers']}, jumping jacks "
            f"{values['jumping_jacks']}"
        )

    return (
        f"Niveau {level} - {label}: {body}. "
        f"(statut du jour: {status_label})"
    )


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db as db_module

    assert adjust_level(0, "red") == 0
    assert adjust_level(10, "green") == 10
    assert adjust_level(3, "yellow") == 3
    assert adjust_level(3, "green") == 4
    assert adjust_level(3, "red") == 2

    assert compute_status({}, None) == "yellow"
    assert compute_status({"sleep_score": 40}, None) == "red"
    assert compute_status(
        {"sleep_score": 80, "recent_minutes": 100, "previous_minutes": 100},
        None,
    ) == "green"
    assert compute_status({"resting_hr": 65}, 58) == "red"
    assert compute_status({"resting_hr": 59}, 58) == "green"
    assert compute_status(
        {"recent_minutes": 200, "previous_minutes": 100,
         "recent_avg_rpe": 8.5}, None,
    ) == "red"
    assert compute_status(
        {"recent_minutes": 200, "previous_minutes": 100,
         "recent_avg_rpe": 4.0}, None,
    ) == "yellow"

    # HRV / training readiness: Garmin-API-only votes, no HC equivalent.
    assert compute_status({"hrv_status": "BALANCED"}, None) == "green"
    assert compute_status({"hrv_status": "UNBALANCED"}, None) == "yellow"
    assert compute_status({"hrv_status": "LOW"}, None) == "red"
    # Unrecognized status casts no vote -> falls back to yellow (no data).
    assert compute_status({"hrv_status": "UNKNOWN_ENUM"}, None) == "yellow"
    assert compute_status({"training_readiness_score": 80}, None) == "green"
    assert compute_status({"training_readiness_score": 60}, None) == "yellow"
    assert compute_status({"training_readiness_score": 30}, None) == "red"
    # A red HRV outvotes an otherwise-green sleep score.
    assert compute_status(
        {"sleep_score": 90, "hrv_status": "LOW"}, None,
    ) == "red"

    assert treadmill_values(0)["speed_kmh"] == 5.5
    assert treadmill_values(20)["speed_kmh"] == 7.0
    assert lower_body_values(3)["rounds"] == 3
    assert lower_body_values(4)["rounds"] == 4
    assert lower_body_values(8)["rounds"] == 5
    assert calisthenics_values(10)["squats"] == 25

    # Density-first duration: fixed until speed caps (level 7), then
    # +4 min per level, never past the cap.
    assert treadmill_values(5)["duration_min"] == 20
    assert treadmill_values(9, cap_min=45)["duration_min"] == 28
    assert treadmill_values(10, cap_min=30)["duration_min"] == 30
    assert lower_body_values(8, cap_min=60)["duration_min"] == 42
    assert lower_body_values(8, cap_min=30)["duration_min"] == 30
    assert upper_body_values(0)["duration_min"] == 30
    assert "min)" in format_description_fr(
        "lower_body", 4, lower_body_values(4), "green",
    )

    tmp = Path(tempfile.mkdtemp()) / "smart_sport.db"
    conn = db_module.connect(tmp)
    db_module.init_db(conn)
    uid = db_module.create_user(conn, "test", "password1234")
    other_uid = db_module.create_user(conn, "other", "password1234")

    assert get_level(conn, uid, "treadmill") == 0
    set_level(conn, uid, "treadmill", 4)
    assert get_level(conn, uid, "treadmill") == 4
    assert get_level(conn, other_uid, "treadmill") == 0  # isolated

    # Schedule: default week, per-user override, corrupt fallback.
    assert session_type_for_weekday(conn, uid, 0) == "treadmill"
    assert session_type_for_weekday(conn, uid, 6) is None
    db_module.set_setting(
        conn, uid, "schedule",
        json.dumps({"6": {"session_type": "treadmill",
                          "title": "Tapis dominical",
                          "start": "10:00", "duration_min": 45}}),
    )
    assert session_type_for_weekday(conn, uid, 6) == "treadmill"
    assert schedule_for_user(conn, uid)[6]["start"] == "10:00"
    # Unspecified weekdays keep the default template.
    assert session_type_for_weekday(conn, uid, 1) == "lower_body"
    # Other user's schedule is untouched.
    assert session_type_for_weekday(conn, other_uid, 6) is None
    db_module.set_setting(conn, uid, "schedule", "not json{")
    assert session_type_for_weekday(conn, uid, 6) is None  # fallback
    db_module.set_setting(
        conn, uid, "schedule", db_module.DEFAULT_SETTINGS["schedule"],
    )

    assert session_cap_min(conn, uid) == 30  # default
    db_module.set_setting(conn, uid, "session_cap_min", "60")
    assert session_cap_min(conn, uid) == 60
    db_module.set_setting(conn, uid, "session_cap_min", "9999")
    assert session_cap_min(conn, uid) == SESSION_CAP_CEIL_MIN
    db_module.set_setting(conn, uid, "session_cap_min", "garbage")
    assert session_cap_min(conn, uid) == 30

    conn.executemany(
        "INSERT INTO resting_heart_rate VALUES (?, ?, ?, ?, ?)",
        [
            (f"r{i}", uid, f"2026-06-{i:02d}T06:00:00+00:00",
             f"2026-06-{i:02d}", 55 + (i % 3))
            for i in range(17, 31)
        ],
    )
    conn.commit()
    baseline = rhr_baseline(conn, uid, "2026-07-01")
    assert baseline is not None and 55 <= baseline <= 58
    assert rhr_baseline(conn, other_uid, "2026-07-01") is None  # isolated

    # Deload guardrail: 3 reds in a row triggers a forced cut + window,
    # not just the normal -1/day.
    set_level(conn, uid, "lower_body", 6)
    d0, d1, d2 = "2026-08-01", "2026-08-03", "2026-08-05"
    r1 = apply_deload_guardrail(conn, uid, "lower_body", "red", d0)
    assert r1 == {"level": 5, "in_deload": False, "deload_triggered": False}
    r2 = apply_deload_guardrail(conn, uid, "lower_body", "red", d1)
    assert r2 == {"level": 4, "in_deload": False, "deload_triggered": False}
    r3 = apply_deload_guardrail(conn, uid, "lower_body", "red", d2)
    assert r3["deload_triggered"] is True
    assert r3["level"] == 2  # 4 - DELOAD_LEVEL_CUT(2)
    assert r3["trigger"] == "red_streak"
    assert get_red_streak(conn, uid, "lower_body") == 0
    deload_until = get_deload_until(conn, uid, "lower_body")
    assert deload_until == (
        dt.date.fromisoformat(d2) + dt.timedelta(days=DELOAD_DAYS)
    ).isoformat()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM deload_events WHERE user_id = ? AND "
        "session_type = 'lower_body'", (uid,),
    ).fetchone()["n"] == 1

    # A green day mid-window doesn't bump the level -- deload holds.
    held = apply_deload_guardrail(
        conn, uid, "lower_body", "green", (
            dt.date.fromisoformat(d2) + dt.timedelta(days=2)
        ).isoformat(),
    )
    assert held == {"level": 2, "in_deload": True, "deload_triggered": False}

    # Once the window passes, normal adjustment resumes.
    after = apply_deload_guardrail(
        conn, uid, "lower_body", "green", deload_until,
    )
    resumed_date = (
        dt.date.fromisoformat(deload_until) + dt.timedelta(days=1)
    ).isoformat()
    after2 = apply_deload_guardrail(
        conn, uid, "lower_body", "green", resumed_date,
    )
    assert after2["in_deload"] is False
    assert after2["level"] == after["level"] + 1
    assert get_deload_until(conn, uid, "lower_body") is None

    # TSB-triggered deload: fires on a SINGLE critical reading, no
    # streak needed, even on a non-red (yellow) day.
    set_level(conn, uid, "calisthenics", 6)
    tsb_day = "2026-09-01"
    no_trigger = apply_deload_guardrail(
        conn, uid, "calisthenics", "yellow", tsb_day, tsb=-5.0,
    )
    assert no_trigger["deload_triggered"] is False  # above threshold
    tsb_r = apply_deload_guardrail(
        conn, uid, "calisthenics", "yellow", tsb_day,
        tsb=TSB_DELOAD_THRESHOLD - 1,
    )
    assert tsb_r["deload_triggered"] is True
    assert tsb_r["trigger"] == "tsb"
    assert tsb_r["level"] == 4  # 6 - DELOAD_LEVEL_CUT(2)
    assert conn.execute(
        "SELECT trigger FROM deload_events WHERE user_id = ? AND "
        "session_type = 'calisthenics'", (uid,),
    ).fetchone()["trigger"] == "tsb"
    # Already in deload -> a second critical reading doesn't re-cut.
    again = apply_deload_guardrail(
        conn, uid, "calisthenics", "yellow",
        (dt.date.fromisoformat(tsb_day) + dt.timedelta(days=1)).isoformat(),
        tsb=TSB_DELOAD_THRESHOLD - 1,
    )
    assert again["deload_triggered"] is False
    assert again["level"] == 4

    print("training.py: all checks passed")
