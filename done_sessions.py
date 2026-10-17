#!/usr/bin/env python3
"""Which session was actually done on a day, when it is not the plan.

The plan (coach_log) says what the morning decided; real life does not
always follow: a kettlebell session on a recovery day, the upper body
instead of the legs. What was done is read from two places, in order:

1. what the person declared on the Sessions page ("j'ai fait : ...") --
   always right, and the only way for a session the watch did not
   record;
2. the recorded activity's name or corrected category: a workout run
   from the watch carries its Smart Coach name ("Smart Coach -
   Kettlebell a la maison niveau 2"), and a custom profile or a
   correction often names it too ("Kettlebell", "Calisthenie").

Everything that cares about what was done reads ``done_on``: muscle
fatigue, ratings and the level they move, "session skipped yesterday",
the progression gate and the dashboard.
"""

import datetime as dt
import sqlite3
from typing import Optional

OTHER, NOTHING = "other", "none"  # declared: another activity / nothing
DECLARE_DAYS = 30  # how far back a session can be declared

# Words in an activity name or corrected category -> session type.
# Checked in order: the most specific first.
_NAME_HINTS = (
    ("kettlebell", "kettlebell"),
    ("bas du corps", "lower_body"),
    ("haut du corps", "upper_body"),
    ("calisth", "calisthenics"),
    ("tapis", "treadmill"),
    ("treadmill", "treadmill"),
)


def _labels() -> dict:
    import training

    return training.SESSION_LABEL_FR


def detected_type(title: Optional[str], label: Optional[str] = None) -> Optional[str]:
    """The session type an activity's name or category points to.

    Parameters:
        title (str | None): Activity name (Garmin ``activityName``).
        label (str | None): The category corrected on the Sessions page.

    Returns:
        str | None: A ``training.SESSION_LABEL_FR`` key, or None.
    """
    for text in (label, title):
        text = (text or "").lower()
        if not text:
            continue
        # Bonus and test workouts are not the plan's sessions.
        if "smart coach - bonus" in text:
            return None
        for label_type, label_fr in _labels().items():
            if label_fr.lower() in text:
                return label_type
        for word, session_type in _NAME_HINTS:
            if word in text:
                return session_type
    return None


def declare(
    conn: sqlite3.Connection, user_id: int, date: str, session_type: str,
    today: Optional[str] = None,
) -> None:
    """Record what the person says they did on ``date``.

    Parameters:
        session_type (str): A session type, ``OTHER`` or ``NOTHING``.
        today (str | None): Today's ISO date (defaults to the real one).

    Raises:
        ValueError: Unknown type, a date in the future or too old.
    """
    if session_type not in (*_labels(), OTHER, NOTHING):
        raise ValueError("Type de séance inconnu.")
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        raise ValueError("Date invalide.") from None
    now = dt.date.fromisoformat(today) if today else dt.date.today()
    if day > now or (now - day).days > DECLARE_DAYS:
        raise ValueError(
            f"La date doit être dans les {DECLARE_DAYS} derniers jours."
        )
    conn.execute(
        "INSERT INTO session_done (user_id, local_date, session_type, "
        "created_at) VALUES (?, ?, ?, ?) ON CONFLICT(user_id, local_date) "
        "DO UPDATE SET session_type = excluded.session_type, "
        "created_at = excluded.created_at",
        (user_id, date, session_type,
         dt.datetime.now(dt.timezone.utc).isoformat()),
    )
    conn.commit()


def clear(conn: sqlite3.Connection, user_id: int, date: str) -> None:
    """Forget a declaration (back to what the activity says)."""
    conn.execute(
        "DELETE FROM session_done WHERE user_id = ? AND local_date = ?",
        (user_id, date),
    )
    conn.commit()


def done_on(conn: sqlite3.Connection, user_id: int, date: str) -> Optional[dict]:
    """What was done on ``date``, as far as anything says.

    Returns:
        dict | None: ``session_type`` (a session type, ``OTHER``,
        ``NOTHING`` or None when an activity was recorded but its type
        is unknown) and ``source`` (``declared`` / ``detected`` /
        ``activity``); None when nothing at all is known.
    """
    declared = conn.execute(
        "SELECT session_type FROM session_done WHERE user_id = ? AND "
        "local_date = ?", (user_id, date),
    ).fetchone()
    if declared:
        return {"session_type": declared["session_type"], "source": "declared"}
    rows = conn.execute(
        "SELECT title, label_override FROM exercise_sessions WHERE "
        "user_id = ? AND local_date = ? ORDER BY start_utc", (user_id, date),
    ).fetchall()
    for row in rows:
        found = detected_type(row["title"], row["label_override"])
        if found:
            return {"session_type": found, "source": "detected"}
    if rows:
        return {"session_type": None, "source": "activity"}
    return None


def actual_type(
    conn: sqlite3.Connection, user_id: int, date: str,
    planned: Optional[str], planned_tier: Optional[str],
) -> Optional[str]:
    """The session type the day counts as, for ratings and levels.

    A known done type wins; otherwise the planned one when the day was a
    training day and something was recorded or nothing contradicts it.
    None for a day with no session to count (rest, recovery or nothing
    planned, and no known session done; or declared nothing / other).
    """
    done = done_on(conn, user_id, date)
    if done and done["session_type"] in _labels():
        return done["session_type"]
    if done and done["session_type"] in (OTHER, NOTHING):
        return None
    if planned and (planned_tier or "train") == "train":
        return planned
    return None


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db

    conn = db.connect(Path(tempfile.mkdtemp()) / "done.db")
    db.init_db(conn)
    uid = db.create_user(conn, "d", "pw-123456")

    # Names from the watch, a custom profile, a correction.
    assert detected_type("Smart Coach - Kettlebell a la maison niveau 2") == "kettlebell"
    assert detected_type("Smart Coach TEST - Kettlebell B") == "kettlebell"
    assert detected_type("Smart Coach - Muscu bas du corps niveau 4") == "lower_body"
    assert detected_type("Musculation", "Calisthenie") == "calisthenics"
    assert detected_type("Tapis du soir") == "treadmill"
    assert detected_type("Smart Coach - Bonus Abdos & bras kettlebell") is None
    assert detected_type("Course a pied") is None and detected_type(None) is None

    day = "2026-10-16"
    assert done_on(conn, uid, day) is None
    # The reported case: a recovery walk planned, kettlebell done.
    conn.execute(
        "INSERT INTO exercise_sessions (uuid, user_id, start_utc, end_utc, "
        "local_date, exercise_type, title) VALUES ('k', ?, "
        "'2026-10-16T18:00:00+00:00', '2026-10-16T18:25:00+00:00', ?, 70, "
        "'Smart Coach - Kettlebell a la maison niveau 2')", (uid, day),
    )
    conn.commit()
    assert done_on(conn, uid, day) == {"session_type": "kettlebell",
                                       "source": "detected"}
    assert actual_type(conn, uid, day, "kettlebell", "recovery") == "kettlebell"
    # An unnamed activity on a training day counts as the plan; on a
    # recovery day it is not a session to rate.
    conn.execute("UPDATE exercise_sessions SET title = 'Musculation'")
    conn.commit()
    assert done_on(conn, uid, day)["source"] == "activity"
    assert actual_type(conn, uid, day, "upper_body", "train") == "upper_body"
    assert actual_type(conn, uid, day, "kettlebell", "recovery") is None
    # A declaration wins over everything, and can be taken back.
    declare(conn, uid, day, "kettlebell", today="2026-10-17")
    assert done_on(conn, uid, day)["source"] == "declared"
    assert actual_type(conn, uid, day, "upper_body", "train") == "kettlebell"
    declare(conn, uid, day, NOTHING, today="2026-10-17")
    assert actual_type(conn, uid, day, "upper_body", "train") is None
    clear(conn, uid, day)
    assert done_on(conn, uid, day)["source"] == "activity"
    for bad_type, bad_day in (("yoga", day), ("kettlebell", "2026-10-18"),
                              ("kettlebell", "2026-08-01"),
                              ("kettlebell", "hier")):
        try:
            declare(conn, uid, bad_day, bad_type, today="2026-10-17")
        except ValueError:
            continue
        raise AssertionError((bad_type, bad_day))
    print("done_sessions.py: all checks passed")
