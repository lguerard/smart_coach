#!/usr/bin/env python3
"""Cron entrypoint: a second and third daily touchpoint beyond the
morning coach run (run_coach.py) -- an afternoon hydration/steps
pace check, and an evening sleep-debt wind-down reminder.

Both stay SILENT when the user is on track: this is a nudge for a
real gap, not a running commentary that would just add notification
noise on top of the one guaranteed morning message.
"""

import datetime as dt
import sqlite3
import sys

import db
import metrics
import notify
import progress

# By this point in the afternoon, expect roughly this fraction of the
# daily hydration/step targets -- a tunable starting point, same
# posture as training.py's thresholds.
AFTERNOON_PACE_PCT = 0.6

SLEEP_DEBT_WINDOW_DAYS = 3
# Average sleep this far under target over the window triggers the
# evening nudge.
SLEEP_DEBT_HOURS_ALERT = 1.0


def _recent_avg_sleep_hours(
    conn: sqlite3.Connection, user_id: int, date: str,
    days: int = SLEEP_DEBT_WINDOW_DAYS,
) -> float | None:
    """Average sleep duration over the ``days`` nights before ``date``.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today), excluded from the window.
        days (int): Window length.

    Returns:
        float | None: Average hours, or ``None`` if no night in the
        window has sleep data at all.
    """
    hours = []
    day = dt.date.fromisoformat(date) - dt.timedelta(days=1)
    for _ in range(days):
        sleep = metrics.sleep_for_date(conn, user_id, day.isoformat())
        if sleep.get("sleep_hours") is not None:
            hours.append(sleep["sleep_hours"])
        day -= dt.timedelta(days=1)
    return sum(hours) / len(hours) if hours else None


def afternoon_checkin(
    conn: sqlite3.Connection, user: dict, date: str | None = None,
) -> None:
    """Nudge if today's hydration or steps are meaningfully behind
    pace -- silent otherwise.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user (dict): Account row (needs ``id``).
        date (str | None): ISO local date to check; defaults to the
            real current date in the user's timezone. Overridable so
            this stays testable without a live clock.
    """
    user_id = user["id"]
    language = db.get_setting(conn, user_id, "language") or "fr"
    ntfy_topic = db.get_setting(conn, user_id, "ntfy_topic") or None
    today = date or dt.datetime.now(
        metrics.local_tz(conn, user_id)
    ).date().isoformat()
    wellness = metrics.daily_wellness(conn, user_id, today)
    targets = progress.macro_targets(conn, user_id, today)

    gaps_fr, gaps_en = [], []
    hydration_target = targets.get("hydration_target_ml")
    hydration_actual = wellness.get("hydration_ml_today") or 0
    if (
        hydration_target
        and hydration_actual < AFTERNOON_PACE_PCT * hydration_target
    ):
        missing_l = round(
            (AFTERNOON_PACE_PCT * hydration_target - hydration_actual)
            / 1000, 1,
        )
        gaps_fr.append(f"encore {missing_l}L d'eau pour etre au rythme")
        gaps_en.append(f"{missing_l}L water still needed to stay on pace")

    step_goal = wellness.get("step_goal")
    steps_actual = wellness.get("steps_today") or 0
    if step_goal and steps_actual < AFTERNOON_PACE_PCT * step_goal:
        missing_steps = round(AFTERNOON_PACE_PCT * step_goal - steps_actual)
        gaps_fr.append(f"{missing_steps} pas de retard sur l'objectif")
        gaps_en.append(f"{missing_steps} steps behind goal")

    if not gaps_fr:
        return  # on track -- no nudge

    message = (
        "Point de 16h : " + ", ".join(gaps_fr) + "."
        if language == "fr" else
        "4pm check-in: " + ", ".join(gaps_en) + "."
    )
    notify.notify(message, title="Smart Coach", topic=ntfy_topic)


def evening_checkin(
    conn: sqlite3.Connection, user: dict, date: str | None = None,
) -> None:
    """Nudge to wind down early if recent sleep is meaningfully short
    -- silent otherwise (including when there's not enough data yet).

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user (dict): Account row (needs ``id``).
        date (str | None): ISO local date to check from; defaults to
            the real current date in the user's timezone. Overridable
            so this stays testable without a live clock.
    """
    user_id = user["id"]
    language = db.get_setting(conn, user_id, "language") or "fr"
    ntfy_topic = db.get_setting(conn, user_id, "ntfy_topic") or None
    today = date or dt.datetime.now(
        metrics.local_tz(conn, user_id)
    ).date().isoformat()
    avg_sleep = _recent_avg_sleep_hours(conn, user_id, today)
    if (
        avg_sleep is None
        or avg_sleep >= metrics.SLEEP_TARGET_HOURS - SLEEP_DEBT_HOURS_ALERT
    ):
        return

    debt = round(metrics.SLEEP_TARGET_HOURS - avg_sleep, 1)
    message = (
        f"Dette de sommeil ~{debt}h sur les {SLEEP_DEBT_WINDOW_DAYS} "
        "derniers jours -- couche-toi tot ce soir."
        if language == "fr" else
        f"~{debt}h sleep debt over the last {SLEEP_DEBT_WINDOW_DAYS} "
        "days -- get to bed early tonight."
    )
    notify.notify(message, title="Smart Coach", topic=ntfy_topic)


CHECKINS = {"afternoon": afternoon_checkin, "evening": evening_checkin}


def main() -> None:
    """Run the requested check-in for every user account."""
    if len(sys.argv) < 2 or sys.argv[1] not in CHECKINS:
        sys.exit("Usage: run_checkin.py afternoon|evening")
    checkin = CHECKINS[sys.argv[1]]

    conn = db.connect()
    db.init_db(conn)
    for user in db.all_users(conn):
        try:
            checkin(conn, dict(user))
        except Exception as error:
            print(f"{user['username']}: FAILED ({sys.argv[1]}) -- {error}")


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "smart_coach.db"
    conn = db.connect(tmp)
    db.init_db(conn)
    uid = db.create_user(conn, "test", "password1234")
    user = dict(db.get_user(conn, uid))

    sent = []
    notify.notify = lambda text, title="Smart Coach", topic=None: sent.append(
        (text, topic),
    )

    # Afternoon: no data at all -- both targets unmet by definition,
    # but no crash, and a real gap message is sent.
    db.set_setting(conn, uid, "step_goal", "10000")
    db.set_setting(conn, uid, "hydration_target_ml_per_kg", "35")
    conn.execute(
        "INSERT INTO weight VALUES ('w1', ?, '2026-07-13T07:00:00+00:00', "
        "'2026-07-13', 80.0)", (uid,),
    )
    conn.commit()
    afternoon_checkin(conn, user, "2026-07-13")
    assert len(sent) == 1, sent
    assert "pas de retard" in sent[0][0] and "eau" in sent[0][0], sent

    # On pace -- silent.
    sent.clear()
    conn.execute(
        "INSERT INTO steps VALUES ('s1', ?, '2026-07-13T08:00:00+00:00', "
        "'2026-07-13T08:10:00+00:00', '2026-07-13', 8000)", (uid,),
    )
    conn.execute(
        "INSERT INTO hydration VALUES ('h1', ?, "
        "'2026-07-13T08:00:00+00:00', '2026-07-13T08:01:00+00:00', "
        "'2026-07-13', 2000)", (uid,),
    )
    conn.commit()
    afternoon_checkin(conn, user, "2026-07-13")
    assert sent == []

    # Evening: not enough sleep data yet -- silent.
    sent.clear()
    evening_checkin(conn, user, "2026-07-13")
    assert sent == []

    # 3 short nights -> sleep-debt nudge.
    for day_offset, hours in ((1, 5.0), (2, 5.5), (3, 5.0)):
        night = dt.date.fromisoformat("2026-07-13") - dt.timedelta(
            days=day_offset,
        )
        start = dt.datetime.combine(night, dt.time(23, 0))
        end = start + dt.timedelta(hours=hours)
        conn.execute(
            "INSERT INTO sleep_sessions VALUES (?, ?, ?, ?, ?, NULL, "
            "NULL)",
            (
                f"night{day_offset}", uid, start.isoformat() + "+00:00",
                end.isoformat() + "+00:00", night.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO sleep_stages VALUES (?, ?, ?, ?, 4)",
            (
                f"night{day_offset}", uid, start.isoformat() + "+00:00",
                end.isoformat() + "+00:00",
            ),
        )
    conn.commit()
    avg = _recent_avg_sleep_hours(conn, uid, "2026-07-13")
    assert avg is not None and 5.0 <= avg <= 5.5, avg
    evening_checkin(conn, user, "2026-07-13")
    assert len(sent) == 1, sent
    assert "Dette de sommeil" in sent[0][0], sent

    print("run_checkin.py: all checks passed (no live push sent)")
