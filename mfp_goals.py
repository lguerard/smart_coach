#!/usr/bin/env python3
"""The day's targets, as typed into MyFitnessPal's goals.

MyFitnessPal has no public API, and Health Connect carries meals but no
goals, so the coach's targets cannot be sent there: they are typed by
hand. This works out what to type -- grams with Premium, percentages in
5 % steps with the free version -- and remembers what was last typed,
so the person hears about it when the targets have moved enough to be
worth typing again (the calorie target follows a 7-day average of the
watch's expenditure; protein and fat follow the weight).
"""

import json
import sqlite3
from typing import Optional

import db
import progress

SAVED_KEY = "mfp_goal"
NOTIFIED_KEY = "mfp_goal_notified"
# Moves smaller than this are not worth retyping.
DRIFT_KCAL = 100
DRIFT_G = 5
STEP_PCT = 5
KCAL_PER_G = {"carbs_g": 4, "fat_g": 9, "protein_g": 4}


def percent_split(kcal: int, protein_g: int, fat_g: int, carbs_g: int) -> dict:
    """The closest 5 %-step split summing to 100, as MFP's free version
    takes it. Protein short of its target counts double: it is the one
    that keeps muscle during the cut.

    Returns:
        dict: ``carbs_pct`` / ``fat_pct`` / ``protein_pct`` and the grams
        MFP will show from them (``carbs_g`` / ``fat_g`` / ``protein_g``).
    """
    wanted = {"carbs_g": carbs_g, "fat_g": fat_g, "protein_g": protein_g}
    best = None
    for protein in range(STEP_PCT, 100, STEP_PCT):
        for fat in range(STEP_PCT, 100 - protein, STEP_PCT):
            pct = {"protein_g": protein, "fat_g": fat,
                   "carbs_g": 100 - protein - fat}
            grams = {k: round(kcal * p / 100 / KCAL_PER_G[k])
                     for k, p in pct.items()}
            cost = 0.0
            for key, target in wanted.items():
                miss = (grams[key] - target) / max(target, 1)
                cost += miss * miss * (4 if key == "protein_g" and miss < 0 else 1)
            if best is None or cost < best[0]:
                best = (cost, pct, grams)
    _, pct, grams = best
    return {"carbs_pct": pct["carbs_g"], "fat_pct": pct["fat_g"],
            "protein_pct": pct["protein_g"], **grams}


def goal(conn: sqlite3.Connection, user_id: int, date: str) -> Optional[dict]:
    """Today's targets in MFP's terms, or None while one is missing.

    Returns:
        dict | None: ``kcal``, ``protein_g``, ``fat_g``, ``carbs_g``,
        ``split`` (:func:`percent_split`), ``saved`` (what was last
        typed, or None) and ``stale`` (it differs enough to retype).
    """
    targets = progress.macro_targets(conn, user_id, date)
    keys = ("calorie_target_kcal", "protein_target_g", "fat_target_g",
            "carb_target_g")
    if not all(targets.get(k) for k in keys):
        return None
    now = {"kcal": targets["calorie_target_kcal"],
           "protein_g": targets["protein_target_g"],
           "fat_g": targets["fat_target_g"],
           "carbs_g": targets["carb_target_g"]}
    last = saved(conn, user_id)
    return {**now, "split": percent_split(**now), "saved": last,
            "stale": _differs(now, last)}


def _differs(now: dict, last: Optional[dict]) -> bool:
    if not last:
        return True
    return (abs(now["kcal"] - last.get("kcal", 0)) >= DRIFT_KCAL
            or abs(now["protein_g"] - last.get("protein_g", 0)) >= DRIFT_G
            or abs(now["fat_g"] - last.get("fat_g", 0)) >= DRIFT_G)


def saved(conn: sqlite3.Connection, user_id: int) -> Optional[dict]:
    """What the person said they typed into MFP last, or None."""
    raw = db.get_setting(conn, user_id, SAVED_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def mark_typed(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    """Remember today's targets as the ones now in MFP."""
    now = goal(conn, user_id, date)
    if now is None:
        return False
    db.set_setting(conn, user_id, SAVED_KEY, json.dumps(
        {"kcal": now["kcal"], "protein_g": now["protein_g"],
         "fat_g": now["fat_g"], "carbs_g": now["carbs_g"], "date": date}))
    conn.commit()
    return True


def drift_line(conn: sqlite3.Connection, user_id: int, date: str) -> str:
    """One line for a notification when MFP's goal is out of date, once
    per new target ('' otherwise, and when nothing is logged in MFP)."""
    now = goal(conn, user_id, date)
    if now is None or not now["stale"] or not logs_food(conn, user_id, date):
        return ""
    stamp = f"{now['kcal']}/{now['protein_g']}/{now['fat_g']}"
    if db.get_setting(conn, user_id, NOTIFIED_KEY) == stamp:
        return ""
    db.set_setting(conn, user_id, NOTIFIED_KEY, stamp)
    conn.commit()
    split = now["split"]
    head = ("Ton objectif a bougé : mets à jour MyFitnessPal"
            if now["saved"] else "Mets l'objectif du coach dans MyFitnessPal")
    return (f"{head} : {now['kcal']} kcal, P{now['protein_g']} "
            f"L{now['fat_g']} G{now['carbs_g']} g (version gratuite : "
            f"G{split['carbs_pct']} L{split['fat_pct']} "
            f"P{split['protein_pct']} %). Page Repas.")


def logs_food(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    """Whether meals came from the food diary in the last week."""
    return conn.execute(
        "SELECT 1 FROM nutrition WHERE user_id = ? AND uuid NOT LIKE "
        "'smartcoach-%' AND local_date BETWEEN date(?, '-7 days') AND ? "
        "LIMIT 1", (user_id, date, date),
    ).fetchone() is not None


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    # The day of the question: 1973 kcal, P105 L68 G235.
    split = percent_split(1973, 105, 68, 235)
    assert (split["carbs_pct"] + split["fat_pct"] + split["protein_pct"]) == 100
    assert all(split[k] % STEP_PCT == 0 for k in ("carbs_pct", "fat_pct", "protein_pct"))
    assert split["protein_g"] >= 99, split  # never far short on protein
    assert abs(split["fat_g"] - 68) <= 8 and abs(split["carbs_g"] - 235) <= 25, split

    conn = db.connect(Path(tempfile.mkdtemp()) / "mfp.db")
    db.init_db(conn)
    uid = db.create_user(conn, "m", "pw-123456")
    date = "2026-10-21"
    # No weight yet: nothing to type.
    assert goal(conn, uid, date) is None and drift_line(conn, uid, date) == ""
    conn.execute("INSERT INTO weight VALUES ('w', ?, '2026-10-20T06:00:00+00:00', "
                 "'2026-10-20', 98.0)", (uid,))
    for day in range(14, 21):
        conn.execute(
            "INSERT INTO total_calories_burned VALUES (?, ?, ?, ?, ?, 2500)",
            (f"t{day}", uid, f"2026-10-{day}T00:00:00+00:00",
             f"2026-10-{day}T23:59:00+00:00", f"2026-10-{day}"))
    conn.commit()
    first = goal(conn, uid, date)
    assert first and first["stale"] and first["saved"] is None, first
    # Nothing logged in MFP: no notification about it.
    assert drift_line(conn, uid, date) == ""
    conn.execute(
        "INSERT INTO nutrition (uuid, user_id, start_utc, end_utc, local_date, "
        "meal_type, calories, protein_g) VALUES ('n', ?, 'x', 'x', '2026-10-20', "
        "1, 500, 30)", (uid,))
    conn.commit()
    line = drift_line(conn, uid, date)
    assert "MyFitnessPal" in line and f"{first['kcal']} kcal" in line, line
    assert drift_line(conn, uid, date) == ""  # once per target
    # Typed in MFP: up to date, nothing more to say.
    assert mark_typed(conn, uid, date)
    assert not goal(conn, uid, date)["stale"]
    # The week burns 300 kcal more a day: worth retyping, said once.
    conn.execute("UPDATE total_calories_burned SET kcal = 2800")
    conn.commit()
    moved = goal(conn, uid, date)
    assert moved["stale"] and moved["kcal"] - first["kcal"] == 300, moved
    assert "a bougé" in drift_line(conn, uid, date)
    assert drift_line(conn, uid, date) == ""
    print("mfp_goals.py: all checks passed")
