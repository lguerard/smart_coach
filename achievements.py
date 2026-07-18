#!/usr/bin/env python3
"""Xbox-Gamerscore-style achievement system.

Every achievement is a deterministic check over data already in
smart_sport's db -- same "never invent it" posture as training.py/
progress.py, just turned into unlock conditions instead of coaching
numbers. Achievements never re-lock once earned (unlocked_keys is
checked before running each check), and only genuinely-verifiable
things get an achievement: e.g. session-count milestones use HC's own
exercise_sessions table (real logged workouts), not coach_log (which
only proves the coach ran, not that the workout happened).

Multi-user: every function takes ``user_id`` and scopes its queries to
that person's rows only -- achievements, XP, and streaks are all
per-account, sharing nothing with other users on the same deployment.
"""

import datetime as dt
import sqlite3

import db
import metrics
import progress
import training

TIER_POINTS = {"bronze": 10, "silver": 25, "gold": 50, "platinum": 100}

# Earned Garmin Connect badges (ingest/garmin_api.py) are surfaced as
# achievements too, not a separate section -- Garmin assigns the
# badge, we don't grade it, so every one gets this flat tier (no
# reliable per-badge difficulty/points field in the ingested data).
GARMIN_BADGE_TIER = "silver"

ACHIEVEMENTS = {
    "first_sync": {
        "icon": "\U0001F4E1", "tier": "bronze",
        "name_fr": "Premiere synchro", "name_en": "First Sync",
        "desc_fr": "Premieres donnees Health Connect importees.",
        "desc_en": "First Health Connect data imported.",
    },
    "first_nutrition_log": {
        "icon": "\U0001F37D", "tier": "bronze",
        "name_fr": "Premier repas logue", "name_en": "Logging In",
        "desc_fr": "Premier repas enregistre.",
        "desc_en": "First meal logged.",
    },
    "streak_3": {
        "icon": "\U0001F525", "tier": "bronze",
        "name_fr": "Sur la lancee", "name_en": "On a Roll",
        "desc_fr": "3 jours verts d'affilee.",
        "desc_en": "3 consecutive green days.",
    },
    "streak_7": {
        "icon": "\U0001F525", "tier": "silver",
        "name_fr": "Semaine de guerrier", "name_en": "Week Warrior",
        "desc_fr": "7 jours verts d'affilee.",
        "desc_en": "7 consecutive green days.",
    },
    "streak_30": {
        "icon": "\U0001F31F", "tier": "gold",
        "name_fr": "Intouchable", "name_en": "Unstoppable",
        "desc_fr": "30 jours verts d'affilee.",
        "desc_en": "30 consecutive green days.",
    },
    "comeback": {
        "icon": "\U0001F4AA", "tier": "bronze",
        "name_fr": "Rebond", "name_en": "Bounce Back",
        "desc_fr": "Un jour rouge suivi d'un jour vert.",
        "desc_en": "A red day immediately followed by a green one.",
    },
    "deload_survived": {
        "icon": "\U0001F6E1", "tier": "silver",
        "name_fr": "Guerrier du deload", "name_en": "Deload Warrior",
        "desc_fr": "Une semaine de deload menee a son terme.",
        "desc_en": "Completed a full deload week.",
    },
    "level_5_any": {
        "icon": "\U0001F4AA", "tier": "bronze",
        "name_fr": "Ca se muscle", "name_en": "Getting Stronger",
        "desc_fr": "Niveau 5 atteint sur un type de seance.",
        "desc_en": "Reached level 5 on any session type.",
    },
    "level_10_any": {
        "icon": "\U0001F3C6", "tier": "gold",
        "name_fr": "Niveau max", "name_en": "Maxed Out",
        "desc_fr": "Niveau 10 atteint sur un type de seance.",
        "desc_en": "Reached level 10 on any session type.",
    },
    "level_10_all": {
        "icon": "\U0001F451", "tier": "platinum",
        "name_fr": "Forme olympique", "name_en": "Peak Form",
        "desc_fr": "Niveau 10 sur les 4 types de seance en meme temps.",
        "desc_en": "Level 10 on all 4 session types at once.",
    },
    "protein_week": {
        "icon": "\U0001F969", "tier": "silver",
        "name_fr": "Proteines parfaites", "name_en": "Protein Perfect",
        "desc_fr": "Objectif proteines atteint 7 jours d'affilee.",
        "desc_en": "Hit the protein target 7 days running.",
    },
    "hydration_week": {
        "icon": "\U0001F4A7", "tier": "silver",
        "name_fr": "Heros de l'hydratation", "name_en": "Hydration Hero",
        "desc_fr": "Objectif hydratation atteint 7 jours d'affilee.",
        "desc_en": "Hit the hydration target 7 days running.",
    },
    "step_goal_week": {
        "icon": "\U0001F45F", "tier": "silver",
        "name_fr": "Semaine de marche", "name_en": "Step It Up",
        "desc_fr": "Objectif de pas atteint 7 jours d'affilee.",
        "desc_en": "Hit the step goal 7 days running.",
    },
    "weight_1kg": {
        "icon": "\U0001F947", "tier": "bronze",
        "name_fr": "Premier pas", "name_en": "First Step",
        "desc_fr": "1 kg de progres vers l'objectif.",
        "desc_en": "1kg of progress toward the goal.",
    },
    "weight_5kg": {
        "icon": "\U0001F947", "tier": "silver",
        "name_fr": "Club des 5 kg", "name_en": "5kg Club",
        "desc_fr": "5 kg de progres vers l'objectif.",
        "desc_en": "5kg of progress toward the goal.",
    },
    "weight_10kg": {
        "icon": "\U0001F947", "tier": "gold",
        "name_fr": "Club des 10 kg", "name_en": "10kg Club",
        "desc_fr": "10 kg de progres vers l'objectif.",
        "desc_en": "10kg of progress toward the goal.",
    },
    "sessions_50": {
        "icon": "\U0001F3CB", "tier": "bronze",
        "name_fr": "Demi-centurion", "name_en": "Half Century",
        "desc_fr": "50 seances enregistrees.",
        "desc_en": "50 logged exercise sessions.",
    },
    "sessions_100": {
        "icon": "\U0001F3CB", "tier": "gold",
        "name_fr": "Club des 100", "name_en": "Century Club",
        "desc_fr": "100 seances enregistrees.",
        "desc_en": "100 logged exercise sessions.",
    },
    "sleep_week": {
        "icon": "\U0001F634", "tier": "silver",
        "name_fr": "Sommeil de champion", "name_en": "Sleep Champion",
        "desc_fr": "Score de sommeil au-dessus de l'objectif 7 jours d'affilee.",
        "desc_en": "Sleep score above target 7 days running.",
    },
    "sleep_month": {
        "icon": "\U0001F31B", "tier": "gold",
        "name_fr": "Bien repose", "name_en": "Well Rested",
        "desc_fr": "Moyenne de 7h de sommeil ou plus sur 30 jours.",
        "desc_en": "7h+ average sleep over 30 days.",
    },
    "rhr_improved": {
        "icon": "\U00002764", "tier": "gold",
        "name_fr": "Coeur d'acier", "name_en": "Heart of Steel",
        "desc_fr": "FC repos amelioree de 3+ bpm sur ~3 mois.",
        "desc_en": "Resting HR improved 3+ bpm over ~3 months.",
    },
    "hrv_week": {
        "icon": "\U0001F9D8", "tier": "silver",
        "name_fr": "VFC equilibree", "name_en": "Balanced HRV",
        "desc_fr": "Statut VFC Garmin equilibre 7 jours d'affilee.",
        "desc_en": "Balanced Garmin HRV status 7 days running.",
    },
    "readiness_week": {
        "icon": "\U0001F50B", "tier": "silver",
        "name_fr": "Toujours pret", "name_en": "Always Ready",
        "desc_fr": "Score de preparation Garmin eleve 7 jours d'affilee.",
        "desc_en": "High Garmin training readiness 7 days running.",
    },
    "steps_100k": {
        "icon": "\U0001F463", "tier": "bronze",
        "name_fr": "100 000 pas", "name_en": "100K Steps",
        "desc_fr": "100 000 pas cumules.",
        "desc_en": "100,000 lifetime steps.",
    },
    "steps_1m": {
        "icon": "\U0001F463", "tier": "silver",
        "name_fr": "Million de pas", "name_en": "Million Steps",
        "desc_fr": "1 000 000 de pas cumules.",
        "desc_en": "1,000,000 lifetime steps.",
    },
    "steps_5m": {
        "icon": "\U0001F463", "tier": "gold",
        "name_fr": "5 millions de pas", "name_en": "5 Million Steps",
        "desc_fr": "5 000 000 de pas cumules.",
        "desc_en": "5,000,000 lifetime steps.",
    },
    "distance_100km": {
        "icon": "\U0001F3C3", "tier": "bronze",
        "name_fr": "100 km parcourus", "name_en": "100km Covered",
        "desc_fr": "100 km cumules.",
        "desc_en": "100km lifetime distance.",
    },
    "distance_500km": {
        "icon": "\U0001F3C3", "tier": "silver",
        "name_fr": "500 km parcourus", "name_en": "500km Covered",
        "desc_fr": "500 km cumules.",
        "desc_en": "500km lifetime distance.",
    },
    "distance_1000km": {
        "icon": "\U0001F3C3", "tier": "gold",
        "name_fr": "1000 km parcourus", "name_en": "1000km Covered",
        "desc_fr": "1000 km cumules.",
        "desc_en": "1000km lifetime distance.",
    },
    "elevation_everest": {
        "icon": "\U0001F3D4", "tier": "gold",
        "name_fr": "Everest", "name_en": "Everest",
        "desc_fr": "8848 m de denivele cumule -- la hauteur de l'Everest.",
        "desc_en": "8,848m cumulative elevation -- Everest's height.",
    },
    "elevation_everest_x2": {
        "icon": "\U0001F3D4", "tier": "platinum",
        "name_fr": "Double Everest", "name_en": "Double Everest",
        "desc_fr": "17 696 m de denivele cumule.",
        "desc_en": "17,696m cumulative elevation -- twice Everest.",
    },
    "floors_1000": {
        "icon": "\U0001F3E2", "tier": "silver",
        "name_fr": "Gratte-ciel", "name_en": "Skyscraper",
        "desc_fr": "1000 etages cumules.",
        "desc_en": "1,000 lifetime floors climbed.",
    },
    "training_hours_10": {
        "icon": "\U000023F1", "tier": "bronze",
        "name_fr": "10 heures", "name_en": "10 Hours In",
        "desc_fr": "10 heures d'entrainement cumulees.",
        "desc_en": "10 cumulative training hours.",
    },
    "training_hours_50": {
        "icon": "\U000023F1", "tier": "silver",
        "name_fr": "50 heures", "name_en": "50 Hours In",
        "desc_fr": "50 heures d'entrainement cumulees.",
        "desc_en": "50 cumulative training hours.",
    },
    "training_hours_100": {
        "icon": "\U0001F9BE", "tier": "gold",
        "name_fr": "Athlete de fer", "name_en": "Iron Athlete",
        "desc_fr": "100 heures d'entrainement cumulees.",
        "desc_en": "100 cumulative training hours.",
    },
    "calories_burned_100k": {
        "icon": "\U0001F525", "tier": "gold",
        "name_fr": "Fournaise", "name_en": "Furnace",
        "desc_fr": "100 000 kcal brulees cumulees.",
        "desc_en": "100,000 lifetime kcal burned.",
    },
    "body_fat_down_2pt": {
        "icon": "\U0001F4C9", "tier": "silver",
        "name_fr": "Perte de gras", "name_en": "Fat Loss",
        "desc_fr": "Masse grasse en baisse de 2+ points.",
        "desc_en": "Body fat % down 2+ points.",
    },
    "body_fat_down_5pt": {
        "icon": "\U0001F4C9", "tier": "gold",
        "name_fr": "Transformation", "name_en": "Transformation",
        "desc_fr": "Masse grasse en baisse de 5+ points.",
        "desc_en": "Body fat % down 5+ points.",
    },
    "lean_mass_up": {
        "icon": "\U0001F4AA", "tier": "silver",
        "name_fr": "Prise de muscle", "name_en": "Muscle Gain",
        "desc_fr": "Masse maigre en hausse depuis le debut du suivi.",
        "desc_en": "Lean mass up since tracking began.",
    },
    "true_recomp": {
        "icon": "\U00002728", "tier": "platinum",
        "name_fr": "Vraie recomposition", "name_en": "True Recomp",
        "desc_fr": "Masse grasse en baisse ET masse maigre en hausse en meme temps.",
        "desc_en": "Body fat down AND lean mass up at the same time.",
    },
    "weigh_in_30": {
        "icon": "\U00002696", "tier": "bronze",
        "name_fr": "Pesee reguliere", "name_en": "Regular Weigher",
        "desc_fr": "Poids logue 30 jours differents.",
        "desc_en": "Weight logged on 30 different days.",
    },
    "nutrition_30days": {
        "icon": "\U0001F4D3", "tier": "bronze",
        "name_fr": "Journal alimentaire", "name_en": "Diary Keeper",
        "desc_fr": "Nutrition loguee 30 jours differents.",
        "desc_en": "Nutrition logged on 30 different days.",
    },
    "tenure_1month": {
        "icon": "\U0001F4C5", "tier": "bronze",
        "name_fr": "1 mois", "name_en": "1 Month In",
        "desc_fr": "1 mois depuis la premiere synchro.",
        "desc_en": "1 month since the first sync.",
    },
    "tenure_3months": {
        "icon": "\U0001F4C5", "tier": "silver",
        "name_fr": "3 mois", "name_en": "3 Months In",
        "desc_fr": "3 mois depuis la premiere synchro.",
        "desc_en": "3 months since the first sync.",
    },
    "tenure_1year": {
        "icon": "\U0001F382", "tier": "platinum",
        "name_fr": "1 an", "name_en": "1 Year In",
        "desc_fr": "1 an depuis la premiere synchro.",
        "desc_en": "1 year since the first sync.",
    },
    "perfect_week": {
        "icon": "\U0001F3C5", "tier": "gold",
        "name_fr": "Semaine parfaite", "name_en": "Flawless Week",
        "desc_fr": "Une semaine (6 seances) 100% verte.",
        "desc_en": "A full week (6 sessions), all green.",
    },
    "no_red_week": {
        "icon": "\U0001F6E1", "tier": "bronze",
        "name_fr": "Semaine sans rouge", "name_en": "Clean Week",
        "desc_fr": "Une semaine complete sans aucun jour rouge.",
        "desc_en": "A full week with zero red days.",
    },
    "serial_comeback": {
        "icon": "\U0001F994", "tier": "silver",
        "name_fr": "Increvable", "name_en": "Never Down Long",
        "desc_fr": "Rebondi apres un rouge au moins 3 fois.",
        "desc_en": "Bounced back from red at least 3 separate times.",
    },
    "treadmill_master": {
        "icon": "\U0001F6B6", "tier": "silver",
        "name_fr": "Roi du tapis", "name_en": "Treadmill King",
        "desc_fr": "Niveau 8 atteint au tapis.",
        "desc_en": "Reached level 8 on treadmill.",
    },
    "lower_body_master": {
        "icon": "\U0001F9B5", "tier": "silver",
        "name_fr": "Jambes d'acier", "name_en": "Leg Day Legend",
        "desc_fr": "Niveau 8 atteint en muscu bas du corps.",
        "desc_en": "Reached level 8 on lower body.",
    },
    "upper_body_master": {
        "icon": "\U0001F4AA", "tier": "silver",
        "name_fr": "Buste en beton", "name_en": "Upper Body Beast",
        "desc_fr": "Niveau 8 atteint en muscu haut du corps.",
        "desc_en": "Reached level 8 on upper body.",
    },
    "calisthenics_master": {
        "icon": "\U0001F938", "tier": "silver",
        "name_fr": "Maitre calisthenique", "name_en": "Calisthenics Master",
        "desc_fr": "Niveau 8 atteint en calisthenie.",
        "desc_en": "Reached level 8 on calisthenics.",
    },
}

WEEK_CHECK_DAYS = 7


def _dedup_coach_log(
    conn: sqlite3.Connection, user_id: int,
) -> list[sqlite3.Row]:
    """One coach_log row per local_date (latest id wins), date
    ascending. Sunday's bike ride (no session_type, no status --
    outside the leveling system) is excluded so it can't break a
    streak it was never part of.
    """
    return conn.execute(
        "SELECT status, local_date FROM coach_log WHERE user_id = ? AND "
        "status IS NOT NULL AND id IN (SELECT MAX(id) FROM coach_log "
        "WHERE user_id = ? GROUP BY local_date) ORDER BY local_date ASC",
        (user_id, user_id),
    ).fetchall()


def _current_green_streak(conn: sqlite3.Connection, user_id: int) -> int:
    """Consecutive green days ending on the most recent coach_log entry."""
    rows = list(reversed(_dedup_coach_log(conn, user_id)))
    streak = 0
    for row in rows:
        if row["status"] != "green":
            break
        streak += 1
    return streak


def _check_first_sync(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return conn.execute(
        "SELECT 1 FROM steps WHERE user_id = ? LIMIT 1", (user_id,),
    ).fetchone() is not None


def _check_first_nutrition_log(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return conn.execute(
        "SELECT 1 FROM nutrition WHERE user_id = ? LIMIT 1", (user_id,),
    ).fetchone() is not None


def _check_streak_3(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _current_green_streak(conn, user_id) >= 3


def _check_streak_7(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _current_green_streak(conn, user_id) >= 7


def _check_streak_30(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _current_green_streak(conn, user_id) >= 30


def _check_comeback(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    rows = _dedup_coach_log(conn, user_id)
    return any(
        rows[i - 1]["status"] == "red" and rows[i]["status"] == "green"
        for i in range(1, len(rows))
    )


def _check_deload_survived(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return conn.execute(
        "SELECT 1 FROM deload_events WHERE user_id = ? AND ends_at <= ? "
        "LIMIT 1", (user_id, date),
    ).fetchone() is not None


def _check_level_5_any(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return any(
        training.get_level(conn, user_id, st) >= 5
        for st in training.SESSION_LABEL_FR
    )


def _check_level_10_any(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return any(
        training.get_level(conn, user_id, st) >= 10
        for st in training.SESSION_LABEL_FR
    )


def _check_level_10_all(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return all(
        training.get_level(conn, user_id, st) >= 10
        for st in training.SESSION_LABEL_FR
    )


def _count_last_n_days_meet(
    conn: sqlite3.Connection, user_id: int, date: str, actual_table: str,
    actual_col: str, target_key: str, n: int = WEEK_CHECK_DAYS,
) -> int:
    """How many of the last ``n`` full days (ending yesterday) had
    logged data meeting or exceeding a macro_targets() value.
    """
    count = 0
    day = dt.date.fromisoformat(date) - dt.timedelta(days=1)
    for _ in range(n):
        iso = day.isoformat()
        targets = progress.macro_targets(conn, user_id, iso)
        target = targets.get(target_key)
        if target:
            actual = conn.execute(
                f"SELECT SUM({actual_col}) AS total FROM {actual_table} "
                "WHERE user_id = ? AND local_date = ?", (user_id, iso),
            ).fetchone()["total"]
            if actual is not None and actual >= target:
                count += 1
        day -= dt.timedelta(days=1)
    return count


def _check_protein_week(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return _count_last_n_days_meet(
        conn, user_id, date, "nutrition", "protein_g", "protein_target_g",
    ) == WEEK_CHECK_DAYS


def _check_hydration_week(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return _count_last_n_days_meet(
        conn, user_id, date, "hydration", "volume_ml",
        "hydration_target_ml",
    ) == WEEK_CHECK_DAYS


def _count_step_goal_days(
    conn: sqlite3.Connection, user_id: int, date: str,
    n: int = WEEK_CHECK_DAYS,
) -> int:
    step_goal = int(db.get_setting(conn, user_id, "step_goal") or 0)
    if not step_goal:
        return 0
    count = 0
    day = dt.date.fromisoformat(date) - dt.timedelta(days=1)
    for _ in range(n):
        total = conn.execute(
            "SELECT SUM(count) AS total FROM steps WHERE user_id = ? "
            "AND local_date = ?", (user_id, day.isoformat()),
        ).fetchone()["total"]
        if total is not None and total >= step_goal:
            count += 1
        day -= dt.timedelta(days=1)
    return count


def _check_step_goal_week(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return _count_step_goal_days(conn, user_id, date) == WEEK_CHECK_DAYS


def _count_sleep_good_days(
    conn: sqlite3.Connection, user_id: int, date: str,
    n: int = WEEK_CHECK_DAYS,
) -> int:
    count = 0
    day = dt.date.fromisoformat(date) - dt.timedelta(days=1)
    for _ in range(n):
        sleep = metrics.sleep_for_date(conn, user_id, day.isoformat())
        score_val = sleep.get("sleep_score")
        if score_val is not None and score_val >= training.SLEEP_SCORE_GOOD:
            count += 1
        day -= dt.timedelta(days=1)
    return count


def _check_sleep_week(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _count_sleep_good_days(conn, user_id, date) == WEEK_CHECK_DAYS


def _count_hrv_good_days(
    conn: sqlite3.Connection, user_id: int, date: str,
    n: int = WEEK_CHECK_DAYS,
) -> int:
    count = 0
    day = dt.date.fromisoformat(date) - dt.timedelta(days=1)
    for _ in range(n):
        wellness = metrics.garmin_wellness(conn, user_id, day.isoformat())
        if wellness.get("hrv_status") == "BALANCED":
            count += 1
        day -= dt.timedelta(days=1)
    return count


def _check_hrv_week(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _count_hrv_good_days(conn, user_id, date) == WEEK_CHECK_DAYS


def _count_readiness_good_days(
    conn: sqlite3.Connection, user_id: int, date: str,
    n: int = WEEK_CHECK_DAYS,
) -> int:
    count = 0
    day = dt.date.fromisoformat(date) - dt.timedelta(days=1)
    for _ in range(n):
        wellness = metrics.garmin_wellness(conn, user_id, day.isoformat())
        score = wellness.get("training_readiness_score")
        if score is not None and score >= training.TRAINING_READINESS_GOOD:
            count += 1
        day -= dt.timedelta(days=1)
    return count


def _check_readiness_week(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return _count_readiness_good_days(conn, user_id, date) == WEEK_CHECK_DAYS


def _avg_sleep_hours_30d(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> float | None:
    """30-day average sleep duration, or None if fewer than 15 nights
    of sleep data exist in the window (too sparse to judge).
    """
    start = (dt.date.fromisoformat(date) - dt.timedelta(days=30)).isoformat()
    end = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT local_date FROM sleep_sessions WHERE "
        "user_id = ? AND local_date BETWEEN ? AND ?", (user_id, start, end),
    ).fetchall()
    if len(rows) < 15:
        return None
    hours = [
        metrics.sleep_for_date(conn, user_id, row["local_date"]).get("sleep_hours")
        for row in rows
    ]
    hours = [h for h in hours if h is not None]
    return sum(hours) / len(hours) if hours else None


def _check_sleep_month(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    avg = _avg_sleep_hours_30d(conn, user_id, date)
    return avg is not None and avg >= 7


def _rhr_improvement_bpm(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> float | None:
    """bpm improvement: 30d-average RHR now vs ~90 days ago, positive
    = better (lower). None if either window has fewer than 5 readings.
    """
    end = dt.date.fromisoformat(date)
    recent_start = (end - dt.timedelta(days=30)).isoformat()
    recent_end = (end - dt.timedelta(days=1)).isoformat()
    old_start = (end - dt.timedelta(days=97)).isoformat()
    old_end = (end - dt.timedelta(days=67)).isoformat()

    def avg_bpm(lo: str, hi: str):
        row = conn.execute(
            "SELECT AVG(bpm) AS avg, COUNT(*) AS n FROM "
            "resting_heart_rate WHERE user_id = ? AND local_date "
            "BETWEEN ? AND ?", (user_id, lo, hi),
        ).fetchone()
        return row["avg"] if row["n"] >= 5 else None

    recent_avg = avg_bpm(recent_start, recent_end)
    old_avg = avg_bpm(old_start, old_end)
    if recent_avg is None or old_avg is None:
        return None
    return old_avg - recent_avg


def _check_rhr_improved(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    improvement = _rhr_improvement_bpm(conn, user_id, date)
    return improvement is not None and improvement >= 3


def _lifetime_sum(
    conn: sqlite3.Connection, user_id: int, table: str, column: str,
) -> float:
    return conn.execute(
        f"SELECT SUM({column}) AS total FROM {table} WHERE user_id = ?",
        (user_id,),
    ).fetchone()["total"] or 0


def _check_steps_100k(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _lifetime_sum(conn, user_id, "steps", "count") >= 100_000


def _check_steps_1m(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _lifetime_sum(conn, user_id, "steps", "count") >= 1_000_000


def _check_steps_5m(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _lifetime_sum(conn, user_id, "steps", "count") >= 5_000_000


def _check_distance_100km(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _lifetime_sum(conn, user_id, "distance", "meters") >= 100_000


def _check_distance_500km(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _lifetime_sum(conn, user_id, "distance", "meters") >= 500_000


def _check_distance_1000km(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _lifetime_sum(conn, user_id, "distance", "meters") >= 1_000_000


def _check_elevation_everest(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return _lifetime_sum(conn, user_id, "elevation_gained", "meters") >= 8_848


def _check_elevation_everest_x2(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return _lifetime_sum(conn, user_id, "elevation_gained", "meters") >= 17_696


def _check_floors_1000(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return _lifetime_sum(conn, user_id, "floors_climbed", "floors") >= 1_000


def _lifetime_training_hours(
    conn: sqlite3.Connection, user_id: int,
) -> float:
    rows = conn.execute(
        "SELECT start_utc, end_utc FROM exercise_sessions WHERE "
        "user_id = ?", (user_id,),
    ).fetchall()
    total_seconds = sum(
        max(
            0.0,
            (
                dt.datetime.fromisoformat(row["end_utc"])
                - dt.datetime.fromisoformat(row["start_utc"])
            ).total_seconds(),
        )
        for row in rows
    )
    return total_seconds / 3600


def _check_training_hours_10(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return _lifetime_training_hours(conn, user_id) >= 10


def _check_training_hours_50(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return _lifetime_training_hours(conn, user_id) >= 50


def _check_training_hours_100(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return _lifetime_training_hours(conn, user_id) >= 100


def _check_calories_burned_100k(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    total = _lifetime_sum(conn, user_id, "total_calories_burned", "kcal")
    if not total:
        total = _lifetime_sum(conn, user_id, "active_calories", "kcal")
    return total >= 100_000


def _body_fat_drop_pt(
    conn: sqlite3.Connection, user_id: int,
) -> float | None:
    rows = conn.execute(
        "SELECT percentage FROM body_fat WHERE user_id = ? ORDER BY "
        "local_date ASC", (user_id,),
    ).fetchall()
    if len(rows) < 2:
        return None
    return rows[0]["percentage"] - rows[-1]["percentage"]


def _lean_mass_gain_kg(
    conn: sqlite3.Connection, user_id: int,
) -> float | None:
    rows = conn.execute(
        "SELECT kg FROM lean_body_mass WHERE user_id = ? ORDER BY "
        "local_date ASC", (user_id,),
    ).fetchall()
    if len(rows) < 2:
        return None
    return rows[-1]["kg"] - rows[0]["kg"]


def _check_body_fat_down_2pt(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    drop = _body_fat_drop_pt(conn, user_id)
    return drop is not None and drop >= 2


def _check_body_fat_down_5pt(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    drop = _body_fat_drop_pt(conn, user_id)
    return drop is not None and drop >= 5


def _check_lean_mass_up(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    gain = _lean_mass_gain_kg(conn, user_id)
    return gain is not None and gain > 0


def _check_true_recomp(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    drop = _body_fat_drop_pt(conn, user_id)
    gain = _lean_mass_gain_kg(conn, user_id)
    return drop is not None and drop >= 1 and gain is not None and gain > 0


def _check_weigh_in_30(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return conn.execute(
        "SELECT COUNT(DISTINCT local_date) AS n FROM weight WHERE "
        "user_id = ?", (user_id,),
    ).fetchone()["n"] >= 30


def _check_nutrition_30days(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return conn.execute(
        "SELECT COUNT(DISTINCT local_date) AS n FROM nutrition WHERE "
        "user_id = ?", (user_id,),
    ).fetchone()["n"] >= 30


def _first_sync_date(conn: sqlite3.Connection, user_id: int) -> str | None:
    row = conn.execute(
        "SELECT MIN(local_date) AS d FROM steps WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return row["d"]


def _check_tenure_1month(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    first = _first_sync_date(conn, user_id)
    if not first:
        return False
    return (dt.date.fromisoformat(date) - dt.date.fromisoformat(first)).days >= 30


def _check_tenure_3months(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    first = _first_sync_date(conn, user_id)
    if not first:
        return False
    return (dt.date.fromisoformat(date) - dt.date.fromisoformat(first)).days >= 90


def _check_tenure_1year(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    first = _first_sync_date(conn, user_id)
    if not first:
        return False
    return (dt.date.fromisoformat(date) - dt.date.fromisoformat(first)).days >= 365


def _full_weeks(
    conn: sqlite3.Connection, user_id: int,
) -> list[list[sqlite3.Row]]:
    """Group dedup'd coach_log rows into ISO (year, week) buckets,
    keeping only weeks with all 6 training days present.
    """
    by_week: dict[tuple, list[sqlite3.Row]] = {}
    for row in _dedup_coach_log(conn, user_id):
        iso_year, iso_week, _ = dt.date.fromisoformat(
            row["local_date"]
        ).isocalendar()
        by_week.setdefault((iso_year, iso_week), []).append(row)
    return [rows for rows in by_week.values() if len(rows) == 6]


def _check_perfect_week(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return any(
        all(row["status"] == "green" for row in week)
        for week in _full_weeks(conn, user_id)
    )


def _check_no_red_week(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return any(
        all(row["status"] != "red" for row in week)
        for week in _full_weeks(conn, user_id)
    )


def _check_serial_comeback(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    rows = _dedup_coach_log(conn, user_id)
    comebacks = sum(
        1 for i in range(1, len(rows))
        if rows[i - 1]["status"] == "red" and rows[i]["status"] == "green"
    )
    return comebacks >= 3


def _check_treadmill_master(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return training.get_level(conn, user_id, "treadmill") >= 8


def _check_lower_body_master(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return training.get_level(conn, user_id, "lower_body") >= 8


def _check_upper_body_master(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return training.get_level(conn, user_id, "upper_body") >= 8


def _check_calisthenics_master(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    return training.get_level(conn, user_id, "calisthenics") >= 8


def _weight_progress_kg(
    conn: sqlite3.Connection, user_id: int,
) -> float | None:
    """Net all-time weight change toward the goal direction (kg,
    positive = progress, regardless of cut/bulk sign convention).
    """
    target = float(
        db.get_setting(conn, user_id, "weekly_weight_change_kg") or 0
    )
    if target == 0:
        return None
    rows = conn.execute(
        "SELECT kg FROM weight WHERE user_id = ? ORDER BY local_date ASC",
        (user_id,),
    ).fetchall()
    if len(rows) < 2:
        return None
    first, last = rows[0]["kg"], rows[-1]["kg"]
    return (first - last) if target < 0 else (last - first)


def _check_weight_1kg(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    weight_progress = _weight_progress_kg(conn, user_id)
    return weight_progress is not None and weight_progress >= 1


def _check_weight_5kg(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    weight_progress = _weight_progress_kg(conn, user_id)
    return weight_progress is not None and weight_progress >= 5


def _check_weight_10kg(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    weight_progress = _weight_progress_kg(conn, user_id)
    return weight_progress is not None and weight_progress >= 10


def _check_sessions_50(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM exercise_sessions WHERE user_id = ?",
        (user_id,),
    ).fetchone()["n"] >= 50


def _check_sessions_100(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM exercise_sessions WHERE user_id = ?",
        (user_id,),
    ).fetchone()["n"] >= 100


CHECKS = {
    "first_sync": _check_first_sync,
    "first_nutrition_log": _check_first_nutrition_log,
    "streak_3": _check_streak_3,
    "streak_7": _check_streak_7,
    "streak_30": _check_streak_30,
    "comeback": _check_comeback,
    "deload_survived": _check_deload_survived,
    "level_5_any": _check_level_5_any,
    "level_10_any": _check_level_10_any,
    "level_10_all": _check_level_10_all,
    "protein_week": _check_protein_week,
    "hydration_week": _check_hydration_week,
    "step_goal_week": _check_step_goal_week,
    "weight_1kg": _check_weight_1kg,
    "weight_5kg": _check_weight_5kg,
    "weight_10kg": _check_weight_10kg,
    "sessions_50": _check_sessions_50,
    "sessions_100": _check_sessions_100,
    "sleep_week": _check_sleep_week,
    "sleep_month": _check_sleep_month,
    "rhr_improved": _check_rhr_improved,
    "hrv_week": _check_hrv_week,
    "readiness_week": _check_readiness_week,
    "steps_100k": _check_steps_100k,
    "steps_1m": _check_steps_1m,
    "steps_5m": _check_steps_5m,
    "distance_100km": _check_distance_100km,
    "distance_500km": _check_distance_500km,
    "distance_1000km": _check_distance_1000km,
    "elevation_everest": _check_elevation_everest,
    "elevation_everest_x2": _check_elevation_everest_x2,
    "floors_1000": _check_floors_1000,
    "training_hours_10": _check_training_hours_10,
    "training_hours_50": _check_training_hours_50,
    "training_hours_100": _check_training_hours_100,
    "calories_burned_100k": _check_calories_burned_100k,
    "body_fat_down_2pt": _check_body_fat_down_2pt,
    "body_fat_down_5pt": _check_body_fat_down_5pt,
    "lean_mass_up": _check_lean_mass_up,
    "true_recomp": _check_true_recomp,
    "weigh_in_30": _check_weigh_in_30,
    "nutrition_30days": _check_nutrition_30days,
    "tenure_1month": _check_tenure_1month,
    "tenure_3months": _check_tenure_3months,
    "tenure_1year": _check_tenure_1year,
    "perfect_week": _check_perfect_week,
    "no_red_week": _check_no_red_week,
    "serial_comeback": _check_serial_comeback,
    "treadmill_master": _check_treadmill_master,
    "lower_body_master": _check_lower_body_master,
    "upper_body_master": _check_upper_body_master,
    "calisthenics_master": _check_calisthenics_master,
}

assert set(CHECKS) == set(ACHIEVEMENTS), "every achievement needs a check"

_LIFETIME_TARGETS = {
    "steps_100k": 100_000, "steps_1m": 1_000_000, "steps_5m": 5_000_000,
    "distance_100km": 100_000, "distance_500km": 500_000,
    "distance_1000km": 1_000_000,
    "sessions_50": 50, "sessions_100": 100,
    "training_hours_10": 10, "training_hours_50": 50,
    "training_hours_100": 100,
    "weight_1kg": 1, "weight_5kg": 5, "weight_10kg": 10,
    "body_fat_down_2pt": 2, "body_fat_down_5pt": 5,
    "weigh_in_30": 30, "nutrition_30days": 30,
    "tenure_1month": 30, "tenure_3months": 90, "tenure_1year": 365,
}


def achievement_progress(
    key: str, conn: sqlite3.Connection, user_id: int, date: str,
) -> tuple[float, float, str] | None:
    """Current/target/unit for a locked achievement's progress bar --
    this is the "verifiable" half of gamification: every locked card
    shows the same real numbers the check itself uses, not just a
    gray box. Returns None for binary/event achievements where a bar
    wouldn't mean anything (first_sync, comeback, true_recomp,
    perfect_week, no_red_week, deload_survived).

    Returns:
        tuple[float, float, str] | None: (current, target, unit).
    """
    if key in ("streak_3", "streak_7", "streak_30"):
        target = {"streak_3": 3, "streak_7": 7, "streak_30": 30}[key]
        return (_current_green_streak(conn, user_id), target, "jours verts")

    if key in ("level_5_any", "level_10_any"):
        target = 5 if key == "level_5_any" else 10
        current = max(
            (training.get_level(conn, user_id, st) for st in training.SESSION_LABEL_FR),
            default=0,
        )
        return (current, target, "niveau max")
    if key == "level_10_all":
        current = min(
            (training.get_level(conn, user_id, st) for st in training.SESSION_LABEL_FR),
            default=0,
        )
        return (current, 10, "niveau (le plus bas)")
    if key.endswith("_master"):
        session_type = key[: -len("_master")]
        return (training.get_level(conn, user_id, session_type), 8, "niveau")

    if key == "protein_week":
        return (
            _count_last_n_days_meet(
                conn, user_id, date, "nutrition", "protein_g",
                "protein_target_g",
            ), WEEK_CHECK_DAYS, "jours",
        )
    if key == "hydration_week":
        return (
            _count_last_n_days_meet(
                conn, user_id, date, "hydration", "volume_ml",
                "hydration_target_ml",
            ), WEEK_CHECK_DAYS, "jours",
        )
    if key == "step_goal_week":
        return (
            _count_step_goal_days(conn, user_id, date), WEEK_CHECK_DAYS,
            "jours",
        )
    if key == "sleep_week":
        return (
            _count_sleep_good_days(conn, user_id, date), WEEK_CHECK_DAYS,
            "jours",
        )
    if key == "hrv_week":
        return (
            _count_hrv_good_days(conn, user_id, date), WEEK_CHECK_DAYS,
            "jours",
        )
    if key == "readiness_week":
        return (
            _count_readiness_good_days(conn, user_id, date),
            WEEK_CHECK_DAYS, "jours",
        )
    if key == "sleep_month":
        avg = _avg_sleep_hours_30d(conn, user_id, date)
        return (round(avg, 1), 7, "h de moyenne") if avg is not None else None
    if key == "rhr_improved":
        improvement = _rhr_improvement_bpm(conn, user_id, date)
        if improvement is None:
            return None
        return (round(max(improvement, 0), 1), 3, "bpm ameliores")

    if key in _LIFETIME_TARGETS and key.startswith(("weight_",)):
        weight_progress = _weight_progress_kg(conn, user_id) or 0
        return (round(max(weight_progress, 0), 1), _LIFETIME_TARGETS[key], "kg")
    if key in _LIFETIME_TARGETS and key.startswith("sessions_"):
        current = conn.execute(
            "SELECT COUNT(*) AS n FROM exercise_sessions WHERE "
            "user_id = ?", (user_id,),
        ).fetchone()["n"]
        return (current, _LIFETIME_TARGETS[key], "seances")
    if key in _LIFETIME_TARGETS and key.startswith("steps_"):
        return (
            round(_lifetime_sum(conn, user_id, "steps", "count")),
            _LIFETIME_TARGETS[key], "pas",
        )
    if key in _LIFETIME_TARGETS and key.startswith("distance_"):
        return (
            round(_lifetime_sum(conn, user_id, "distance", "meters") / 1000),
            round(_LIFETIME_TARGETS[key] / 1000), "km",
        )
    if key in _LIFETIME_TARGETS and key.startswith("training_hours_"):
        return (
            round(_lifetime_training_hours(conn, user_id), 1),
            _LIFETIME_TARGETS[key], "heures",
        )
    if key in _LIFETIME_TARGETS and key.startswith("body_fat_down_"):
        drop = _body_fat_drop_pt(conn, user_id) or 0
        return (round(max(drop, 0), 1), _LIFETIME_TARGETS[key], "points")
    if key in ("weigh_in_30", "nutrition_30days"):
        table = "weight" if key == "weigh_in_30" else "nutrition"
        current = conn.execute(
            f"SELECT COUNT(DISTINCT local_date) AS n FROM {table} WHERE "
            "user_id = ?", (user_id,),
        ).fetchone()["n"]
        return (current, 30, "jours")
    if key in _LIFETIME_TARGETS and key.startswith("tenure_"):
        first = _first_sync_date(conn, user_id)
        if not first:
            return None
        current = (
            dt.date.fromisoformat(date) - dt.date.fromisoformat(first)
        ).days
        return (max(current, 0), _LIFETIME_TARGETS[key], "jours")

    if key in ("elevation_everest", "elevation_everest_x2"):
        target = 8_848 if key == "elevation_everest" else 17_696
        return (
            round(_lifetime_sum(conn, user_id, "elevation_gained", "meters")),
            target, "m",
        )
    if key == "floors_1000":
        return (
            round(_lifetime_sum(conn, user_id, "floors_climbed", "floors")),
            1000, "etages",
        )
    if key == "calories_burned_100k":
        total = _lifetime_sum(conn, user_id, "total_calories_burned", "kcal")
        if not total:
            total = _lifetime_sum(conn, user_id, "active_calories", "kcal")
        return (round(total), 100_000, "kcal")
    if key == "lean_mass_up":
        gain = _lean_mass_gain_kg(conn, user_id) or 0
        return (round(max(gain, 0), 2), 0.1, "kg (tout gain compte)")
    if key == "serial_comeback":
        rows = _dedup_coach_log(conn, user_id)
        comebacks = sum(
            1 for i in range(1, len(rows))
            if rows[i - 1]["status"] == "red" and rows[i]["status"] == "green"
        )
        return (comebacks, 3, "rebonds")

    return None


def unlocked_keys(conn: sqlite3.Connection, user_id: int) -> set[str]:
    """Keys of already-unlocked achievements."""
    return {
        row["key"]
        for row in conn.execute(
            "SELECT key FROM achievements WHERE user_id = ?", (user_id,),
        )
    }


def _grant_xp(
    conn: sqlite3.Connection, user_id: int, date: str, source: str,
    amount: int, detail: str,
) -> None:
    """Append one row to the XP ledger (does not commit)."""
    conn.execute(
        "INSERT INTO xp_ledger (user_id, date, source, amount, detail, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            user_id, date, source, amount, detail,
            dt.datetime.now(dt.timezone.utc).isoformat(),
        ),
    )


# Small, continuous XP drip so the player level moves between
# achievement unlocks too, not just in big jumps -- mirrors the daily
# readiness status directly, same numbers already shown elsewhere.
XP_PER_STATUS = {"green": 10, "yellow": 3, "red": 0}


def grant_daily_status_xp(
    conn: sqlite3.Connection, user_id: int, date: str, status: str | None,
) -> None:
    """Grant today's status-based XP, once per date (idempotent --
    safe to call even if run_coach.py's pipeline is re-run).

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        date (str): ISO local date.
        status (str | None): Today's ``compute_status`` result, or
            ``None`` on a non-training day (no XP granted).
    """
    if status not in XP_PER_STATUS:
        return
    already = conn.execute(
        "SELECT 1 FROM xp_ledger WHERE user_id = ? AND date = ? AND "
        "source = 'daily_status'", (user_id, date),
    ).fetchone()
    if already:
        return
    amount = XP_PER_STATUS[status]
    if amount <= 0:
        return  # a red day earns nothing, but still no duplicate rows
    _grant_xp(
        conn, user_id, date, "daily_status", amount,
        f"Statut du jour : {status}",
    )
    conn.commit()


def check_and_unlock(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> list[str]:
    """Run every not-yet-unlocked check, persisting and returning
    newly-unlocked keys (empty list if none). Each unlock also grants
    its point value as XP, ledgered under "achievement:<key>".

    Returns:
        list[str]: Newly unlocked achievement keys, insertion order.
    """
    already = unlocked_keys(conn, user_id)
    newly_unlocked = []
    for key, check_fn in CHECKS.items():
        if key in already:
            continue
        if check_fn(conn, user_id, date):
            conn.execute(
                "INSERT INTO achievements (user_id, key, unlocked_at) "
                "VALUES (?, ?, ?)", (user_id, key, date),
            )
            definition = ACHIEVEMENTS[key]
            _grant_xp(
                conn, user_id, date, f"achievement:{key}",
                TIER_POINTS[definition["tier"]], definition["name_fr"],
            )
            newly_unlocked.append(key)
    if newly_unlocked:
        conn.commit()
    return newly_unlocked


LEVEL_XP_STEP = 100  # triangular growth: level L needs 100*(L-1) more XP than L-1 needed


def _xp_to_reach_level(level: int) -> int:
    """Cumulative XP needed to have reached ``level`` (level 1 = 0)."""
    return LEVEL_XP_STEP * level * (level - 1) // 2


def xp_total(conn: sqlite3.Connection, user_id: int) -> int:
    """All-time XP: sum of every ledger row (fully auditable)."""
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM xp_ledger WHERE "
        "user_id = ?", (user_id,),
    ).fetchone()["total"]


def player_level(conn: sqlite3.Connection, user_id: int) -> dict:
    """Player level derived from the XP ledger -- open-ended (unlike
    Coach Score, which is capped at ACHIEVEMENTS' total points), grows
    with daily activity too so there's always visible progress.

    Returns:
        dict: ``level``, ``xp`` (total), ``xp_into_level``,
        ``xp_for_next_level``, ``xp_to_next_level``.
    """
    total = xp_total(conn, user_id)
    level = 1
    while _xp_to_reach_level(level + 1) <= total:
        level += 1
    into_level = total - _xp_to_reach_level(level)
    for_next = _xp_to_reach_level(level + 1) - _xp_to_reach_level(level)
    return {
        "level": level, "xp": total, "xp_into_level": into_level,
        "xp_for_next_level": for_next,
        "xp_to_next_level": for_next - into_level,
    }


def xp_ledger_entries(
    conn: sqlite3.Connection, user_id: int, limit: int = 50,
) -> list[dict]:
    """Most recent XP grants, for the Achievements page's audit trail.

    Returns:
        list[dict]: Newest first.
    """
    rows = conn.execute(
        "SELECT date, source, amount, detail FROM xp_ledger WHERE "
        "user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _garmin_badge_count(conn: sqlite3.Connection, user_id: int) -> int:
    """Number of earned Garmin badges on record for this user."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM garmin_badges WHERE user_id = ?",
        (user_id,),
    ).fetchone()["n"]


def _garmin_badge_items(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Earned Garmin badges, shaped like an ACHIEVEMENTS entry each.

    Passthrough only: Garmin's own catalog and criteria, not
    verified or graded here -- see GARMIN_BADGE_TIER. Only earned
    badges are ingested (no locked/available-badge tracking), so
    every entry here is always unlocked.

    Returns:
        list[dict]: Same shape ``all_achievements_with_status``
        produces per entry, ready to merge into the same list.
    """
    rows = conn.execute(
        "SELECT badge_key, name, earned_date FROM garmin_badges WHERE "
        "user_id = ? ORDER BY earned_date DESC", (user_id,),
    ).fetchall()
    return [
        {
            "key": f"garmin:{row['badge_key']}",
            "icon": "\U0001F396", "tier": GARMIN_BADGE_TIER,
            "name_fr": row["name"], "name_en": row["name"],
            "desc_fr": "Badge officiel Garmin Connect.",
            "desc_en": "Official Garmin Connect badge.",
            "points": TIER_POINTS[GARMIN_BADGE_TIER],
            "unlocked": True, "unlocked_at": row["earned_date"],
            "progress": None,
        }
        for row in rows
    ]


def score(conn: sqlite3.Connection, user_id: int) -> dict:
    """Coach Score summary, Gamerscore-style.

    Earned Garmin badges count too (see ``_garmin_badge_items``) --
    always fully "unlocked" against their own count, since only
    earned badges are tracked, not Garmin's full catalog.

    Returns:
        dict: ``unlocked_points``, ``total_points``, ``unlocked_count``,
        ``total_count``.
    """
    unlocked = unlocked_keys(conn, user_id)
    unlocked_points = sum(
        TIER_POINTS[ACHIEVEMENTS[key]["tier"]] for key in unlocked
    )
    total_points = sum(
        TIER_POINTS[definition["tier"]] for definition in ACHIEVEMENTS.values()
    )
    garmin_count = _garmin_badge_count(conn, user_id)
    garmin_points = garmin_count * TIER_POINTS[GARMIN_BADGE_TIER]
    return {
        "unlocked_points": unlocked_points + garmin_points,
        "total_points": total_points + garmin_points,
        "unlocked_count": len(unlocked) + garmin_count,
        "total_count": len(ACHIEVEMENTS) + garmin_count,
    }


def all_achievements_with_status(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> list[dict]:
    """Every achievement (homegrown + earned Garmin badges) plus
    unlock status, for the Achievements page: unlocked first (most
    recent first), then locked ones ordered bronze->platinum. Locked
    entries carry a ``progress`` dict (current/target/unit/pct) when
    computable, so the page shows real numbers, not just a locked
    icon. Garmin badges (see ``_garmin_badge_items``) are always
    unlocked, so they only ever land in the first group.

    Returns:
        list[dict]: ``key``, definition fields, ``points``,
        ``unlocked`` (bool), ``unlocked_at`` (str | None),
        ``progress`` (dict | None).
    """
    unlocked_rows = {
        row["key"]: row["unlocked_at"]
        for row in conn.execute(
            "SELECT key, unlocked_at FROM achievements WHERE user_id = ?",
            (user_id,),
        )
    }
    tier_order = {"bronze": 0, "silver": 1, "gold": 2, "platinum": 3}
    items = []
    for key, definition in ACHIEVEMENTS.items():
        unlocked_at = unlocked_rows.get(key)
        is_unlocked = unlocked_at is not None
        progress_info = None
        if not is_unlocked:
            raw = achievement_progress(key, conn, user_id, date)
            if raw is not None:
                current, target, unit = raw
                progress_info = {
                    "current": current, "target": target, "unit": unit,
                    "pct": min(100, round(100 * current / target))
                    if target else 0,
                }
        items.append({
            "key": key, **definition, "points": TIER_POINTS[definition["tier"]],
            "unlocked": is_unlocked, "unlocked_at": unlocked_at,
            "progress": progress_info,
        })
    items.extend(_garmin_badge_items(conn, user_id))

    def sort_key(item: dict) -> tuple:
        if item["unlocked"]:
            ordinal = dt.date.fromisoformat(item["unlocked_at"]).toordinal()
            return (0, -ordinal, 0)
        return (1, 0, tier_order[item["tier"]])

    items.sort(key=sort_key)
    return items


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "smart_sport.db"
    conn = db.connect(tmp)
    db.init_db(conn)
    uid = db.create_user(conn, "test", "password1234")
    other_uid = db.create_user(conn, "other", "password1234")

    assert check_and_unlock(conn, uid, "2026-08-01") == []  # nothing yet

    conn.execute(
        "INSERT INTO steps VALUES ('s1', ?, '2026-08-01T08:00:00+00:00', "
        "'2026-08-01T08:10:00+00:00', '2026-08-01', 1000)", (uid,),
    )
    conn.commit()
    unlocked = check_and_unlock(conn, uid, "2026-08-01")
    assert unlocked == ["first_sync"], unlocked
    # Idempotent: re-running doesn't re-unlock or duplicate.
    assert check_and_unlock(conn, uid, "2026-08-02") == []
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM achievements WHERE user_id = ?", (uid,),
    ).fetchone()["n"] == 1
    # The other user has none of this -- no cross-user leakage.
    assert check_and_unlock(conn, other_uid, "2026-08-02") == []
    assert unlocked_keys(conn, other_uid) == set()

    training.set_level(conn, uid, "treadmill", 5)
    unlocked2 = check_and_unlock(conn, uid, "2026-08-02")
    assert "level_5_any" in unlocked2

    for st in training.SESSION_LABEL_FR:
        training.set_level(conn, uid, st, 10)
    unlocked3 = check_and_unlock(conn, uid, "2026-08-03")
    assert "level_10_any" in unlocked3
    assert "level_10_all" in unlocked3

    streak_dates = [
        (dt.date(2026, 7, 1) + dt.timedelta(days=i)).isoformat()
        for i in range(3)
    ]
    for i, iso in enumerate(streak_dates):
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, "
            "status, session_type, level, message) VALUES (?, ?, ?, "
            "'green', 'treadmill', 5, 'x')", (uid, f"{iso}T06:00:00+00:00", iso),
        )
    conn.commit()
    assert _current_green_streak(conn, uid) == 3
    unlocked4 = check_and_unlock(conn, uid, "2026-08-04")
    assert "streak_3" in unlocked4

    conn.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message) VALUES (?, '2026-07-04T06:00:00+00:00',"
        " '2026-07-04', 'red', 'treadmill', 5, 'x')", (uid,),
    )
    conn.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message) VALUES (?, '2026-07-05T06:00:00+00:00',"
        " '2026-07-05', 'green', 'treadmill', 5, 'x')", (uid,),
    )
    conn.commit()
    unlocked5 = check_and_unlock(conn, uid, "2026-08-05")
    assert "comeback" in unlocked5

    # 7 days of balanced HRV + high readiness -> both weekly Garmin
    # achievements unlock; a lone gap breaks the streak.
    for i in range(7):
        day = (dt.date(2026, 8, 6) + dt.timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO garmin_hrv VALUES (?, ?, 55.0, 58.0, 'BALANCED')",
            (uid, day),
        )
        conn.execute(
            "INSERT INTO garmin_training_readiness VALUES "
            "(?, ?, 80, 'HIGH', 'note')", (uid, day),
        )
    conn.commit()
    unlocked6 = check_and_unlock(conn, uid, "2026-08-13")
    assert "hrv_week" in unlocked6, unlocked6
    assert "readiness_week" in unlocked6, unlocked6
    assert _count_hrv_good_days(conn, other_uid, "2026-08-13") == 0

    summary = score(conn, uid)
    assert summary["unlocked_count"] >= 6
    assert summary["total_count"] == len(ACHIEVEMENTS)
    assert summary["unlocked_points"] <= summary["total_points"]
    assert score(conn, other_uid)["unlocked_count"] == 0

    items = all_achievements_with_status(conn, uid, "2026-08-06")
    assert len(items) == len(ACHIEVEMENTS)
    assert items[0]["unlocked"] is True  # unlocked ones sort first
    locked_keys = {i["key"] for i in items if not i["unlocked"]}
    assert "sessions_100" in locked_keys
    by_key = {i["key"]: i for i in items}
    # A locked, numeric-threshold achievement carries real progress...
    assert by_key["sessions_100"]["progress"]["target"] == 100
    assert by_key["sessions_100"]["progress"]["current"] >= 0
    # ...an event-based one (no meaningful single-number bar) doesn't.
    assert by_key["true_recomp"]["progress"] is None
    assert by_key["comeback"]["unlocked"] is True  # already unlocked above

    # --- Earned Garmin badges, surfaced as achievements ---
    conn.execute(
        "INSERT INTO garmin_badges VALUES (?, '42', '5K Runner', "
        "'2026-08-20')", (uid,),
    )
    conn.commit()
    garmin_summary = score(conn, uid)
    assert garmin_summary["unlocked_count"] == summary["unlocked_count"] + 1
    assert garmin_summary["total_count"] == summary["total_count"] + 1
    assert (
        garmin_summary["unlocked_points"]
        == summary["unlocked_points"] + TIER_POINTS[GARMIN_BADGE_TIER]
    )
    assert score(conn, other_uid)["unlocked_count"] == 0  # isolated

    items_with_badge = all_achievements_with_status(conn, uid, "2026-08-06")
    assert len(items_with_badge) == len(ACHIEVEMENTS) + 1
    badge_item = next(
        i for i in items_with_badge if i["key"] == "garmin:42"
    )
    assert badge_item["unlocked"] is True
    assert badge_item["unlocked_at"] == "2026-08-20"
    assert badge_item["name_fr"] == "5K Runner"
    assert badge_item["tier"] == GARMIN_BADGE_TIER
    assert items_with_badge[0]["key"] == "garmin:42"  # most recent first

    # --- Player Level / XP ledger ---
    xp_before = xp_total(conn, uid)
    grant_daily_status_xp(conn, uid, "2026-08-10", "green")
    assert xp_total(conn, uid) == xp_before + XP_PER_STATUS["green"]
    # Idempotent: same date doesn't grant twice.
    grant_daily_status_xp(conn, uid, "2026-08-10", "green")
    assert xp_total(conn, uid) == xp_before + XP_PER_STATUS["green"]
    # Red days grant nothing (no ledger row at all).
    grant_daily_status_xp(conn, uid, "2026-08-11", "red")
    assert xp_total(conn, uid) == xp_before + XP_PER_STATUS["green"]
    assert xp_total(conn, other_uid) == 0  # isolated
    # Achievement unlocks already granted XP as a side effect of every
    # check_and_unlock() call above -- confirm the ledger has entries
    # for them, matching the achievements table exactly.
    achievement_xp_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM xp_ledger WHERE user_id = ? AND "
        "source LIKE 'achievement:%'", (uid,),
    ).fetchone()["n"]
    assert achievement_xp_rows == len(unlocked_keys(conn, uid))

    level_info = player_level(conn, uid)
    assert level_info["xp"] == xp_total(conn, uid)
    assert level_info["level"] >= 1
    assert level_info["xp_into_level"] + level_info["xp_to_next_level"] == \
        level_info["xp_for_next_level"]
    # Level formula sanity: exactly at a threshold reads as that level.
    assert _xp_to_reach_level(1) == 0
    assert _xp_to_reach_level(2) == LEVEL_XP_STEP
    assert _xp_to_reach_level(3) == LEVEL_XP_STEP * 3

    entries = xp_ledger_entries(conn, uid, limit=5)
    assert len(entries) <= 5
    assert entries[0]["amount"] is not None  # newest-first, non-empty

    # --- Lifetime-volume milestones ---
    conn.execute(
        "INSERT INTO steps VALUES ('bigsteps', ?, "
        "'2026-01-01T08:00:00+00:00', '2026-01-01T09:00:00+00:00', "
        "'2026-01-01', 150000)", (uid,),
    )
    conn.execute(
        "INSERT INTO distance VALUES ('dx1', ?, '2026-01-01T08:00:00+00:00', "
        "'2026-01-01T09:00:00+00:00', '2026-01-01', 150000)", (uid,),
    )
    conn.execute(
        "INSERT INTO elevation_gained VALUES ('ex1', ?, "
        "'2026-01-01T08:00:00+00:00', '2026-01-01T09:00:00+00:00', "
        "'2026-01-01', 9000)", (uid,),
    )
    conn.execute(
        "INSERT INTO floors_climbed VALUES ('fx1', ?, "
        "'2026-01-01T08:00:00+00:00', '2026-01-01T09:00:00+00:00', "
        "'2026-01-01', 1200)", (uid,),
    )
    conn.execute(
        "INSERT INTO total_calories_burned VALUES ('cx1', ?, "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T23:59:00+00:00', "
        "'2026-01-01', 150000)", (uid,),
    )
    conn.execute(
        "INSERT INTO exercise_sessions VALUES ('long1', ?, "
        "'2026-01-01T06:00:00+00:00', '2026-01-01T18:00:00+00:00', "
        "'2026-01-01', 56, NULL, NULL, NULL, NULL)", (uid,),
    )
    conn.commit()
    assert _check_steps_100k(conn, uid, "2026-08-06") is True
    assert _check_distance_100km(conn, uid, "2026-08-06") is True
    assert _check_elevation_everest(conn, uid, "2026-08-06") is True
    assert _check_floors_1000(conn, uid, "2026-08-06") is True
    assert _check_calories_burned_100k(conn, uid, "2026-08-06") is True
    assert _lifetime_training_hours(conn, uid) >= 12
    # None of this leaked to the other user.
    assert _check_steps_100k(conn, other_uid, "2026-08-06") is False

    # --- Body recomposition ---
    conn.execute(
        "INSERT INTO body_fat VALUES ('bf1', ?, '2026-01-01T07:00:00+00:00', "
        "'2026-01-01', 25.0)", (uid,),
    )
    conn.execute(
        "INSERT INTO body_fat VALUES ('bf2', ?, '2026-06-01T07:00:00+00:00', "
        "'2026-06-01', 21.5)", (uid,),
    )
    conn.execute(
        "INSERT INTO lean_body_mass VALUES ('lm1', ?, "
        "'2026-01-01T07:00:00+00:00', '2026-01-01', 60.0)", (uid,),
    )
    conn.execute(
        "INSERT INTO lean_body_mass VALUES ('lm2', ?, "
        "'2026-06-01T07:00:00+00:00', '2026-06-01', 61.5)", (uid,),
    )
    conn.commit()
    assert _check_body_fat_down_2pt(conn, uid, "2026-08-06") is True
    assert _check_body_fat_down_5pt(conn, uid, "2026-08-06") is False
    assert _check_lean_mass_up(conn, uid, "2026-08-06") is True
    assert _check_true_recomp(conn, uid, "2026-08-06") is True

    # --- Tenure (earliest steps row is now 2026-01-01, from the
    # lifetime-volume seed above) ---
    assert _first_sync_date(conn, uid) == "2026-01-01"
    assert _check_tenure_1month(conn, uid, "2026-01-15") is False  # 14 days in
    assert _check_tenure_1month(conn, uid, "2026-02-05") is True  # 35 days in
    assert _check_tenure_3months(conn, uid, "2026-02-05") is False
    assert _check_tenure_1year(conn, uid, "2026-09-05") is False
    assert _check_tenure_1year(conn, uid, "2027-01-05") is True

    # --- Per-type level masters (all 4 types were set to level 10
    # earlier for the level_10_all test, so all masters clear too) ---
    assert _check_lower_body_master(conn, uid, "2026-08-06") is True
    assert _check_treadmill_master(conn, uid, "2026-08-06") is True
    training.set_level(conn, uid, "upper_body", 3)
    assert _check_upper_body_master(conn, uid, "2026-08-06") is False

    # --- Perfect / clean / serial-comeback weeks: build 2 full ISO
    # weeks (Mon-Sat) -- first all-green, second green/yellow (no red).
    week1_monday = dt.date(2026, 9, 7)  # a real Monday
    for offset, st in zip(
        range(6),
        ["treadmill", "lower_body", "treadmill", "upper_body",
         "treadmill", "calisthenics"],
    ):
        day = (week1_monday + dt.timedelta(days=offset)).isoformat()
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, "
            "status, session_type, level, message) VALUES (?, ?, ?, "
            "'green', ?, 5, 'x')", (uid, f"{day}T06:00:00+00:00", day, st),
        )
    week2_monday = week1_monday + dt.timedelta(days=7)
    for offset, st in zip(
        range(6),
        ["treadmill", "lower_body", "treadmill", "upper_body",
         "treadmill", "calisthenics"],
    ):
        day = (week2_monday + dt.timedelta(days=offset)).isoformat()
        status = "yellow" if offset == 2 else "green"
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, "
            "status, session_type, level, message) VALUES (?, ?, ?, ?, "
            "?, 5, 'x')", (uid, f"{day}T06:00:00+00:00", day, status, st),
        )
    conn.commit()
    assert _check_perfect_week(conn, uid, "2026-09-20") is True
    assert _check_no_red_week(conn, uid, "2026-09-20") is True  # both weeks qualify

    print("achievements.py: all checks passed")
