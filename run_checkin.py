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
import desk
import metrics
import notify
import progress

# By this point in the afternoon, expect roughly this fraction of the
# daily hydration/step targets -- a tunable starting point, same
# posture as training.py's thresholds.
AFTERNOON_PACE_PCT = 0.6

# Sitting time by mid-afternoon that makes a desk break worth sending
# on its own, without a steps/hydration gap.
SEDENTARY_AFTERNOON_H = 7

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


def _refresh_garmin_today(conn: sqlite3.Connection, user: dict) -> None:
    """Re-fetch today's Garmin rollups so the check-in reads this
    afternoon, not the 07:15 snapshot.

    The ingest runs once, in the morning, when today's steps and floors
    are close to zero -- so without this the 16:00 pace check compared a
    morning snapshot against an afternoon target and always found a gap.
    Best-effort: a Garmin failure leaves the check-in on what it has.
    """
    try:
        from ingest import garmin_api

        garmin_api.fetch_and_upsert(conn, user["id"], user["username"], days=1)
    except Exception as error:
        print(f"{user['username']}: garmin refresh skipped -- {error}")


def _desk_nudge(
    conn: sqlite3.Connection, user_id: int, today: str, language: str,
    behind: bool, wellness: dict,
) -> str:
    """One silent exercise for the afternoon message, or ``""``.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        today (str): ISO local date.
        language (str): ``fr`` or ``en``.
        behind (bool): Steps/hydration are behind pace.
        wellness (dict): Today's ``metrics.daily_wellness``.

    Returns:
        str: A line starting with a space-free label, or ``""`` when it
        is not a desk day or there is no reason to send one.
    """
    if not desk.desk_mode(conn, user_id, today):
        return ""
    flags = []
    if behind:
        flags.append("steps_low")
    sedentary = wellness.get("sedentary_seconds")
    if sedentary is not None and sedentary / 3600 >= SEDENTARY_AFTERNOON_H:
        flags.append("sedentary_high")
    if not flags:
        return ""
    entry = conn.execute(
        "SELECT status, tier FROM coach_log WHERE user_id = ? AND "
        "local_date = ? ORDER BY id DESC LIMIT 1", (user_id, today),
    ).fetchone()
    plan = desk.build_break(
        today, flags, (entry["tier"] if entry and entry["tier"] else "train"),
        entry["status"] if entry else None, language, items=1,
    )
    if not plan["items"]:
        return ""
    item = plan["items"][0]
    label = (
        "Pause bureau silencieuse" if language == "fr"
        else "Silent desk break"
    )
    return f"\n{label} : {item['name']} -- {item['how']}"


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
    if date is None:
        _refresh_garmin_today(conn, user)
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
    steps_behind = bool(
        step_goal and steps_actual < AFTERNOON_PACE_PCT * step_goal
    )
    if steps_behind:
        missing_steps = round(AFTERNOON_PACE_PCT * step_goal - steps_actual)
        gaps_fr.append(f"{missing_steps} pas de retard sur l'objectif")
        gaps_en.append(f"{missing_steps} steps behind goal")

    desk_line = _desk_nudge(
        conn, user_id, today, language,
        behind=steps_behind,
        wellness=wellness,
    )
    if not gaps_fr and not desk_line:
        return  # on track -- no nudge

    if gaps_fr:
        message = (
            "Point de 16h : " + ", ".join(gaps_fr) + "."
            if language == "fr" else
            "4pm check-in: " + ", ".join(gaps_en) + "."
        )
    else:
        message = (
            "Point de 16h : tu es reste assis longtemps." if language == "fr"
            else "4pm check-in: you have been sitting a long while."
        )
    message += desk_line
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
            "INSERT INTO sleep_sessions (uuid, user_id, start_utc, "
            "end_utc, local_date) VALUES (?, ?, ?, ?, ?)",
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


    # Desk break in the afternoon message: a step gap on a weekday
    # carries one silent exercise; the same gap on a Sunday does not.
    sent.clear()
    db.set_setting(conn, uid, "step_goal", "10000")
    afternoon_checkin(conn, user, "2026-07-15")  # Wednesday, no steps
    assert "Pause bureau silencieuse" in sent[0][0], sent
    sent.clear()
    afternoon_checkin(conn, user, "2026-07-19")  # Sunday
    assert sent and "Pause bureau" not in sent[0][0], sent
    sent.clear()
    db.set_setting(conn, uid, "desk_job", "0")
    afternoon_checkin(conn, user, "2026-07-15")
    assert sent and "Pause bureau" not in sent[0][0], sent
    db.set_setting(conn, uid, "desk_job", "1")

    # Sitting all day alone is worth a message, with no pace gap at all.
    sent.clear()
    day = "2026-07-16"  # Thursday
    conn.execute(
        "INSERT INTO steps VALUES ('s16', ?, '2026-07-16T08:00:00+00:00', "
        "'2026-07-16T08:10:00+00:00', ?, 9000)", (uid, day),
    )
    conn.execute(
        "INSERT INTO hydration VALUES ('h16', ?, "
        "'2026-07-16T08:00:00+00:00', '2026-07-16T08:01:00+00:00', ?, 2500)",
        (uid, day),
    )
    conn.execute(
        "INSERT INTO garmin_daily_summary (user_id, local_date, "
        "sedentary_seconds) VALUES (?, ?, 9 * 3600)", (uid, day),
    )
    conn.commit()
    afternoon_checkin(conn, user, day)
    assert len(sent) == 1 and "assis" in sent[0][0], sent
    assert "Pause bureau silencieuse" in sent[0][0], sent
    # ...and little sitting with no gap stays silent.
    sent.clear()
    conn.execute(
        "UPDATE garmin_daily_summary SET sedentary_seconds = 3 * 3600 "
        "WHERE user_id = ?", (uid,),
    )
    conn.commit()
    afternoon_checkin(conn, user, day)
    assert sent == [], sent

    print("run_checkin.py: all checks passed (no live push sent)")
