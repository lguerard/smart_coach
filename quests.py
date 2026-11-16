#!/usr/bin/env python3
"""Daily quests: three small goals a day, worth XP once done.

The day's three are picked once and kept (``daily_quests``): the
session first on a training day, then two of the others, rotating from
day to day so the same ones do not always come back. A quest done
earns its XP once (``xp_ledger`` source ``quest:<key>``), and the three
done the same day earn a bonus (``quest:bonus``). The home page grants
them as soon as they are done; the morning run grants yesterday's, for
a day the page was not opened after the last one was done.

Each quest reads numbers the rest of the app already has: the steps of
the dashboard, the protein and water targets of Repas, the medication
doses of Médicaments, the sleep answer of the morning.
"""

import datetime as dt
import random
import sqlite3
from typing import Optional

import achievements
import db

# key: (label, XP, icon)
QUESTS = {
    "seance": ("Fais ta séance du jour", 25, "dumbbell"),
    "pas": ("Atteins ton objectif de pas", 15, "footprints"),
    "proteines": ("Atteins tes protéines", 15, "target"),
    "eau": ("Bois ton objectif d'eau", 10, "droplet"),
    "nuit": ("Dis comment tu as dormi", 5, "moon"),
    "meds": ("Note toutes tes prises", 10, "pill"),
    "etages": ("Monte tes étages", 10, "mountain"),
}
PER_DAY = 3
BONUS_XP = 20
DEFAULT_STEPS = 8000


def _planned_session(conn, user_id: int, date: str) -> Optional[str]:
    row = conn.execute(
        "SELECT session_type, tier FROM coach_log WHERE user_id = ? AND "
        "local_date = ? ORDER BY id DESC LIMIT 1", (user_id, date)).fetchone()
    if row and row["session_type"] and (row["tier"] or "train") == "train":
        return row["session_type"]
    return None


def _measure(conn, user_id: int, date: str, key: str) -> Optional[tuple]:
    """(current, target, unit) for one quest on ``date``; None when it
    cannot be asked that day (no target, nothing planned, no meds)."""
    import done_sessions
    import meds
    import metrics
    import progress
    import sleep_feedback

    if key == "seance":
        if not _planned_session(conn, user_id, date):
            return None
        known = done_sessions.done_on(conn, user_id, date)
        done = bool(known) and known["session_type"] not in (
            done_sessions.NOTHING, done_sessions.OTHER)
        return (1 if done else 0, 1, "séance")
    if key == "pas":
        target = int(db.get_setting(conn, user_id, "step_goal") or 0) or DEFAULT_STEPS
        steps = metrics.steps_for_range(conn, user_id, date, 1).get(date) or 0
        return (steps, target, "pas")
    if key in ("proteines", "eau"):
        targets = progress.macro_targets(conn, user_id, date)
        target = targets.get("protein_target_g" if key == "proteines"
                             else "hydration_target_ml")
        if not target:
            return None
        intake = progress.intake_for_date(conn, user_id, date)
        current = intake.get("protein_g" if key == "proteines" else "hydration_ml") or 0
        return (round(current), round(target), "g" if key == "proteines" else "ml")
    if key == "nuit":
        if db.get_setting(conn, user_id, "ask_sleep") == "0":
            return None
        return (1 if sleep_feedback.felt(conn, user_id, date) else 0, 1, "réponse")
    if key == "meds":
        noon = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(23, 59))
        doses = meds.day(conn, user_id, noon)
        if not doses:
            return None
        marked = sum(d["status"] in ("taken", "skipped") for d in doses)
        return (marked, len(doses), "prises")
    if key == "etages":
        target = int(db.get_setting(conn, user_id, "floors_goal") or 0)
        if not target:
            return None
        floors = metrics.sum_for_date(conn, user_id, "floors_climbed", "floors", date)
        return (round(floors or 0), target, "étages")
    return None


def pick(conn: sqlite3.Connection, user_id: int, date: str,
         refresh: bool = False) -> list[str]:
    """The day's quests, picked once and kept.

    ``refresh`` picks again (the morning run, once the day is planned),
    unless one of them has already earned its XP.
    """
    row = conn.execute(
        "SELECT keys FROM daily_quests WHERE user_id = ? AND local_date = ?",
        (user_id, date)).fetchone()
    if row and (not refresh or _earned(conn, user_id, date)):
        return [k for k in row["keys"].split(",") if k in QUESTS]
    possible = [k for k in QUESTS if _measure(conn, user_id, date, k)]
    chosen = ["seance"] if "seance" in possible else []
    others = [k for k in possible if k != "seance"]
    random.Random(f"{user_id}:{date}").shuffle(others)
    chosen += others[:PER_DAY - len(chosen)]
    conn.execute(
        "INSERT INTO daily_quests (user_id, local_date, keys) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, local_date) DO UPDATE SET keys = excluded.keys",
        (user_id, date, ",".join(chosen)))
    conn.commit()
    return chosen


def _earned(conn, user_id: int, date: str) -> set:
    return {r["source"] for r in conn.execute(
        "SELECT source FROM xp_ledger WHERE user_id = ? AND date = ? AND "
        "source LIKE 'quest:%'", (user_id, date))}


def for_day(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """The day's quests with their progress: ``{quests: [{key, label,
    xp, icon, current, target, unit, pct, done}], done, total,
    bonus_xp, bonus_done, xp_today}``."""
    earned = _earned(conn, user_id, date)
    quests = []
    for key in pick(conn, user_id, date):
        measure = _measure(conn, user_id, date, key)
        if measure is None:
            continue
        current, target, unit = measure
        label, xp, icon = QUESTS[key]
        quests.append({
            "key": key, "label": label, "xp": xp, "icon": icon,
            "current": current, "target": target, "unit": unit,
            "pct": min(100, round(100 * current / target)) if target else 0,
            "done": current >= target or f"quest:{key}" in earned,
        })
    done = sum(q["done"] for q in quests)
    return {
        "quests": quests, "done": done, "total": len(quests),
        "bonus_xp": BONUS_XP, "bonus_done": "quest:bonus" in earned,
        "xp_today": sum(q["xp"] for q in quests if q["done"])
                    + (BONUS_XP if "quest:bonus" in earned else 0),
    }


def award(conn: sqlite3.Connection, user_id: int, date: str) -> list[dict]:
    """Grant the XP of the day's quests done and not paid yet, and the
    bonus once all are done. Returns what was granted now:
    ``[{label, xp}]`` (empty when nothing new)."""
    day = for_day(conn, user_id, date)
    earned = _earned(conn, user_id, date)
    granted = []
    for quest in day["quests"]:
        if quest["done"] and f"quest:{quest['key']}" not in earned:
            achievements._grant_xp(conn, user_id, date, f"quest:{quest['key']}",
                                   quest["xp"], f"Défi : {quest['label']}")
            granted.append({"label": quest["label"], "xp": quest["xp"]})
    if (day["total"] and day["done"] == day["total"]
            and "quest:bonus" not in earned):
        achievements._grant_xp(conn, user_id, date, "quest:bonus", BONUS_XP,
                               "Les trois défis du jour")
        granted.append({"label": "Les trois défis du jour", "xp": BONUS_XP})
    if granted:
        conn.commit()
    return granted


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import done_sessions
    import meds

    conn = db.connect(Path(tempfile.mkdtemp()) / "quests.db")
    db.init_db(conn)
    uid = db.create_user(conn, "q", "pw-123456")
    other = db.create_user(conn, "o", "pw-123456")
    day = "2026-11-16"
    db.set_setting(conn, uid, "step_goal", "10000")
    conn.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message, tier) VALUES (?, 'x', ?, 'green', "
        "'treadmill', 4, 'm', 'train')", (uid, day))
    conn.commit()
    meds.add(conn, uid, "Magnésium", "20:00")

    # A training day: the session always, then two others; kept all day.
    first = pick(conn, uid, day)
    assert first[0] == "seance" and len(first) == PER_DAY, first
    assert pick(conn, uid, day) == first
    # Another account, another day: its own pick, never this one's state.
    assert "seance" not in pick(conn, other, day)

    # Nothing done: no XP.
    assert award(conn, uid, day) == []
    # The session declared done: its XP once, never twice.
    done_sessions.declare(conn, uid, day, "treadmill", today=day)
    got = award(conn, uid, day)
    assert got == [{"label": QUESTS["seance"][0], "xp": 25}], got
    assert award(conn, uid, day) == []
    # Once one is paid, the morning refresh keeps the day's three.
    assert pick(conn, uid, day, refresh=True) == first

    # Every quest done: each one's XP, then the bonus (three known ones).
    first = ["seance", "pas", "nuit"]
    conn.execute("UPDATE daily_quests SET keys = ? WHERE user_id = ? AND "
                 "local_date = ?", (",".join(first), uid, day))
    for key in first[1:]:
        if key == "pas":
            conn.execute("INSERT INTO garmin_daily_summary (user_id, local_date, "
                         "total_steps) VALUES (?, ?, 12000)", (uid, day))
        elif key == "nuit":
            conn.execute("INSERT INTO sleep_feedback VALUES (?, ?, 'good', 'x')",
                         (uid, day))
        elif key == "meds":
            mg = meds.list_meds(conn, uid)[0]["id"]
            meds.mark(conn, uid, mg, day, "20:00", "taken")
    conn.commit()
    assert for_day(conn, uid, day)["done"] == 3
    got = award(conn, uid, day)
    assert [g["xp"] for g in got] == [15, 5, BONUS_XP], got
    assert for_day(conn, uid, day)["bonus_done"]
    assert for_day(conn, uid, day)["xp_today"] == 25 + 15 + 5 + BONUS_XP
    assert award(conn, uid, day) == []
    assert conn.execute("SELECT COUNT(*) FROM xp_ledger WHERE user_id = ?",
                        (other,)).fetchone()[0] == 0
    print("quests.py: all checks passed")
