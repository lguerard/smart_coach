#!/usr/bin/env python3
"""Daily-adaptive training levels.

Ported from garmin-coach/training.py: same per-session-type level
(0-10), same green/yellow/red daily status, same level -> concrete
workout numbers. Changes from the original:

1. State lives in smart_coach's db (``levels`` table) instead of a
   flat JSON file.
2. Resting-HR baseline is computed directly from smart_coach's own
   ingested history (no more manual rolling-window bookkeeping -- the
   full history is already in the db), plus a NEW activity-load vote
   with no garmin-coach equivalent.
3. HRV and training-readiness are back as real votes (they were
   dropped in the Health-Connect-only era -- see metrics.py's
   ``garmin_wellness`` -- now pulled straight from the Garmin API via
   ingest/garmin_api.py). Body battery and stress have no vote: body
   battery is a running energy gauge, not a morning score, and a
   separate stress vote would double-count what training-readiness's
   own aggregate already factors in -- both are dashboard/LLM context
   only (metrics.daily_wellness).
"""

import datetime as dt
import json
import sqlite3
from typing import Optional

import db
import exercise_library
import muscles
# For the sleep window in calibration_report(). metrics imports db and
# training_load, never training, so this stays acyclic.
import metrics

LEVEL_MIN, LEVEL_MAX = 0, 10

SESSION_LABEL_FR = {
    "treadmill": "Tapis",
    "lower_body": "Muscu bas du corps",
    "upper_body": "Muscu haut du corps + gainage",
    "calisthenics": "Calisthenie",
}

STATUS_LABEL_FR = {
    "green": "vert - progression",
    "yellow": "jaune - maintien",
    "red": "rouge - seance allegee",
}

# Tunable thresholds -- reasonable starting points, same posture as
# garmin-coach's original constants: retune against real mornings.
SLEEP_SCORE_GOOD, SLEEP_SCORE_POOR = 75, 60
RHR_SPIKE_RED_MIN = 5  # bpm above rolling personal baseline
RHR_BASELINE_DAYS = 14
ACTIVITY_LOAD_SPIKE_RATIO = 1.5  # recent-7d vs previous-7d minutes
ACTIVITY_LOAD_HIGH_RPE = 7.0
TRAINING_READINESS_GOOD, TRAINING_READINESS_POOR = 75, 50  # 0-100 scale
# TSB (yesterday's CTL - ATL): very negative means fatigue has been
# accumulating for a while -- unlike a single red day, TSB is already
# a smoothed signal (42d/7d windows), so one critical reading is
# trustworthy enough to force a deload immediately, no streak needed.
TSB_DELOAD_THRESHOLD = -20.0


def get_level(
    conn: sqlite3.Connection, user_id: int, session_type: str,
) -> int:
    """Current level for a session type (0 if never set).

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        session_type (str): One of ``SESSION_LABEL_FR``'s
            values.

    Returns:
        int: Current level.
    """
    row = conn.execute(
        "SELECT level FROM levels WHERE user_id = ? AND session_type = ?",
        (user_id, session_type),
    ).fetchone()
    return row["level"] if row else 0


def set_level(
    conn: sqlite3.Connection, user_id: int, session_type: str, level: int,
) -> None:
    """Persist a session type's new level.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        session_type (str): Session type key.
        level (int): New level.
    """
    conn.execute(
        "INSERT INTO levels (user_id, session_type, level) VALUES "
        "(?, ?, ?) ON CONFLICT(user_id, session_type) DO UPDATE SET "
        "level = excluded.level",
        (user_id, session_type, level),
    )
    conn.commit()


def get_red_streak(
    conn: sqlite3.Connection, user_id: int, session_type: str,
) -> int:
    """Current consecutive-red count for a session type (0 if unset)."""
    row = conn.execute(
        "SELECT red_streak FROM levels WHERE user_id = ? AND "
        "session_type = ?", (user_id, session_type),
    ).fetchone()
    return row["red_streak"] if row else 0


def set_red_streak(
    conn: sqlite3.Connection, user_id: int, session_type: str, streak: int,
) -> None:
    """Persist a session type's consecutive-red count."""
    conn.execute(
        "INSERT INTO levels (user_id, session_type, level, red_streak) "
        "VALUES (?, ?, 0, ?) ON CONFLICT(user_id, session_type) DO "
        "UPDATE SET red_streak = excluded.red_streak",
        (user_id, session_type, streak),
    )
    conn.commit()


def get_deload_until(
    conn: sqlite3.Connection, user_id: int, session_type: str,
) -> Optional[str]:
    """ISO date a session type's active deload window ends, if any."""
    row = conn.execute(
        "SELECT deload_until FROM levels WHERE user_id = ? AND "
        "session_type = ?", (user_id, session_type),
    ).fetchone()
    return row["deload_until"] if row else None


def set_deload_until(
    conn: sqlite3.Connection, user_id: int, session_type: str,
    date: Optional[str],
) -> None:
    """Persist (or clear, with ``None``) a session type's deload window."""
    conn.execute(
        "INSERT INTO levels (user_id, session_type, level, deload_until) "
        "VALUES (?, ?, 0, ?) ON CONFLICT(user_id, session_type) DO "
        "UPDATE SET deload_until = excluded.deload_until",
        (user_id, session_type, date),
    )
    conn.commit()


# Deload guardrail: the daily +-1 adjustment has no memory beyond
# yesterday, so a bad stretch can grind on indefinitely one small step
# at a time. 3 reds in a row instead forces a bigger cut and a
# no-increase window, resetting the streak.
RED_STREAK_THRESHOLD = 3
DELOAD_DAYS = 7
DELOAD_LEVEL_CUT = 2


def _trigger_deload(
    conn: sqlite3.Connection, user_id: int, session_type: str, level: int,
    date: str, trigger: str,
) -> dict:
    """Force the level cut + deload window, shared by both triggers.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        session_type (str): Session type key.
        level (int): Current level, before the cut.
        date (str): ISO local date (today) -- deload window start.
        trigger (str): ``"red_streak"`` or ``"tsb"``, logged to
            ``deload_events`` for the Progress page's history.

    Returns:
        dict: ``level``, ``in_deload=True``, ``deload_triggered=True``,
        ``trigger``.
    """
    new_level = max(level - DELOAD_LEVEL_CUT, LEVEL_MIN)
    ends_at = (
        dt.date.fromisoformat(date) + dt.timedelta(days=DELOAD_DAYS)
    ).isoformat()
    set_level(conn, user_id, session_type, new_level)
    set_red_streak(conn, user_id, session_type, 0)
    set_deload_until(conn, user_id, session_type, ends_at)
    conn.execute(
        "INSERT INTO deload_events (user_id, session_type, triggered_at, "
        "ends_at, trigger) VALUES (?, ?, ?, ?, ?)",
        (user_id, session_type, date, ends_at, trigger),
    )
    conn.commit()
    return {
        "level": new_level, "in_deload": True, "deload_triggered": True,
        "trigger": trigger,
    }


# --- Tiers: what a day actually asks for ---------------------------
#
# The level system has a floor. At level 0 the treadmill session is
# still 5.5 km/h at 12% incline for 20 minutes, which is a real
# workout, and every red day after that changes nothing: a person ill
# for a week was handed the identical prescription each morning,
# because "lighter" had run out of room. Below the floor there are two
# more steps, neither of which has a level:
#
#   recovery  an easy flat walk, in place of the scheduled session
#   rest      no session at all
TIER_TRAIN, TIER_RECOVERY, TIER_REST = "train", "recovery", "rest"
TIER_LABEL_FR = {
    TIER_TRAIN: "entrainement", TIER_RECOVERY: "recuperation",
    TIER_REST: "repos",
}
RECOVERY_VALUES = {"speed_kmh": 4.5, "incline_pct": 0, "duration_min": 20}


def sick_until(conn: sqlite3.Connection, user_id: int) -> Optional[str]:
    """The date the person said they are ill until, if it is set.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.

    Returns:
        str | None: ISO date, or None when unset or not a valid date.
    """
    value = (db.get_setting(conn, user_id, "sick_until") or "").strip()
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return None
    return value


def is_self_reported_sick(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> bool:
    """Whether ``date`` falls inside a declared illness.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date.

    Returns:
        bool: True up to and including the ``sick_until`` date.
    """
    until = sick_until(conn, user_id)
    return until is not None and date <= until


def set_sick(
    conn: sqlite3.Connection, user_id: int, today: str, days: int,
) -> Optional[str]:
    """Declare an illness for ``days`` days counting today, or clear it.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        today (str): ISO local date.
        days (int): 1 means today only; 0 (or less) clears the flag.

    Returns:
        str | None: The new ``sick_until`` date, None when cleared.
    """
    if days <= 0:
        db.set_setting(conn, user_id, "sick_until", "")
        return None
    until = (
        dt.date.fromisoformat(today) + dt.timedelta(days=days - 1)
    ).isoformat()
    db.set_setting(conn, user_id, "sick_until", until)
    return until


def consecutive_red_days(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> int:
    """Run of red days immediately before ``date``.

    Across every session type, unlike ``get_red_streak`` which belongs
    to one type: what a red day says about the body does not depend on
    which session happened to be scheduled. A day with no coach_log row
    ends the run, and the latest row wins when a day was logged twice.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today, excluded).

    Returns:
        int: How many days in a row, ending yesterday, were red.
    """
    rows = conn.execute(
        "SELECT local_date, status FROM coach_log WHERE id IN ("
        "SELECT MAX(id) FROM coach_log WHERE user_id = ? AND "
        "local_date < ? GROUP BY local_date) ORDER BY local_date DESC",
        (user_id, date),
    ).fetchall()
    run = 0
    expected = dt.date.fromisoformat(date) - dt.timedelta(days=1)
    for row in rows:
        if row["local_date"] != expected.isoformat() or row["status"] != "red":
            break
        run += 1
        expected -= dt.timedelta(days=1)
    return run


def previous_tier(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> str:
    """What yesterday asked for; ``train`` when unknown.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today).

    Returns:
        str: One of the ``TIER_*`` constants.
    """
    yesterday = (
        dt.date.fromisoformat(date) - dt.timedelta(days=1)
    ).isoformat()
    row = conn.execute(
        "SELECT tier FROM coach_log WHERE user_id = ? AND local_date = ? "
        "ORDER BY id DESC LIMIT 1", (user_id, yesterday),
    ).fetchone()
    return (row["tier"] if row and row["tier"] else TIER_TRAIN)


def tier_before_guardrail(
    conn: sqlite3.Connection, user_id: int, date: str, illness: dict,
) -> Optional[dict]:
    """Rest or recovery decided without needing today's level.

    Checked BEFORE the deload guardrail on purpose: when the body is
    the problem, the day should not also be scored against the level
    system -- no further level cut, no red streak counted on a day the
    session is not even happening.

    Order of precedence: a declared illness, then the sensors' own
    reading of one (``illness_watch``), then the first day back after a
    rest, which is a walk and not the full session again.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today).
        illness (dict): ``illness_watch`` output.

    Returns:
        dict | None: ``{"tier", "reasons"}``, or None to carry on with
        the normal level-based flow.
    """
    if is_self_reported_sick(conn, user_id, date):
        return {
            "tier": TIER_REST,
            "reasons": [f"tu es declare malade jusqu'au {sick_until(conn, user_id)}"],
        }
    if illness.get("suspected"):
        shown = ", ".join(illness.get("signals", [])[:3])
        return {
            "tier": TIER_REST,
            "reasons": [
                "signes compatibles avec une maladie ou un gros "
                f"surmenage ({shown})"
            ],
        }
    if previous_tier(conn, user_id, date) == TIER_REST:
        return {
            "tier": TIER_RECOVERY,
            "reasons": ["reprise douce apres un jour de repos"],
        }
    return None


def tier_after_guardrail(
    status: Optional[str], level: Optional[int], red_days_before: int,
) -> Optional[dict]:
    """Recovery forced by red days the level system cannot absorb.

    Parameters:
        status (str | None): Today's ``compute_status`` result.
        level (int | None): Today's level AFTER the guardrail.
        red_days_before (int): ``consecutive_red_days`` for yesterday
            backwards.

    Returns:
        dict | None: ``{"tier": recovery, "reasons"}`` when today is
        red and either the level is already at its floor (nothing left
        to cut) or it is the second red day running; None otherwise.
    """
    if status != "red" or level is None:
        return None
    if level <= LEVEL_MIN:
        return {
            "tier": TIER_RECOVERY,
            "reasons": [
                "jour rouge et niveau deja au minimum : la seance ne "
                "peut plus s'alleger"
            ],
        }
    if red_days_before >= 1:
        return {
            "tier": TIER_RECOVERY,
            "reasons": [f"{red_days_before + 1}e jour rouge de suite"],
        }
    return None


def describe_tier_fr(
    tier: str, scheduled_title: str, reasons: list[str],
    values: Optional[dict] = None,
) -> str:
    """French calendar/coach description of a rest or recovery day.

    Parameters:
        tier (str): ``TIER_REST`` or ``TIER_RECOVERY``.
        scheduled_title (str): The session the week plan had scheduled.
        reasons (list[str]): Why, from the tier functions above.
        values (dict | None): ``RECOVERY_VALUES`` for a recovery day.

    Returns:
        str: One sentence, reasons included.
    """
    why = " ; ".join(reasons)
    if tier == TIER_REST:
        return (
            f"REPOS - pas de seance aujourd'hui ({scheduled_title} "
            f"annule) : {why}. Hydrate-toi, dors, et au plus une marche "
            "tranquille si l'envie est la."
        )
    values = values or RECOVERY_VALUES
    return (
        "RECUPERATION - marche tranquille a plat : "
        f"{values['speed_kmh']} km/h, inclinaison "
        f"{values['incline_pct']}%, {values['duration_min']} min continu, "
        f"a la place de {scheduled_title} ({why})."
    )


# Illness/overreaching watch: independent physiological signals
# agreeing on the same day, sustained across days, rather than any
# single vote -- a hard training day alone can spike RHR or dent
# readiness, but RHR, HRV and readiness all bad together, more than
# one day running, is a much more specific pattern. Reuses the exact
# thresholds each vote already uses (RHR_SPIKE_RED_MIN,
# TRAINING_READINESS_POOR, HRV "LOW"), not new ones invented for
# this -- there is no clinical basis here to pick a different cutoff,
# only a basis to combine the ones already tuned.
ILLNESS_WATCH_DAYS = 3
ILLNESS_MIN_SIGNALS = 2  # bad signals on one day, at least one a core one
ILLNESS_MIN_DISTRESSED_DAYS = 2  # of ILLNESS_WATCH_DAYS
# Enough on a single day to act without waiting for a second one: three
# independent signals agreeing, one of them core, is not a bad night.
ILLNESS_FAST_PATH_SIGNALS = 3
# Supporting signals. None of these can make a day distressed alone or
# together -- a stressful week and a poor night look the same from the
# wrist -- they only count next to a core signal.
BODY_BATTERY_POOR = 40  # morning peak below this: never really recharged
STRESS_HIGH = metrics.STRESS_HIGH  # Garmin's "medium" band starts at 51
RESPIRATION_RISE_BRPM = 2.0  # waking rate above the personal baseline
RESPIRATION_BASELINE_DAYS = 14


def respiration_baseline(
    conn: sqlite3.Connection, user_id: int, date: str,
    days: int = RESPIRATION_BASELINE_DAYS,
) -> Optional[float]:
    """Mean waking respiration rate over the ``days`` before ``date``.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date, excluded from the window.
        days (int): Window length.

    Returns:
        float | None: Mean breaths per minute, or None with fewer than
        5 readings (a baseline from two nights is not one).
    """
    start = (dt.date.fromisoformat(date) - dt.timedelta(days=days)).isoformat()
    end = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    row = conn.execute(
        "SELECT AVG(avg_waking) AS mean, COUNT(avg_waking) AS n FROM "
        "garmin_respiration WHERE user_id = ? AND local_date BETWEEN ? "
        "AND ?", (user_id, start, end),
    ).fetchone()
    return row["mean"] if row["n"] >= 5 else None


def _distress_signals(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> list[tuple[str, bool]]:
    """Which recovery signals were bad on one date.

    Core signals are the autonomic ones -- resting HR above baseline,
    HRV status LOW, training readiness poor. Supporting signals are
    body battery that never recharged, high stress, and a waking
    respiration rate above the personal baseline: each real, none
    specific to illness, so they only add weight beside a core one.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date.

    Returns:
        list[tuple[str, bool]]: ``(French label, is_core)`` for each
        signal that was bad that day (empty if none, or if there is no
        data at all).
    """
    wellness = metrics.daily_wellness(conn, user_id, date)
    baseline = rhr_baseline(conn, user_id, date)
    signals: list[tuple[str, bool]] = []
    resting_hr = wellness.get("resting_hr")
    if resting_hr is not None and baseline is not None:
        spike = resting_hr - baseline
        if spike >= RHR_SPIKE_RED_MIN:
            signals.append((f"FC repos {spike:+.0f} vs base", True))
    if wellness.get("hrv_status") == "LOW":
        signals.append(("VFC basse", True))
    readiness = wellness.get("training_readiness_score")
    if readiness is not None and readiness < TRAINING_READINESS_POOR:
        signals.append((f"recuperation {readiness:.0f}/100", True))
    battery = wellness.get("body_battery_highest")
    if battery is not None and battery < BODY_BATTERY_POOR:
        signals.append((f"batterie corporelle {battery:.0f}", False))
    stress = wellness.get("stress_avg_level")
    if stress is not None and stress >= STRESS_HIGH:
        signals.append((f"stress {stress:.0f}", False))
    breathing = wellness.get("respiration_avg_waking")
    breathing_base = respiration_baseline(conn, user_id, date)
    if (
        breathing is not None and breathing_base is not None
        and breathing - breathing_base >= RESPIRATION_RISE_BRPM
    ):
        signals.append(
            (f"respiration {breathing:.0f} vs base {breathing_base:.0f}", False)
        )
    return signals


def _is_distressed(signals: list[tuple[str, bool]]) -> bool:
    """Enough signals on one day, at least one of them core."""
    return (
        len(signals) >= ILLNESS_MIN_SIGNALS
        and any(core for _, core in signals)
    )


def illness_watch(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> dict:
    """Flag a pattern of recovery signals consistent with illness or
    serious overreaching -- not a diagnosis, a reason to back off.

    A day counts as "distressed" when at least ``ILLNESS_MIN_SIGNALS``
    signals are bad together and at least one is a core one (see
    ``_distress_signals``). ``suspected`` is true when at least
    ``ILLNESS_MIN_DISTRESSED_DAYS`` of the last ``ILLNESS_WATCH_DAYS``
    days (today included) were distressed -- or when today alone shows
    ``ILLNESS_FAST_PATH_SIGNALS`` signals including a core one, since a
    person who woke up with a resting HR spike, low HRV and a body
    battery that never filled should not be sent out on the treadmill
    to wait for a second day to confirm it. Deliberately harder to
    trigger than any single vote: what this feeds is telling someone to
    train less, and the sensors' own reading is only half the story --
    ``sick_until`` in Settings is the other half.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today).

    Returns:
        dict: ``suspected`` (bool), ``distressed_days`` (int, out of
        ``ILLNESS_WATCH_DAYS``), and ``signals`` (the union of every
        distress label seen in the window, most recent day's signals
        first) -- ``signals`` is empty when ``suspected`` is False.
    """
    per_day = []
    for offset in range(ILLNESS_WATCH_DAYS):
        day = (
            dt.date.fromisoformat(date) - dt.timedelta(days=offset)
        ).isoformat()
        per_day.append((day, _distress_signals(conn, user_id, day)))
    distressed_days = [
        (day, signals) for day, signals in per_day if _is_distressed(signals)
    ]
    today_signals = per_day[0][1]
    fast_path = (
        len(today_signals) >= ILLNESS_FAST_PATH_SIGNALS
        and any(core for _, core in today_signals)
    )
    suspected = (
        len(distressed_days) >= ILLNESS_MIN_DISTRESSED_DAYS or fast_path
    )
    all_signals: list[str] = []
    shown = distressed_days if distressed_days else [(date, today_signals)]
    for _, signals in shown:
        for label, _core in signals:
            if label not in all_signals:
                all_signals.append(label)
    return {
        "suspected": suspected,
        "distressed_days": len(distressed_days),
        "signals": all_signals if suspected else [],
    }


def apply_illness_deload(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> dict[str, dict]:
    """Force a deload across every session type at once.

    apply_deload_guardrail is scoped to one session_type, tied to
    that type's own red streak -- the right shape for "this exercise
    has been going badly," the wrong one for "the body is fighting
    something," which has no opinion about which session type is
    scheduled today. Called once illness_watch says suspected, this
    cuts every type in one pass instead of waiting for each one to
    separately rack up its own red streak, which a sick week spread
    across different session types might never do.

    Idempotent by design: a type already inside an active deload
    window is left alone rather than cut again, so calling this every
    morning the watch stays suspected does not compound the cut.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today) -- deload window start.

    Returns:
        dict[str, dict]: ``_trigger_deload`` result per session type
        this call actually cut; types already in deload are omitted
        entirely (empty dict if illness was already accounted for
        everywhere).
    """
    triggered = {}
    for session_type in SESSION_LABEL_FR:
        deload_until = get_deload_until(conn, user_id, session_type)
        if deload_until is not None and date <= deload_until:
            continue  # already reduced -- don't cut further
        level = get_level(conn, user_id, session_type)
        triggered[session_type] = _trigger_deload(
            conn, user_id, session_type, level, date, "illness",
        )
    return triggered


def apply_deload_guardrail(
    conn: sqlite3.Connection, user_id: int, session_type: str, status: str,
    date: str, tsb: Optional[float] = None,
) -> dict:
    """Adjust today's level, applying the deload guardrail on top of
    the normal +-1 rule.

    Two independent triggers force a deload: 3 reds in a row (the
    original guardrail), or TSB dropping below
    ``TSB_DELOAD_THRESHOLD`` -- the latter fires on a single reading
    (no streak needed), since TSB is already a smoothed 42d/7d signal
    that can miss the red-streak counter entirely (e.g. red/yellow/
    red never reaches 3 in a row while fatigue keeps climbing).

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        session_type (str): Session type key.
        status (str): Today's ``compute_status`` result.
        date (str): ISO local date (today).
        tsb (float | None): Yesterday's Training Stress Balance
            (``training_load.latest_training_load``), if computed yet.

    Returns:
        dict: ``level`` (new level, already persisted),
        ``in_deload`` (a deload window is active today),
        ``deload_triggered`` (this call is what triggered it), and
        ``trigger`` (``"red_streak"`` or ``"tsb"``) only present when
        ``deload_triggered`` is true.
    """
    level = get_level(conn, user_id, session_type)
    deload_until = get_deload_until(conn, user_id, session_type)
    in_deload = deload_until is not None and date <= deload_until

    if (
        not in_deload and tsb is not None
        and tsb <= TSB_DELOAD_THRESHOLD
    ):
        return _trigger_deload(conn, user_id, session_type, level, date, "tsb")

    if status == "red":
        streak = get_red_streak(conn, user_id, session_type) + 1
        if streak >= RED_STREAK_THRESHOLD:
            return _trigger_deload(
                conn, user_id, session_type, level, date, "red_streak",
            )
        set_red_streak(conn, user_id, session_type, streak)
        new_level = adjust_level(level, status)
        set_level(conn, user_id, session_type, new_level)
        return {
            "level": new_level, "in_deload": in_deload,
            "deload_triggered": False,
        }

    set_red_streak(conn, user_id, session_type, 0)
    if in_deload:
        # Hold the level through the deload window regardless of a
        # green/yellow day -- the point is a forced lighter week, not
        # a one-day pause.
        return {
            "level": level, "in_deload": True, "deload_triggered": False,
        }
    if deload_until is not None:
        # Window just ended: clear it so next time starts fresh.
        set_deload_until(conn, user_id, session_type, None)
    new_level = adjust_level(level, status)
    set_level(conn, user_id, session_type, new_level)
    return {
        "level": new_level, "in_deload": False, "deload_triggered": False,
    }


def rhr_baseline(
    conn: sqlite3.Connection, user_id: int, date: str,
    days: int = RHR_BASELINE_DAYS,
) -> Optional[float]:
    """Mean resting HR over the ``days`` before ``date``.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today), excluded from the window.
        days (int): Window length.

    Returns:
        float | None: Rolling average, or ``None`` if fewer than 3
        readings are on record in that window.
    """
    start = (
        dt.date.fromisoformat(date) - dt.timedelta(days=days)
    ).isoformat()
    end = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT bpm FROM resting_heart_rate WHERE user_id = ? AND "
        "local_date BETWEEN ? AND ?", (user_id, start, end),
    ).fetchall()
    if len(rows) < 3:
        return None
    return sum(r["bpm"] for r in rows) / len(rows)


def _sleep_vote(wellness: dict) -> Optional[str]:
    score = wellness.get("sleep_score")
    if score is None:
        return None
    if score >= SLEEP_SCORE_GOOD:
        return "green"
    if score < SLEEP_SCORE_POOR:
        return "red"
    return "yellow"


def _activity_load_vote(wellness: dict) -> Optional[str]:
    recent = wellness.get("recent_minutes")
    previous = wellness.get("previous_minutes")
    if recent is None or previous is None:
        return None
    high_rpe = (wellness.get("recent_avg_rpe") or 0) >= ACTIVITY_LOAD_HIGH_RPE
    if previous == 0:
        return "yellow" if recent > 0 and high_rpe else None
    ratio = recent / previous
    if ratio >= ACTIVITY_LOAD_SPIKE_RATIO:
        return "red" if high_rpe else "yellow"
    return "green"


def _resting_hr_vote(
    wellness: dict, baseline_rhr: Optional[float],
) -> Optional[str]:
    resting_hr = wellness.get("resting_hr")
    if resting_hr is None or baseline_rhr is None:
        return None
    spike = resting_hr - baseline_rhr
    return "red" if spike >= RHR_SPIKE_RED_MIN else "green"


_HRV_STATUS_VOTE = {"BALANCED": "green", "UNBALANCED": "yellow", "LOW": "red"}


def _hrv_vote(wellness: dict) -> Optional[str]:
    return _HRV_STATUS_VOTE.get(wellness.get("hrv_status"))


def _training_readiness_vote(wellness: dict) -> Optional[str]:
    score = wellness.get("training_readiness_score")
    if score is None:
        return None
    if score >= TRAINING_READINESS_GOOD:
        return "green"
    if score < TRAINING_READINESS_POOR:
        return "red"
    return "yellow"


def _body_battery_vote(wellness: dict) -> Optional[str]:
    """Morning body battery peak: never really recharged -> yellow.

    Capped at yellow on purpose. Body battery and stress are the
    watch's own blend of the same heart-rate and HRV signals the other
    votes already read, so letting them also turn a day red would count
    one bad night twice -- but a day that was not recharged is not a day
    to be pushed up a level either, which is all yellow does.
    """
    peak = wellness.get("body_battery_highest")
    if peak is None:
        return None
    return "yellow" if peak < BODY_BATTERY_POOR else "green"


def _stress_vote(wellness: dict) -> Optional[str]:
    """Yesterday's whole-day stress average: high -> yellow, capped.

    Yesterday's, not today's: at wake-up today's average only covers the
    night. Capped at yellow for the same reason as the battery vote.
    """
    average = wellness.get("stress_avg_yesterday")
    if average is None:
        return None
    return "yellow" if average >= STRESS_HIGH else "green"


# Two sessions in a row felt too hard means the level is wrong, whatever
# this morning's recovery says -- one bad evening is noise, two is a
# pattern. Same, mirrored, for a level that has become too easy.
FEEDBACK_STREAK = 2

_FEEDBACK_LABEL_FR = {
    "easy": "trop facile", "right": "au bon niveau", "hard": "trop dure",
}


def recent_feedback(
    conn: sqlite3.Connection, user_id: int, session_type: str,
    limit: int = FEEDBACK_STREAK,
) -> list[str]:
    """The last ratings given for one session type, most recent first.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        session_type (str): Session type the ratings belong to.
        limit (int): How many to look back on.

    Returns:
        list[str]: ``"easy"`` / ``"right"`` / ``"hard"``, newest first.
    """
    return [
        row["rating"] for row in conn.execute(
            "SELECT rating FROM session_feedback WHERE user_id = ? AND "
            "session_type = ? ORDER BY local_date DESC LIMIT ?",
            (user_id, session_type, limit),
        )
    ]


def _feedback_vote(feedback: Optional[list[str]]) -> Optional[str]:
    if not feedback or len(feedback) < FEEDBACK_STREAK:
        return None
    streak = feedback[:FEEDBACK_STREAK]
    if all(r == "hard" for r in streak):
        return "red"
    if all(r == "easy" for r in streak):
        return "green"
    return None


_HRV_STATUS_LABEL_FR = {
    "BALANCED": "equilibree", "UNBALANCED": "desequilibree", "LOW": "basse",
}


def _all_votes(
    wellness: dict, baseline_rhr: Optional[float],
    feedback: Optional[list[str]] = None,
    extra_votes: Optional[list[tuple[str, str, str]]] = None,
) -> list[tuple[str, str, str]]:
    """Every signal that has data today, as (label, vote, detail).

    One list, used both to decide the day's status and to explain it on
    the dashboard -- so the explanation can never drift from the verdict
    it explains.

    Parameters:
        wellness (dict): Today's metrics (``metrics.daily_wellness``).
        baseline_rhr (float | None): Rolling personal resting-HR baseline.

    Returns:
        list[tuple[str, str, str]]: Signals with data, in reading order.
    """
    sleep_score = wellness.get("sleep_score")
    recent = wellness.get("recent_minutes")
    previous = wellness.get("previous_minutes")
    resting_hr = wellness.get("resting_hr")
    readiness = wellness.get("training_readiness_score")
    hrv_status = wellness.get("hrv_status")

    rhr_detail = ""
    if resting_hr is not None and baseline_rhr is not None:
        rhr_detail = (
            f"{resting_hr:.0f} bpm, "
            f"{resting_hr - baseline_rhr:+.0f} vs ta base "
            f"({baseline_rhr:.0f})"
        )

    load_detail = ""
    if recent is not None and previous is not None:
        load_detail = f"{recent:.0f} min cette semaine contre {previous:.0f}"

    candidates = (
        ("Sommeil", _sleep_vote(wellness),
         "" if sleep_score is None else f"score {sleep_score:.0f}"),
        ("Charge recente", _activity_load_vote(wellness), load_detail),
        ("FC de repos", _resting_hr_vote(wellness, baseline_rhr), rhr_detail),
        ("VFC", _hrv_vote(wellness),
         _HRV_STATUS_LABEL_FR.get(hrv_status or "", "")),
        ("Recuperation", _training_readiness_vote(wellness),
         "" if readiness is None else f"{readiness:.0f}/100"),
        ("Batterie", _body_battery_vote(wellness),
         "" if wellness.get("body_battery_highest") is None
         else f"pic {wellness['body_battery_highest']:.0f}/100 ce matin"),
        ("Stress (veille)", _stress_vote(wellness),
         "" if wellness.get("stress_avg_yesterday") is None
         else f"moyenne {wellness['stress_avg_yesterday']:.0f}/100"),
        ("Ressenti", _feedback_vote(feedback),
         ", ".join(
             _FEEDBACK_LABEL_FR.get(r, r)
             for r in (feedback or [])[:FEEDBACK_STREAK]
         )),
    )
    # Votes computed outside wellness (muscle fatigue: muscles.py),
    # already capped by whoever computed them.
    candidates = candidates + tuple(extra_votes or ())
    return [
        (label, vote, detail)
        for label, vote, detail in candidates
        if vote is not None
    ]


def explain_status(
    wellness: dict, baseline_rhr: Optional[float],
    feedback: Optional[list[str]] = None,
    extra_votes: Optional[list[tuple[str, str, str]]] = None,
) -> list[dict]:
    """The signals behind today's status, for display.

    A green/yellow/red chip on its own asks to be trusted blindly; the
    same chip with "FC de repos +5 vs ta base" under it can be argued
    with, which is the point of a coach you self-host.

    Parameters:
        wellness (dict): Today's metrics (``metrics.daily_wellness``).
        baseline_rhr (float | None): Rolling personal resting-HR baseline.

    Returns:
        list[dict]: ``{"label", "vote", "detail"}`` per signal with data.
        Empty when nothing was measured -- the caller shows nothing
        rather than a table of dashes.
    """
    return [
        {"label": label, "vote": vote, "detail": detail}
        for label, vote, detail in _all_votes(
            wellness, baseline_rhr, feedback, extra_votes,
        )
    ]


def compute_status(
    wellness: dict, baseline_rhr: Optional[float],
    feedback: Optional[list[str]] = None,
    extra_votes: Optional[list[tuple[str, str, str]]] = None,
) -> str:
    """Combine wellness signals into a green/yellow/red daily status.

    Parameters:
        wellness (dict): Today's wellness metrics (as produced by
            ``metrics.daily_wellness``).
        baseline_rhr (float | None): Rolling personal resting-HR
            baseline (``training.rhr_baseline``).

    Returns:
        str: ``"green"``, ``"yellow"``, or ``"red"``. No data at all
        falls back to ``"yellow"`` (maintain level). Any red signal
        wins over green/yellow; green requires all available votes
        to be green.
    """
    votes = [
        vote for _, vote, _ in _all_votes(
            wellness, baseline_rhr, feedback, extra_votes,
        )
    ]
    if not votes:
        return "yellow"
    if "red" in votes:
        return "red"
    if all(v == "green" for v in votes):
        return "green"
    return "yellow"


CALIBRATION_MIN_DAYS = 30


def _share(values: list[float], predicate) -> int:
    return round(100 * sum(1 for v in values if predicate(v)) / len(values))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def calibration_report(
    conn: sqlite3.Connection, user_id: int, date: str, days: int = 90,
) -> list[dict]:
    """Do the built-in thresholds describe THIS body, or someone else's?

    SLEEP_SCORE_GOOD, TRAINING_READINESS_GOOD and the resting-HR spike
    are the same numbers for everyone, and the code has always carried a
    "retune against real mornings" note that nobody ever acts on. This
    reports how often each threshold is actually cleared, next to the
    person's own median -- someone green 8 % of the time is not lazy,
    their thresholds are simply set for another body.

    Deliberately a report and not an auto-tune: silently moving the
    goalposts under someone would make every past status unreadable and
    every future one unfalsifiable.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date, end of the window.
        days (int): Window length.

    Returns:
        list[dict]: ``{"label", "median", "threshold", "share", "unit",
        "note"}`` per signal with at least CALIBRATION_MIN_DAYS of data.
        Empty early on -- a verdict drawn from three nights would be
        worse than none.
    """
    start = (
        dt.date.fromisoformat(date) - dt.timedelta(days=days)
    ).isoformat()
    out: list[dict] = []

    sleep = [
        v for v in metrics.sleep_scores_for_range(
            conn, user_id, date, days,
        ).values() if v is not None
    ]
    if len(sleep) >= CALIBRATION_MIN_DAYS:
        share = _share(sleep, lambda v: v >= SLEEP_SCORE_GOOD)
        out.append({
            "label": "Sommeil", "median": round(_median(sleep)),
            "threshold": SLEEP_SCORE_GOOD, "share": share, "unit": "",
            "days": len(sleep),
            "note": _calibration_note(share, "SLEEP_SCORE_GOOD"),
        })

    readiness = [
        row["score"] for row in conn.execute(
            "SELECT score FROM garmin_training_readiness WHERE "
            "user_id = ? AND local_date >= ? AND score IS NOT NULL",
            (user_id, start),
        )
    ]
    if len(readiness) >= CALIBRATION_MIN_DAYS:
        share = _share(readiness, lambda v: v >= TRAINING_READINESS_GOOD)
        out.append({
            "label": "Recuperation", "median": round(_median(readiness)),
            "threshold": TRAINING_READINESS_GOOD, "share": share,
            "unit": "/100", "days": len(readiness),
            "note": _calibration_note(share, "TRAINING_READINESS_GOOD"),
        })

    return out


def _calibration_note(share: int, constant: str) -> Optional[str]:
    """One line, only when the numbers actually say something."""
    if share < 15:
        return (
            f"Seuil franchi {share} % du temps : {constant} est probablement "
            "trop haut pour toi."
        )
    if share > 85:
        return (
            f"Seuil franchi {share} % du temps : {constant} ne discrimine "
            "plus grand-chose."
        )
    return None


def adjust_level(
    level: int, status: str, lo: int = LEVEL_MIN, hi: int = LEVEL_MAX,
) -> int:
    """Apply the day's status to a session type's current level.

    Parameters:
        level (int): Current level for the session type.
        status (str): ``"green"``, ``"yellow"``, or ``"red"``.
        lo (int): Floor.
        hi (int): Ceiling.

    Returns:
        int: New level. Green +1 (capped), yellow unchanged,
        red -1 (floored).
    """
    if status == "green":
        return min(level + 1, hi)
    if status == "red":
        return max(level - 1, lo)
    return level


def schedule_for_user(
    conn: sqlite3.Connection, user_id: int,
) -> dict[int, dict]:
    """The user's weekly plan: weekday (Monday=0) -> session template.

    Each template carries ``session_type`` (or ``None`` for a day
    outside the leveling system), ``title``, ``start`` (HH:MM) and
    ``duration_min`` -- the same shape as ``db.DEFAULT_SCHEDULE``.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.

    Returns:
        dict[int, dict]: One template per weekday; a corrupt or
        missing setting falls back to the default week.
    """
    raw = db.get_setting(conn, user_id, "schedule")
    try:
        schedule = json.loads(raw) if raw else db.DEFAULT_SCHEDULE
    except json.JSONDecodeError:
        schedule = db.DEFAULT_SCHEDULE
    return {
        weekday: {**db.DEFAULT_SCHEDULE[str(weekday)],
                  **schedule.get(str(weekday), {})}
        for weekday in range(7)
    }


def session_type_for_weekday(
    conn: sqlite3.Connection, user_id: int, weekday: int,
) -> Optional[str]:
    """The user's session type for an ISO weekday (Monday=0).

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        weekday (int): ``date.weekday()`` result.

    Returns:
        str | None: Session type key, or ``None`` for a day outside
        the leveling system.
    """
    return schedule_for_user(conn, user_id)[weekday].get("session_type")


# Density-first philosophy: a higher level first packs more work into
# the same slot (speed, reps, rounds); only once intensity is maxed
# does the session get LONGER, and never past the user's cap.
DEFAULT_SESSION_CAP_MIN = 30
SESSION_CAP_FLOOR_MIN, SESSION_CAP_CEIL_MIN = 10, 240


def session_cap_min(conn: sqlite3.Connection, user_id: int) -> int:
    """The user's max session duration in minutes (Settings).

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.

    Returns:
        int: Cap clamped to a sane range; default 30.
    """
    raw = db.get_setting(conn, user_id, "session_cap_min")
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        cap = DEFAULT_SESSION_CAP_MIN
    return max(SESSION_CAP_FLOOR_MIN, min(SESSION_CAP_CEIL_MIN, cap))


def treadmill_values(
    level: int, cap_min: int = DEFAULT_SESSION_CAP_MIN,
    date: Optional[str] = None,
    equipment: Optional[dict] = None,
) -> dict:
    """Level -> treadmill workout values.

    Speed climbs until level 8 caps it at 7.0 km/h; levels above that
    extend the walk instead (+4 min per level), up to ``cap_min``.
    """
    return {
        "speed_kmh": min(round(5.5 + level * 0.2, 1), 7.0),
        "incline_pct": 12,
        "duration_min": min(20 + 4 * max(0, level - 7), cap_min),
    }


def _circuit_duration_min(rounds: int, cap_min: int) -> int:
    """Estimated circuit duration: ~6 min/round + warm-up, capped."""
    return min(12 + 6 * rounds, cap_min)


def _laddered(
    values: dict, level: int, date: Optional[str],
    equipment: Optional[dict] = None,
) -> dict:
    """Apply the exercise ladders to a circuit's values.

    Rep slots with a range take their count from the double
    progression (exercise_library.reps_for); every slot gets its
    variant for the level (and the week, when ``date`` is known) under
    ``values["variants"]``. Time slots and range-less rep slots keep
    the session's own linear count.
    """
    for slot in list(values):
        if slot in exercise_library.LADDERS:
            reps = exercise_library.reps_for(slot, level, equipment)
            if reps is not None:
                values[slot] = reps
    values["variants"] = exercise_library.session_variants(
        values, level, date, equipment,
    )
    return values


def lower_body_values(
    level: int, cap_min: int = DEFAULT_SESSION_CAP_MIN,
    date: Optional[str] = None,
    equipment: Optional[dict] = None,
) -> dict:
    """Level -> lower-body bodyweight circuit values."""
    rounds = 3 if level <= 3 else 4 if level <= 7 else 5
    return _laddered({
        "squats": 12 + level,
        "lunges_per_leg": 10 + level,
        "wall_sit_sec": 30 + level * 4,
        "calf_raises": 15 + level,
        "glute_bridge": 15 + level,
        "rounds": rounds,
        "duration_min": _circuit_duration_min(rounds, cap_min),
    }, level, date, equipment)


def upper_body_values(
    level: int, cap_min: int = DEFAULT_SESSION_CAP_MIN,
    date: Optional[str] = None,
    equipment: Optional[dict] = None,
) -> dict:
    """Level -> upper-body + core circuit values."""
    rounds = 3 if level <= 3 else 4
    return _laddered({
        "pushups": 8 + level,
        "dips": 10 + level,
        "superman": 12 + level,
        "plank_sec": 20 + level * 4,
        "rounds": rounds,
        "duration_min": _circuit_duration_min(rounds, cap_min),
    }, level, date, equipment)


def calisthenics_values(
    level: int, cap_min: int = DEFAULT_SESSION_CAP_MIN,
    date: Optional[str] = None,
    equipment: Optional[dict] = None,
) -> dict:
    """Level -> full-body calisthenics circuit values."""
    rounds = 3 if level <= 3 else 4 if level <= 7 else 5
    return _laddered({
        "squats": 15 + level,
        "pushups": 10 + level,
        "reverse_lunges_per_leg": 10 + level,
        "side_plank_sec": 15 + level * 2,
        "mountain_climbers": 20 + level * 2,
        "jumping_jacks": 20 + level * 2,
        "rounds": rounds,
        "duration_min": _circuit_duration_min(rounds, cap_min),
    }, level, date, equipment)


SESSION_VALUE_FUNCS = {
    "treadmill": treadmill_values,
    "lower_body": lower_body_values,
    "upper_body": upper_body_values,
    "calisthenics": calisthenics_values,
}


def session_values(
    session_type: str, level: int,
    cap_min: int = DEFAULT_SESSION_CAP_MIN, date: Optional[str] = None,
    equipment: Optional[dict] = None,
) -> dict:
    """Dispatch to the value-mapping function for a session type.

    ``date`` picks the week's variant among equals (exercise_library);
    without it the first alternative is used.
    """
    return SESSION_VALUE_FUNCS[session_type](level, cap_min, date, equipment)


def format_description_fr(
    session_type: str, level: int, values: dict, status: str,
) -> str:
    """Render the French calendar-event description body."""
    label = SESSION_LABEL_FR[session_type]
    status_label = STATUS_LABEL_FR[status]

    if session_type == "treadmill":
        body = (
            f"{values['speed_kmh']} km/h, marche, inclinaison "
            f"{values['incline_pct']}%, {values['duration_min']} min "
            "continu"
        )
    else:
        # Circuits: each slot named by its variant of the day (the
        # ladder rung for this level, the week's alternative).
        variants = values.get("variants") or {}
        parts = []
        for slot, value in values.items():
            if slot in ("rounds", "duration_min", "variants"):
                continue
            name = variants.get(slot, {}).get("name", slot)
            unit = "s" if slot.endswith("_sec") else ""
            per = (
                "/jambe" if slot.endswith("_per_leg")
                else "/cote" if slot == "side_plank_sec"
                else variants.get(slot, {}).get("per", "")
            )
            parts.append(f"{name} {value}{unit}{per}")
        body = (
            f"{values['rounds']} tours (~{values['duration_min']} "
            f"min) - " + ", ".join(parts)
        )

    return (
        f"Niveau {level} - {label}: {body}. "
        f"(statut du jour: {status_label})"
    )


_DELOAD_REASON = {
    "red_streak": ("3 rouges d'affilee", "3 reds in a row"),
    "tsb": ("fatigue accumulee (TSB)", "accumulated fatigue (TSB)"),
}


def _render_session(
    conn: sqlite3.Connection, user_id: int, session_type: Optional[str],
    level: Optional[int], status: Optional[str], tier: str,
    reasons: list[str], title: str, date: Optional[str] = None,
) -> tuple[dict, Optional[str]]:
    """Values and French description for whatever the day asks for.

    One place for this so the morning run, the dashboard and a
    regenerated message can never describe the same day differently.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        session_type (str | None): The scheduled type, None off-system.
        level (int | None): Today's level for that type.
        status (str | None): Today's green/yellow/red.
        tier (str): ``TIER_TRAIN`` / ``TIER_RECOVERY`` / ``TIER_REST``.
        reasons (list[str]): Why the tier is not training.
        title (str): The scheduled session's title.

    Returns:
        tuple[dict, str | None]: ``(values, description)``; description
        is None for an ordinary off-system day.
    """
    if tier == TIER_REST:
        return {}, describe_tier_fr(TIER_REST, title, reasons)
    if tier == TIER_RECOVERY:
        values = dict(RECOVERY_VALUES)
        return values, describe_tier_fr(TIER_RECOVERY, title, reasons, values)
    if session_type is None:
        return {}, None
    values = session_values(
        session_type, level, session_cap_min(conn, user_id), date,
        exercise_library.equipment_for(conn, user_id),
    )
    return values, format_description_fr(session_type, level, values, status)


def muscle_votes(
    conn: sqlite3.Connection, user_id: int, date: str,
    session_type: Optional[str],
) -> list[tuple[str, str, str]]:
    """The muscle-fatigue vote for tonight's session, as a vote list.

    Shared by the morning decision and the dashboard's explanation, so
    the two always agree on whether tired legs held tonight back.
    """
    if session_type is None:
        return []
    weights = muscles.session_muscle_weights(
        session_type, get_level(conn, user_id, session_type), date,
        exercise_library.equipment_for(conn, user_id),
    )
    vote = muscles.session_vote(
        muscles.muscle_fatigue(conn, user_id, date), weights,
    )
    return [vote] if vote else []


LAST_SESSION_LOOKBACK_DAYS = 14


def progression_gate(
    conn: sqlite3.Connection, user_id: int, session_type: str, date: str,
) -> Optional[str]:
    """Why a green day should NOT raise the level, if there is a reason.

    The rule progression systems that people trust share: you only move
    up on work you actually did and handled. A green morning says the
    body is fresh; it says nothing about whether last time's session of
    this type was done, or how it went. One "too hard" or one skipped
    session holds the level (two "too hard" already turn the day red
    through the feedback vote).

    Returns:
        str | None: The reason, in French, or None when the level may
        go up.
    """
    feedback = recent_feedback(conn, user_id, session_type, limit=1)
    if feedback and feedback[0] == "hard":
        return "la derniere seance de ce type etait trop dure"
    since = (
        dt.date.fromisoformat(date)
        - dt.timedelta(days=LAST_SESSION_LOOKBACK_DAYS)
    ).isoformat()
    last = conn.execute(
        "SELECT local_date FROM coach_log WHERE user_id = ? AND "
        "session_type = ? AND COALESCE(tier, 'train') = 'train' AND "
        "local_date < ? AND local_date >= ? AND id IN (SELECT MAX(id) "
        "FROM coach_log WHERE user_id = ? GROUP BY local_date) "
        "ORDER BY local_date DESC LIMIT 1",
        (user_id, session_type, date, since, user_id),
    ).fetchone()
    # Only for someone who records their workouts at all: without any
    # logged activity lately, "nothing logged" says nothing about the
    # session, and the gate would hold the level forever.
    tracks = conn.execute(
        "SELECT 1 FROM exercise_sessions WHERE user_id = ? AND "
        "local_date >= ? LIMIT 1", (user_id, since),
    ).fetchone()
    if last and tracks:
        # Done = an activity logged that day, or a rating given for it.
        done = conn.execute(
            "SELECT 1 FROM exercise_sessions WHERE user_id = ? AND "
            "local_date = ? UNION SELECT 1 FROM session_feedback WHERE "
            "user_id = ? AND local_date = ? LIMIT 1",
            (user_id, last["local_date"], user_id, last["local_date"]),
        ).fetchone()
        if not done:
            return (
                f"la derniere seance de ce type ({last['local_date']}) "
                "n'a pas ete faite"
            )
    return None


_STATUS_FR = {"green": "vert", "yellow": "jaune", "red": "rouge"}


def level_reason(
    before: int, after: int, status: str, gate: Optional[str],
    deload: dict, signals: list[tuple[str, str, str]],
) -> str:
    """One sentence on why tonight's level is what it is.

    Parameters:
        before (int): Level before today's decision.
        after (int): Level after it.
        status (str): Today's status.
        gate (str | None): ``progression_gate`` reason, if it held a
            green day.
        deload (dict): ``apply_deload_guardrail`` output.
        signals (list): ``_all_votes`` output.

    Returns:
        str: French, plain language, the deciding signals named.
    """
    def _named(vote: str) -> str:
        names = [label.lower() for label, v, _ in signals if v == vote]
        return f" ({', '.join(names[:3])})" if names else ""

    if deload.get("deload_triggered"):
        why = _DELOAD_REASON[deload["trigger"]][0]
        return f"semaine allegee declenchee ({why}) : niveau {before} -> {after}"
    if deload.get("in_deload"):
        return f"semaine allegee en cours : niveau maintenu a {after}"
    if status == "red":
        if after == before:
            return f"jour rouge{_named('red')}, niveau deja au minimum"
        return f"jour rouge{_named('red')} : niveau {before} -> {after}"
    if status == "green":
        if gate:
            return f"jour vert mais {gate} : niveau maintenu a {after}"
        if after == before:
            return f"jour vert, niveau maximum ({after}) atteint"
        return f"jour vert{_named('green')} : niveau {before} -> {after}"
    return f"jour jaune{_named('yellow')} : niveau maintenu a {after}"


def plan_day(
    conn: sqlite3.Connection, user_id: int, date: str, template: dict,
    wellness: dict, illness: dict, tsb: Optional[float] = None,
    language: str = "fr",
) -> dict:
    """Everything the morning decides about today's training.

    Pulled out of run_coach.py so the part that matters -- what the
    person is actually told to do -- is a function with tests instead of
    a pipeline that needs a calendar, a watch and a phone to run. It
    still writes state (levels, deload windows, red streaks), exactly as
    the inline code did; the calendar and the watch stay in the caller.

    Order of decisions: an illness (declared, or read from the sensors)
    cuts every session type and rests the day, skipping the level
    system entirely; otherwise the normal level adjustment runs and a
    red day it cannot absorb becomes a recovery walk.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today).
        template (dict): Today's ``schedule_for_user`` entry.
        wellness (dict): ``metrics.daily_wellness`` for today.
        illness (dict): ``illness_watch`` output.
        tsb (float | None): Yesterday's training stress balance.
        language (str): ``fr`` or ``en``, for the deload note.

    Returns:
        dict: ``session_type`` (the scheduled one), ``status``,
        ``level``, ``tier``, ``tier_reasons``, ``values``,
        ``description`` (None for an ordinary off-system day),
        ``in_deload``, ``deload_triggered`` and ``illness_deload``.
    """
    session_type = template.get("session_type")
    sick = bool(illness.get("suspected")) or is_self_reported_sick(
        conn, user_id, date,
    )
    illness_deload = apply_illness_deload(conn, user_id, date) if sick else {}
    tier_info = tier_before_guardrail(conn, user_id, date, illness)

    status = level = None
    reason = None
    extra = []
    deload = {"in_deload": False, "deload_triggered": False}
    if session_type is not None:
        extra = muscle_votes(conn, user_id, date, session_type)
        baseline = rhr_baseline(conn, user_id, date)
        feedback = recent_feedback(conn, user_id, session_type)
        status = compute_status(wellness, baseline, feedback, extra)
        if tier_info is None:
            before = get_level(conn, user_id, session_type)
            gate = (
                progression_gate(conn, user_id, session_type, date)
                if status == "green" else None
            )
            # A held green day moves the level like a yellow one; the
            # day itself stays green (the body is fine, the level is
            # waiting on the work).
            deload = apply_deload_guardrail(
                conn, user_id, session_type,
                "yellow" if gate else status, date, tsb=tsb,
            )
            level = deload["level"]
            reason = level_reason(
                before, level, status, gate, deload,
                _all_votes(wellness, baseline, feedback, extra),
            )
            tier_info = tier_after_guardrail(
                status, level, consecutive_red_days(conn, user_id, date),
            )
        else:
            # A rest or recovery day is not scored against the level
            # system: no further cut, no red counted for a session that
            # is not happening.
            level = get_level(conn, user_id, session_type)
            until = get_deload_until(conn, user_id, session_type)
            deload = {
                "in_deload": bool(until and date <= until),
                "deload_triggered": False,
            }
    tier = tier_info["tier"] if tier_info else TIER_TRAIN
    reasons = list(tier_info["reasons"]) if tier_info else []
    values, description = _render_session(
        conn, user_id, session_type, level, status, tier, reasons,
        template.get("title", ""), date,
    )
    if tier == TIER_TRAIN and deload.get("deload_triggered") and description:
        reason_fr, reason_en = _DELOAD_REASON[deload["trigger"]]
        note = (
            f"SEMAINE DE DELOAD ({reason_fr})" if language == "fr"
            else f"DELOAD WEEK ({reason_en})"
        )
        description = f"{description}\n{note}"
    return {
        "session_type": session_type, "status": status, "level": level,
        "tier": tier, "tier_reasons": reasons, "values": values,
        "description": description,
        "in_deload": bool(deload.get("in_deload")),
        "deload_triggered": bool(deload.get("deload_triggered")),
        "illness_deload": illness_deload,
        "level_reason": reason,
        "tired_muscles": extra[0][2] if extra else None,
    }


def session_payload(plan: dict, template: dict, language: str = "fr") -> dict:
    """The ``today_session`` block the coach message is written from.

    Parameters:
        plan (dict): ``plan_day`` output (or ``plan_from_log``).
        template (dict): Today's ``schedule_for_user`` entry.
        language (str): ``fr`` or ``en``, for the off-system note.

    Returns:
        dict: ``type`` is ``rest`` / ``recovery`` / the session type /
        ``off_system``; ``scheduled_type`` is what the week plan said,
        so the message can say what was replaced.
    """
    tier = plan["tier"]
    if tier == TIER_TRAIN and plan["session_type"] is None:
        return {
            "type": "off_system", "tier": tier,
            "note": (
                f"{template['title']}, hors systeme de niveaux"
                if language == "fr"
                else f"{template['title']}, outside the level system"
            ),
        }
    return {
        "type": tier if tier != TIER_TRAIN else plan["session_type"],
        "scheduled_type": plan["session_type"],
        "scheduled_title": template.get("title"),
        "tier": tier, "tier_reasons": plan["tier_reasons"],
        "status": plan["status"], "level": plan["level"],
        "values": plan["values"], "description_fr": plan["description"],
        "in_deload": plan["in_deload"],
        "deload_triggered": plan["deload_triggered"],
        **({"level_reason": plan["level_reason"]}
           if plan.get("level_reason") else {}),
        **({"tired_muscles": plan["tired_muscles"]}
           if plan.get("tired_muscles") else {}),
    }


def plan_from_log(
    conn: sqlite3.Connection, user_id: int, date: str, template: dict,
    entry: sqlite3.Row,
) -> dict:
    """Rebuild a day's plan from its coach_log row, changing nothing.

    For the dashboard and for regenerating a message: both need today's
    session described exactly as the morning run decided it, and neither
    may re-run the decision, which moves levels and counts red days.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date.
        template (dict): That day's ``schedule_for_user`` entry.
        entry (sqlite3.Row): The ``coach_log`` row.

    Returns:
        dict: Same shape as ``plan_day``; ``deload_triggered`` is always
        False, since regenerating never re-triggers one.
    """
    session_type = entry["session_type"]
    tier = entry["tier"] or TIER_TRAIN
    reasons = [entry["tier_reason"]] if entry["tier_reason"] else []
    values, description = _render_session(
        conn, user_id, session_type, entry["level"], entry["status"],
        tier, reasons, template.get("title", ""), date,
    )
    until = (
        get_deload_until(conn, user_id, session_type) if session_type else None
    )
    return {
        "session_type": session_type, "status": entry["status"],
        "level": entry["level"], "tier": tier, "tier_reasons": reasons,
        "values": values, "description": description,
        "in_deload": bool(until and until >= date),
        "deload_triggered": False, "illness_deload": {},
        "level_reason": (
            entry["level_reason"] if "level_reason" in entry.keys() else None
        ),
        "tired_muscles": None,
    }


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db as db_module

    assert adjust_level(0, "red") == 0
    assert adjust_level(10, "green") == 10
    assert adjust_level(3, "yellow") == 3
    assert adjust_level(3, "green") == 4
    assert adjust_level(3, "red") == 2

    assert compute_status({}, None) == "yellow"
    # Battery and stress hold a day back, never turn it red alone: they
    # are the watch's blend of signals the other votes already read.
    well = {"sleep_score": 90}
    assert compute_status({**well, "body_battery_highest": 80}, None) == "green"
    assert compute_status({**well, "body_battery_highest": 30}, None) == "yellow"
    assert compute_status({**well, "body_battery_highest": 3}, None) == "yellow"
    assert compute_status({**well, "stress_avg_yesterday": 30}, None) == "green"
    assert compute_status({**well, "stress_avg_yesterday": 70}, None) == "yellow"
    assert compute_status({"stress_avg_yesterday": 99}, None) == "yellow"
    # ...but a real red signal still wins over them.
    assert compute_status(
        {"sleep_score": 40, "body_battery_highest": 80}, None,
    ) == "red"
    labels = {
        row["label"]: row for row in explain_status(
            {**well, "body_battery_highest": 30, "stress_avg_yesterday": 70},
            None,
        )
    }
    assert labels["Batterie"]["vote"] == "yellow", labels
    assert "30" in labels["Batterie"]["detail"], labels
    assert "70" in labels["Stress (veille)"]["detail"], labels
    assert compute_status({"sleep_score": 40}, None) == "red"
    assert compute_status(
        {"sleep_score": 80, "recent_minutes": 100, "previous_minutes": 100},
        None,
    ) == "green"
    assert compute_status({"resting_hr": 65}, 58) == "red"
    assert compute_status({"resting_hr": 59}, 58) == "green"
    assert compute_status(
        {"recent_minutes": 200, "previous_minutes": 100,
         "recent_avg_rpe": 8.5}, None,
    ) == "red"
    assert compute_status(
        {"recent_minutes": 200, "previous_minutes": 100,
         "recent_avg_rpe": 4.0}, None,
    ) == "yellow"

    # HRV / training readiness: Garmin-API-only votes, no HC equivalent.
    assert compute_status({"hrv_status": "BALANCED"}, None) == "green"
    assert compute_status({"hrv_status": "UNBALANCED"}, None) == "yellow"
    assert compute_status({"hrv_status": "LOW"}, None) == "red"
    # Unrecognized status casts no vote -> falls back to yellow (no data).
    assert compute_status({"hrv_status": "UNKNOWN_ENUM"}, None) == "yellow"
    assert compute_status({"training_readiness_score": 80}, None) == "green"
    assert compute_status({"training_readiness_score": 60}, None) == "yellow"
    assert compute_status({"training_readiness_score": 30}, None) == "red"
    # A red HRV outvotes an otherwise-green sleep score.
    assert compute_status(
        {"sleep_score": 90, "hrv_status": "LOW"}, None,
    ) == "red"

    assert treadmill_values(0)["speed_kmh"] == 5.5
    assert treadmill_values(20)["speed_kmh"] == 7.0
    assert lower_body_values(3)["rounds"] == 3
    assert lower_body_values(4)["rounds"] == 4
    assert lower_body_values(8)["rounds"] == 5
    # Double progression from the ladders: the top level is the top of
    # the hardest rung's range, and each slot names its variant.
    assert calisthenics_values(10)["squats"] == 20
    assert calisthenics_values(10)["variants"]["squats"]["name"] in (
        "squat pause 2 s", "squat saute",
    )
    assert upper_body_values(0)["pushups"] == 6
    assert upper_body_values(0)["variants"]["pushups"]["name"] == (
        "pompes inclinees"
    )
    # Time slots keep their linear count.
    assert upper_body_values(5)["plank_sec"] == 40
    described = format_description_fr(
        "upper_body", 0, upper_body_values(0), "green",
    )
    assert "pompes inclinees 6" in described, described
    assert "planche sur les genoux 20s" in described, described
    assert described.split(": ", 1)[1].split(" (statut")[0], described
    kb_text = format_description_fr(
        "upper_body", 0,
        upper_body_values(0, equipment={"kettlebell_kg": 16}), "green",
    )
    assert "rowing kettlebell a deux mains (16 kg) 8" in kb_text, kb_text
    assert "developpe au sol, 1 bras (16 kg) 6/bras" in kb_text, kb_text

    # Density-first duration: fixed until speed caps (level 7), then
    # +4 min per level, never past the cap.
    assert treadmill_values(5)["duration_min"] == 20
    assert treadmill_values(9, cap_min=45)["duration_min"] == 28
    assert treadmill_values(10, cap_min=30)["duration_min"] == 30
    assert lower_body_values(8, cap_min=60)["duration_min"] == 42
    assert lower_body_values(8, cap_min=30)["duration_min"] == 30
    assert upper_body_values(0)["duration_min"] == 30
    assert "min)" in format_description_fr(
        "lower_body", 4, lower_body_values(4), "green",
    )

    tmp = Path(tempfile.mkdtemp()) / "smart_coach.db"
    conn = db_module.connect(tmp)
    db_module.init_db(conn)
    uid = db_module.create_user(conn, "test", "password1234")
    other_uid = db_module.create_user(conn, "other", "password1234")

    assert get_level(conn, uid, "treadmill") == 0
    set_level(conn, uid, "treadmill", 4)
    assert get_level(conn, uid, "treadmill") == 4
    assert get_level(conn, other_uid, "treadmill") == 0  # isolated

    # Schedule: default week, per-user override, corrupt fallback.
    assert session_type_for_weekday(conn, uid, 0) == "treadmill"
    assert session_type_for_weekday(conn, uid, 6) is None
    db_module.set_setting(
        conn, uid, "schedule",
        json.dumps({"6": {"session_type": "treadmill",
                          "title": "Tapis dominical",
                          "start": "10:00", "duration_min": 45}}),
    )
    assert session_type_for_weekday(conn, uid, 6) == "treadmill"
    assert schedule_for_user(conn, uid)[6]["start"] == "10:00"
    # Unspecified weekdays keep the default template.
    assert session_type_for_weekday(conn, uid, 1) == "lower_body"
    # Other user's schedule is untouched.
    assert session_type_for_weekday(conn, other_uid, 6) is None
    db_module.set_setting(conn, uid, "schedule", "not json{")
    assert session_type_for_weekday(conn, uid, 6) is None  # fallback
    db_module.set_setting(
        conn, uid, "schedule", db_module.DEFAULT_SETTINGS["schedule"],
    )

    assert session_cap_min(conn, uid) == 30  # default
    db_module.set_setting(conn, uid, "session_cap_min", "60")
    assert session_cap_min(conn, uid) == 60
    db_module.set_setting(conn, uid, "session_cap_min", "9999")
    assert session_cap_min(conn, uid) == SESSION_CAP_CEIL_MIN
    db_module.set_setting(conn, uid, "session_cap_min", "garbage")
    assert session_cap_min(conn, uid) == 30

    conn.executemany(
        "INSERT INTO resting_heart_rate VALUES (?, ?, ?, ?, ?)",
        [
            (f"r{i}", uid, f"2026-06-{i:02d}T06:00:00+00:00",
             f"2026-06-{i:02d}", 55 + (i % 3))
            for i in range(17, 31)
        ],
    )
    conn.commit()
    baseline = rhr_baseline(conn, uid, "2026-07-01")
    assert baseline is not None and 55 <= baseline <= 58
    assert rhr_baseline(conn, other_uid, "2026-07-01") is None  # isolated

    # Illness watch: independent baseline (2026-07-06..07-19, flat
    # 56 bpm) so "2026-07-20" onward has a clean, unambiguous 56 bpm
    # reference distinct from the 2026-07-01 fixture above.
    conn.executemany(
        "INSERT INTO resting_heart_rate VALUES (?, ?, ?, ?, ?)",
        [
            (f"ib{i}", uid, f"2026-07-{i:02d}T06:00:00+00:00",
             f"2026-07-{i:02d}", 56)
            for i in range(6, 20)
        ],
    )
    conn.commit()
    assert rhr_baseline(conn, uid, "2026-07-20") == 56.0

    # A single bad signal, even a large one, is not "distressed" --
    # ILLNESS_MIN_SIGNALS(2) exists specifically to filter this out.
    conn.execute(
        "INSERT INTO resting_heart_rate VALUES ('is1', ?, "
        "'2026-07-20T06:00:00+00:00', '2026-07-20', 70)", (uid,),
    )  # +14 vs baseline, on its own
    conn.commit()
    only_one = illness_watch(conn, uid, "2026-07-20")
    assert only_one["suspected"] is False, only_one
    assert only_one["distressed_days"] == 0, only_one

    # Two signals together make that same day count...
    conn.execute(
        "INSERT INTO garmin_hrv (user_id, local_date, status) "
        "VALUES (?, '2026-07-20', 'LOW')", (uid,),
    )
    conn.commit()
    two_signals = illness_watch(conn, uid, "2026-07-20")
    assert two_signals["distressed_days"] == 1, two_signals
    assert two_signals["suspected"] is False, two_signals  # only 1 day

    # ...and a second distressed day (today) tips it to suspected --
    # ILLNESS_MIN_DISTRESSED_DAYS(2) of the last ILLNESS_WATCH_DAYS(3).
    conn.execute(
        "INSERT INTO resting_heart_rate VALUES ('is2', ?, "
        "'2026-07-21T06:00:00+00:00', '2026-07-21', 68)", (uid,),
    )
    conn.execute(
        "INSERT INTO garmin_hrv (user_id, local_date, status) "
        "VALUES (?, '2026-07-21', 'LOW')", (uid,),
    )
    conn.execute(
        "INSERT INTO garmin_training_readiness (user_id, local_date, "
        "score) VALUES (?, '2026-07-21', 30)", (uid,),
    )
    conn.commit()
    suspected = illness_watch(conn, uid, "2026-07-21")
    assert suspected["suspected"] is True, suspected
    assert suspected["distressed_days"] == 2, suspected
    assert "VFC basse" in suspected["signals"], suspected
    assert any("recuperation" in s for s in suspected["signals"]), suspected
    assert any("FC repos" in s for s in suspected["signals"]), suspected
    # A clean day (07-19, part of the baseline itself) contributes no
    # signals and isn't in the union.
    assert not any("07-19" in s for s in suspected["signals"])

    # Isolation: another user's readings never feed uid's watch.
    conn.execute(
        "INSERT INTO resting_heart_rate VALUES ('is-other', ?, "
        "'2026-07-21T06:00:00+00:00', '2026-07-21', 120)", (other_uid,),
    )
    conn.commit()
    assert illness_watch(conn, other_uid, "2026-07-21") == {
        "suspected": False, "distressed_days": 0, "signals": [],
    }

    # --- plan_day: what the person is actually told to do ------------
    def _fresh():
        conn_ = db_module.connect(Path(tempfile.mkdtemp()) / "plan.db")
        db_module.init_db(conn_)
        return conn_, db_module.create_user(conn_, "planner", "password1234")

    def _template(conn_, uid_, weekday):
        return schedule_for_user(conn_, uid_)[weekday]

    RED = {"sleep_score": 40}
    GREEN = {"sleep_score": 90, "hrv_status": "BALANCED"}
    HEALTHY = {"suspected": False, "distressed_days": 0, "signals": []}
    D = "2026-10-05"  # a Monday: treadmill

    # THE REPORTED CASE. Level 0, a red morning: the old flow could only
    # hand back the level-0 session -- 5.5 km/h at 12% incline -- again
    # and again. It must become an easy flat walk.
    pc, pu = _fresh()
    plan = plan_day(pc, pu, D, _template(pc, pu, 0), RED, HEALTHY)
    assert get_level(pc, pu, "treadmill") == 0
    assert plan["tier"] == TIER_RECOVERY, plan
    assert plan["values"] == RECOVERY_VALUES, plan
    assert plan["values"]["incline_pct"] == 0
    assert plan["description"].startswith("RECUPERATION"), plan
    assert "niveau deja au minimum" in plan["description"], plan
    payload = session_payload(plan, _template(pc, pu, 0))
    assert payload["type"] == "recovery", payload
    assert payload["scheduled_type"] == "treadmill", payload
    assert payload["tier_reasons"], payload

    # A red day with room left to lighten still just lightens...
    pc, pu = _fresh()
    set_level(pc, pu, "treadmill", 3)
    plan = plan_day(pc, pu, D, _template(pc, pu, 0), RED, HEALTHY)
    assert plan["tier"] == TIER_TRAIN and plan["level"] == 2, plan
    assert plan["description"].startswith("Niveau 2"), plan
    # ...a green one progresses...
    pc, pu = _fresh()
    set_level(pc, pu, "treadmill", 3)
    plan = plan_day(pc, pu, D, _template(pc, pu, 0), GREEN, HEALTHY)
    assert plan["tier"] == TIER_TRAIN and plan["level"] == 4, plan
    # Explainable progression: the reason names the deciding signal.
    assert "jour vert" in plan["level_reason"], plan
    assert "3 -> 4" in plan["level_reason"], plan
    # Only on work done and handled: last session too hard -> held.
    pc, pu = _fresh()
    set_level(pc, pu, "treadmill", 3)
    pc.execute(
        "INSERT INTO session_feedback (user_id, local_date, session_type, "
        "rating, created_at) VALUES (?, '2026-10-02', 'treadmill', 'hard', "
        "'x')", (pu,))
    pc.commit()
    held = plan_day(pc, pu, D, _template(pc, pu, 0), GREEN, HEALTHY)
    assert held["status"] == "green" and held["level"] == 3, held
    assert "trop dure" in held["level_reason"], held
    # ...and a skipped last session of the type holds it too.
    pc, pu = _fresh()
    set_level(pc, pu, "treadmill", 3)
    pc.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message, tier) VALUES (?, 'x', '2026-10-02', "
        "'green', 'treadmill', 3, 'm', 'train')", (pu,))
    pc.commit()
    # Nobody who never records a workout is held by this rule...
    untracked = plan_day(pc, pu, D, _template(pc, pu, 0), GREEN, HEALTHY)
    assert untracked["level"] == 4, untracked
    # ...but someone who does (a walk logged on another day) is.
    set_level(pc, pu, "treadmill", 3)
    pc.execute(
        "INSERT INTO exercise_sessions (uuid, user_id, start_utc, end_utc, "
        "local_date, exercise_type) VALUES ('w0', ?, "
        "'2026-09-30T18:00:00+00:00', '2026-09-30T18:30:00+00:00', "
        "'2026-09-30', 79)", (pu,))
    pc.commit()
    skipped = plan_day(pc, pu, D, _template(pc, pu, 0), GREEN, HEALTHY)
    assert skipped["level"] == 3, skipped
    assert "pas ete faite" in skipped["level_reason"], skipped
    # A rating given for that day counts as done, logged or not.
    pc.execute(
        "INSERT INTO session_feedback (user_id, local_date, session_type, "
        "rating, created_at) VALUES (?, '2026-10-02', 'treadmill', 'right', "
        "'x')", (pu,))
    pc.commit()
    set_level(pc, pu, "treadmill", 3)
    rated = plan_day(pc, pu, D, _template(pc, pu, 0), GREEN, HEALTHY)
    assert rated["level"] == 4, rated
    pc.execute("DELETE FROM session_feedback WHERE user_id = ?", (pu,))
    pc.commit()
    # Done (an exercise session that day) -> it progresses again.
    pc.execute(
        "INSERT INTO exercise_sessions (uuid, user_id, start_utc, end_utc, "
        "local_date, exercise_type) VALUES ('t1', ?, "
        "'2026-10-02T18:00:00+00:00', '2026-10-02T18:25:00+00:00', "
        "'2026-10-02', 57)", (pu,))
    pc.commit()
    set_level(pc, pu, "treadmill", 3)
    done = plan_day(pc, pu, D, _template(pc, pu, 0), GREEN, HEALTHY)
    assert done["level"] == 4, done
    # Tired legs from a long hike yesterday hold a leg day (yellow),
    # with the muscles and the cause in the reason; no red.
    pc, pu = _fresh()
    set_level(pc, pu, "lower_body", 5)
    pc.execute(
        "INSERT INTO exercise_sessions (uuid, user_id, start_utc, end_utc, "
        "local_date, exercise_type) VALUES ('h1', ?, "
        "'2026-10-05T08:00:00+00:00', '2026-10-05T11:30:00+00:00', "
        "'2026-10-05', 37)", (pu,))
    pc.commit()
    legs = plan_day(pc, pu, "2026-10-06", _template(pc, pu, 1), GREEN,
                    HEALTHY)
    assert legs["status"] == "yellow" and legs["level"] == 5, legs
    assert "quadriceps" in legs["tired_muscles"], legs
    assert "randonnee" in legs["tired_muscles"], legs
    assert "fatigue musculaire" in legs["level_reason"], legs
    assert session_payload(legs, _template(pc, pu, 1))["tired_muscles"]
    # The same hike does not touch an upper-body day.
    arms = plan_day(pc, pu, "2026-10-08", _template(pc, pu, 3), GREEN,
                    HEALTHY)
    assert arms["tired_muscles"] is None and arms["status"] == "green"

    # ...and the second red day in a row is a walk even with room left.
    pc, pu = _fresh()
    set_level(pc, pu, "treadmill", 3)
    pc.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message) VALUES (?, '2026-10-04T06:00:00', "
        "'2026-10-04', 'red', 'upper_body', 3, 'm')", (pu,))
    pc.commit()
    plan = plan_day(pc, pu, D, _template(pc, pu, 0), RED, HEALTHY)
    assert plan["tier"] == TIER_RECOVERY, plan
    assert "2e jour rouge" in plan["tier_reasons"][0], plan

    # Declared illness: no session, EVERY type cut, and the level
    # system untouched -- no red counted for a session not happening.
    pc, pu = _fresh()
    for st in SESSION_LABEL_FR:
        set_level(pc, pu, st, 6)
    set_red_streak(pc, pu, "treadmill", 1)
    set_sick(pc, pu, D, 3)
    plan = plan_day(pc, pu, D, _template(pc, pu, 0), RED, HEALTHY)
    assert plan["tier"] == TIER_REST, plan
    assert plan["values"] == {} and plan["description"].startswith("REPOS")
    assert "tu es declare malade" in plan["description"], plan
    assert set(plan["illness_deload"]) == set(SESSION_LABEL_FR), plan
    for st in SESSION_LABEL_FR:
        assert get_level(pc, pu, st) == 4, st  # one cut of 2, not two
    assert get_red_streak(pc, pu, "treadmill") == 0  # reset by the cut...
    rest_payload = session_payload(plan, _template(pc, pu, 0))
    assert rest_payload["type"] == "rest", rest_payload
    assert rest_payload["scheduled_title"] == _template(pc, pu, 0)["title"]
    # ...and a second sick morning cuts nothing further.
    plan2 = plan_day(
        pc, pu, "2026-10-06", _template(pc, pu, 1), RED, HEALTHY,
    )
    assert plan2["tier"] == TIER_REST and plan2["illness_deload"] == {}
    for st in SESSION_LABEL_FR:
        assert get_level(pc, pu, st) == 4, st

    # Coming back: the first day after a rest is a walk, the next
    # trains again -- at the reduced level, held through the deload.
    pc.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message, tier) VALUES (?, "
        "'2026-10-07T06:00:00', '2026-10-07', 'red', 'upper_body', 4, "
        "'m', 'rest')", (pu,))
    pc.commit()
    set_sick(pc, pu, D, 0)  # recovered
    back = plan_day(pc, pu, "2026-10-08", _template(pc, pu, 3), GREEN, HEALTHY)
    assert back["tier"] == TIER_RECOVERY, back
    assert "reprise douce" in back["tier_reasons"][0], back
    assert get_level(pc, pu, "upper_body") == 4  # neither cut nor raised
    pc.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message, tier) VALUES (?, "
        "'2026-10-08T06:00:00', '2026-10-08', 'green', 'upper_body', 4, "
        "'m', 'recovery')", (pu,))
    pc.commit()
    train = plan_day(pc, pu, "2026-10-09", _template(pc, pu, 4), GREEN, HEALTHY)
    assert train["tier"] == TIER_TRAIN and train["level"] == 4, train
    assert train["in_deload"] is True, train

    # The sensors' own reading rests the day too, and an off-system day
    # (Sunday's bike ride) is rested like any other.
    pc, pu = _fresh()
    watched = {"suspected": True, "distressed_days": 2,
               "signals": ["VFC basse", "FC repos +9 vs base"]}
    sunday = plan_day(pc, pu, "2026-10-11", _template(pc, pu, 6), {}, watched)
    assert sunday["tier"] == TIER_REST and sunday["status"] is None, sunday
    assert "VFC basse" in sunday["description"], sunday
    sunday_payload = session_payload(sunday, _template(pc, pu, 6))
    assert sunday_payload["type"] == "rest", sunday_payload
    assert sunday_payload["scheduled_type"] is None, sunday_payload
    # Healthy, the same day is the plain off-system one.
    pc, pu = _fresh()
    free = plan_day(pc, pu, "2026-10-11", _template(pc, pu, 6), {}, HEALTHY)
    free_payload = session_payload(free, _template(pc, pu, 6))
    assert free["tier"] == TIER_TRAIN and free["description"] is None, free
    assert free_payload["type"] == "off_system", free_payload
    assert "hors systeme de niveaux" in free_payload["note"], free_payload

    # The deload note still rides on an ordinary training day.
    pc, pu = _fresh()
    set_level(pc, pu, "lower_body", 6)
    set_red_streak(pc, pu, "lower_body", 2)
    for lang, expected in (("fr", "SEMAINE DE DELOAD (3 rouges d'affilee)"),
                           ("en", "DELOAD WEEK (3 reds in a row)")):
        pc2, pu2 = _fresh()
        set_level(pc2, pu2, "lower_body", 6)
        set_red_streak(pc2, pu2, "lower_body", 2)
        plan = plan_day(
            pc2, pu2, "2026-10-06", _template(pc2, pu2, 1), RED, HEALTHY,
            language=lang,
        )
        assert plan["deload_triggered"] is True, plan
        assert expected in plan["description"], plan["description"]

    # plan_from_log gives back the very same day -- the dashboard and a
    # regenerated message must describe what the morning decided, never
    # re-run it (that would move levels and count red days again).
    pc, pu = _fresh()
    set_sick(pc, pu, D, 2)
    morning = plan_day(pc, pu, D, _template(pc, pu, 0), RED, HEALTHY)
    pc.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message, tier, tier_reason) VALUES (?, "
        "'2026-10-05T06:00:00', ?, ?, ?, ?, 'm', ?, ?)",
        (pu, D, morning["status"], morning["session_type"],
         morning["level"], morning["tier"], morning["tier_reasons"][0]))
    pc.commit()
    entry = pc.execute("SELECT * FROM coach_log").fetchone()
    levels_before = {st: get_level(pc, pu, st) for st in SESSION_LABEL_FR}
    rebuilt = plan_from_log(pc, pu, D, _template(pc, pu, 0), entry)
    assert rebuilt["tier"] == TIER_REST, rebuilt
    assert rebuilt["description"] == morning["description"], rebuilt
    assert levels_before == {
        st: get_level(pc, pu, st) for st in SESSION_LABEL_FR
    }, "rebuilding a plan must change nothing"
    # A pre-tier row (NULL tier) reads as an ordinary training day.
    pc.execute("UPDATE coach_log SET tier = NULL, tier_reason = NULL")
    pc.commit()
    old_entry = pc.execute("SELECT * FROM coach_log").fetchone()
    legacy_plan = plan_from_log(pc, pu, D, _template(pc, pu, 0), old_entry)
    assert legacy_plan["tier"] == TIER_TRAIN, legacy_plan
    assert legacy_plan["description"].startswith("Niveau"), legacy_plan

    # --- Wider illness watch: supporting signals and the fast path ---
    watch_conn = db_module.connect(Path(tempfile.mkdtemp()) / "watch.db")
    db_module.init_db(watch_conn)
    wuid = db_module.create_user(watch_conn, "watcher", "password1234")
    watch_conn.executemany(
        "INSERT INTO resting_heart_rate VALUES (?, ?, ?, ?, 56)",
        [(f"wb{i}", wuid, f"2026-11-{i:02d}T06:00:00+00:00",
          f"2026-11-{i:02d}") for i in range(1, 15)],
    )
    # A waking respiration baseline of 14 brpm over five nights.
    watch_conn.executemany(
        "INSERT INTO garmin_respiration (user_id, local_date, "
        "avg_waking) VALUES (?, ?, 14.0)",
        [(wuid, f"2026-11-{i:02d}") for i in range(10, 15)],
    )
    watch_conn.commit()
    assert respiration_baseline(watch_conn, wuid, "2026-11-15") == 14.0
    assert respiration_baseline(watch_conn, wuid, "2026-11-12") is None  # 2

    def _day(day, hrv=None, battery=None, stress=None, breathing=None,
             rhr=None):
        if hrv:
            watch_conn.execute(
                "INSERT INTO garmin_hrv (user_id, local_date, status) "
                "VALUES (?, ?, ?)", (wuid, day, hrv))
        if battery is not None:
            watch_conn.execute(
                "INSERT INTO garmin_body_battery (user_id, local_date, "
                "highest) VALUES (?, ?, ?)", (wuid, day, battery))
        if stress is not None:
            watch_conn.execute(
                "INSERT INTO garmin_stress (user_id, local_date, "
                "avg_level) VALUES (?, ?, ?)", (wuid, day, stress))
        if breathing is not None:
            watch_conn.execute(
                "INSERT INTO garmin_respiration (user_id, local_date, "
                "avg_waking) VALUES (?, ?, ?)", (wuid, day, breathing))
        if rhr is not None:
            watch_conn.execute(
                "INSERT INTO resting_heart_rate VALUES (?, ?, ?, ?, ?)",
                (f"wr-{day}", wuid, f"{day}T06:00:00+00:00", day, rhr))
        watch_conn.commit()

    # Supporting signals alone -- a drained battery, high stress, fast
    # breathing, three days running -- are a hard week, not an illness.
    for day in ("2026-11-15", "2026-11-16", "2026-11-17"):
        _day(day, battery=20, stress=75, breathing=18.0)
    drained = illness_watch(watch_conn, wuid, "2026-11-17")
    assert drained == {
        "suspected": False, "distressed_days": 0, "signals": [],
    }, drained

    # Next to a core signal they count: HRV LOW + a battery that never
    # filled is a distressed day, and two of them suspect an illness.
    _day("2026-11-18", hrv="LOW", battery=25)
    _day("2026-11-19", hrv="LOW", battery=30)
    _day("2026-11-20")  # a clean day in the window
    pair = illness_watch(watch_conn, wuid, "2026-11-19")
    assert pair["suspected"] is True and pair["distressed_days"] == 2, pair
    assert "VFC basse" in pair["signals"], pair
    assert "batterie corporelle 25" in pair["signals"], pair

    # Fourteen-day respiration baseline filler for the later dates.
    watch_conn.executemany(
        "INSERT INTO garmin_respiration (user_id, local_date, "
        "avg_waking) VALUES (?, ?, 14.0)",
        [(wuid, f"2026-11-{i:02d}") for i in range(20, 27)],
    )
    watch_conn.commit()

    # ...and a resting-HR baseline near the fast-path date (14-day
    # window), since rhr_baseline needs three readings inside it.
    watch_conn.executemany(
        "INSERT INTO resting_heart_rate VALUES (?, ?, ?, ?, 56)",
        [(f"wf{n}", wuid, f"{d}T06:00:00+00:00", d) for n, d in enumerate(
            [f"2026-11-{i:02d}" for i in range(21, 31)]
            + [f"2026-12-{i:02d}" for i in range(1, 5)])],
    )
    watch_conn.commit()

    # Stress bounds: 50 is still Garmin's "low" band, 51 is not.
    _day("2026-11-24", hrv="LOW", stress=50)
    assert illness_watch(watch_conn, wuid, "2026-11-24")["distressed_days"] == 0
    watch_conn.execute(
        "UPDATE garmin_stress SET avg_level = 51 WHERE local_date = "
        "'2026-11-24'")
    watch_conn.commit()
    assert illness_watch(watch_conn, wuid, "2026-11-24")["distressed_days"] == 1

    # Respiration only counts when it clears the personal baseline by
    # the threshold -- checked well on both sides of it, computed
    # rather than hard-coded so float rounding cannot make it flaky.
    base = respiration_baseline(watch_conn, wuid, "2026-11-27")
    assert base is not None
    _day("2026-11-27", hrv="LOW", breathing=base + RESPIRATION_RISE_BRPM - 0.5)
    assert illness_watch(watch_conn, wuid, "2026-11-27")["distressed_days"] == 0
    watch_conn.execute(
        "UPDATE garmin_respiration SET avg_waking = ? WHERE local_date = "
        "'2026-11-27'", (base + RESPIRATION_RISE_BRPM + 0.5,))
    watch_conn.commit()
    risen = illness_watch(watch_conn, wuid, "2026-11-27")
    assert risen["distressed_days"] == 1, risen

    # The fast path: three signals on the morning itself, one of them
    # core, acts at once -- no second day needed. Two signals on a
    # single morning still wait for confirmation.
    _day("2026-11-30", hrv="LOW", battery=22)
    waiting = illness_watch(watch_conn, wuid, "2026-11-30")
    assert waiting["suspected"] is False, waiting
    assert waiting["distressed_days"] == 1, waiting
    _day("2026-12-05", hrv="LOW", battery=22, rhr=66)
    fast = illness_watch(watch_conn, wuid, "2026-12-05")
    assert fast["suspected"] is True, fast
    assert fast["distressed_days"] == 1, fast  # one day, but enough
    assert "FC repos +10 vs base" in fast["signals"], fast
    # Three SUPPORTING signals and no core one never take the fast path.
    _day("2026-12-10", battery=20, stress=80, breathing=base + 4)
    assert illness_watch(watch_conn, wuid, "2026-12-10")["suspected"] is False

    # --- Tiers: rest and recovery sit below the level floor ---------
    tier_conn = db_module.connect(Path(tempfile.mkdtemp()) / "tiers.db")
    db_module.init_db(tier_conn)
    tuid = db_module.create_user(tier_conn, "tiers", "password1234")

    def _log(day, status, tier=None, created=None):
        tier_conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, "
            "status, session_type, level, message, tier) VALUES "
            "(?, ?, ?, ?, 'treadmill', 3, 'm', ?)",
            (tuid, created or f"{day}T06:00:00+00:00", day, status, tier),
        )
        tier_conn.commit()

    TODAY = "2026-10-02"
    no_illness = {"suspected": False, "distressed_days": 0, "signals": []}

    # The reported failure: nothing changes once the level is at its
    # floor. Red at level 0 must now become a recovery walk...
    floor = tier_after_guardrail("red", 0, 0)
    assert floor["tier"] == TIER_RECOVERY, floor
    assert "niveau deja au minimum" in floor["reasons"][0], floor
    # ...a red day with room left to lighten keeps the normal flow...
    assert tier_after_guardrail("red", 3, 0) is None
    # ...until it is the second red day in a row, whatever the level.
    second = tier_after_guardrail("red", 3, 1)
    assert second["tier"] == TIER_RECOVERY, second
    assert "2e jour rouge" in second["reasons"][0], second
    assert "3e jour rouge" in tier_after_guardrail("red", 5, 2)["reasons"][0]
    assert tier_after_guardrail("yellow", 0, 4) is None  # not red
    assert tier_after_guardrail(None, None, 4) is None  # off-system day

    # consecutive_red_days: per day across all types, a gap ends the
    # run, and the latest row for a date wins.
    assert consecutive_red_days(tier_conn, tuid, TODAY) == 0
    _log("2026-10-01", "red")
    _log("2026-09-30", "red")
    _log("2026-09-29", "yellow")
    _log("2026-09-28", "red")
    assert consecutive_red_days(tier_conn, tuid, TODAY) == 2
    _log("2026-10-01", "green", created="2026-10-01T09:00:00+00:00")
    assert consecutive_red_days(tier_conn, tuid, TODAY) == 0  # re-logged
    tier_conn.execute("DELETE FROM coach_log WHERE user_id = ?", (tuid,))
    _log("2026-10-01", "red")
    _log("2026-09-29", "red")  # 09-30 missing: the run stops at one
    assert consecutive_red_days(tier_conn, tuid, TODAY) == 1
    tier_conn.execute("DELETE FROM coach_log WHERE user_id = ?", (tuid,))
    tier_conn.commit()

    # Nothing wrong: the normal level-based flow carries on.
    assert previous_tier(tier_conn, tuid, TODAY) == TIER_TRAIN
    assert tier_before_guardrail(tier_conn, tuid, TODAY, no_illness) is None

    # A declared illness wins, through the last day it covers.
    assert set_sick(tier_conn, tuid, TODAY, 3) == "2026-10-04"
    for day, sick in (("2026-10-02", True), ("2026-10-04", True),
                      ("2026-10-05", False)):
        assert is_self_reported_sick(tier_conn, tuid, day) is sick, day
    declared = tier_before_guardrail(tier_conn, tuid, TODAY, no_illness)
    assert declared["tier"] == TIER_REST, declared
    assert "2026-10-04" in declared["reasons"][0], declared
    # Past its date it no longer applies, and a bad value is ignored
    # rather than crashing the morning run.
    assert tier_before_guardrail(
        tier_conn, tuid, "2026-10-05", no_illness,
    ) is None
    db_module.set_setting(tier_conn, tuid, "sick_until", "tomorrow-ish")
    assert sick_until(tier_conn, tuid) is None
    assert tier_before_guardrail(tier_conn, tuid, TODAY, no_illness) is None
    assert set_sick(tier_conn, tuid, TODAY, 0) is None  # cleared
    assert sick_until(tier_conn, tuid) is None

    # The sensors' own reading of an illness rests the day too, and
    # says why.
    watched = {
        "suspected": True, "distressed_days": 2,
        "signals": ["VFC basse", "FC repos +9 vs base", "batterie 31"],
    }
    by_signals = tier_before_guardrail(tier_conn, tuid, TODAY, watched)
    assert by_signals["tier"] == TIER_REST, by_signals
    assert "VFC basse" in by_signals["reasons"][0], by_signals

    # The first day after a rest is a walk, not the full session again.
    _log("2026-10-01", "red", tier=TIER_REST)
    assert previous_tier(tier_conn, tuid, TODAY) == TIER_REST
    back = tier_before_guardrail(tier_conn, tuid, TODAY, no_illness)
    assert back["tier"] == TIER_RECOVERY, back
    # ...and after that walk, training resumes (illness already cut
    # every level by apply_illness_deload).
    tier_conn.execute("UPDATE coach_log SET tier = 'recovery'")
    tier_conn.commit()
    assert tier_before_guardrail(tier_conn, tuid, TODAY, no_illness) is None

    # Descriptions name the session replaced and the reason.
    rest_text = describe_tier_fr(
        TIER_REST, "Tapis - marche rapide inclinee", ["malade"],
    )
    assert rest_text.startswith("REPOS") and "Tapis" in rest_text, rest_text
    walk_text = describe_tier_fr(
        TIER_RECOVERY, "Muscu bas du corps", ["2e jour rouge de suite"],
        RECOVERY_VALUES,
    )
    assert "4.5 km/h" in walk_text and "inclinaison 0%" in walk_text, walk_text
    assert "Muscu bas du corps" in walk_text, walk_text

    # apply_illness_deload cuts every known type in one pass...
    for session_type in SESSION_LABEL_FR:
        set_level(conn, uid, session_type, 6)
    illness_cut = apply_illness_deload(conn, uid, "2026-07-21")
    assert set(illness_cut) == set(SESSION_LABEL_FR), illness_cut
    for session_type in SESSION_LABEL_FR:
        assert get_level(conn, uid, session_type) == 4, session_type  # 6-2
        assert illness_cut[session_type]["trigger"] == "illness"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM deload_events WHERE user_id = ? AND "
        "trigger = 'illness'", (uid,),
    ).fetchone()["n"] == len(SESSION_LABEL_FR)

    # ...and is idempotent: calling it again the next morning, still
    # inside the same 7-day window, cuts nothing further.
    again_cut = apply_illness_deload(conn, uid, "2026-07-22")
    assert again_cut == {}, again_cut
    for session_type in SESSION_LABEL_FR:
        assert get_level(conn, uid, session_type) == 4, session_type

    # A type that had ALREADY deloaded for its own red streak, before
    # illness_watch ever fired, is left alone rather than cut twice.
    set_level(conn, uid, "treadmill", 6)
    set_deload_until(conn, uid, "treadmill", "2026-08-01")
    partial_cut = apply_illness_deload(conn, uid, "2026-07-25")
    assert "treadmill" not in partial_cut, partial_cut
    assert get_level(conn, uid, "treadmill") == 6  # untouched

    # Deload guardrail: 3 reds in a row triggers a forced cut + window,
    # not just the normal -1/day.
    set_level(conn, uid, "lower_body", 6)
    d0, d1, d2 = "2026-08-01", "2026-08-03", "2026-08-05"
    r1 = apply_deload_guardrail(conn, uid, "lower_body", "red", d0)
    assert r1 == {"level": 5, "in_deload": False, "deload_triggered": False}
    r2 = apply_deload_guardrail(conn, uid, "lower_body", "red", d1)
    assert r2 == {"level": 4, "in_deload": False, "deload_triggered": False}
    r3 = apply_deload_guardrail(conn, uid, "lower_body", "red", d2)
    assert r3["deload_triggered"] is True
    assert r3["level"] == 2  # 4 - DELOAD_LEVEL_CUT(2)
    assert r3["trigger"] == "red_streak"
    assert get_red_streak(conn, uid, "lower_body") == 0
    deload_until = get_deload_until(conn, uid, "lower_body")
    assert deload_until == (
        dt.date.fromisoformat(d2) + dt.timedelta(days=DELOAD_DAYS)
    ).isoformat()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM deload_events WHERE user_id = ? AND "
        "session_type = 'lower_body' AND trigger = 'red_streak'", (uid,),
    ).fetchone()["n"] == 1

    # A green day mid-window doesn't bump the level -- deload holds.
    held = apply_deload_guardrail(
        conn, uid, "lower_body", "green", (
            dt.date.fromisoformat(d2) + dt.timedelta(days=2)
        ).isoformat(),
    )
    assert held == {"level": 2, "in_deload": True, "deload_triggered": False}

    # Once the window passes, normal adjustment resumes.
    after = apply_deload_guardrail(
        conn, uid, "lower_body", "green", deload_until,
    )
    resumed_date = (
        dt.date.fromisoformat(deload_until) + dt.timedelta(days=1)
    ).isoformat()
    after2 = apply_deload_guardrail(
        conn, uid, "lower_body", "green", resumed_date,
    )
    assert after2["in_deload"] is False
    assert after2["level"] == after["level"] + 1
    assert get_deload_until(conn, uid, "lower_body") is None

    # TSB-triggered deload: fires on a SINGLE critical reading, no
    # streak needed, even on a non-red (yellow) day.
    set_level(conn, uid, "calisthenics", 6)
    tsb_day = "2026-09-01"
    no_trigger = apply_deload_guardrail(
        conn, uid, "calisthenics", "yellow", tsb_day, tsb=-5.0,
    )
    assert no_trigger["deload_triggered"] is False  # above threshold
    tsb_r = apply_deload_guardrail(
        conn, uid, "calisthenics", "yellow", tsb_day,
        tsb=TSB_DELOAD_THRESHOLD - 1,
    )
    assert tsb_r["deload_triggered"] is True
    assert tsb_r["trigger"] == "tsb"
    assert tsb_r["level"] == 4  # 6 - DELOAD_LEVEL_CUT(2)
    assert conn.execute(
        "SELECT trigger FROM deload_events WHERE user_id = ? AND "
        "session_type = 'calisthenics' AND triggered_at = ?",
        (uid, tsb_day),
    ).fetchone()["trigger"] == "tsb"
    # Already in deload -> a second critical reading doesn't re-cut.
    again = apply_deload_guardrail(
        conn, uid, "calisthenics", "yellow",
        (dt.date.fromisoformat(tsb_day) + dt.timedelta(days=1)).isoformat(),
        tsb=TSB_DELOAD_THRESHOLD - 1,
    )
    assert again["deload_triggered"] is False
    assert again["level"] == 4

    print("training.py: all checks passed")
