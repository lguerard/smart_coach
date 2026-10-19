#!/usr/bin/env python3
"""What the morning message says about yesterday and about what changed.

The message used to make one point about yesterday and list today's
session. The person wants the day before reviewed -- was the session
done, how did it feel, did the food and the steps follow -- and the
changes explained: what is different tonight from the last session of
the same kind, and why. Both are worked out here, from the database,
so the message only has to say them.
"""

import datetime as dt
import json
import re
import sqlite3
from typing import Optional

RATING_FR = {"easy": "trop facile", "right": "bien", "hard": "trop dure"}
_KG = re.compile(r" \((\d+(?:[.,]\d+)?) kg\)")


def _yesterday(date: str) -> str:
    return (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()


def yesterday_training(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> dict:
    """Yesterday's session: planned, done, how long, how it felt.

    Returns:
        dict: ``planned`` (session label, or None), ``planned_tier``
        (train / recovery / rest / None), ``done`` (label of what was
        done; None when nothing is known), ``recorded`` (a workout was
        recorded or declared), ``minutes`` (longest recorded workout),
        ``rating`` (French, or None).
    """
    import done_sessions
    import training

    day = _yesterday(date)
    log = conn.execute(
        "SELECT session_type, tier FROM coach_log WHERE user_id = ? AND "
        "local_date = ? ORDER BY id DESC LIMIT 1", (user_id, day),
    ).fetchone()
    labels = training.SESSION_LABEL_FR
    planned = log["session_type"] if log else None
    tier = (log["tier"] or training.TIER_TRAIN) if log else None
    known = done_sessions.done_on(conn, user_id, day)
    actual = done_sessions.actual_type(conn, user_id, day, planned, tier)
    rating = conn.execute(
        "SELECT rating FROM session_feedback WHERE user_id = ? AND "
        "local_date = ?", (user_id, day),
    ).fetchone()
    minutes = conn.execute(
        "SELECT MAX((julianday(end_utc) - julianday(start_utc)) * 1440) AS m "
        "FROM exercise_sessions WHERE user_id = ? AND local_date = ?",
        (user_id, day),
    ).fetchone()["m"]
    done = None
    if known and known["session_type"] == done_sessions.NOTHING:
        done = "rien"
    elif known and known["session_type"] == done_sessions.OTHER:
        done = "une autre activite"
    elif actual and known:
        done = labels.get(actual, actual)
    return {
        "date": day,
        "planned": labels.get(planned, planned) if planned else None,
        "planned_tier": tier,
        "done": done,
        "recorded": bool(known) and done != "rien",
        "minutes": round(minutes) if minutes else None,
        "rating": RATING_FR.get(rating["rating"]) if rating else None,
    }


def _bell(snapshot: dict) -> Optional[str]:
    for move in snapshot.get("moves") or []:
        found = _KG.search(move["name"])
        if found:
            return found.group(1)
    return None


def _plain(name: str) -> str:
    return _KG.sub("", name)


def since_last_session(
    conn: sqlite3.Connection, user_id: int, date: str, session_type: str,
    level: int, values: dict,
) -> Optional[dict]:
    """Tonight's session against the last one of the same type.

    Returns:
        dict | None: ``last_date``, ``days``, ``level`` / ``rounds`` /
        ``work_sec`` / ``duration_min`` / ``bell_kg`` as ``{from, to}``
        (only those that changed), ``harder`` (moves stepped up to a
        harder variant: ``{from, to}``), ``added`` and ``removed`` (move
        names). None when there is no earlier session of that type.
    """
    import session_history
    import training

    row = conn.execute(
        "SELECT local_date, level, values_json FROM coach_log WHERE "
        "user_id = ? AND session_type = ? AND local_date < ? AND "
        "COALESCE(tier, 'train') = 'train' ORDER BY local_date DESC, id DESC "
        "LIMIT 1", (user_id, session_type, date),
    ).fetchone()
    if row is None:
        return None
    if row["values_json"]:
        before = json.loads(row["values_json"])
    else:
        before = session_history.snapshot(training.user_session_values(
            conn, user_id, session_type, row["level"], row["local_date"]))
    now = session_history.snapshot(values)
    out = {
        "last_date": row["local_date"],
        "days": (dt.date.fromisoformat(date)
                 - dt.date.fromisoformat(row["local_date"])).days,
    }

    def changed(key, a, b):
        if a is not None and b is not None and a != b:
            out[key] = {"from": a, "to": b}

    changed("level", row["level"], level)
    if "moves" in now:
        changed("rounds", before.get("rounds"), now.get("rounds"))
        changed("duration_min", before.get("duration_min"), now.get("duration_min"))
        timed = [m["amount"] for m in now["moves"] if m["unit"] == "s"]
        timed_before = [m["amount"] for m in before.get("moves") or []
                        if m["unit"] == "s"]
        if timed and timed_before and len(set(timed)) == 1 and len(set(timed_before)) == 1:
            changed("work_sec", timed_before[0], timed[0])
        changed("bell_kg", _bell(before), _bell(now))
        old = {session_history._key(m): m for m in before.get("moves") or []}
        new = {session_history._key(m): m for m in now["moves"]}
        harder = [{"from": _plain(old[k]["name"]), "to": _plain(m["name"])}
                  for k, m in new.items()
                  if k in old and _plain(old[k]["name"]) != _plain(m["name"])]
        added = [_plain(m["name"]) for k, m in new.items() if k not in old]
        removed = [_plain(m["name"]) for k, m in old.items() if k not in new]
        if harder:
            out["harder"] = harder
        if added:
            out["added"] = added
        if removed:
            out["removed"] = removed
    else:
        for key in ("speed_kmh", "incline_pct", "duration_min"):
            changed(key, before.get(key), now.get(key))
    return out


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db
    import session_history
    import training

    conn = db.connect(Path(tempfile.mkdtemp()) / "review.db")
    db.init_db(conn)
    uid = db.create_user(conn, "r", "pw-123456")
    db.set_setting(conn, uid, "kettlebell_kg", "6")
    today = "2026-10-20"

    # Nothing known yet: no planned session, nothing done.
    empty = yesterday_training(conn, uid, today)
    assert empty["planned"] is None and empty["done"] is None, empty

    # Yesterday: kettlebell planned, done for 26 min, rated "right".
    conn.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message, tier, values_json) VALUES (?, 'x', "
        "'2026-10-19', 'yellow', 'kettlebell', 4, 'm', 'train', ?)",
        (uid, json.dumps(session_history.snapshot(training.user_session_values(
            conn, uid, "kettlebell", 4, "2026-10-19")))),
    )
    conn.execute(
        "INSERT INTO exercise_sessions (uuid, user_id, start_utc, end_utc, "
        "local_date, title) VALUES ('e', ?, '2026-10-19T18:00:00+00:00', "
        "'2026-10-19T18:26:00+00:00', '2026-10-19', 'Smart Coach - Kettlebell')",
        (uid,))
    conn.execute(
        "INSERT INTO session_feedback (user_id, local_date, session_type, "
        "rating, created_at) VALUES (?, '2026-10-19', 'kettlebell', 'right', "
        "'x')", (uid,))
    conn.commit()
    done = yesterday_training(conn, uid, today)
    assert done["planned"] == done["done"] == "Kettlebell a la maison", done
    assert done["minutes"] == 26 and done["rating"] == "bien", done
    assert done["recorded"], done

    # Tonight one level up, 8 kg bell: what changed is listed.
    db.set_setting(conn, uid, "kettlebell_kg", "8")
    tonight = training.user_session_values(conn, uid, "kettlebell", 5, today)
    diff = since_last_session(conn, uid, today, "kettlebell", 5, tonight)
    assert diff["last_date"] == "2026-10-19" and diff["days"] == 1, diff
    assert diff["level"] == {"from": 4, "to": 5}, diff
    assert diff["work_sec"]["to"] > diff["work_sec"]["from"], diff
    assert diff["bell_kg"] == {"from": "6", "to": "8"}, diff
    assert "added" not in diff and "removed" not in diff, diff
    # An own move added on the Programme page shows up as added.
    import custom_moves

    custom_moves.add_move(conn, uid, "kettlebell", "", "Pompes diamant", "",
                          "reps", 10, "round")
    tonight = training.user_session_values(conn, uid, "kettlebell", 5, today)
    diff = since_last_session(conn, uid, today, "kettlebell", 5, tonight)
    assert diff["added"] == ["Pompes diamant"], diff
    # No earlier session of that type: nothing to compare.
    assert since_last_session(conn, uid, today, "lower_body", 3, {}) is None

    # Declared "nothing": said as such, not as a session done.
    conn.execute(
        "INSERT INTO session_done (user_id, local_date, session_type, "
        "created_at) VALUES (?, '2026-10-19', 'none', 'x')", (uid,))
    conn.commit()
    nothing = yesterday_training(conn, uid, today)
    assert nothing["done"] == "rien" and not nothing["recorded"], nothing
    print("day_review.py: all checks passed")
