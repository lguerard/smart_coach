#!/usr/bin/env python3
"""See what the morning message would say, without waiting for 07:45.

Builds the brief for five typical mornings and prints it. With ``--live``
it also asks the configured provider (LLM_PROVIDER, default the claude
CLI) to write each message and prints the word count next to it -- the
quickest way to judge the voice after changing the prompt in llm.py.

    python preview_message.py            # the briefs only, no LLM call
    python preview_message.py --live     # briefs + the written messages
    python preview_message.py --live --en
"""

import json
import sys

import coach_brief
import llm

FOOD_OK = {
    "date": "2026-10-04", "gap": {"protein_g": 5.0}, "actual": {
        "protein_g": 135.0,
    }, "data_missing": False, "hydration_data_missing": False,
    "log_looks_incomplete": False,
}
DESK = {"items": [
    {"name": "Montees sur pointes", "how": "Debout, une main sur le "
     "bureau, monte sur les pointes en 2 s, descends en 2 s. 20 "
     "repetitions, sans bruit.", "seconds": 60, "kind": "strength",
     "posture": "standing", "when": "apres le dejeuner, quand l'energie "
     "retombe"},
    {"name": "Ouverture de poitrine", "how": "Assis au bord de la "
     "chaise, mains jointes derriere le dos, ouvre la poitrine. "
     "Tiens 20-30 s.", "seconds": 30, "kind": "stretch",
     "posture": "seated", "when": "en milieu de matinee, quand le dos "
     "commence a tirer"},
], "gentle_only": False, "notes": []}
GENTLE_DESK = {"items": [
    {"name": "Respiration carree", "how": "Inspire 4 s, retiens 4 s, "
     "expire 4 s, retiens 4 s. 6 cycles, yeux sur l'ecran.",
     "seconds": 90, "kind": "breath", "posture": "seated",
     "when": "avant ta prochaine reunion, ou quand les epaules montent"},
], "gentle_only": True, "notes": []}
TREADMILL = {
    "type": "treadmill", "status": "green", "level": 4,
    "values": {"speed_kmh": 6.5, "incline_pct": 12, "duration_min": 25},
}


def _base(**overrides) -> dict:
    payload = {
        "date": "2026-10-06", "language": "fr",  # a Tuesday
        "wellness_today": {"sleep_score": 82, "body_battery_highest": 74,
                           "hrv_status": "BALANCED"},
        "today_session": TREADMILL,
        "weekly_progress": {"nutrition_yesterday": FOOD_OK},
        "movement_yesterday": {"flags": []},
        "statuses_last_7_days": [],
        "illness_watch": {"suspected": False, "signals": []},
        "session_skipped_yesterday": None,
        "desk_break": DESK,
    }
    payload.update(overrides)
    return payload


SCENARIOS = {
    "Malade, repos": _base(
        wellness_today={"sleep_score": 55, "body_battery_highest": 23},
        today_session={
            "type": "rest", "status": "red", "scheduled_title": "Tapis",
            "tier": "rest", "values": {},
            "tier_reasons": ["tu es declare malade jusqu'au 2026-10-08"],
        },
        today_targets={"hydration_target_ml": 2500},
        illness_watch={"suspected": True, "signals": [
            "VFC basse", "FC de repos +6 vs ta base"]},
        movement_yesterday={"flags": ["steps_low", "sedentary_high"],
                            "steps": {"value": 2765, "goal": 10000}},
        desk_break=GENTLE_DESK,
    ),
    "Rouge, marche de recuperation": _base(
        wellness_today={"sleep_score": 48, "body_battery_highest": 38},
        today_session={
            "type": "recovery", "status": "red", "level": 0,
            "scheduled_title": "Tapis", "tier": "recovery",
            "values": {"speed_kmh": 4.5, "incline_pct": 0,
                       "duration_min": 20},
            "tier_reasons": ["jour rouge et niveau deja au minimum"],
        },
        desk_break=GENTLE_DESK,
    ),
    "Belle forme, proteines en retard": _base(
        weekly_progress={"nutrition_yesterday": {
            **FOOD_OK, "gap": {"protein_g": 38.0},
        }},
    ),
    "Seance sautee hier": _base(
        session_skipped_yesterday={
            "date": "2026-10-05", "session_type": "upper_body"},
        movement_yesterday={"flags": ["sedentary_high"],
                            "sedentary_hours": 12.5},
    ),
    "Dimanche, serie de verts": _base(
        date="2026-10-04",
        statuses_last_7_days=[
            {"date": f"2026-10-0{d}", "status": "green"} for d in (1, 2, 3)
        ],
        weekly_progress={
            "nutrition_yesterday": FOOD_OK,
            "weight_trend_14d": {"current_avg": 80.1, "past_avg": 80.9,
                                 "delta": -0.8, "current_days": 5,
                                 "past_days": 6},
            "weight_progression": {"per_week": -0.3},
            "plateau": {"plateau": False}, "recalibration": {
                "flagged": False},
        },
    ),
}


def main() -> None:
    """Print the five briefs, and the written messages with ``--live``."""
    live = "--live" in sys.argv
    language = "en" if "--en" in sys.argv else "fr"
    for title, payload in SCENARIOS.items():
        payload = {**payload, "language": language}
        print(f"\n=== {title} ===")
        print(json.dumps(coach_brief.build_brief(payload),
                         ensure_ascii=False, indent=1))
        if live:
            message = llm.coach(payload)
            print(f"\n--- message ({len(message.split())} mots) ---")
            print(message)


if __name__ == "__main__":
    main()
