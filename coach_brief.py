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

import aids

# Protein or calorie gaps smaller than this are noise, not a point.
PROTEIN_GAP_G = 15
CALORIES_OVER_KCAL = 200
WATER_GAP_ML = 500
GREEN_STREAK_MIN = 3
WEEKLY_WEEKDAY = 6  # Sunday: the one day the weight/trend paragraph appears

# Movement flags in the order worth mentioning. Stress and a drained
# battery come first only when the day does not already explain them.
# Garmin's "sedentary" time is every awake minute without much
# movement -- standing, driving, lying awake -- not sitting, so it is not
# a message point: "you sat 13 hours" was simply wrong.
MOVEMENT_PRIORITY = (
    "steps_low", "floors_low", "intensity_low", "stress_high",
    "battery_drained",
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
            "sleep_felt", "sleep_score", "body_battery_highest", "hrv_status",
            "training_readiness_score", "resting_hr",
            "menstrual_cycle_phase",
        ) if wellness.get(key) is not None
    }
    night = payload.get("night") or {}
    if night.get("has_hr"):
        summary["night"] = {
            "sleep_min": night["sleep_min"],
            "watch_sleep_min": night["watch_sleep_min"],
            "wakes": [w["at"] for w in night["wakes"]],
            **({"final_wake": night["final_wake"]} if night.get("final_wake") else {}),
        }
    if session.get("status"):
        summary["status"] = session["status"]
    if session.get("tired_muscles"):
        summary["tired_muscles"] = session["tired_muscles"]
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
    values = dict(brief.get("values") or {})
    variants = values.pop("variants", None)
    if variants:
        # A circuit: the moves by name with their count, not the slot
        # keys and the whole variant catalogue -- that is what the
        # message has to say.
        brief["values"] = {k: v for k, v in values.items()
                           if k in ("rounds", "duration_min", "rest_sec")}
        brief["moves"] = [
            f"{info['name']} {values[slot]}"
            f"{'s' if slot.endswith('_sec') else ''}"
            f"{'/jambe' if slot.endswith('_per_leg') else ''}"
            for slot, info in variants.items() if slot in values
        ]
        if values.get("finisher"):
            import custom_moves

            brief["moves"].append(custom_moves.finisher_text(values))
        # The bell's weight once, not after every move.
        import plain_message

        brief["moves"], kg = plain_message._bell_kg(brief["moves"])
        if kg:
            brief["bell_kg"] = kg
        nexts = [info["next"] for info in variants.values()
                 if info.get("next")]
        if nexts:
            brief["next_rung"] = nexts[0]
    if session.get("level_reason") and session.get("type") not in (
        "rest", "recovery",
    ):
        brief["level_reason"] = session["level_reason"]
        if session.get("progress_hint"):
            brief["progress_hint"] = session["progress_hint"]
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
    # The calorie target follows the scale (progress.observed_tdee):
    # said on Sundays with its numbers, since the budget line alone
    # shows only a lower figure.
    observed = weekly.get("observed_tdee")
    targets = payload.get("today_targets") or {}
    if observed and targets.get("tdee_source") == "observed":
        brief["target_follows_weight"] = {
            "logged_kcal": observed["intake_kcal"],
            "weight_kg_per_week": observed["weekly_kg"],
            "burns_kcal": observed["tdee_kcal"],
            "watch_kcal": targets.get("device_tdee_kcal"),
            "target_kcal": targets.get("calorie_target_kcal"),
        }
    return brief


def help_brief(
    payload: dict, chosen_angle: str, point: dict, has_desk: bool,
) -> dict:
    """The concrete help for today, already sized.

    A miss on food becomes a short list of real foods (desk-friendly when
    the person works at one), a water gap becomes glasses, a rest day
    becomes care points with when to see a doctor. The model repeats
    these; it no longer makes amounts up.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.
        chosen_angle (str): ``angle(payload)``.
        point (dict): ``yesterday_point`` output.
        has_desk (bool): Whether a desk break is part of the day.

    Returns:
        dict: ``food`` / ``water`` / ``lighter_dinner`` / ``care``,
        only the ones that apply.
    """
    language = payload.get("language", "fr")
    date = payload.get("date")
    help_: dict = {}
    if point.get("topic") == "food" and date:
        kind = point.get("kind")
        if kind == "protein_short":
            help_["food"] = aids.food_help(
                point["gap_g"], date, language, office=has_desk,
            )
        elif kind == "calories_over":
            help_["lighter_dinner"] = aids.lighter_dinner(
                point["over_kcal"], language,
            )
        elif kind == "water_short":
            help_["water"] = aids.water_help(point["gap_ml"], language)
    if chosen_angle == ANGLE_REST:
        help_["care"] = aids.care_help(
            (payload.get("today_targets") or {}).get("hydration_target_ml"),
            language,
        )
    return help_


def review_brief(payload: dict) -> dict:
    """Yesterday reviewed: the session, the food, the steps, a verdict.

    Every figure is checked here (a food log the export never reached, or
    one that looks partial, is said as such, never turned into a miss),
    so the message can draw conclusions without doing any arithmetic.

    Returns:
        dict: ``training`` (``day_review.yesterday_training``), ``food``
        (``kind`` plus ``protein_g`` / ``calories_kcal`` as ``{eaten,
        target}`` when credible), ``steps`` (``{value, goal}``), and
        ``verdict``: ``good`` (what was planned was done and the rest
        followed), ``mixed`` or ``off``; ``wins`` and ``misses`` name
        what drives it.
    """
    training_day = dict(payload.get("yesterday_training") or {})
    review: dict = {}
    wins, misses = [], []
    if training_day.get("planned") or training_day.get("done"):
        review["training"] = {k: v for k, v in training_day.items()
                              if v not in (None, "") and k != "date"}
        tier = training_day.get("planned_tier")
        if training_day.get("done") == "rien":
            if tier == "train":
                misses.append("seance")
        elif training_day.get("recorded"):
            wins.append("seance")
        elif tier in ("rest", "recovery"):
            wins.append("repos")

    point = food_point(payload)
    food = {"kind": point["kind"]}
    weekly = payload.get("weekly_progress") or {}
    day = weekly.get("nutrition_yesterday") or {}
    if point["kind"] in ("protein_short", "calories_over", "water_short",
                         "on_track"):
        actual, gap = day.get("actual") or {}, day.get("gap") or {}
        for key in ("protein_g", "calories_kcal"):
            if actual.get(key) is not None and gap.get(key) is not None:
                food[key] = {"eaten": round(actual[key]),
                             "target": round(actual[key] + gap[key])}
        (misses if point["kind"] != "on_track" else wins).append(
            "proteines" if point["kind"] in ("protein_short", "on_track")
            else "calories" if point["kind"] == "calories_over" else "eau")
    review["food"] = food

    steps = (payload.get("movement_yesterday") or {}).get("steps") or {}
    if steps.get("value") is not None:
        review["steps"] = {"value": steps["value"], "goal": steps.get("goal")}
        if steps.get("verdict") == "low":
            misses.append("pas")
        elif steps.get("verdict") in ("reached", "close"):
            wins.append("pas")
    review["wins"], review["misses"] = wins, misses
    review["verdict"] = ("good" if wins and not misses
                         else "off" if misses and not wins
                         else "mixed" if wins or misses else "none")
    return review


def changes_brief(payload: dict) -> dict:
    """What is different tonight, and why.

    Returns:
        dict: ``since_last_session`` figures (``day_review``), plus
        ``level_text``: the level decision in plain words (up a notch,
        same, lighter, and the signals behind it).
    """
    import plain_message

    session = payload.get("today_session") or {}
    changes = dict(payload.get("since_last_session") or {})
    text = plain_message._level_line(
        {"level_reason": session.get("level_reason")},
        {"tired_muscles": session.get("tired_muscles")}, True,
    ) if session.get("level_reason") else ""
    if text:
        changes["level_text"] = text
    return changes


def build_brief(payload: dict) -> dict:
    """Reduce the full payload to what the message is written from.

    Parameters:
        payload (dict): ``coach_payload.build_payload`` output.

    Returns:
        dict: ``language``, ``date``, ``angle``, ``body``, ``session``,
        ``yesterday``, ``help`` (sized foods/water/care),
        ``next_step`` (what the closing line is built on), optional
        ``skipped`` / ``green_streak`` / ``weather`` / ``weekly``. Small
        enough that every field gets used.
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
    # No desk exercises in the morning message: the person found them
    # noise. The afternoon check-in still has its silent break.
    help_ = help_brief(payload, chosen, brief["yesterday"],
                       bool(payload.get("desk_break")))
    if help_:
        brief["help"] = help_
    # Off-system days have no session to adjust, so no stock close.
    # A training day has no closing step any more: the evening
    # notification's rating buttons already ask how it went.
    session_kind = (payload.get("today_session") or {}).get("type")
    step = (
        "" if session_kind == "off_system" or chosen == ANGLE_TRAIN
        else aids.next_step(chosen, brief["language"])
    )
    if step:
        brief["next_step"] = step
    skipped = payload.get("session_skipped_yesterday")
    if skipped:
        brief["skipped"] = skipped
    bell = payload.get("heavier_bell")
    if bell:
        brief["heavier_bell"] = {"current_kg": bell["current"],
                                 "next_kg": bell["next"]}
    changed = payload.get("session_changed_yesterday")
    if changed:
        import training

        labels = training.SESSION_LABEL_FR
        planned = (
            "repos" if changed["planned_tier"] == "rest"
            else "marche de recuperation" if changed["planned_tier"] == "recovery"
            else labels.get(changed["planned"], changed["planned"])
        )
        brief["yesterday_session"] = {
            "done": labels.get(changed["done"], changed["done"]),
            "instead_of": planned,
        }
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
    brief["review"] = review_brief(payload)
    if payload.get("sleep_trend"):
        brief["sleep_trend"] = payload["sleep_trend"]
    changes = changes_brief(payload)
    if changes:
        brief["changes"] = changes
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
    assert "desk" not in rest  # no desk exercises in the morning
    assert "weekly" not in rest and "skipped" not in rest

    train = build_brief(_payload(weather_today={"temp_max_c": 18}))
    assert train["angle"] == ANGLE_TRAIN and "weather" in train, train
    assert train["session"]["values"]["speed_kmh"] == 6.5, train
    assert "why" not in train["session"], train
    # Small on purpose: nothing in it that the message should not use.
    assert set(train) <= {
        "language", "date", "angle", "body", "session", "yesterday",
        "help", "next_step", "skipped", "green_streak",
        "weather", "weekly", "review", "changes", "sleep_trend",
    }, train

    # --- yesterday reviewed, and what changed -----------------------------
    reviewed = build_brief(_payload(
        weekly_progress=_food(gap={"protein_g": 38.0}),
        yesterday_training={"date": "d", "planned": "Kettlebell a la maison",
                            "planned_tier": "train",
                            "done": "Kettlebell a la maison",
                            "recorded": True, "minutes": 26, "rating": "bien"},
        movement_yesterday={"flags": ["steps_low"], "steps": {
            "value": 3900, "goal": 8000, "verdict": "low"}},
        since_last_session={"last_date": "2026-10-03", "days": 2,
                            "level": {"from": 4, "to": 5}},
        today_session={"type": "treadmill", "status": "yellow", "level": 5,
                       "values": {"speed_kmh": 6.5},
                       "level_reason": "ta derniere seance de ce type etait "
                                       "trop facile : niveau 4 -> 5"},
    ))
    review = reviewed["review"]
    assert review["training"]["minutes"] == 26, review
    assert review["food"]["kind"] == "protein_short", review
    assert review["wins"] == ["seance"] and set(review["misses"]) == {
        "proteines", "pas"}, review
    assert review["verdict"] == "mixed", review
    assert reviewed["changes"]["level"] == {"from": 4, "to": 5}
    assert reviewed["changes"]["level_text"].startswith("On monte d'un cran")
    # Last night read from the heart rate rides in body; a night
    # without heart rate does not.
    night = {"has_hr": True, "sleep_min": 320, "watch_sleep_min": 350,
             "wakes": [{"at": "02:41"}, {"at": "04:40"}], "final_wake": "04:40"}
    assert build_brief(_payload(night=night))["body"]["night"] == {
        "sleep_min": 320, "watch_sleep_min": 350, "wakes": ["02:41", "04:40"],
        "final_wake": "04:40"}
    assert "night" not in build_brief(_payload(
        night={**night, "has_hr": False}))["body"]
    # A food log that never reached the export is not a miss.
    unsynced = build_brief(_payload(weekly_progress={}))["review"]
    assert unsynced["food"] == {"kind": "not_synced"} and not unsynced["misses"]

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


    # --- help and the closing step ---------------------------------------
    short = _payload(weekly_progress=_food(gap={"protein_g": 38.0}),
                     desk_break={"items": [
                         {"name": "a", "how": "b", "seconds": 5,
                          "when": "apres le dejeuner"},
                     ]})
    helped = build_brief(short)
    assert helped["help"]["food"]["items"], helped
    assert helped["help"]["food"]["covers_gap"], helped
    assert "desk" not in helped
    assert "next_step" not in helped, helped  # the evening buttons ask
    plain = build_brief(_payload())
    assert "help" not in plain and "next_step" not in plain, plain
    # Water and calories get their own help.
    assert build_brief(_payload(weekly_progress=_food(
        gap={"hydration_ml": 900.0},
    )))["help"]["water"]["glasses"] == 4
    assert "lighter_dinner" in build_brief(_payload(
        weekly_progress=_food(gap={"calories_kcal": -400.0}),
    ))["help"]
    # A rest day carries care points, with when to see a doctor, and a
    # next step about the restart -- not a training prompt.
    resting = build_brief(_payload(
        today_session={"type": "rest", "tier_reasons": ["x"]},
        today_targets={"hydration_target_ml": 2500},
    ))
    assert resting["help"]["care"]["drink_ml"] == 2500, resting
    assert "fievre" in resting["help"]["care"]["see_doctor_if"], resting
    assert "reprise" in resting["next_step"], resting
    assert "tableau de bord" not in resting["next_step"], resting
    # Off-system days have no session to adjust, so no stock close.
    assert "next_step" not in build_brief(_payload(
        today_session={"type": "off_system", "note": "x"},
    ))
    # English help comes back in English.
    assert build_brief({**short, "language": "en"})["help"]["food"][
        "items"
    ][0]["name"] != helped["help"]["food"]["items"][0]["name"]

    # A circuit reaches the brief as named moves, its level reason and
    # the next rung -- not as slot keys and the variant catalogue.
    import training

    circuit_values = training.session_values("upper_body", 2, date="2026-10-08")
    circuit = build_brief(_payload(today_session={
        "type": "upper_body", "status": "green", "level": 2,
        "values": circuit_values, "level_reason": "jour vert : 1 -> 2",
        "tired_muscles": "pectoraux encore fatigues (natation)",
    }))
    session = circuit["session"]
    assert "variants" not in session["values"], session
    assert any(m.startswith("pompes") for m in session["moves"]), session
    assert session["level_reason"] == "jour vert : 1 -> 2", session
    assert "pompes" in session["next_rung"], session
    assert circuit["body"]["tired_muscles"].startswith("pectoraux")

    changed_brief = build_brief(_payload(session_changed_yesterday={
        "date": "d", "done": "kettlebell", "planned": "kettlebell",
        "planned_tier": "recovery"}))
    assert changed_brief["yesterday_session"] == {
        "done": "Kettlebell a la maison",
        "instead_of": "marche de recuperation"}, changed_brief
    assert "yesterday_session" not in build_brief(_payload())
    print("coach_brief.py: all checks passed")
