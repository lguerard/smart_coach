#!/usr/bin/env python3
"""The day's edits the dashboard offers, in one place.

Tonight's level and "J'ai fait : ..." used to live inside their web
routes. The in-app assistant (assistant.py) can make the same edits on
request, and an edit made from the chat must go through exactly the
code path, and the checks, of the button that makes it on the page --
so both call these functions instead of carrying two copies.
"""

import datetime as dt
import sqlite3
from typing import Optional

import db
import done_sessions
import training


def coach_entry(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> Optional[sqlite3.Row]:
    """That day's coach_log row (the latest one), if the morning run ran."""
    return conn.execute(
        "SELECT * FROM coach_log WHERE user_id = ? AND local_date = ? "
        "ORDER BY id DESC LIMIT 1", (user_id, date),
    ).fetchone()


def editable_level(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> Optional[sqlite3.Row]:
    """Today's coach_log row when its level can be edited, else None.

    A rest or recovery day has no level to edit: the Home form is not
    shown for it, and a crafted post (or a chat request) must not turn
    the rest back into a session behind the coach's back.
    """
    entry = coach_entry(conn, user_id, date)
    if (
        not entry or not entry["session_type"]
        or (entry["tier"] or training.TIER_TRAIN) != training.TIER_TRAIN
    ):
        return None
    return entry


def set_today_level(
    conn: sqlite3.Connection, user_id: int, username: str, date: str,
    level: int,
) -> Optional[dict]:
    """Set tonight's level (clamped 0-10) and sync the calendar event.

    The ``levels`` row is updated too, so tomorrow's daily +-1 starts
    from the edit. The DB write is committed before the calendar push: a
    gcal failure (missing token, calendar renamed) comes back as a note,
    never as a lost edit.

    Returns:
        dict | None: ``session_type``, ``previous`` and ``level``, plus
        ``note`` (calendar problem) or None; None when today has no
        editable level (see ``editable_level``).
    """
    import gcal

    entry = editable_level(conn, user_id, date)
    if entry is None:
        return None
    level = max(training.LEVEL_MIN, min(training.LEVEL_MAX, int(level)))
    session_type = entry["session_type"]
    training.set_level(conn, user_id, session_type, level)
    conn.execute(
        "UPDATE coach_log SET level = ?, level_reason = ? WHERE id = ? "
        "AND user_id = ?",
        (level, f"niveau regle a la main a {level}", entry["id"], user_id),
    )
    conn.commit()

    values = training.user_session_values(
        conn, user_id, session_type, level, date,
    )
    description = training.format_description_fr(
        session_type, level, values, entry["status"],
    )
    note = None
    calendar_name = db.get_setting(conn, user_id, "calendar_name")
    day = dt.date.fromisoformat(date)
    if not calendar_name:
        note = "Calendrier non configure (reglez calendar_name)."
    else:
        try:
            gcal.push_description(
                username, calendar_name, day,
                training.schedule_for_user(conn, user_id)[day.weekday()],
                description, duration_min=values.get("duration_min"),
            )
        except Exception as error:  # noqa: BLE001 -- shown, never lost
            note = f"Calendrier non mis a jour: {error}"
    return {"session_type": session_type, "previous": entry["level"],
            "level": level, "note": note}


def declare_done(
    conn: sqlite3.Connection, user_id: int, date: str, choice: str,
    today: str,
) -> None:
    """"J'ai fait : ..." -- the session actually done on a day.

    ``choice`` is a session type, ``done_sessions.OTHER``,
    ``done_sessions.NOTHING`` or ``"planned"`` (back to what was planned
    or recorded). A rating already given for that day follows the new
    type, or is dropped when nothing countable was done.

    Raises:
        ValueError: Unknown type, a date in the future or too old
            (``done_sessions.declare``'s own checks).
    """
    if choice == "planned":
        done_sessions.clear(conn, user_id, date)
    else:
        done_sessions.declare(conn, user_id, date, choice, today=today)
    rated = rated_type(conn, user_id, date)
    if rated:
        conn.execute(
            "UPDATE session_feedback SET session_type = ? WHERE user_id = ? "
            "AND local_date = ?", (rated, user_id, date),
        )
    else:
        conn.execute(
            "DELETE FROM session_feedback WHERE user_id = ? AND local_date = ?",
            (user_id, date),
        )
    conn.commit()


def rated_type(conn: sqlite3.Connection, user_id: int, date: str):
    """The session type a rating for ``date`` belongs to, or None."""
    plan = conn.execute(
        "SELECT session_type, tier FROM coach_log WHERE user_id = ? AND "
        "local_date = ? ORDER BY id DESC LIMIT 1", (user_id, date),
    ).fetchone()
    return done_sessions.actual_type(
        conn, user_id, date, plan["session_type"] if plan else None,
        plan["tier"] if plan else None,
    )


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import gcal

    conn = db.connect(Path(tempfile.mkdtemp()) / "actions.db")
    db.init_db(conn)
    uid = db.create_user(conn, "a", "pw-123456")
    other = db.create_user(conn, "b", "pw-123456")
    today = "2026-10-20"

    def log(user, tier="train", session_type="lower_body", level=4):
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, status, "
            "session_type, level, message, tier) VALUES (?, 'x', ?, 'yellow', "
            "?, ?, 'm', ?)", (user, today, session_type, level, tier))
        conn.commit()

    pushed = []
    gcal.push_description = lambda *a, **k: pushed.append(a)
    # No coach run yet: nothing to edit.
    assert set_today_level(conn, uid, "a", today, 6) is None
    log(uid)
    done = set_today_level(conn, uid, "a", today, 42)
    assert done["level"] == 10 and done["previous"] == 4, done
    assert training.get_level(conn, uid, "lower_body") == 10
    assert coach_entry(conn, uid, today)["level"] == 10
    assert "calendar_name" in done["note"] and not pushed
    db.set_setting(conn, uid, "calendar_name", "Sport")
    assert set_today_level(conn, uid, "a", today, 5)["note"] is None
    assert len(pushed) == 1
    # The other account's day is untouched, and a rest day is not edited.
    assert coach_entry(conn, other, today) is None
    log(other, tier="rest")
    assert set_today_level(conn, other, "b", today, 7) is None
    assert training.get_level(conn, other, "lower_body") != 7

    # Declared nothing: the day's rating goes with it.
    conn.execute(
        "INSERT INTO session_feedback VALUES (?, ?, 'lower_body', 'hard', 'x')",
        (uid, today))
    declare_done(conn, uid, today, done_sessions.NOTHING, today)
    assert conn.execute(
        "SELECT COUNT(*) FROM session_feedback WHERE user_id = ?",
        (uid,)).fetchone()[0] == 0
    declare_done(conn, uid, today, "planned", today)
    assert done_sessions.done_on(conn, uid, today) is None
    try:
        declare_done(conn, uid, "2026-10-21", "treadmill", today)
        raise AssertionError("a future day must be refused")
    except ValueError:
        pass
    print("day_actions.py: all checks passed")
