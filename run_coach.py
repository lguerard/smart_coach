#!/usr/bin/env python3
"""Cron entrypoint: compute today's session + progress, get a
coaching message, update the calendar, push a notification -- for
every user account.

Run after run_ingest.py. Structurally the same pipeline as
garmin-coach's coach.py:main(), but every input now comes from
smart_coach's own db (Health Connect derived, plus Garmin API for
exercise/sleep/wellness -- see ingest/garmin_api.py) instead of live
Garmin Connect API calls at coaching time, and the payload
additionally carries progress.weekly_progress() so the message can
speak to actual weight/muscle-gain progress, not just today's
snapshot. Also applies the deload guardrail on top of the daily +-1
level adjustment, pushes tonight's session to the watch as a Garmin
workout, and checks/announces achievement unlocks. One user's failure
(e.g. an expired Calendar token) doesn't block the others.
"""

import datetime as dt

import achievements
import coach_payload
import db
import gcal
import llm
import metrics
import notify
import progress
import training
import training_load
import weather
from ingest import garmin_api


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

    # The whole training decision (illness, level, deload, rest or
    # recovery instead of a session) lives in training.plan_day: this
    # pipeline only carries it out on the calendar, the watch and the
    # phone.
    illness = training.illness_watch(conn, user_id, today)
    weekday = dt.date.fromisoformat(today).weekday()
    template = training.schedule_for_user(conn, user_id)[weekday]
    # Yesterday's TSB (today's isn't computed until after this pipeline
    # runs, see the training_load call below) -- lets the guardrail force
    # a deload on accumulated fatigue alone, not just 3 reds in a row.
    latest_load = training_load.latest_training_load(conn, user_id)
    plan = training.plan_day(
        conn, user_id, today, template, wellness, illness,
        tsb=latest_load["tsb"] if latest_load else None,
        language=language,
    )
    today_session = training.session_payload(plan, template, language)
    session_type = plan["session_type"]
    status, level, tier = plan["status"], plan["level"], plan["tier"]
    values = plan["values"]
    calendar_note = None
    workout_note = None

    if session_type is not None:
        # Calendar update happens this morning for tonight's session,
        # so it should reflect everything the coach knows today, not
        # just the workout numbers -- append a short, deterministic
        # nutrition/hydration nudge (no LLM call, so it's never blocked
        # on or delayed by the coaching-message step below).
        description = plan["description"]
        nudge = progress.format_nutrition_nudge(
            weekly["nutrition_yesterday"], language,
        )
        calendar_description = f"{description}\n{nudge}" if nudge else description
        calendar_name = db.get_setting(conn, user_id, "calendar_name")
        if not calendar_name:
            calendar_note = (
                "(Calendrier non configure: reglez calendar_name dans "
                "les Reglages)"
            )
        else:
            push_template = template
            if tier != training.TIER_REST:
                try:
                    # Real life first: if tonight's usual slot conflicts
                    # with something already on the user's day (a
                    # meeting, travel...), move the session rather than
                    # silently double-booking. Best-effort -- a failure
                    # here just keeps the original template time.
                    service = gcal.get_calendar_service(username)
                    busy_calendar = (
                        db.get_setting(conn, user_id, "busy_calendar_name")
                        or "primary"
                    )
                    new_start, moved = gcal.find_available_start(
                        service, busy_calendar,
                        dt.date.fromisoformat(today), template,
                        values.get("duration_min") or template["duration_min"],
                    )
                    if moved:
                        push_template = {**template, "start": new_start}
                        moved_note = (
                            f"(Horaire deplace a {new_start} -- journee "
                            "chargee)" if language == "fr"
                            else f"(Moved to {new_start} -- busy day)"
                        )
                        calendar_description = (
                            f"{calendar_description}\n{moved_note}"
                        )
                except Exception:
                    pass
            try:
                # A rest day keeps its slot with the rest note in it,
                # rather than leaving yesterday's prescription on the
                # calendar: the event must say what the message says.
                gcal.push_description(
                    username, calendar_name,
                    dt.date.fromisoformat(today), push_template,
                    calendar_description,
                    duration_min=values.get("duration_min"),
                )
            except Exception as error:
                calendar_note = f"(Calendrier non mis a jour: {error})"


    # The watch carries the whole week. Today's workout is only replaced
    # when this morning's decision differs from what is already on it
    # (rest, a recovery walk, a level change); the coming days are each
    # pushed once, at their planned level, and wait for their own
    # morning.
    try:
        watch_client = garmin_api.get_client(username)
        kind = (
            None if session_type is None or tier == training.TIER_REST
            else "recovery" if tier == training.TIER_RECOVERY
            else session_type
        )
        garmin_api.sync_planned_workout(
            conn, user_id, watch_client, today, kind, level or 0, values,
        )
        garmin_api.plan_week_ahead(conn, user_id, watch_client, today)
    except Exception as error:
        workout_note = f"(Entrainement non envoye a la montre: {error})"

    weather_today = None
    city = db.get_setting(conn, user_id, "city")
    if city:
        try:
            weather_today = weather.today_weather(
                city, tz=metrics.local_tz(conn, user_id).key,
            )
        except Exception:
            weather_today = None  # best-effort context only

    payload = coach_payload.build_payload(
        conn, user_id, today, language, today_session, illness,
        weekly=weekly, wellness=wellness, nutrition=nutrition,
        weather_today=weather_today,
    )

    message = llm.coach(payload)
    if calendar_note:
        message = f"{message}\n{calendar_note}"
    if workout_note:
        message = f"{message}\n{workout_note}"

    conn.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message, tier, tier_reason, level_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, dt.datetime.now(dt.timezone.utc).isoformat(), today,
            status, session_type, level, message, tier,
            "; ".join(plan["tier_reasons"]) or None, plan["level_reason"],
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

    # The push always opens with the deterministic day budget, so the
    # numbers reach the phone even if the LLM phrases around them.
    header = progress.format_plan_header(payload["today_targets"], language)
    notify.notify(
        f"{header}\n{message}" if header else message, topic=ntfy_topic,
    )
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
                    title="Smart Coach ERROR", topic=ntfy_topic,
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
