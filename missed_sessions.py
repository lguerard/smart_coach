#!/usr/bin/env python3
"""A planned session not done: said so, moved once, or let go.

The evening notification used to ask how tonight's session felt, even
when the watch had recorded nothing. Now, with nothing recorded, it asks
what happened instead: not done, done without the watch, or done
tomorrow -- the last only when tomorrow is a rest day in the plan, so a
missed session never lands on top of the next one, and only once (a
session already moved is not moved again). A missed session is never
made up by adding its work to the next ones: the level just holds
(training.progression_gate), and a real break starts again a notch lower
(``layoff``).
"""

import datetime as dt
import sqlite3
from typing import Optional

import db
import done_sessions
import training

CHOICES = ("none", "nowatch", "tomorrow")
# A type not done for this long, or missed this many times in a row,
# starts again one level lower.
LAYOFF_DAYS = 10
LAYOFF_MISSES = 2
LAYOFF_KEY = "layoff_lowered"


def _plus(date: str, days: int) -> str:
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


def postponed_to(conn: sqlite3.Connection, user_id: int, date: str):
    """The session moved onto ``date``, if one was."""
    return conn.execute(
        "SELECT from_date, session_type, start, duration_min FROM "
        "postponed_sessions WHERE user_id = ? AND to_date = ?",
        (user_id, date),
    ).fetchone()


def day_template(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """That day's plan entry, with a session moved onto it in place of
    the rest day it was."""
    template = training.schedule_for_user(conn, user_id)[
        dt.date.fromisoformat(date).weekday()]
    moved = postponed_to(conn, user_id, date)
    if not moved:
        return template
    label = training.SESSION_LABEL_FR.get(moved["session_type"], moved["session_type"])
    return {**template, "session_type": moved["session_type"],
            "title": f"{label} (reportee)",
            "start": moved["start"] or template.get("start"),
            "duration_min": moved["duration_min"] or template.get("duration_min"),
            "postponed_from": moved["from_date"]}


def _planned(conn, user_id: int, date: str):
    entry = conn.execute(
        "SELECT session_type, level, tier FROM coach_log WHERE user_id = ? "
        "AND local_date = ? ORDER BY id DESC LIMIT 1", (user_id, date),
    ).fetchone()
    if not entry or not entry["session_type"] or (
            entry["tier"] or training.TIER_TRAIN) != training.TIER_TRAIN:
        return None
    return entry


def can_postpone(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    """Whether the day's session may move to the next day: a rest day in
    the plan with nothing moved onto it yet, and the session not itself
    a moved one."""
    tomorrow = _plus(date, 1)
    return (_planned(conn, user_id, date) is not None
            and not training.schedule_for_user(conn, user_id)[
                dt.date.fromisoformat(tomorrow).weekday()].get("session_type")
            and postponed_to(conn, user_id, tomorrow) is None
            and postponed_to(conn, user_id, date) is None)


def slot_over(conn: sqlite3.Connection, user_id: int, date: str,
              now: dt.datetime) -> bool:
    """Whether the day's planned slot has ended by ``now`` (local time);
    a day without a usable slot counts as over by the evening."""
    template = day_template(conn, user_id, date)
    try:
        hour, minute = (int(x) for x in str(template.get("start") or "").split(":"))
    except ValueError:
        return True
    end = hour * 60 + minute + int(template.get("duration_min") or 0)
    return now.hour * 60 + now.minute >= end


def evening_question(
    conn: sqlite3.Connection, user_id: int, date: str,
    today: Optional[str] = None,
) -> Optional[dict]:
    """The question for a training day with nothing recorded or said.

    Returns:
        dict | None: ``session_type``, ``label``, ``can_postpone``; None
        when the day had no session planned or something is known done.
    """
    entry = _planned(conn, user_id, date)
    if entry is None or done_sessions.done_on(conn, user_id, date) is not None:
        return None
    return {"session_type": entry["session_type"],
            "label": training.SESSION_LABEL_FR.get(entry["session_type"],
                                                   entry["session_type"]),
            # Only tonight's session moves to tomorrow: an older one
            # would land on a day the morning has already planned.
            "can_postpone": (today in (None, date)
                             and can_postpone(conn, user_id, date))}


def answer(
    conn: sqlite3.Connection, user_id: int, date: str, choice: str,
    today: Optional[str] = None,
) -> Optional[str]:
    """Record the answer; what to tell the person, or None when the
    answer does not apply (unknown, or the day cannot move)."""
    entry = _planned(conn, user_id, date)
    if entry is None or choice not in CHOICES:
        return None
    label = training.SESSION_LABEL_FR.get(entry["session_type"], entry["session_type"])
    if choice == "nowatch":
        done_sessions.declare(conn, user_id, date, entry["session_type"], today=today)
        return f"Noté : {label} faite sans la montre."
    if choice == "tomorrow":
        if today not in (None, date) or not can_postpone(conn, user_id, date):
            return None
        template = training.schedule_for_user(conn, user_id)[
            dt.date.fromisoformat(date).weekday()]
        conn.execute(
            "INSERT INTO postponed_sessions (user_id, from_date, to_date, "
            "session_type, start, duration_min, created_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?)",
            (user_id, date, _plus(date, 1), entry["session_type"],
             template.get("start"), template.get("duration_min"),
             dt.datetime.now(dt.timezone.utc).isoformat()))
        conn.commit()
    done_sessions.declare(conn, user_id, date, done_sessions.NOTHING, today=today)
    if choice == "tomorrow":
        return (f"{label} reportée à demain : le coach la prévoit demain matin, "
                "sur la montre et dans l'agenda.")
    return (f"Noté : {label} pas faite. Rien à rattraper : la prochaine reste "
            "au même niveau.")


def layoff(
    conn: sqlite3.Connection, user_id: int, session_type: str, date: str,
) -> Optional[str]:
    """Why this session should start again a level lower, or None: the
    last ``LAYOFF_MISSES`` planned sessions of the type were not done, or
    none was done for ``LAYOFF_DAYS`` days while some were planned."""
    planned = conn.execute(
        "SELECT c.local_date FROM coach_log c WHERE c.user_id = ? AND "
        "c.session_type = ? AND c.local_date < ? AND COALESCE(c.tier, 'train') "
        "= 'train' AND c.id = (SELECT MAX(id) FROM coach_log WHERE user_id = "
        "c.user_id AND local_date = c.local_date) ORDER BY c.local_date DESC "
        "LIMIT 8", (user_id, session_type, date),
    ).fetchall()
    if not planned:
        return None

    def done(day: str) -> bool:
        known = done_sessions.done_on(conn, user_id, day)
        return bool(known) and known["session_type"] not in (
            done_sessions.NOTHING, done_sessions.OTHER)

    recent = [row["local_date"] for row in planned[:LAYOFF_MISSES]]
    if len(recent) == LAYOFF_MISSES and not any(done(d) for d in recent):
        return f"reprise : les {LAYOFF_MISSES} dernieres seances de ce type n'ont pas ete faites"
    last_done = next((row["local_date"] for row in planned if done(row["local_date"])), None)
    if last_done and (dt.date.fromisoformat(date)
                      - dt.date.fromisoformat(last_done)).days > LAYOFF_DAYS:
        return f"reprise : pas de seance de ce type depuis plus de {LAYOFF_DAYS} jours"
    return None


def apply_layoff(
    conn: sqlite3.Connection, user_id: int, session_type: str, date: str,
) -> Optional[str]:
    """Lower the level by one after a break (``layoff``), once per day
    and type -- a second run of the morning the same day changes nothing.

    Returns:
        str | None: The reason, when the level went down.
    """
    reason = layoff(conn, user_id, session_type, date)
    stamp = f"{session_type}:{date}"
    if not reason or db.get_setting(conn, user_id, LAYOFF_KEY) == stamp:
        return None
    level = training.get_level(conn, user_id, session_type)
    if level <= training.LEVEL_MIN:
        return None
    training.set_level(conn, user_id, session_type, level - 1)
    db.set_setting(conn, user_id, LAYOFF_KEY, stamp)
    conn.commit()
    return reason


def week_sessions(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """The seven days before ``date``: training sessions planned and done."""
    start = _plus(date, -7)
    rows = conn.execute(
        "SELECT c.local_date, c.session_type FROM coach_log c WHERE c.user_id = ? "
        "AND c.local_date BETWEEN ? AND ? AND COALESCE(c.tier, 'train') = 'train' "
        "AND c.session_type IS NOT NULL AND c.id = (SELECT MAX(id) FROM coach_log "
        "WHERE user_id = c.user_id AND local_date = c.local_date)",
        (user_id, start, _plus(date, -1)),
    ).fetchall()
    done = 0
    for row in rows:
        known = done_sessions.done_on(conn, user_id, row["local_date"])
        if known and known["session_type"] not in (done_sessions.NOTHING,
                                                    done_sessions.OTHER):
            done += 1
    return {"planned": len(rows), "done": done}


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    conn = db.connect(Path(tempfile.mkdtemp()) / "missed.db")
    db.init_db(conn)
    uid = db.create_user(conn, "m", "pw-123456")
    schedule = training.schedule_for_user(conn, uid)
    # Find a training day followed by a rest day, and one followed by a
    # training day, in the default plan.
    base = dt.date(2026, 11, 2)  # a Monday
    days = [(base + dt.timedelta(days=k)).isoformat() for k in range(7)]
    kind = {d: schedule[dt.date.fromisoformat(d).weekday()].get("session_type") for d in days}
    before_rest = next(d for d in days if kind[d] and not kind[_plus(d, 1)])
    before_train = next(d for d in days if kind[d] and kind[_plus(d, 1)])

    def plan(date, session_type, tier="train", level=4):
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, status, "
            "session_type, level, message, tier) VALUES (?, 'x', ?, 'green', ?, "
            "?, 'm', ?)", (uid, date, session_type, level, tier))
        conn.commit()

    # Yesterday's session (asked on Today the next day) cannot move: the
    # morning has planned today already.
    plan("2026-10-05", "lower_body")
    assert evening_question(conn, uid, "2026-10-05", today="2026-10-06")[
        "can_postpone"] is False
    assert answer(conn, uid, "2026-10-05", "tomorrow", today="2026-10-06") is None
    # Nothing planned: no question.
    assert evening_question(conn, uid, before_rest) is None
    plan(before_rest, kind[before_rest])
    question = evening_question(conn, uid, before_rest)
    assert question and question["can_postpone"], question
    # Something recorded: no question (the rating one comes instead).
    conn.execute(
        "INSERT INTO exercise_sessions (uuid, user_id, start_utc, end_utc, "
        "local_date, title) VALUES ('x', ?, ?, ?, ?, 'Footing')",
        (uid, before_rest + "T18:00:00+00:00", before_rest + "T18:30:00+00:00",
         before_rest))
    conn.commit()
    assert evening_question(conn, uid, before_rest) is None
    conn.execute("DELETE FROM exercise_sessions")
    conn.commit()

    # Tomorrow: the rest day becomes that session, moved once only.
    note = answer(conn, uid, before_rest, "tomorrow", today=before_rest)
    assert "demain" in note, note
    moved = day_template(conn, uid, _plus(before_rest, 1))
    assert moved["session_type"] == kind[before_rest], moved
    assert moved["postponed_from"] == before_rest and "reportee" in moved["title"]
    assert done_sessions.done_on(conn, uid, before_rest)["session_type"] == "none"
    plan(_plus(before_rest, 1), kind[before_rest])
    assert not can_postpone(conn, uid, _plus(before_rest, 1))  # once
    assert evening_question(conn, uid, _plus(before_rest, 1))["can_postpone"] is False
    # Not before a training day, never twice onto the same day.
    plan(before_train, kind[before_train])
    assert not can_postpone(conn, uid, before_train)
    assert answer(conn, uid, before_train, "tomorrow", today=before_train) is None
    # Without the watch: counted as done.
    assert "sans la montre" in answer(conn, uid, before_train, "nowatch",
                                      today=before_train)
    assert done_sessions.done_on(conn, uid, before_train)["session_type"] == kind[before_train]

    # Two missed in a row: one level down, once per day.
    other = db.create_user(conn, "o", "pw-123456")
    for date in ("2026-10-20", "2026-10-22"):
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, status, "
            "session_type, level, message, tier) VALUES (?, 'x', ?, 'green', "
            "'lower_body', 5, 'm', 'train')", (other, date))
        done_sessions.declare(conn, other, date, done_sessions.NOTHING, today=date)
    training.set_level(conn, other, "lower_body", 5)
    assert "2 dernieres" in layoff(conn, other, "lower_body", "2026-10-24")
    assert apply_layoff(conn, other, "lower_body", "2026-10-24")
    assert training.get_level(conn, other, "lower_body") == 4
    assert apply_layoff(conn, other, "lower_body", "2026-10-24") is None  # rerun
    assert training.get_level(conn, other, "lower_body") == 4
    # One done since: back to normal.
    done_sessions.declare(conn, other, "2026-10-22", "lower_body", today="2026-10-22")
    assert layoff(conn, other, "lower_body", "2026-10-24") is None
    # Last done long ago: a break.
    assert "10 jours" in layoff(conn, other, "lower_body", "2026-11-05")
    # In the morning decision: the level comes down one and says why,
    # with no step up on top -- even on a green morning.
    third = db.create_user(conn, "t", "pw-123456")
    for date in ("2026-10-27", "2026-10-29"):
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, status, "
            "session_type, level, message, tier) VALUES (?, 'x', ?, 'green', "
            "'lower_body', 6, 'm', 'train')", (third, date))
        done_sessions.declare(conn, third, date, done_sessions.NOTHING, today=date)
    training.set_level(conn, third, "lower_body", 6)
    decided = training.plan_day(conn, third, "2026-10-31",
                                {"session_type": "lower_body", "title": "Bas"},
                                {}, {})
    assert decided["level"] == 5, decided["level"]
    assert decided["level_reason"].startswith("reprise"), decided["level_reason"]

    # The week: planned against done.
    week = week_sessions(conn, other, "2026-10-26")
    assert week == {"planned": 2, "done": 1}, week
    print("missed_sessions.py: all checks passed")
