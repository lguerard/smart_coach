#!/usr/bin/env python3
"""How the person says they slept: the watch can be wrong.

A low resting heart rate lying still, awake, looks like sleep to the
watch, so its score can say a good night after a bad one. The morning
question ("Comment as-tu dormi ?", three buttons in the notification
or on Today) records what the person felt; ``metrics.daily_wellness``
carries it as ``sleep_felt`` and the sleep vote then follows it
instead of the score (``training._sleep_vote``).
"""

import datetime as dt
import sqlite3
from typing import Optional

RATINGS = ("good", "ok", "bad")
LABEL_FR = {"good": "bien dormi", "ok": "nuit moyenne", "bad": "mal dormi"}
BUTTON_FR = {"good": "Bien", "ok": "Moyen", "bad": "Mal"}
# "bad" is a yellow vote that also keeps the day from being green
# (training.compute_status) and the level from going up: it holds the
# session, it never cancels it -- nights are often bad for a while, and
# a walk every time would stop the progress the sessions are for.
VOTE = {"good": "green", "ok": "yellow", "bad": "yellow"}


def record(
    conn: sqlite3.Connection, user_id: int, date: str, rating: str,
) -> bool:
    """Store (or change) the night that ended on ``date``.

    Returns:
        bool: False for an unknown rating (nothing stored).
    """
    if rating not in RATINGS:
        return False
    conn.execute(
        "INSERT INTO sleep_feedback (user_id, local_date, rating, created_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (user_id, local_date) DO UPDATE SET "
        "rating = excluded.rating, created_at = excluded.created_at",
        (user_id, date, rating, dt.datetime.now(dt.timezone.utc).isoformat()),
    )
    conn.commit()
    return True


def felt(conn: sqlite3.Connection, user_id: int, date: str) -> Optional[str]:
    """The rating of the night that ended on ``date``, or None."""
    row = conn.execute(
        "SELECT rating FROM sleep_feedback WHERE user_id = ? AND local_date = ?",
        (user_id, date),
    ).fetchone()
    return row["rating"] if row else None


def series(
    conn: sqlite3.Connection, user_id: int, start: str, end: str,
) -> dict:
    """``{date: rating}`` between two dates, inclusive."""
    return {
        r["local_date"]: r["rating"] for r in conn.execute(
            "SELECT local_date, rating FROM sleep_feedback WHERE user_id = ? "
            "AND local_date BETWEEN ? AND ?", (user_id, start, end),
        )
    }


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db

    conn = db.connect(Path(tempfile.mkdtemp()) / "sleep.db")
    db.init_db(conn)
    uid = db.create_user(conn, "s", "pw-123456")
    other = db.create_user(conn, "o", "pw-123456")
    assert felt(conn, uid, "2026-10-19") is None
    assert record(conn, uid, "2026-10-19", "bad")
    assert record(conn, uid, "2026-10-19", "ok")  # changed: kept once
    assert felt(conn, uid, "2026-10-19") == "ok"
    assert not record(conn, uid, "2026-10-19", "terrible")
    assert felt(conn, other, "2026-10-19") is None
    record(conn, uid, "2026-10-17", "bad")
    assert series(conn, uid, "2026-10-17", "2026-10-19") == {
        "2026-10-17": "bad", "2026-10-19": "ok"}
    print("sleep_feedback.py: all checks passed")
