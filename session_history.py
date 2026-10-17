#!/usr/bin/env python3
"""History per session type: what each session asked for, then and now.

A level is a number; what it means is in the session -- how many tours,
which moves, how many reps. The morning run keeps a compact copy of the
day's session (``coach_log.values_json``, ``snapshot``) so the history
shows what was really asked, own moves and Garmin edits included. Days
logged before that column existed, and sessions done on another day
than planned, are rebuilt from the library at their level (marked
``approx``: the person's own moves of that time are not known).
"""

import datetime as dt
import json
import sqlite3
from typing import Optional

import exercise_library
import training

HISTORY_DAYS = 180
# "Then" in the comparison: the session closest to this many days ago
# (the oldest one when the history is shorter).
COMPARE_DAYS = 28
RATING_LABEL_FR = {"easy": "trop facile", "right": "bien", "hard": "trop dur"}
_TREADMILL_KEYS = ("speed_kmh", "incline_pct", "duration_min")


def snapshot(values: dict) -> dict:
    """The part of a session's values the history shows.

    Returns:
        dict: ``rounds``, ``duration_min``, ``moves`` (each ``name``,
        ``amount``, ``unit`` ``s``/``reps``, ``per_leg``), ``after`` (the
        once-at-the-end moves) for a circuit; the walk's speed, incline
        and length for the treadmill.
    """
    if "speed_kmh" in values:
        return {k: values[k] for k in _TREADMILL_KEYS if k in values}
    moves = [
        {"slot": slot, "name": info["name"], "amount": values[slot],
         "unit": "s" if slot.endswith("_sec") else "reps",
         "per_leg": slot.endswith("_per_leg")}
        for slot, info in (values.get("variants") or {}).items()
        if slot in values
    ]
    after = [
        {"name": item["name"], "amount": item["amount"],
         "unit": "s" if item["unit"] == "sec" else "reps", "per_leg": False}
        for item in values.get("finisher") or []
    ]
    return {"rounds": values.get("rounds") or 1,
            "duration_min": values.get("duration_min"),
            "moves": moves, "after": after}


def totals(snap: dict) -> dict:
    """Total work of a circuit: reps and seconds over all its tours."""
    if "moves" not in snap:
        return {}
    rounds = snap.get("rounds") or 1
    reps = secs = 0
    for move in snap["moves"]:
        amount = move["amount"] * (2 if move["per_leg"] else 1)
        if move["unit"] == "s":
            secs += amount * rounds
        else:
            reps += amount * rounds
    for move in snap.get("after") or []:
        if move["unit"] == "s":
            secs += move["amount"]
        else:
            reps += move["amount"]
    return {"reps": reps, "work_sec": secs}


def _logged(conn: sqlite3.Connection, user_id: int, start: str, end: str) -> dict:
    return {
        r["local_date"]: r for r in conn.execute(
            "SELECT c.local_date, c.session_type, c.tier, c.level_reason, "
            "c.values_json FROM coach_log c WHERE c.user_id = ? AND "
            "c.local_date BETWEEN ? AND ? AND c.id = (SELECT MAX(id) FROM "
            "coach_log WHERE user_id = c.user_id AND local_date = "
            "c.local_date)", (user_id, start, end),
        )
    }


def _activity_minutes(conn: sqlite3.Connection, user_id: int, date: str) -> Optional[int]:
    """Length of the day's longest recorded workout, in minutes."""
    row = conn.execute(
        "SELECT MAX((julianday(end_utc) - julianday(start_utc)) * 1440) AS m "
        "FROM exercise_sessions WHERE user_id = ? AND local_date = ?",
        (user_id, date),
    ).fetchone()
    return round(row["m"]) if row and row["m"] else None


def history(
    conn: sqlite3.Connection, user_id: int, session_type: str, date: str,
    days: int = HISTORY_DAYS,
) -> list[dict]:
    """Every session of ``session_type`` over ``days``, newest first.

    A day counts when that session was done (declared or read from the
    watch) or planned as a training day (``training.rateable_sessions``).

    Returns:
        list[dict]: ``date``, ``level``, ``snapshot``, ``totals``,
        ``approx`` (rebuilt, not stored), ``rating`` (French label or
        None), ``done`` (``recorded`` / ``declared`` / ``unknown``),
        ``planned`` (False when done instead of something else),
        ``minutes`` (recorded workout length), ``level_reason``.
    """
    start = (dt.date.fromisoformat(date) - dt.timedelta(days=days - 1)).isoformat()
    logged = _logged(conn, user_id, start, date)
    cap = training.session_cap_min(conn, user_id)
    equipment = exercise_library.equipment_for(conn, user_id)
    rows = []
    for row in training.rateable_sessions(conn, user_id, date, days):
        if row["session_type"] != session_type:
            continue
        log = logged.get(row["date"])
        as_planned = row["planned"] == session_type and (
            row["planned_tier"] or training.TIER_TRAIN) == training.TIER_TRAIN
        stored = log["values_json"] if log is not None and as_planned else None
        if stored:
            snap = json.loads(stored)
        else:
            snap = snapshot(training.session_values(
                session_type, row["level"], cap, row["date"], equipment,
            ))
        done = row["done"] or {}
        rows.append({
            "date": row["date"], "level": row["level"], "snapshot": snap,
            "totals": totals(snap), "approx": not stored,
            "rating": RATING_LABEL_FR.get(row["rating"]),
            "done": ("declared" if done.get("source") == "declared"
                     else "recorded" if done else "unknown"),
            "planned": as_planned,
            "minutes": _activity_minutes(conn, user_id, row["date"]),
            "level_reason": log["level_reason"] if log is not None and as_planned else None,
        })
    return rows


def _delta(then, now) -> Optional[dict]:
    if then is None or now is None:
        return None
    return {"then": then, "now": now, "delta": round(now - then, 1)}


def compare(rows: list[dict], compare_days: int = COMPARE_DAYS) -> Optional[dict]:
    """The latest session against the one about ``compare_days`` before.

    Returns:
        dict | None: ``then_date``, ``now_date``, ``days``, ``fields``
        (label -> ``{then, now, delta}``; only what both have),
        ``moves`` (per programme slot: same move with its change, a
        harder variant with ``then_name``, or ``new``) and ``harder``
        (a variant went up: fewer reps can still be progress). None with
        fewer than two sessions.
    """
    if len(rows) < 2:
        return None
    now = rows[0]
    target = dt.date.fromisoformat(now["date"]) - dt.timedelta(days=compare_days)
    # Same moves first: kettlebell circuits A and B alternate, and A
    # against B compares nothing.
    slots = _slots(now["snapshot"])
    older = [r for r in rows[1:] if _slots(r["snapshot"]) == slots] or rows[1:]
    then = min(older, key=lambda r: abs((dt.date.fromisoformat(r["date"]) - target).days))
    a, b = then["snapshot"], now["snapshot"]
    fields = {"Niveau": _delta(then["level"], now["level"])}
    if "moves" in b:
        fields["Tours"] = _delta(a.get("rounds"), b.get("rounds"))
        fields["Répétitions au total"] = _delta(
            then["totals"].get("reps"), now["totals"].get("reps"))
        fields["Secondes d'effort"] = _delta(
            then["totals"].get("work_sec"), now["totals"].get("work_sec"))
        fields["Durée prévue (min)"] = _delta(a.get("duration_min"), b.get("duration_min"))
    else:
        fields["Vitesse (km/h)"] = _delta(a.get("speed_kmh"), b.get("speed_kmh"))
        fields["Pente (%)"] = _delta(a.get("incline_pct"), b.get("incline_pct"))
        fields["Durée (min)"] = _delta(a.get("duration_min"), b.get("duration_min"))
    before = {_key(m): m for m in a.get("moves") or []}
    moves = []
    for move in b.get("moves") or []:
        old = before.get(_key(move))
        if old is None:
            moves.append({"name": move["name"], "new": True,
                          "now": move["amount"], "unit": move["unit"]})
        else:
            moves.append({"name": move["name"], "new": False,
                          "then_name": (old["name"] if old["name"] != move["name"]
                                        else None),
                          "then": old["amount"], "now": move["amount"],
                          "unit": move["unit"]})
    fields = {k: v for k, v in fields.items()
              if v is not None and (v["then"] or v["now"])}
    return {
        "then_date": then["date"], "now_date": now["date"],
        "days": (dt.date.fromisoformat(now["date"])
                 - dt.date.fromisoformat(then["date"])).days,
        "fields": fields, "moves": moves,
        "harder": any(m.get("then_name") for m in moves),
    }


def _key(move: dict) -> str:
    """A move's place in the programme (its slot), or its name for
    snapshots without one."""
    return move.get("slot") or move["name"]


def _slots(snap: dict) -> frozenset:
    return frozenset(_key(m) for m in snap.get("moves") or [])


def types_with_history(
    conn: sqlite3.Connection, user_id: int, date: str,
    days: int = HISTORY_DAYS,
) -> list[str]:
    """Session types with at least one session, most frequent first."""
    counts: dict = {}
    for row in training.rateable_sessions(conn, user_id, date, days):
        counts[row["session_type"]] = counts.get(row["session_type"], 0) + 1
    return sorted(counts, key=lambda t: -counts[t])


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db

    conn = db.connect(Path(tempfile.mkdtemp()) / "history.db")
    db.init_db(conn)
    uid = db.create_user(conn, "h", "pw-123456")
    other = db.create_user(conn, "o", "pw-123456")
    today = "2026-10-17"

    def log(day, session_type, level, tier="train", values=None):
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, status, "
            "session_type, level, message, tier, values_json) VALUES "
            "(?, ?, ?, 'yellow', ?, ?, 'm', ?, ?)",
            (uid, day, day, session_type, level, tier,
             json.dumps(snapshot(values)) if values else None),
        )

    # Old row without stored values (rebuilt), a recent one stored with
    # an own move, a treadmill day, a recovery day with kettlebell done.
    log("2026-09-19", "upper_body", 2)
    stored = training.session_values("upper_body", 6, 30, "2026-10-16")
    stored["custom_1"] = 12
    stored["variants"]["custom_1"] = {"name": "curl kettlebell"}
    log("2026-10-16", "upper_body", 6, values=stored)
    log("2026-10-15", "treadmill", 3,
        values=training.session_values("treadmill", 3, 30, "2026-10-15"))
    log("2026-10-14", "upper_body", 5, tier="recovery")
    conn.execute(
        "INSERT INTO session_done (user_id, local_date, session_type, "
        "created_at) VALUES (?, '2026-10-14', 'kettlebell', 'x')", (uid,))
    conn.execute(
        "INSERT INTO session_feedback (user_id, local_date, session_type, "
        "rating, created_at) VALUES (?, '2026-10-16', 'upper_body', 'easy', "
        "'x')", (uid,))
    conn.execute(
        "INSERT INTO exercise_sessions (uuid, user_id, start_utc, end_utc, "
        "local_date, title) VALUES ('e1', ?, '2026-10-16T17:00:00+00:00', "
        "'2026-10-16T17:32:00+00:00', '2026-10-16', 'Smart Coach - Haut')",
        (uid,))
    conn.commit()

    upper = history(conn, uid, "upper_body", today)
    assert [r["date"] for r in upper] == ["2026-10-16", "2026-09-19"], upper
    latest, old = upper
    assert not latest["approx"] and old["approx"]
    assert any(m["name"] == "curl kettlebell" for m in latest["snapshot"]["moves"])
    assert latest["rating"] == "trop facile" and latest["minutes"] == 32
    assert latest["done"] == "recorded" and old["done"] == "unknown"
    assert latest["totals"]["reps"] > old["totals"]["reps"]
    diff = compare(upper)
    assert diff["days"] == 27 and diff["fields"]["Niveau"]["delta"] == 4
    assert diff["fields"]["Répétitions au total"]["delta"] > 0
    assert any(m["new"] and m["name"] == "curl kettlebell" for m in diff["moves"])
    # Level 2 -> 6: the push-ups moved to a harder variant, same slot.
    pushups = next(m for m in diff["moves"] if not m["new"]
                   and m["then_name"])
    assert diff["harder"] and pushups["then_name"] != pushups["name"], diff
    assert "Secondes d'effort" in diff["fields"], diff["fields"]
    # The kettlebell done on a recovery day counts as kettlebell, rebuilt.
    bell = history(conn, uid, "kettlebell", today)
    assert [(r["date"], r["planned"], r["approx"], r["done"]) for r in bell] == [
        ("2026-10-14", False, True, "declared")]
    assert compare(bell) is None
    # Kettlebell: compared with the same circuit, not the other one.
    circuit = {"A": [{"slot": "kb_a", "name": "a", "amount": 20, "unit": "s",
                      "per_leg": False}],
               "B": [{"slot": "kb_b", "name": "b", "amount": 20, "unit": "s",
                      "per_leg": False}]}
    rows = [{"date": d, "level": 3, "snapshot": {"rounds": 5, "moves": circuit[c]},
             "totals": {"reps": 0, "work_sec": 100}}
            for d, c in (("2026-10-17", "A"), ("2026-09-19", "B"),
                         ("2026-09-18", "A"))]
    diff = compare(rows)
    assert diff["then_date"] == "2026-09-18" and not diff["moves"][0]["new"], diff
    assert "Répétitions au total" not in diff["fields"], diff  # 0 -> 0
    walk = history(conn, uid, "treadmill", today)
    assert set(walk[0]["snapshot"]) == set(_TREADMILL_KEYS) and walk[0]["totals"] == {}
    assert types_with_history(conn, uid, today)[0] == "upper_body"
    assert history(conn, other, "upper_body", today) == []
    # Totals: per-leg counts both legs, the finisher once.
    assert totals({"rounds": 3, "moves": [
        {"name": "fente", "amount": 8, "unit": "reps", "per_leg": True},
        {"name": "planche", "amount": 30, "unit": "s", "per_leg": False}],
        "after": [{"name": "etirement", "amount": 30, "unit": "s",
                   "per_leg": False}]}) == {"reps": 48, "work_sec": 120}
    print("session_history.py: all checks passed")
