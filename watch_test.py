#!/usr/bin/env python3
"""Try new workouts on the watch without touching the real plan.

    python watch_test.py lguerard                    # preview, no Garmin call
    python watch_test.py lguerard --push [AAAA-MM-JJ] # send TEST workouts
    python watch_test.py lguerard --remove           # delete them again

What it sends, all named "Smart Coach TEST - ...": both kettlebell
circuits (A and B) at the current kettlebell level, and every bonus
workout of the day. Each is scheduled on the given date (tomorrow by
default) so it shows in the watch calendar after a sync; the abs & arms
bonus is also uploaded once WITHOUT a date, to see whether the watch
lists unscheduled workouts (Entrainements > Mes entrainements) or only
scheduled ones.

The real week on the watch (garmin_planned_workouts) is never read or
changed: the TEST workouts are tracked in data/db/watch_test.json and
--remove deletes exactly those.

On the server, from a checkout of the branch, without rebuilding the
image:

    docker compose run --rm -v "$PWD":/app smart_coach-worker \\
        python watch_test.py lguerard --push
"""

import datetime as dt
import json
import sys
from pathlib import Path

import bonus
import db
import exercise_library
import metrics
import training
from ingest import garmin_api

STATE_FILE = Path("data/db/watch_test.json")
PREFIX = "Smart Coach TEST - "


def test_workouts(conn, user_id: int, date: str) -> list[tuple[str, object, bool]]:
    """``(name, workout, scheduled)`` for every TEST workout."""
    level = training.get_level(conn, user_id, "kettlebell")
    equipment = exercise_library.equipment_for(conn, user_id)
    out = []
    next_day = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
    for circuit in ("A", "B"):
        # Circuits alternate by date: whichever of the day and the next
        # has this one gives its values (both go on ``date``).
        day = date if training.kettlebell_circuit(date) == circuit else next_day
        values = training.session_values(
            "kettlebell", level, training.session_cap_min(conn, user_id),
            day, equipment,
        )
        name = f"{PREFIX}Kettlebell {circuit}"
        out.append((name, garmin_api._circuit_workout(
            "kettlebell", level, values, name=name,
        ), True))
    for extra in bonus.bonus_workouts(conn, user_id, date, training.TIER_TRAIN):
        name = f"{PREFIX}Bonus {extra['title']}"
        out.append((name, garmin_api.build_bonus_workout(extra, name), True))
        if extra["key"].startswith("abs_arms"):
            unscheduled = f"{name} (sans date)"
            out.append((
                unscheduled,
                garmin_api.build_bonus_workout(extra, unscheduled), False,
            ))
    return out


def describe(workout) -> list[str]:
    """What the watch will show, step by step."""
    data = workout.to_dict()
    lines = [f"{data['workoutName']}  (~{data['estimatedDurationInSecs'] // 60} min)"]
    for segment in data["workoutSegments"]:
        for step in segment["workoutSteps"]:
            if "numberOfIterations" in step:
                lines.append(f"  {step['numberOfIterations']} tours de :")
                inner = step["workoutSteps"]
            else:
                inner = [step]
            for s in inner:
                kind = s["stepType"]["stepTypeKey"]
                unit = s["endCondition"]["conditionTypeKey"]
                amount = int(s.get("endConditionValue") or 0)
                amount = f"{amount} s" if unit == "time" else f"{amount} reps"
                if kind == "rest":
                    lines.append(f"    repos {amount}")
                    continue
                garmin = (f"{s.get('category', '-')} / "
                          f"{s.get('exerciseName') or '(nom generique)'}")
                lines.append(f"    {amount:>8}  {garmin}")
    if data.get("description"):
        lines.append(f"  description : {data['description'][:160]}")
    return lines


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    username, flags = argv[0], argv[1:]
    conn = db.connect()
    db.init_db(conn)
    problem = db.unknown_account_message(conn, username)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    user = next(u for u in db.all_users(conn) if u["username"] == username)

    if flags[:1] == ["--remove"]:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []
        client = garmin_api.get_client(username)
        for entry in state:
            garmin_api._delete_quietly(client, entry["workout_id"])
            print(f"supprime : {entry['name']}")
        STATE_FILE.unlink(missing_ok=True)
        print("Aucun entrainement TEST a supprimer." if not state else "Fait.")
        return 0

    today = dt.datetime.now(metrics.local_tz(conn, user["id"])).date()
    date = (flags[1] if len(flags) > 1
            else (today + dt.timedelta(days=1)).isoformat())
    dt.date.fromisoformat(date)  # fail early on a typo
    workouts = test_workouts(conn, user["id"], date)
    for _, workout, scheduled in workouts:
        print("\n".join(describe(workout)))
        print(f"  -> {'planifie le ' + date if scheduled else 'sans date'}\n")
    if flags[:1] != ["--push"]:
        print("Apercu seulement. Ajoute --push pour envoyer sur Garmin.")
        return 0

    client = garmin_api.get_client(username)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []
    for name, workout, scheduled in workouts:
        result = client.upload_workout(workout.to_dict())
        workout_id = result.get("workoutId") if isinstance(result, dict) else None
        if not workout_id:
            print(f"ECHEC envoi {name} : {result!r}", file=sys.stderr)
            continue
        state.append({"name": name, "workout_id": workout_id})
        STATE_FILE.write_text(json.dumps(state, indent=1))
        if scheduled:
            client.schedule_workout(workout_id, date)
        print(f"envoye : {name}")
    print(f"\nSynchronise la montre, puis regarde le {date} dans le calendrier "
          "et Entrainements > Mes entrainements. Ensuite :\n"
          f"  python watch_test.py {username} --remove")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
