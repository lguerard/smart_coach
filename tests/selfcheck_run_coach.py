#!/usr/bin/env python3
"""Self-check for run_coach.run_for_user with every outside world stubbed.

The pure decision is tested in training.py; this checks the part around
it -- that what plan_day decides is what the calendar, the watch, the
message payload and coach_log are actually given. The reported failure
was exactly there: a red morning at level 0 produced the same level-0
treadmill session on the calendar, the watch and the message every day.
"""

import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import coach_payload  # noqa: E402
import db  # noqa: E402
import gcal  # noqa: E402
import llm  # noqa: E402
import metrics  # noqa: E402
import notify  # noqa: E402
import run_coach  # noqa: E402
import training  # noqa: E402
from ingest import garmin_api  # noqa: E402

calls: dict = {}


def _reset() -> None:
    calls.clear()
    calls.update(pushed=[], retired=[], calendar=[], payloads=[], sent=[])


def _install(wellness: dict) -> None:
    metrics.daily_wellness = lambda conn, uid, day: dict(wellness)
    llm.coach = lambda payload: (
        calls["payloads"].append(payload) or "message du coach"
    )
    notify.notify = lambda text, **kw: calls["sent"].append(text)

    def _service(username):
        raise RuntimeError("no calendar in tests")

    gcal.get_calendar_service = _service
    gcal.push_description = lambda username, name, day, template, desc, **kw: (
        calls["calendar"].append(desc)
    )
    garmin_api.get_client = lambda username: object()
    garmin_api.push_workout_for_session = (
        lambda conn, uid, client, stype, level, values, day: (
            calls["pushed"].append((stype, level, dict(values))) or "w1"
        )
    )
    garmin_api.retire_pushed_workout = lambda conn, uid, client: (
        calls["retired"].append(True) or True
    )
    # Every weekday the same treadmill slot: the check must not depend
    # on which day of the week it is run.
    all_days = {
        "session_type": "treadmill", "title": "Tapis", "start": "20:00",
        "duration_min": 30,
    }
    training.schedule_for_user = lambda conn, uid: {
        day: dict(all_days) for day in range(7)
    }


def _user():
    conn = db.connect(Path(tempfile.mkdtemp()) / "run.db")
    db.init_db(conn)
    uid = db.create_user(conn, "runner", "password1234")
    db.set_setting(conn, uid, "calendar_name", "Sport")
    return conn, {"id": uid, "username": "runner"}


def _logged(conn, uid):
    return conn.execute(
        "SELECT * FROM coach_log WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (uid,),
    ).fetchone()


if __name__ == "__main__":
    # A red morning at level 0: a flat easy walk, everywhere.
    _reset()
    _install({"sleep_score": 40})
    conn, user = _user()
    run_coach.run_for_user(conn, user)
    stype, level, values = calls["pushed"][0]
    assert stype == "recovery" and values["incline_pct"] == 0, calls
    assert calls["retired"] == [], calls
    assert calls["calendar"][0].startswith("RECUPERATION"), calls
    session = calls["payloads"][0]["today_session"]
    assert session["type"] == "recovery", session
    assert session["scheduled_type"] == "treadmill", session
    row = _logged(conn, user["id"])
    assert row["tier"] == training.TIER_RECOVERY and row["tier_reason"], dict(row)
    # The payload carries the same blocks a regenerated message gets.
    for key in ("activity_yesterday", "illness_watch", "today_remaining"):
        assert key in calls["payloads"][0], key

    # Declared illness: rest. Nothing pushed, yesterday's workout taken
    # off the watch, the calendar says rest, the log says rest.
    _reset()
    _install({"sleep_score": 40})
    conn, user = _user()
    today = dt.datetime.now(metrics.local_tz(conn, user["id"])).date()
    training.set_sick(conn, user["id"], today.isoformat(), 3)
    run_coach.run_for_user(conn, user)
    assert calls["pushed"] == [] and calls["retired"] == [True], calls
    assert calls["calendar"][0].startswith("REPOS"), calls
    assert calls["payloads"][0]["today_session"]["type"] == "rest"
    row = _logged(conn, user["id"])
    assert row["tier"] == training.TIER_REST, dict(row)

    # A rest day is not a skipped session, and asks no feedback.
    snapshot = metrics.history_snapshot(conn, user["id"], today.isoformat())
    assert all(
        a["tier"] != "train" for a in snapshot["adherence_last_7_days"]
        if a["date"] == today.isoformat()
    ), snapshot["adherence_last_7_days"]

    # Regenerating from the log rebuilds the same plan, same payload
    # shape, and changes no level.
    plan = training.plan_from_log(
        conn, user["id"], today.isoformat(),
        training.schedule_for_user(conn, user["id"])[0], row,
    )
    assert plan["tier"] == training.TIER_REST, plan
    payload = coach_payload.build_payload(
        conn, user["id"], today.isoformat(), "fr",
        training.session_payload(plan, {"title": "Tapis"}, "fr"),
        training.illness_watch(conn, user["id"], today.isoformat()),
    )
    assert payload["today_session"]["type"] == "rest", payload
    assert "activity_yesterday" in payload and "illness_watch" in payload

    print("selfcheck_run_coach.py: all checks passed (no live call made)")
