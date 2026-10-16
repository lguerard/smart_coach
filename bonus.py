#!/usr/bin/env python3
"""Optional extra workouts, "just in case": short sessions the person
can add on a day with time and energy left, or do instead of a planned
session that cannot happen (no treadmill, away from home...).

They never move a level and are never asked about: the plan stays the
plan. Numbers come from the same ladders as the real sessions, at the
person's current level, so a bonus is never harder than what they
already do. Muscles still tired from a recent activity are flagged on
the workout that would load them again, not hidden.
"""

import sqlite3
from typing import Optional

import exercise_library
import muscles
import training

# Kettlebell bonus: 30 s on / 15 s off -- shorter rest than the main
# circuit's 10 s would make 30 s too much, longer work makes it count.
BONUS_WORK_SEC, BONUS_REST_SEC = 30, 15
ACTIVE_WALK = {"speed_kmh": 5.0, "incline_pct": 5, "duration_min": 30}
MAIN_MUSCLE = 0.8  # a workout "loads" a muscle at this weight or more


def _tours(level: int) -> int:
    """About 10 minutes at low levels, 13 from level 5."""
    return 3 if level < 5 else 4


def _timed(slots, level, date, equipment) -> list[dict]:
    items = []
    for slot in slots:
        variant = exercise_library.variant_for(slot, level, date, equipment)
        items.append({"slot": slot, "name": variant["name"],
                      "amount": f"{BONUS_WORK_SEC} s", "cue": variant["cue"],
                      "muscles": variant["muscles"]})
    return items


def _counted(values: dict, slots, level, date) -> list[dict]:
    items = []
    for slot in slots:
        variant, line = exercise_library.describe_slot(
            slot, level, values[slot], date,
        )
        amount = line[len(variant["name"]):].strip()
        items.append({"slot": slot, "name": variant["name"],
                      "amount": amount, "cue": variant["cue"],
                      "muscles": variant["muscles"]})
    return items


def _main_muscles(items: list[dict]) -> set:
    return {m for item in items for m, w in item["muscles"].items()
            if w >= MAIN_MUSCLE}


def bonus_workouts(
    conn: sqlite3.Connection, user_id: int, date: str, tier: str,
    fatigue: Optional[dict] = None,
) -> list[dict]:
    """The day's optional extra workouts.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): Today's ISO local date.
        tier (str): Today's ``training`` tier. A rest day gets none (the
            body asked for rest); a recovery day only the walk.
        fatigue (dict | None): ``muscles.muscle_fatigue`` output, read
            when not given.

    Returns:
        list[dict]: ``key``, ``title``, ``goal``, ``minutes``, ``format``
        (one line on how to run it), ``items`` (name/amount/cue) and
        ``tired`` (French labels of loaded muscles still fatigued).
    """
    if tier == training.TIER_REST:
        return []
    walk = {
        "key": "active_walk", "title": "Marche active",
        "goal": "Dépense en plus, sans fatiguer les muscles (~200 kcal).",
        "minutes": ACTIVE_WALK["duration_min"],
        "format": (f"{ACTIVE_WALK['speed_kmh']} km/h, "
                   f"{ACTIVE_WALK['incline_pct']} % de pente, "
                   f"{ACTIVE_WALK['duration_min']} min d'affilée"),
        "items": [], "tired": [],
    }
    if tier == training.TIER_RECOVERY:
        return [walk]

    equipment = exercise_library.equipment_for(conn, user_id)
    upper = training.get_level(conn, user_id, "upper_body")
    kb_level = training.get_level(conn, user_id, "kettlebell")
    bodyweight = training.session_values("upper_body", upper, date=date)
    workouts = []

    if equipment.get("kettlebell_kg"):
        items = _timed(("kb_russian_twist_sec", "kb_hammer_curl_sec",
                        "kb_triceps_ext_sec", "kb_situp_press_sec"),
                       kb_level, date, equipment)
        tours = _tours(kb_level)
        workouts.append({
            "key": "abs_arms_kb", "title": "Abdos & bras kettlebell",
            "goal": "Abdos, biceps et triceps.",
            "items": items, "tours": tours,
            "format": (f"{tours} tours, {BONUS_WORK_SEC} s d'effort / "
                       f"{BONUS_REST_SEC} s de repos par mouvement"),
        })
        chest = _counted(bodyweight, ("pushups",), upper, date) + _timed(
            ("kb_floor_press_sec", "kb_pullover_sec"), kb_level, date,
            equipment,
        )
        chest_format = (f"{_tours(upper)} tours ; pompes au nombre indiqué, "
                        f"kettlebell {BONUS_WORK_SEC} s par mouvement, "
                        f"{BONUS_REST_SEC} s de repos entre chaque")
    else:
        items = _counted(bodyweight, ("plank_sec",), upper, date) + [{
            "slot": "crunch", "name": "crunch", "amount": "15",
            "cue": "Allonge, genoux plies, decolle les epaules en soufflant, "
                   "le bas du dos reste au sol.",
            "muscles": {"abs": 1.0},
        }] + _counted(bodyweight, ("dips",), upper, date)
        workouts.append({
            "key": "abs_arms", "title": "Abdos & triceps sans matériel",
            "goal": "Abdos et triceps.", "items": items,
            "format": f"{_tours(upper)} tours, 30 s de repos entre les tours",
        })
        chest = _counted(bodyweight, ("pushups", "dips"), upper, date)
        chest_format = f"{_tours(upper)} tours, 45 s de repos entre les tours"
    workouts.append({
        "key": "chest_triceps", "title": "Pecs & triceps",
        "goal": "Pectoraux et triceps.", "items": chest,
        "format": chest_format,
    })

    fatigue = fatigue or muscles.muscle_fatigue(conn, user_id, date)
    fatigued = {m for m, info in fatigue["muscles"].items()
                if info["state"] == "fatigued"}
    for workout in workouts:
        workout["tired"] = sorted(
            muscles.MUSCLES[m] for m in _main_muscles(workout["items"]) & fatigued
        )
        per_tour_s = sum(
            (BONUS_WORK_SEC if item["amount"].endswith(" s") else 45)
            + BONUS_REST_SEC for item in workout["items"]
        )
        workout["minutes"] = round(
            workout.get("tours", _tours(upper)) * per_tour_s / 60
        )
        for item in workout["items"]:
            item.pop("slot", None)
    return workouts + [walk]


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db

    conn = db.connect(Path(tempfile.mkdtemp()) / "bonus.db")
    db.init_db(conn)
    uid = db.create_user(conn, "b", "pw-123456")
    day = "2026-10-16"
    rested = {"muscles": {m: {"state": "ready", "fatigue": 0.0}
                          for m in muscles.MUSCLES}}

    # A rest day: nothing. A recovery day: the walk only.
    assert bonus_workouts(conn, uid, day, training.TIER_REST, rested) == []
    only_walk = bonus_workouts(conn, uid, day, training.TIER_RECOVERY, rested)
    assert [w["key"] for w in only_walk] == ["active_walk"], only_walk

    # Bodyweight only: abs/triceps and chest/triceps, plus the walk.
    plain = bonus_workouts(conn, uid, day, training.TIER_TRAIN, rested)
    assert [w["key"] for w in plain] == ["abs_arms", "chest_triceps",
                                         "active_walk"], plain
    assert all(w["items"] and w["minutes"] > 0 for w in plain[:2]), plain
    assert not any("kg" in i["name"] for w in plain for i in w["items"])

    # With a kettlebell: the loaded abs/arms circuit, weight in names,
    # and between them they work abs, biceps, triceps and chest.
    db.set_setting(conn, uid, "kettlebell_kg", "12")
    loaded = bonus_workouts(conn, uid, day, training.TIER_TRAIN, rested)
    assert loaded[0]["key"] == "abs_arms_kb", loaded
    assert "(12 kg)" in loaded[0]["items"][0]["name"], loaded[0]
    worked = set().union(*(_main_muscles(w["items"]) for w in loaded[:2]))
    assert {"abs", "biceps", "triceps", "chest"} <= worked, worked
    assert 5 <= loaded[0]["minutes"] <= 15, loaded[0]

    # Tired chest: flagged on the chest workout, not hidden.
    sore = {"muscles": {**rested["muscles"],
                        "chest": {"state": "fatigued", "fatigue": 0.9}}}
    flagged = bonus_workouts(conn, uid, day, training.TIER_TRAIN, sore)
    chest = next(w for w in flagged if w["key"] == "chest_triceps")
    assert chest["tired"] == [muscles.MUSCLES["chest"]], chest
    assert flagged[0]["tired"] == [], flagged[0]  # abs/arms: chest < 0.8
    print("bonus.py: all checks passed")
