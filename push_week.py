#!/usr/bin/env python3
"""Put the coming week's sessions on the watch now, without the coach run.

The morning run (run_coach.py) does this every day at COACH_TIME. Use
this to fill the week straight after a deployment, or to see what is
planned and why a day is missing:

    docker compose run --rm smart_coach-worker python push_week.py lguerard

It never touches today's workout (only the morning decision changes it)
and never moves a level -- unlike re-running run_coach.py, which would
make the whole day's decision a second time.

    docker compose run --rm smart_coach-worker python push_week.py lguerard --today

also rebuilds TODAY's workout from this morning's decision (same tier,
same level) with the current code, and replaces the one on the watch if
it differs -- for a change deployed after the morning run, like more
kettlebell tours. Still no new decision and no level change.
"""

import datetime as dt
import sys

import db
import metrics
import training
from ingest import garmin_api

NAMES = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
         "dimanche"]


TODAY_STATE = {
    "unchanged": "deja a jour sur la montre",
    "created": "ajoute sur la montre",
    "updated": "remplace sur la montre",
    "removed": "retire de la montre (repos)",
    "none": "rien a mettre sur la montre",
}


def refresh_today_workout(conn, user_id: int, client, today: str) -> str:
    """Today's workout rebuilt from this morning's coach_log row
    (``garmin_api.refresh_today``), in French."""
    outcome = garmin_api.refresh_today(conn, user_id, client, today)
    if outcome == "no_log":
        return "pas encore de seance du matin, rien change"
    return TODAY_STATE.get(outcome, outcome)


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2) or argv[1:] not in ([], ["--today"]):
        print(__doc__, file=sys.stderr)
        return 2
    username, refresh_today = argv[0], argv[1:] == ["--today"]
    conn = db.connect()
    db.init_db(conn)
    problem = db.unknown_account_message(conn, username)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    user = next(u for u in db.all_users(conn) if u["username"] == username)
    today = dt.datetime.now(metrics.local_tz(conn, user["id"])).date()
    client = garmin_api.get_client(username)
    if refresh_today:
        print(f"aujourd'hui : {refresh_today_workout(conn, user['id'], client, today.isoformat())}")
    result = garmin_api.plan_week_ahead(
        conn, user["id"], client, today.isoformat(),
    )
    planned = {
        row["local_date"]: row["workout_id"] for row in conn.execute(
            "SELECT local_date, workout_id FROM garmin_planned_workouts "
            "WHERE user_id = ?", (user["id"],),
        )
    }
    schedule = training.schedule_for_user(conn, user["id"])
    for offset in range(garmin_api.WEEK_AHEAD_DAYS):
        day = today + dt.timedelta(days=offset)
        session_type = schedule[day.weekday()].get("session_type")
        date = day.isoformat()
        if date in planned:
            state = "sur la montre"
            if date in result["created"]:
                state += " (ajoute maintenant)"
        elif not session_type:
            state = "pas de seance prevue"
        elif offset == 0:
            state = "aujourd'hui : gere par la seance du matin"
        else:
            state = "MANQUANT"
        print(f"{NAMES[day.weekday()]:9} {date}  {session_type or '-':13} "
              f"{state}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:
        import tempfile
        from pathlib import Path

        class _Watch:
            def __init__(self):
                self.uploaded, self.deleted, self.next_id = [], [], 0

            def upload_workout(self, data):
                self.next_id += 1
                self.uploaded.append(data)
                return {"workoutId": f"w{self.next_id}"}

            def schedule_workout(self, workout_id, date):
                return {}

            def delete_workout(self, workout_id):
                self.deleted.append(workout_id)

        conn = db.connect(Path(tempfile.mkdtemp()) / "pw.db")
        db.init_db(conn)
        uid = db.create_user(conn, "pw", "pw-123456")
        day = "2026-10-17"  # a Saturday
        watch = _Watch()
        assert "pas encore" in refresh_today_workout(conn, uid, watch, day)
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, status, "
            "session_type, level, message, tier) VALUES (?, 'x', ?, 'green', "
            "'kettlebell', 0, 'm', 'train')", (uid, day),
        )
        # This morning's watch workout, built before the change.
        conn.execute(
            "INSERT INTO garmin_planned_workouts (user_id, local_date, "
            "workout_id, signature) VALUES (?, ?, 'old', 'stale')", (uid, day),
        )
        conn.commit()
        assert refresh_today_workout(conn, uid, watch, day) == TODAY_STATE["updated"]
        assert watch.deleted == ["old"], watch.deleted
        group = watch.uploaded[0]["workoutSegments"][0]["workoutSteps"][0]
        assert group["numberOfIterations"] == 5, group  # today's code
        # Run again: nothing to do, nothing re-sent.
        assert refresh_today_workout(conn, uid, watch, day) == TODAY_STATE["unchanged"]
        assert len(watch.uploaded) == 1
        print("push_week.py: all checks passed")
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[1:]))
