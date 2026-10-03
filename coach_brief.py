#!/usr/bin/env python3
"""What the morning message is about, decided in code.

The model used to be handed the whole payload -- forty-odd blocks -- and a
page of rules, and asked to decide what mattered, check every data-quality
caveat and write it up. The result read like an audit: six labelled
sections, every number quoted, the same weight on a missing food log as on
being ill.

This module makes the decisions instead. It picks the day's *angle* (rest,
recovery, a skipped session, a good streak, an ordinary training day),
reduces yesterday to ONE point worth making, keeps the few body signals
that explain the day, and hands the model a small brief. The caveats the
old prompt spelled out (a food log that did not sync is not a fast, a
hydration total a day behind is not a deficit, a plateau is not announced
over a recent real move) are applied here, once, so they cannot be
forgotten by a paraphrase.

Pure functions of the payload dict: no database, no clock.
"""

import datetime as dt
from typing import Optional

# Protein or calorie gaps smaller than this are noise, not a point.
PROTEIN_GAP_G = 15
CALORIES_OVER_KCAL = 200
WATER_GAP_ML = 500
GREEN_STREAK_MIN = 3
WEEKLY_WEEKDAY = 6  # Sunday: the one day the weight/trend paragraph appears

# Movement flags in the order worth mentioning. Stress and a drained
# battery come first only when the day does not already explain them.
MOVEMENT_PRIORITY = (
    "sedentary_high", "steps_low", "floors_low", "intensity_low",
    "stress_high", "battery_drained",
)

ANGLE_REST = "rest"
ANGLE_RECOVERY = "recovery"
ANGLE_SKIPPED = "skipped_yesterday"
ANGLE_STREAK = "green_streak"
ANGLE_TRAIN = "train"


def _trailing_greens(statuses: list[dict]) -> int:
    """Consecutive green days at the end of the history."""
    count = 0
    for row in reversed(statuses):
        if row.get("status") != "green":
            break
        count += 1
    return count


def angle(payload: dict) -> str:
    """The one thing today's message is mostly about.

    Precedence: the body's verdict (rest, then recovery) beats anything
    about the past; a skipped session beats a streak, because it is the
    one fact the person should not have to discover for themselves.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.

    Returns:
        str: One of the ``ANGLE_*`` constants.
    """
    session = payload.get("today_session") or {}
    kind = session.get("type")
    if kind == "rest":
        return ANGLE_REST
    if kind == "recovery":
        return ANGLE_RECOVERY
    if payload.get("session_skipped_yesterday"):
        return ANGLE_SKIPPED
    if _trailing_greens(
        payload.get("statuses_last_7_days") or []
    ) >= GREEN_STREAK_MIN:
        return ANGLE_STREAK
    return ANGLE_TRAIN


def food_point(payload: dict) -> dict:
    """Yesterday's food and water reduced to a single, trustworthy point.

    Every data-quality rule lives here: a day the export never reached,
    a log that looks partial, a hydration total a sync behind -- none of
    these is ever turned into a deficit.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.

    Returns:
        dict: ``kind`` is ``protein_short`` / ``calories_over`` /
        ``water_short`` / ``on_track`` (with its figures), or
        ``not_synced`` / ``log_partial`` / ``older_day`` when yesterday
        cannot be judged (``older_day`` carries the earlier day's own
        date and figures, never passed off as yesterday's).
    """
    weekly = payload.get("weekly_progress") or {}
    food = weekly.get("nutrition_yesterday") or {}
    if not food:
        return {"kind": "not_synced"}
    if food.get("data_missing"):
        fallback = food.get("nutrition_fallback")
        if fallback:
            return {
                "kind": "older_day", "date": fallback["date"],
                "actual": fallback.get("actual", {}),
                "gap": fallback.get("gap", {}),
            }
        return {"kind": "not_synced"}
    if food.get("log_looks_incomplete"):
        return {"kind": "log_partial"}

    gap = food.get("gap") or {}
    protein = gap.get("protein_g")
    if protein is not None and protein >= PROTEIN_GAP_G:
        return {"kind": "protein_short", "gap_g": round(protein)}
    calories = gap.get("calories_kcal")
    if calories is not None and -calories >= CALORIES_OVER_KCAL:
        return {"kind": "calories_over", "over_kcal": round(-calories)}
    # Water only when its own feed reached yesterday: it syncs a day
    # behind food, so a missing total says nothing about drinking.
    water = gap.get("hydration_ml")
    if (
        water is not None and water >= WATER_GAP_ML
        and not food.get("hydration_data_missing")
    ):
        return {"kind": "water_short", "gap_ml": round(water)}
    return {
        "kind": "on_track",
        **({"protein_g": round(food["actual"]["protein_g"])}
           if (food.get("actual") or {}).get("protein_g") is not None
           else {}),
    }


def movement_point(payload: dict) -> Optional[dict]:
    """The most useful movement flag from yesterday, with its figure.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.

    Returns:
        dict | None: ``{"flag", ...figures}`` or None when nothing
        stands out.
    """
    movement = payload.get("movement_yesterday") or {}
    flags = movement.get("flags") or []
    for flag in MOVEMENT_PRIORITY:
        if flag not in flags:
            continue
        figures = {
            "steps_low": ("steps", movement.get("steps")),
            "floors_low": ("floors", movement.get("floors")),
            "intensity_low": ("intensity_week", movement.get("intensity_week")),
        }.get(flag)
        point = {"flag": flag}
        if figures and figures[1]:
            point["value"] = figures[1].get("value")
            point["goal"] = figures[1].get("goal")
        elif flag == "sedentary_high":
            point["hours"] = movement.get("sedentary_hours")
        elif flag == "stress_high":
            point["stress_avg"] = movement.get("stress_avg")
        elif flag == "battery_drained":
            point["battery_lowest"] = movement.get("battery_lowest")
        return point
    return None


def yesterday_point(payload: dict, chosen_angle: str) -> dict:
    """The single point to make about yesterday.

    A food or water miss comes first on a training day -- it is the one
    the person can fix before dinner. A movement flag comes next. On a
    rest or recovery day nothing about movement is ever a reproach: only
    food can be mentioned, and only when it is a real miss.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.
        chosen_angle (str): ``angle(payload)``.

    Returns:
        dict: ``{"topic": "food"|"movement"|"none", ...}``.
    """
    food = food_point(payload)
    food_matters = food["kind"] in (
        "protein_short", "calories_over", "water_short", "older_day",
    )
    if food_matters:
        return {"topic": "food", **food}
    if chosen_angle in (ANGLE_REST, ANGLE_RECOVERY):
        return {"topic": "none"}
    move = movement_point(payload)
    if move:
        return {"topic": "movement", **move}
    if food["kind"] == "on_track":
        return {"topic": "food", **food}
    return {"topic": "none"}


def body_summary(payload: dict) -> dict:
    """The few signals that explain how the person is, for the opener.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.

    Returns:
        dict: Only the keys that have data, plus ``illness_signs`` when
        the watch suspects an illness (signs to watch, not a diagnosis).
    """
    wellness = payload.get("wellness_today") or {}
    session = payload.get("today_session") or {}
    summary = {
        key: wellness[key] for key in (
            "sleep_score", "body_battery_highest", "hrv_status",
            "training_readiness_score", "resting_hr",
            "menstrual_cycle_phase",
        ) if wellness.get(key) is not None
    }
    if session.get("status"):
        summary["status"] = session["status"]
    illness = payload.get("illness_watch") or {}
    if illness.get("suspected"):
        summary["illness_signs"] = list(illness.get("signals") or [])[:2]
    return summary


def session_brief(payload: dict) -> dict:
    """Today's session as the message needs it: what, how much, why.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.

    Returns:
        dict: ``type`` (``rest`` / ``recovery`` / a session type /
        ``off_system``), ``values``, ``level``, ``replaces`` (the
        scheduled session title on a rest or recovery day), ``why``
        (the tier reasons, quoted as-is) and ``deload`` (True when this
        is a deliberately lighter week).
    """
    session = payload.get("today_session") or {}
    brief = {"type": session.get("type")}
    for key in ("values", "level", "note"):
        if session.get(key) not in (None, {}, ""):
            brief[key] = session[key]
    if session.get("type") in ("rest", "recovery"):
        brief["replaces"] = session.get("scheduled_title")
        brief["why"] = session.get("tier_reasons") or []
    elif session.get("in_deload") or session.get("deload_triggered"):
        brief["deload"] = True
        brief["why"] = [session["description_fr"].split("\n")[-1]] if (
            session.get("deload_triggered")
            and session.get("description_fr")
        ) else []
    return brief


def desk_brief(payload: dict) -> dict:
    """At most two desk exercises and one note, or ``{}``.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.

    Returns:
        dict: ``items`` (name/how/seconds), optional ``note`` and
        ``gentle_only``.
    """
    desk = payload.get("desk_break") or {}
    items = (desk.get("items") or [])[:2]
    if not items:
        return {}
    brief = {
        "items": [
            {"name": i["name"], "how": i["how"], "seconds": i["seconds"]}
            for i in items
        ],
        "gentle_only": bool(desk.get("gentle_only")),
    }
    if desk.get("notes"):
        brief["note"] = desk["notes"][0]
    return brief


def weekly_brief(payload: dict) -> dict:
    """The weight/body-composition paragraph, on Sunday only.

    The old daily message carried this every morning, which made a
    two-day weight wobble a daily topic. Once a week is enough to see a
    direction, and the dashboard has the rest.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.

    Returns:
        dict: Compact trend facts, or ``{}`` on any other day or when
        there is nothing trustworthy to say.
    """
    day = dt.date.fromisoformat(payload["date"]) if payload.get(
        "date"
    ) else None
    if day is None or day.weekday() != WEEKLY_WEEKDAY:
        return {}
    weekly = payload.get("weekly_progress") or {}
    brief: dict = {}
    trend = weekly.get("weight_trend_14d") or {}
    plateau = weekly.get("plateau") or {}
    if trend and not plateau.get("too_few_weigh_ins"):
        brief["weight_trend"] = {
            key: trend[key] for key in (
                "current_avg", "past_avg", "delta", "current_days",
                "past_days",
            ) if key in trend
        }
    progression = weekly.get("weight_progression") or {}
    if progression.get("per_week") is not None:
        brief["weight_per_week"] = progression["per_week"]
    lean = weekly.get("lean_mass_trend_28d") or {}
    if lean:
        brief["lean_mass_trend"] = lean
    # A real recent move cancels the plateau reading, always.
    if plateau.get("plateau") and not plateau.get("recent_move"):
        brief["plateau"] = True
    elif plateau.get("recent_move"):
        brief["recent_move"] = plateau["recent_move"]
    if (weekly.get("recalibration") or {}).get("flagged"):
        brief["recalibration"] = weekly["recalibration"]
    return brief


def build_brief(payload: dict) -> dict:
    """Reduce the full payload to what the message is written from.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.

    Returns:
        dict: ``language``, ``date``, ``angle``, ``body``, ``session``,
        ``yesterday``, ``desk``, optional ``skipped`` /
        ``green_streak`` / ``weather`` / ``weekly``. Small enough that
        every field gets used.
    """
    chosen = angle(payload)
    brief = {
        "language": payload.get("language", "fr"),
        "date": payload.get("date"),
        "angle": chosen,
        "body": body_summary(payload),
        "session": session_brief(payload),
        "yesterday": yesterday_point(payload, chosen),
    }
    desk = desk_brief(payload)
    if desk:
        brief["desk"] = desk
    skipped = payload.get("session_skipped_yesterday")
    if skipped:
        brief["skipped"] = skipped
    if chosen == ANGLE_STREAK:
        brief["green_streak"] = _trailing_greens(
            payload.get("statuses_last_7_days") or []
        )
    weather = payload.get("weather_today")
    if weather and chosen in (ANGLE_TRAIN, ANGLE_STREAK):
        brief["weather"] = weather
    weekly = weekly_brief(payload)
    if weekly:
        brief["weekly"] = weekly
    return brief


if __name__ == "__main__":
    def _payload(**overrides) -> dict:
        base = {
            "date": "2026-10-05",  # a Monday
            "language": "fr",
            "wellness_today": {"sleep_score": 80, "body_battery_highest": 72},
            "today_session": {
                "type": "treadmill", "status": "green", "level": 4,
                "values": {"speed_kmh": 6.5, "incline_pct": 12,
                           "duration_min": 25},
            },
            "weekly_progress": {"nutrition_yesterday": {
                "date": "2026-10-04", "gap": {}, "actual": {},
                "data_missing": False, "hydration_data_missing": False,
                "log_looks_incomplete": False,
            }},
            "movement_yesterday": {"flags": []},
            "statuses_last_7_days": [],
            "illness_watch": {"suspected": False, "signals": []},
            "session_skipped_yesterday": None,
        }
        base.update(overrides)
        return base

    def _food(**fields) -> dict:
        food = {
            "date": "2026-10-04", "gap": {}, "actual": {},
            "data_missing": False, "hydration_data_missing": False,
            "log_looks_incomplete": False,
        }
        food.update(fields)
        return {"nutrition_yesterday": food}

    # --- angle -------------------------------------------------------
    assert angle(_payload()) == ANGLE_TRAIN
    assert angle(_payload(today_session={"type": "rest"})) == ANGLE_REST
    assert angle(_payload(today_session={"type": "recovery"})) == (
        ANGLE_RECOVERY
    )
    skipped = {"date": "2026-10-04", "session_type": "upper_body"}
    assert angle(_payload(session_skipped_yesterday=skipped)) == ANGLE_SKIPPED
    greens = [{"date": f"2026-10-0{d}", "status": "green"} for d in (1, 2, 3)]
    assert angle(_payload(statuses_last_7_days=greens)) == ANGLE_STREAK
    assert angle(_payload(
        statuses_last_7_days=greens + [{"date": "x", "status": "yellow"}],
    )) == ANGLE_TRAIN  # the streak is the trailing run, not a count
    # The body's verdict beats a skip: nobody is scolded on a rest day.
    assert angle(_payload(
        today_session={"type": "rest"}, session_skipped_yesterday=skipped,
    )) == ANGLE_REST

    # --- food_point: every caveat the old prompt spelled out ----------
    assert food_point(_payload(weekly_progress=_food(
        gap={"protein_g": 38.0},
    ))) == {"kind": "protein_short", "gap_g": 38}
    assert food_point(_payload(weekly_progress=_food(
        gap={"protein_g": 5.0, "calories_kcal": -350.0},
    ))) == {"kind": "calories_over", "over_kcal": 350}
    assert food_point(_payload(weekly_progress=_food(
        gap={"hydration_ml": 900.0},
    )))["kind"] == "water_short"
    # Water a sync behind is not a deficit...
    assert food_point(_payload(weekly_progress=_food(
        gap={"hydration_ml": 900.0}, hydration_data_missing=True,
    )))["kind"] == "on_track"
    # ...a day the export never reached is not a fast...
    assert food_point(_payload(weekly_progress=_food(
        data_missing=True, gap={"protein_g": 140.0},
    ))) == {"kind": "not_synced"}
    # ...an earlier day is named by ITS date, never as yesterday...
    older = food_point(_payload(weekly_progress=_food(
        data_missing=True, nutrition_fallback={
            "date": "2026-10-03", "actual": {"calories_kcal": 1900},
            "gap": {"protein_g": 20.0},
        },
    )))
    assert older["kind"] == "older_day" and older["date"] == "2026-10-03"
    # ...and a partial log is never turned into a deficit.
    assert food_point(_payload(weekly_progress=_food(
        log_looks_incomplete=True, gap={"protein_g": 90.0},
    ))) == {"kind": "log_partial"}

    # --- one point about yesterday ------------------------------------
    flags = {"flags": ["steps_low", "stress_high"],
             "steps": {"value": 2765, "goal": 10000}}
    point = yesterday_point(_payload(movement_yesterday=flags), ANGLE_TRAIN)
    assert point == {
        "topic": "movement", "flag": "steps_low", "value": 2765,
        "goal": 10000,
    }, point
    # A food miss outranks movement on a training day...
    miss = _payload(movement_yesterday=flags, weekly_progress=_food(
        gap={"protein_g": 40.0},
    ))
    assert yesterday_point(miss, ANGLE_TRAIN)["topic"] == "food"
    # ...but on a rest day movement is never a reproach.
    assert yesterday_point(
        _payload(movement_yesterday=flags), ANGLE_REST,
    ) == {"topic": "none"}
    assert yesterday_point(miss, ANGLE_REST)["topic"] == "food"

    # --- the whole brief -----------------------------------------------
    rest = build_brief(_payload(
        today_session={
            "type": "rest", "status": "red", "scheduled_title": "Tapis",
            "tier_reasons": ["tu es declare malade jusqu'au 2026-10-07"],
            "values": {},
        },
        illness_watch={"suspected": True, "signals": ["VFC basse", "x", "y"]},
        movement_yesterday=flags,
        desk_break={
            "items": [
                {"name": "Respiration carree", "how": "4-4-4-4",
                 "seconds": 90, "kind": "breath", "posture": "seated"},
                {"name": "a", "how": "b", "seconds": 1},
                {"name": "c", "how": "d", "seconds": 1},
            ],
            "gentle_only": True, "notes": ["n1", "n2"],
        },
    ))
    assert rest["angle"] == ANGLE_REST, rest
    assert rest["session"]["replaces"] == "Tapis", rest
    assert rest["session"]["why"], rest
    assert rest["yesterday"] == {"topic": "none"}, rest
    assert rest["body"]["illness_signs"] == ["VFC basse", "x"], rest
    assert len(rest["desk"]["items"]) == 2 and rest["desk"]["note"] == "n1"
    assert "weekly" not in rest and "skipped" not in rest

    train = build_brief(_payload(weather_today={"temp_max_c": 18}))
    assert train["angle"] == ANGLE_TRAIN and "weather" in train, train
    assert train["session"]["values"]["speed_kmh"] == 6.5, train
    assert "why" not in train["session"], train
    # Small on purpose: nothing in it that the message should not use.
    assert set(train) <= {
        "language", "date", "angle", "body", "session", "yesterday",
        "desk", "skipped", "green_streak", "weather", "weekly",
    }, train

    # Weather is context for an outdoor-able training day only.
    assert "weather" not in build_brief(_payload(
        today_session={"type": "rest"}, weather_today={"temp_max_c": 18},
    ))

    # --- weekly: Sunday only, caveats applied --------------------------
    trend = {"current_avg": 80.1, "past_avg": 80.9, "delta": -0.8,
             "current_days": 5, "past_days": 6}
    sunday_progress = {
        **_food(), "weight_trend_14d": trend,
        "weight_progression": {"per_week": -0.3},
        "plateau": {"plateau": True, "note": "x"},
        "recalibration": {"flagged": False},
    }
    assert "weekly" not in build_brief(_payload(  # Monday
        weekly_progress=sunday_progress,
    ))
    week = build_brief(_payload(
        date="2026-10-04", weekly_progress=sunday_progress,
    ))["weekly"]
    assert week["weight_trend"]["delta"] == -0.8, week
    assert week["plateau"] is True and week["weight_per_week"] == -0.3, week
    # A real recent move cancels the plateau reading.
    moved = {**sunday_progress, "plateau": {
        "plateau": True, "recent_move": {"direction": "up", "change": 0.6},
    }}
    week = build_brief(_payload(
        date="2026-10-04", weekly_progress=moved,
    ))["weekly"]
    assert "plateau" not in week and week["recent_move"]["change"] == 0.6
    # Too few weigh-ins: no trend at all.
    thin = {**sunday_progress, "plateau": {
        "plateau": False, "too_few_weigh_ins": True,
    }}
    assert "weight_trend" not in build_brief(_payload(
        date="2026-10-04", weekly_progress=thin,
    ))["weekly"]

    print("coach_brief.py: all checks passed")
