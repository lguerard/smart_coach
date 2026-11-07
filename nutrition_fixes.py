#!/usr/bin/env python3
"""A day's food totals typed in by hand, until Health Connect has them.

MyFitnessPal hands the diary to Health Connect only when it syncs; a day
logged but not synced before the nightly export reaches the coach short
(a dinner missing), and the morning message is written from it. The
person can type the day's totals as MyFitnessPal shows them, on the
Repas page. They are stored here, and the gap between them and what
Health Connect has is kept as one nutrition row (``smartcoach-fix-``):
every sum of the day -- the review, the week, the calorie target -- is
then right at once. The Health Connect reconciliation leaves
``smartcoach-`` rows alone; after each import the gap is recomputed, and
once Health Connect has caught up the correction removes itself.
"""

import datetime as dt
import sqlite3
from typing import Optional

PREFIX = "smartcoach-fix-"
KCAL_RANGE = (300, 8000)
# Health Connect within this of the typed total: it has caught up.
CAUGHT_UP_KCAL = 50
KEYS = ("calories", "protein_g", "carbs_g", "fat_g")


def _logged(conn, user_id: int, date: str) -> dict:
    row = conn.execute(
        "SELECT SUM(calories) AS calories, SUM(protein_g) AS protein_g, "
        "SUM(carbs_g) AS carbs_g, SUM(fat_g) AS fat_g FROM nutrition WHERE "
        "user_id = ? AND local_date = ? AND uuid != ?",
        (user_id, date, PREFIX + date),
    ).fetchone()
    return {k: row[k] or 0.0 for k in KEYS}


def set_fix(
    conn: sqlite3.Connection, user_id: int, date: str, calories,
    protein_g=None, carbs_g=None, fat_g=None,
) -> str:
    """Store the day's real totals and apply them.

    Returns:
        str: What happened, in French.

    Raises:
        ValueError: A date that is not a past or current day, or totals
            out of range.
    """
    dt.date.fromisoformat(date)
    values = {}
    for key, raw in zip(KEYS, (calories, protein_g, carbs_g, fat_g)):
        text = str(raw if raw is not None else "").strip().replace(",", ".")
        if not text:
            values[key] = None
            continue
        try:
            values[key] = float(text)
        except ValueError:
            raise ValueError("Les totaux doivent être des nombres.") from None
        if values[key] < 0:
            raise ValueError("Les totaux ne peuvent pas être négatifs.")
    low, high = KCAL_RANGE
    if values["calories"] is None or not low <= values["calories"] <= high:
        raise ValueError(f"Les calories du jour : entre {low} et {high} kcal.")
    conn.execute(
        "INSERT INTO nutrition_fixes (user_id, local_date, calories, protein_g, "
        "carbs_g, fat_g, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT "
        "(user_id, local_date) DO UPDATE SET calories = excluded.calories, "
        "protein_g = excluded.protein_g, carbs_g = excluded.carbs_g, "
        "fat_g = excluded.fat_g, created_at = excluded.created_at",
        (user_id, date, *(values[k] for k in KEYS),
         dt.datetime.now(dt.timezone.utc).isoformat()))
    conn.commit()
    outcome = apply(conn, user_id, date)
    if outcome == "caught_up":
        return "Health Connect a déjà ces totaux : rien à corriger."
    return (f"Journée du {date[8:10]}/{date[5:7]} corrigée : "
            f"{round(values['calories'])} kcal.")


def apply(conn: sqlite3.Connection, user_id: int, date: str) -> Optional[str]:
    """Bring the day's correction row in line with the typed totals.

    Returns:
        str | None: ``applied``, ``caught_up`` (Health Connect has them:
        the correction is gone) or None (no correction that day).
    """
    fix = conn.execute(
        "SELECT * FROM nutrition_fixes WHERE user_id = ? AND local_date = ?",
        (user_id, date)).fetchone()
    if fix is None:
        return None
    logged = _logged(conn, user_id, date)
    conn.execute("DELETE FROM nutrition WHERE uuid = ? AND user_id = ?",
                 (PREFIX + date, user_id))
    if logged["calories"] >= fix["calories"] - CAUGHT_UP_KCAL:
        conn.execute("DELETE FROM nutrition_fixes WHERE user_id = ? AND "
                     "local_date = ?", (user_id, date))
        conn.commit()
        return "caught_up"
    gap = {k: max(0.0, (fix[k] or logged[k]) - logged[k]) for k in KEYS}
    noon = f"{date}T12:00:00+00:00"
    conn.execute(
        "INSERT INTO nutrition (uuid, user_id, start_utc, end_utc, local_date, "
        "meal_type, calories, protein_g, carbs_g, fat_g) VALUES "
        "(?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (PREFIX + date, user_id, noon, noon, date, *(gap[k] for k in KEYS)))
    conn.commit()
    return "applied"


def apply_all(conn: sqlite3.Connection, user_id: int) -> dict:
    """After an import: every correction rechecked against Health
    Connect. Returns ``{date: outcome}``."""
    return {row["local_date"]: apply(conn, user_id, row["local_date"])
            for row in conn.execute(
                "SELECT local_date FROM nutrition_fixes WHERE user_id = ?",
                (user_id,)).fetchall()}


def remove(conn: sqlite3.Connection, user_id: int, date: str) -> None:
    conn.execute("DELETE FROM nutrition_fixes WHERE user_id = ? AND "
                 "local_date = ?", (user_id, date))
    conn.execute("DELETE FROM nutrition WHERE uuid = ? AND user_id = ?",
                 (PREFIX + date, user_id))
    conn.commit()


def active(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """The corrections waiting for Health Connect, newest first."""
    return [dict(r) for r in conn.execute(
        "SELECT local_date, calories, protein_g, carbs_g, fat_g FROM "
        "nutrition_fixes WHERE user_id = ? ORDER BY local_date DESC",
        (user_id,))]


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db

    conn = db.connect(Path(tempfile.mkdtemp()) / "fixes.db")
    db.init_db(conn)
    uid = db.create_user(conn, "f", "pw-123456")
    other = db.create_user(conn, "o", "pw-123456")
    day = "2026-11-06"

    def meal(uuid, user, kcal, protein):
        conn.execute(
            "INSERT INTO nutrition (uuid, user_id, start_utc, end_utc, "
            "local_date, meal_type, calories, protein_g, carbs_g, fat_g) "
            "VALUES (?, ?, 'x', 'x', ?, 1, ?, ?, 100, 30)",
            (uuid, user, day, kcal, protein))
        conn.commit()

    def total(user):
        return conn.execute(
            "SELECT SUM(calories) AS k, SUM(protein_g) AS p FROM nutrition "
            "WHERE user_id = ? AND local_date = ?", (user, day)).fetchone()

    # Health Connect has breakfast and lunch only: 1,250 kcal.
    meal("hc-1", uid, 600, 40)
    meal("hc-2", uid, 650, 35)
    meal("hc-o", other, 900, 50)
    try:
        set_fix(conn, uid, day, "100")
        raise AssertionError("too low a day must be refused")
    except ValueError:
        pass
    note = set_fix(conn, uid, day, "2327", "101", "269", "75")
    assert "2327 kcal" in note, note
    assert round(total(uid)["k"]) == 2327 and round(total(uid)["p"]) == 101
    assert round(total(other)["k"]) == 900  # the other account untouched
    # Typed again, lower: the gap follows, never a second row.
    set_fix(conn, uid, day, "2000")
    assert round(total(uid)["k"]) == 2000
    assert conn.execute("SELECT COUNT(*) FROM nutrition WHERE uuid LIKE ?",
                        (PREFIX + "%",)).fetchone()[0] == 1
    # Without protein typed: the protein stays what Health Connect has.
    assert round(total(uid)["p"]) == 75
    # Health Connect catches up (the dinner arrives): the fix removes itself.
    meal("hc-3", uid, 760, 26)
    assert apply_all(conn, uid) == {day: "caught_up"}
    assert round(total(uid)["k"]) == 2010 and active(conn, uid) == []
    # Removed by hand.
    set_fix(conn, uid, "2026-11-05", "1800")
    remove(conn, uid, "2026-11-05")
    assert active(conn, uid) == [] and conn.execute(
        "SELECT COUNT(*) FROM nutrition WHERE uuid LIKE ?",
        (PREFIX + "%",)).fetchone()[0] == 0
    print("nutrition_fixes.py: all checks passed")
