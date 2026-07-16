#!/usr/bin/env python3
"""Cron entrypoint: compute today's session + progress, get a
coaching message, update the calendar, push a notification -- for
every user account.

Run after run_ingest.py. Structurally the same pipeline as
garmin-coach's coach.py:main(), but every input now comes from
smart_sport's own db (Health Connect derived) instead of live Garmin
Connect API calls, and the payload additionally carries
progress.weekly_progress() so the message can speak to actual
weight/muscle-gain progress, not just today's snapshot. Also applies
the deload guardrail on top of the daily +-1 level adjustment, and
checks/announces achievement unlocks. One user's failure (e.g. an
expired Calendar token) doesn't block the others.
"""

import datetime as dt

import achievements
import db
import gcal
import llm
import metrics
import notify
import progress
import training
import training_load


def run_for_user(conn, user: dict) -> None:
    """Run the daily pipeline for a single user."""
    user_id = user["id"]
    username = user["username"]
    language = db.get_setting(conn, user_id, "language") or "fr"
    ntfy_topic = db.get_setting(conn, user_id, "ntfy_topic") or None
    today = dt.datetime.now(metrics.local_tz(conn, user_id)).date().isoformat()

    wellness = metrics.daily_wellness(conn, user_id, today)
    nutrition = metrics.nutrition_for_date(conn, user_id, today)
    weekly = progress.weekly_progress(conn, user_id, today)

    weekday = dt.date.fromisoformat(today).weekday()
    session_type = training.session_type_for_weekday(weekday)
    today_session = {
        "type": "bike",
        "note": (
            "Sortie velo en famille, 10-15 km minimum, hors systeme "
            "de niveaux"
        ),
    }
    status = None
    level = None
    calendar_note = None

    if session_type is not None:
        baseline = training.rhr_baseline(conn, user_id, today)
        status = training.compute_status(wellness, baseline)
        deload = training.apply_deload_guardrail(
            conn, user_id, session_type, status, today,
        )
        level = deload["level"]

        values = training.session_values(session_type, level)
        description = training.format_description_fr(
            session_type, level, values, status,
        )
        if deload["deload_triggered"]:
            deload_note = (
                "SEMAINE DE DELOAD (3 rouges d'affilee)" if language == "fr"
                else "DELOAD WEEK (3 reds in a row)"
            )
            description = f"{description}\n{deload_note}"
        today_session = {
            "type": session_type, "status": status, "level": level,
            "values": values, "description_fr": description,
            "in_deload": deload["in_deload"],
            "deload_triggered": deload["deload_triggered"],
        }
        # Calendar update happens this morning for tonight's session,
        # so it should reflect everything the coach knows today, not
        # just the workout numbers -- append a short, deterministic
        # nutrition/hydration nudge (no LLM call, so it's never blocked
        # on or delayed by the coaching-message step below).
        nudge = progress.format_nutrition_nudge(
            weekly["nutrition_yesterday"]["gap"], language,
        )
        calendar_description = f"{description}\n{nudge}" if nudge else description
        calendar_name = db.get_setting(conn, user_id, "calendar_name")
        if not calendar_name:
            calendar_note = (
                "(Calendrier non configure: reglez calendar_name dans "
                "les Reglages)"
            )
        else:
            try:
                service = gcal.get_calendar_service(username)
                calendar_id = gcal.resolve_calendar_id(service, calendar_name)
                gcal.upsert_session_event(
                    service, calendar_id, dt.date.fromisoformat(today),
                    weekday, calendar_description,
                )
            except Exception as error:
                calendar_note = f"(Calendrier non mis a jour: {error})"

    payload = {
        "date": today,
        "language": language,
        "wellness_today": wellness,
        "nutrition_today": nutrition,
        "weekly_progress": weekly,
        "today_session": today_session,
        "today_targets": progress.macro_targets(conn, user_id, today),
        **metrics.history_snapshot(conn, user_id, today),
    }

    message = llm.coach(payload)
    if calendar_note:
        message = f"{message}\n{calendar_note}"

    conn.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, dt.datetime.now(dt.timezone.utc).isoformat(), today,
            status, session_type, level, message,
        ),
    )
    conn.commit()

    # Recomputed after today's coach_log/exercise rows land, since
    # today's level feeds today's daily_load.
    training_load.compute_training_load(conn, user_id, today)

    # Small continuous XP drip from today's status (idempotent, safe
    # even if this pipeline is ever re-run for the same date).
    achievements.grant_daily_status_xp(conn, user_id, today, status)

    # Achievement checks read coach_log (streaks/comebacks), so this
    # runs after today's row is committed -- an Xbox-style toast pops
    # before the daily message, celebration first.
    for key in achievements.check_and_unlock(conn, user_id, today):
        definition = achievements.ACHIEVEMENTS[key]
        name = definition["name_fr" if language == "fr" else "name_en"]
        desc = definition["desc_fr" if language == "fr" else "desc_en"]
        title = "Succes debloque !" if language == "fr" else "Achievement Unlocked!"
        notify.notify(
            f"{definition['icon']} {name} -- {desc}", title=title,
            topic=ntfy_topic,
        )

    notify.notify(message, topic=ntfy_topic)
    print(f"{username}: {message}")


def main() -> None:
    """Run the daily pipeline for every user account.

    One user's failure (e.g. an expired Calendar token) doesn't block
    the others -- but a silent morning should still mean cron trouble,
    not a swallowed error, so failures are collected and re-raised
    together after every user has had their turn.
    """
    conn = db.connect()
    db.init_db(conn)
    failures = []
    for user in db.all_users(conn):
        username = user["username"]
        try:
            run_for_user(conn, dict(user))
        except Exception as error:
            try:
                ntfy_topic = db.get_setting(conn, user["id"], "ntfy_topic") or None
                notify.notify(
                    f"Coach failed for {username}: {error}",
                    title="Smart Sport ERROR", topic=ntfy_topic,
                )
            except Exception:
                pass
            print(f"{username}: FAILED -- {error}")
            failures.append((username, error))
    if failures:
        names = ", ".join(name for name, _ in failures)
        raise RuntimeError(f"Coach failed for: {names}")


if __name__ == "__main__":
    main()
