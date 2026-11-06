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
import missed_sessions
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
                missed_sessions.day_template(conn, user_id, date),
                description, duration_min=values.get("duration_min"),
            )
        except Exception as error:  # noqa: BLE001 -- shown, never lost
            note = f"Calendrier non mis a jour: {error}"
    return {"session_type": session_type, "previous": entry["level"],
            "level": level, "note": note}


def anyway_offer(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> Optional[dict]:
    """The session the person may still do on a day the coach cancelled
    (rest), replaced (a recovery walk) or lightened (a lower level than
    last time), or None.

    The coach's call stands -- it is not re-made or overridden -- but it
    is a recommendation: the person may feel fine and want the planned
    session anyway, and before this the watch then had nothing to follow.

    Returns:
        dict | None: ``kind`` (``rest`` / ``recovery`` / ``lighter``),
        ``session_type``, ``label``, ``level`` (the one to send: the
        current level, or last time's for a lightened day) and
        ``coach_level`` (the coach's, for a lightened day).
    """
    entry = coach_entry(conn, user_id, date)
    if not entry or not entry["session_type"]:
        return None
    session_type = entry["session_type"]
    tier = entry["tier"] or training.TIER_TRAIN
    base = {"session_type": session_type,
            "label": training.SESSION_LABEL_FR.get(session_type, session_type)}
    if tier in (training.TIER_REST, training.TIER_RECOVERY):
        return {**base, "kind": tier,
                "level": training.get_level(conn, user_id, session_type)}
    last = conn.execute(
        "SELECT level FROM coach_log WHERE user_id = ? AND session_type = ? "
        "AND local_date < ? AND COALESCE(tier, 'train') = 'train' AND "
        "level IS NOT NULL ORDER BY local_date DESC, id DESC LIMIT 1",
        (user_id, session_type, date),
    ).fetchone()
    if last and entry["level"] is not None and last["level"] > entry["level"]:
        return {**base, "kind": "lighter", "level": last["level"],
                "coach_level": entry["level"]}
    return None


def anyway_for(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> Optional[sqlite3.Row]:
    """The session sent "anyway" for that day, if one was."""
    return conn.execute(
        "SELECT session_type, level FROM day_overrides WHERE user_id = ? AND "
        "local_date = ?", (user_id, date),
    ).fetchone()


def send_anyway(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> Optional[dict]:
    """Keep the planned session for the day despite the coach's call;
    the watch push (``garmin_api.refresh_today``) then sends it.

    Returns:
        dict | None: The ``anyway_offer`` taken, or None when there is
        nothing to send anyway that day.
    """
    offer = anyway_offer(conn, user_id, date)
    if offer is None:
        return None
    conn.execute(
        "INSERT INTO day_overrides (user_id, local_date, session_type, level, "
        "created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (user_id, local_date) "
        "DO UPDATE SET session_type = excluded.session_type, "
        "level = excluded.level, created_at = excluded.created_at",
        (user_id, date, offer["session_type"], offer["level"],
         dt.datetime.now(dt.timezone.utc).isoformat()),
    )
    conn.commit()
    return offer


def cancel_anyway(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    """Back to the coach's call for the day."""
    deleted = conn.execute(
        "DELETE FROM day_overrides WHERE user_id = ? AND local_date = ?",
        (user_id, date),
    ).rowcount
    conn.commit()
    return bool(deleted)


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
    # "Do it anyway": a rest day offers the planned session at its level.
    training.set_level(conn, other, "lower_body", 5)
    offer = anyway_offer(conn, other, today)
    assert offer == {"session_type": "lower_body", "label": "Muscu bas du corps",
                     "kind": "rest", "level": 5}, offer
    assert send_anyway(conn, other, today) == offer
    assert dict(anyway_for(conn, other, today)) == {"session_type": "lower_body",
                                                    "level": 5}
    assert coach_entry(conn, other, today)["tier"] == "rest"  # the coach's call stays
    assert cancel_anyway(conn, other, today) and anyway_for(conn, other, today) is None
    # A lightened day offers last time's level; a normal one offers nothing.
    conn.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message, tier) VALUES (?, 'x', '2026-10-18', "
        "'green', 'lower_body', 7, 'm', 'train')", (uid,))
    conn.commit()
    lighter = anyway_offer(conn, uid, today)  # today's level is 5, last time 7
    assert lighter["kind"] == "lighter" and lighter["level"] == 7, lighter
    assert lighter["coach_level"] == 5, lighter
    set_today_level(conn, uid, "a", today, 7)
    assert anyway_offer(conn, uid, today) is None and send_anyway(conn, uid, today) is None
    print("day_actions.py: all checks passed")
