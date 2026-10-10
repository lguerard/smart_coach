#!/usr/bin/env python3
"""Change which session a weekday gets, from the command line.

    docker compose run --rm smart_coach-worker \\
        python set_schedule.py lguerard vendredi=kettlebell samedi=kettlebell

The same as editing the week plan in Settings: the day keeps its start
time and duration, gets the session type and its default title.
``jour=aucun`` makes the day outside the level system.
"""

import json
import sys

import db
import training

DAYS = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4,
    "samedi": 5, "dimanche": 6, "monday": 0, "tuesday": 1,
    "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
    "sunday": 6,
}
NONE = {"aucun", "none", "off", "-"}


def apply(schedule: dict, changes: list[str]) -> dict:
    """``schedule`` with ``jour=type`` changes applied.

    Raises:
        ValueError: Unknown day or session type.
    """
    updated = {str(day): dict(entry) for day, entry in schedule.items()}
    for change in changes:
        day_name, _, session_type = change.partition("=")
        day = DAYS.get(day_name.strip().lower())
        session_type = session_type.strip()
        if day is None:
            raise ValueError(f"jour inconnu : {day_name!r}")
        if session_type.lower() in NONE:
            updated[str(day)]["session_type"] = None
            continue
        if session_type not in training.SESSION_LABEL_FR:
            raise ValueError(
                f"seance inconnue : {session_type!r} (choix : "
                f"{', '.join(training.SESSION_LABEL_FR)}, aucun)"
            )
        updated[str(day)]["session_type"] = session_type
        updated[str(day)]["title"] = training.SESSION_LABEL_FR[session_type]
    return updated


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    username, changes = argv[0], argv[1:]
    conn = db.connect()
    db.init_db(conn)
    problem = db.unknown_account_message(conn, username)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    user = next(u for u in db.all_users(conn) if u["username"] == username)
    current = training.schedule_for_user(conn, user["id"])
    try:
        updated = apply(current, changes)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    db.set_setting(conn, user["id"], "schedule", json.dumps(updated))
    names = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
             "dimanche"]
    for day in range(7):
        entry = updated[str(day)]
        print(f"{names[day]:9} {entry.get('session_type') or '-':13} "
              f"{entry['title']} ({entry['start']})")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:
        base = {str(d): {"session_type": "treadmill", "title": "T",
                         "start": "20:00", "duration_min": 30}
                for d in range(7)}
        out = apply(base, ["vendredi=kettlebell", "Samedi=kettlebell",
                           "dimanche=aucun"])
        assert out["4"]["session_type"] == "kettlebell"
        assert out["5"]["title"] == training.SESSION_LABEL_FR["kettlebell"]
        assert out["5"]["start"] == "20:00"  # time kept
        assert out["6"]["session_type"] is None
        assert base["4"]["session_type"] == "treadmill"  # input untouched
        for bad in (["jourdi=kettlebell"], ["lundi=yoga"]):
            try:
                apply(base, bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        print("set_schedule.py: all checks passed")
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[1:]))
