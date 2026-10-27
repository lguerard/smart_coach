#!/usr/bin/env python3
"""Waist circumference, entered by hand on the Corps page.

The scale alone cannot tell fat from water and muscle: in the first
weeks of training the weight can rise while fat goes. The waist, taken
once a week the same way (navel height, morning, tape not pulled
tight), tells them apart -- down with the weight up is muscle and water,
both up is fat. ``trend`` reads the two together.
"""

import datetime as dt
import sqlite3
from typing import Optional

MIN_CM, MAX_CM = 50.0, 200.0
# A change smaller than this is the tape, not the body.
NOISE_CM = 0.5
NOISE_KG = 0.3
# The reading compared with the latest: the closest to four weeks
# before it, between two and six.
COMPARE_MIN_DAYS, COMPARE_DAYS, COMPARE_MAX_DAYS = 14, 28, 42


def record(conn: sqlite3.Connection, user_id: int, date: str, cm) -> bool:
    """Store (or replace) the day's measure; False when out of range."""
    try:
        value = round(float(str(cm).replace(",", ".")), 1)
    except ValueError:
        return False
    if not MIN_CM <= value <= MAX_CM:
        return False
    conn.execute(
        "INSERT INTO waist_measurements (user_id, local_date, cm, created_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (user_id, local_date) DO UPDATE SET "
        "cm = excluded.cm, created_at = excluded.created_at",
        (user_id, date, value, dt.datetime.now(dt.timezone.utc).isoformat()),
    )
    conn.commit()
    return True


def delete(conn: sqlite3.Connection, user_id: int, date: str) -> None:
    conn.execute("DELETE FROM waist_measurements WHERE user_id = ? AND "
                 "local_date = ?", (user_id, date))
    conn.commit()


def _weight_near(conn, user_id: int, date: str) -> Optional[float]:
    """The weight around a date: the average of the readings within
    three days, so one odd morning does not decide."""
    row = conn.execute(
        "SELECT AVG(kg) AS kg FROM weight WHERE user_id = ? AND local_date "
        "BETWEEN date(?, '-3 days') AND date(?, '+3 days')",
        (user_id, date, date),
    ).fetchone()
    return round(row["kg"], 1) if row and row["kg"] is not None else None


def history(conn: sqlite3.Connection, user_id: int, limit: int = 26) -> list[dict]:
    """The latest measures, newest first: ``{date, cm, weight_kg}``."""
    return [
        {"date": row["local_date"], "cm": row["cm"],
         "weight_kg": _weight_near(conn, user_id, row["local_date"])}
        for row in conn.execute(
            "SELECT local_date, cm FROM waist_measurements WHERE user_id = ? "
            "ORDER BY local_date DESC LIMIT ?", (user_id, limit))
    ]


def trend(conn: sqlite3.Connection, user_id: int, date: str) -> Optional[dict]:
    """The latest measure against the one about four weeks before it,
    with the weight over the same span and what the two say together.

    Returns:
        dict | None: ``latest_cm``, ``latest_date``, ``change_cm``,
        ``days``, ``weight_change_kg`` (None without weights) and
        ``reading``: ``fat_loss`` (both down), ``recomposition`` (waist
        down, weight up or flat: muscle and water), ``fat_gain`` (both
        up), ``waist_up`` (waist up, weight flat or down), ``steady``.
        None without two measures two to six weeks apart.
    """
    latest = conn.execute(
        "SELECT local_date, cm FROM waist_measurements WHERE user_id = ? AND "
        "local_date <= ? ORDER BY local_date DESC LIMIT 1", (user_id, date),
    ).fetchone()
    if latest is None:
        return None
    day = dt.date.fromisoformat(latest["local_date"])
    earlier = [
        row for row in conn.execute(
            "SELECT local_date, cm FROM waist_measurements WHERE user_id = ? "
            "AND local_date BETWEEN ? AND ?",
            (user_id, (day - dt.timedelta(days=COMPARE_MAX_DAYS)).isoformat(),
             (day - dt.timedelta(days=COMPARE_MIN_DAYS)).isoformat()))
    ]
    if not earlier:
        return None
    before = min(earlier, key=lambda r: abs(
        (day - dt.date.fromisoformat(r["local_date"])).days - COMPARE_DAYS))
    change = round(latest["cm"] - before["cm"], 1)
    w_now = _weight_near(conn, user_id, latest["local_date"])
    w_then = _weight_near(conn, user_id, before["local_date"])
    weight_change = (round(w_now - w_then, 1)
                     if w_now is not None and w_then is not None else None)
    if change <= -NOISE_CM:
        reading = ("fat_loss" if weight_change is not None
                   and weight_change <= -NOISE_KG else "recomposition")
    elif change >= NOISE_CM:
        reading = ("fat_gain" if weight_change is not None
                   and weight_change >= NOISE_KG else "waist_up")
    else:
        reading = "steady"
    return {
        "latest_cm": latest["cm"], "latest_date": latest["local_date"],
        "change_cm": change,
        "days": (day - dt.date.fromisoformat(before["local_date"])).days,
        "weight_change_kg": weight_change, "reading": reading,
    }


READING_FR = {
    "fat_loss": "le tour de taille et le poids baissent : tu perds du gras",
    "recomposition": ("le tour de taille baisse alors que le poids ne baisse "
                      "pas : c'est du muscle et de l'eau, le gras part"),
    "fat_gain": ("le tour de taille et le poids montent : c'est surtout du "
                 "gras, les calories sont à resserrer"),
    "waist_up": "le tour de taille monte un peu, le poids non",
    "steady": "le tour de taille ne bouge pas",
}


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db

    conn = db.connect(Path(tempfile.mkdtemp()) / "waist.db")
    db.init_db(conn)
    uid = db.create_user(conn, "w", "pw-123456")
    assert not record(conn, uid, "2026-10-01", "12")       # out of range
    assert not record(conn, uid, "2026-10-01", "beaucoup")
    assert record(conn, uid, "2026-10-01", "112,5")
    assert trend(conn, uid, "2026-10-01") is None           # one measure
    assert record(conn, uid, "2026-10-08", 112)
    assert trend(conn, uid, "2026-10-08") is None           # only a week apart
    # Four weeks later the waist is down 2 cm while the weight rose:
    # muscle and water, not fat.
    for day, kg in (("2026-10-01", 98.0), ("2026-10-02", 98.2),
                    ("2026-10-28", 98.9), ("2026-10-29", 99.1)):
        conn.execute("INSERT INTO weight VALUES (?, ?, ?, ?, ?)",
                     (f"w{day}", uid, day + "T06:00:00+00:00", day, kg))
    conn.commit()
    assert record(conn, uid, "2026-10-29", 110.5)
    seen = trend(conn, uid, "2026-10-30")
    assert seen["change_cm"] == -2.0 and seen["days"] == 28, seen
    assert seen["weight_change_kg"] == 0.9 and seen["reading"] == "recomposition", seen
    # Both up: fat.
    assert record(conn, uid, "2026-10-29", 114)
    assert trend(conn, uid, "2026-10-30")["reading"] == "fat_gain"
    # A re-entry the same day replaces; the list is newest first.
    assert [h["date"] for h in history(conn, uid)] == [
        "2026-10-29", "2026-10-08", "2026-10-01"]
    assert history(conn, uid)[0]["cm"] == 114 and history(conn, uid)[0]["weight_kg"] == 99.0
    delete(conn, uid, "2026-10-29")
    assert len(history(conn, uid)) == 2
    print("waist.py: all checks passed")
