#!/usr/bin/env python3
"""Put the coming week's sessions on the watch now, without the coach run.

The morning run (run_coach.py) does this every day at COACH_TIME. Use
this to fill the week straight after a deployment, or to see what is
planned and why a day is missing:

    docker compose run --rm smart_coach-worker python push_week.py lguerard

It never touches today's workout (only the morning decision changes it)
and never moves a level -- unlike re-running run_coach.py, which would
make the whole day's decision a second time.
"""

import datetime as dt
import sys

import db
import metrics
import training
from ingest import garmin_api

NAMES = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
         "dimanche"]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    username = argv[0]
    conn = db.connect()
    db.init_db(conn)
    problem = db.unknown_account_message(conn, username)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    user = next(u for u in db.all_users(conn) if u["username"] == username)
    today = dt.datetime.now(metrics.local_tz(conn, user["id"])).date()
    client = garmin_api.get_client(username)
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
    raise SystemExit(main(sys.argv[1:]))
