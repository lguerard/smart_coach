#!/usr/bin/env python3
"""The data the coaching message is written from, in one place.

The morning run (run_coach.py) and the dashboard's regenerate button
both feed the LLM. They used to build their payloads separately, so every
signal added to one was missing from the other: a regenerated message
silently lost yesterday's activity, the illness watch and the remaining
budget the morning one had. One builder means a regenerated message is
the same message with a different phrasing, not a poorer one.
"""

import day_review
import db
import desk
import metrics
import progress
import sleep_analysis
import training


def build_payload(
    conn, user_id: int, today: str, language: str, today_session: dict,
    illness: dict, weekly: dict | None = None, wellness: dict | None = None,
    nutrition: dict | None = None, weather_today: dict | None = None,
) -> dict:
    """Assemble everything ``llm.coach`` is given.

    Parameters:
        conn: smart_coach db connection.
        user_id (int): Owning user.
        today (str): ISO local date.
        language (str): ``fr`` or ``en``.
        today_session (dict): ``training.session_payload`` output.
        illness (dict): ``training.illness_watch`` output.
        weekly (dict | None): ``progress.weekly_progress``, when the
            caller already has it.
        wellness (dict | None): ``metrics.daily_wellness`` for today,
            when the caller already has it.
        nutrition (dict | None): ``metrics.nutrition_for_date`` for
            today, when the caller already has it.
        weather_today (dict | None): Best-effort weather context.

    Returns:
        dict: The payload, JSON-serialisable.
    """
    wellness = wellness if wellness is not None else metrics.daily_wellness(
        conn, user_id, today,
    )
    nutrition = nutrition if nutrition is not None else (
        metrics.nutrition_for_date(conn, user_id, today)
    )
    weekly = weekly if weekly is not None else progress.weekly_progress(
        conn, user_id, today,
    )
    # What would make tonight's session go up next -- so a session that
    # stays the same comes with a way forward, not just "maintained".
    scheduled = today_session.get("scheduled_type") or today_session.get("type")
    if (
        today_session.get("tier", "train") == "train"
        and scheduled in training.SESSION_LABEL_FR
        and today_session.get("level") is not None
        and "progress_hint" not in today_session
    ):
        hint = training.progress_hint(
            conn, user_id, scheduled, today, today_session["level"],
        )
        if hint:
            today_session = {**today_session, "progress_hint": hint}
    desk_break = desk.break_for_day(
        conn, user_id, today, today_session.get("tier", "train"),
        today_session.get("status"), language,
    )
    since_last = None
    if (
        today_session.get("tier", "train") == "train"
        and scheduled in training.SESSION_LABEL_FR
        and today_session.get("level") is not None
    ):
        since_last = day_review.since_last_session(
            conn, user_id, today, scheduled, today_session["level"],
            today_session.get("values") or {},
        )
    return {
        "date": today,
        "language": language,
        # Yesterday's session (planned, done, minutes, rating) and what
        # is different tonight from the last session of the same type.
        "yesterday_training": day_review.yesterday_training(conn, user_id, today),
        # Last night read from the heart rate, beyond the watch's score.
        **({"night": night} if (night := sleep_analysis.night(
            conn, user_id, today)) else {}),
        # The two weeks of nights (the Sommeil page), reduced to one or
        # two points for this morning.
        **({"sleep_trend": trend} if (trend := sleep_analysis.morning_trend(
            sleep_analysis.period(conn, user_id, today), today)) else {}),
        **({"since_last_session": since_last} if since_last else {}),
        # "texte" (written by the model, the default) or "fiche" (the
        # same points assembled without a model).
        "message_style": db.get_setting(conn, user_id, "message_style") or "texte",
        "wellness_today": wellness,
        # The morning run fires just after wake-up, so wellness_today's
        # movement counters are still ~0. Yesterday's are the ones that
        # actually say something at this hour -- same reason
        # weekly_progress.nutrition_yesterday exists.
        "activity_yesterday": metrics.activity_yesterday(
            conn, user_id, today,
        ),
        # Yesterday judged against the person's own goals, with the
        # verdicts already computed (see metrics.movement_summary).
        "movement_yesterday": metrics.movement_summary(
            conn, user_id, today,
        ),
        "nutrition_today": nutrition,
        "weekly_progress": weekly,
        "today_session": today_session,
        "today_targets": progress.macro_targets(conn, user_id, today),
        "illness_watch": illness,
        # Precomputed rather than left to the LLM: the message quotes
        # what is LEFT to eat today, and this project never asks the
        # model to do arithmetic on figures it is meant to repeat.
        "today_remaining": progress.remaining_today(conn, user_id, today),
        # Silent exercises for a shared office, picked from the same
        # movement flags; absent on weekends or with desk_job off.
        **({"desk_break": desk_break} if desk_break else {}),
        # The kettlebell has become too light (training.heavier_bell_hint).
        **({"heavier_bell": hint} if (hint := training.heavier_bell_hint(
            conn, user_id)) else {}),
        **({"weather_today": weather_today} if weather_today else {}),
        **metrics.history_snapshot(conn, user_id, today),
    }
