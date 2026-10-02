#!/usr/bin/env python3
"""The data the coaching message is written from, in one place.

The morning run (run_coach.py) and the dashboard's regenerate button
both feed the LLM. They used to build their payloads separately, so every
signal added to one was missing from the other: a regenerated message
silently lost yesterday's activity, the illness watch and the remaining
budget the morning one had. One builder means a regenerated message is
the same message with a different phrasing, not a poorer one.
"""

import desk
import metrics
import progress
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
    desk_break = desk.break_for_day(
        conn, user_id, today, today_session.get("tier", "train"),
        today_session.get("status"), language,
    )
    return {
        "date": today,
        "language": language,
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
        **({"weather_today": weather_today} if weather_today else {}),
        **metrics.history_snapshot(conn, user_id, today),
    }
