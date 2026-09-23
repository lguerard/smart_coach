#!/usr/bin/env python3
"""Weekly trend / goal-tracking layer.

No garmin-coach equivalent -- this is what turns the daily readiness
bot into an adaptive body-recomposition coach: weight/body-fat trend,
calorie balance, protein-vs-target, and plateau/slowdown detection.
Same split as training.py: these are deterministic computations, the
LLM (llm.py) only narrates around numbers already computed here.

Multi-user: every function takes ``user_id`` and scopes its queries to
that person's rows/settings only.
"""

import datetime as dt
import sqlite3
from typing import Optional

import db
import metrics
from ingest.parse_health_connect import EXERCISE_TYPE_LABELS

# Tunable thresholds -- starting points, same "retune against real
# weeks" posture as training.py's constants.
PLATEAU_WEIGHT_DELTA_KG = 0.2  # "flat" if |delta| below this
PLATEAU_MIN_DAYS = 10
REAL_DEFICIT_KCAL = -150  # a deficit this size should show on the scale


def _weight_like_trend(
    conn: sqlite3.Connection, user_id: int, table: str, value_col: str,
    end_date: str, days: int,
) -> dict:
    """Rolling-average trend for a weight-shaped point table.

    Compares the average of readings in the most recent half of the
    window to the average in the earlier half.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        table (str): ``weight`` or ``body_fat``.
        value_col (str): ``kg`` or ``percentage``.
        end_date (str): ISO local date, window end.
        days (int): Total window length.

    Returns:
        dict: ``current_avg``, ``past_avg``, ``delta`` (current minus
        past) and ``current_days``/``past_days`` (how many distinct
        dates each average rests on), or ``{}`` if either half is
        empty.
    """
    # Two equal, non-overlapping halves of days//2 dates each (BETWEEN
    # is inclusive on both ends, so bounds are half-1 / half / 2*half-1
    # days back -- a naive days//2 / days split double-counts the mid
    # date and spans days+1 dates).
    end = dt.date.fromisoformat(end_date)
    half = days // 2
    current_lo = (end - dt.timedelta(days=half - 1)).isoformat()
    past_hi = (end - dt.timedelta(days=half)).isoformat()
    start = (end - dt.timedelta(days=2 * half - 1)).isoformat()

    def avg(lo: str, hi: str) -> tuple[Optional[float], int]:
        # Averaged per date first, then across dates. These tables
        # are multi-source -- the scale writes to Garmin and to
        # Health Connect, and both land here -- so a day carrying two
        # readings would otherwise weigh twice as much in the half as
        # a day carrying one, tilting the trend toward whichever days
        # happened to sync twice.
        row = conn.execute(
            f"SELECT AVG(daily) AS avg, COUNT(*) AS n FROM ("
            f"SELECT AVG({value_col}) AS daily FROM {table} "
            "WHERE user_id = ? AND local_date BETWEEN ? AND ? "
            "GROUP BY local_date)",
            (user_id, lo, hi),
        ).fetchone()
        return (row["avg"], row["n"]) if row["n"] else (None, 0)

    current_avg, current_days = avg(current_lo, end_date)
    past_avg, past_days = avg(start, past_hi)
    if current_avg is None or past_avg is None:
        return {}
    return {
        "current_avg": round(current_avg, 2),
        "past_avg": round(past_avg, 2),
        "delta": round(current_avg - past_avg, 2),
        "current_days": current_days,
        "past_days": past_days,
    }


def weight_trend(
    conn: sqlite3.Connection, user_id: int, end_date: str, days: int = 14,
) -> dict:
    """Rolling weight trend over the last ``days`` days."""
    return _weight_like_trend(conn, user_id, "weight", "kg", end_date, days)


def body_fat_trend(
    conn: sqlite3.Connection, user_id: int, end_date: str, days: int = 28,
) -> dict:
    """Rolling body-fat-% trend over the last ``days`` days (default 28
    -- body fat % readings are noisier and sparser than daily weigh-ins).
    """
    return _weight_like_trend(
        conn, user_id, "body_fat", "percentage", end_date, days
    )


def lean_mass_trend(
    conn: sqlite3.Connection, user_id: int, end_date: str, days: int = 28,
) -> dict:
    """Rolling lean-body-mass trend (kg) over the last ``days`` days.

    The recomposition truth signal: weight falling while lean mass
    holds or rises means the loss is fat, not muscle.
    """
    return _weight_like_trend(
        conn, user_id, "lean_body_mass", "kg", end_date, days
    )


def estimate_bmr(
    conn: sqlite3.Connection, user_id: int, weight_kg: float,
) -> Optional[float]:
    """Mifflin-St Jeor BMR estimate from settings + current weight.

    Only used as a last-resort fallback when neither a device-reported
    basal_metabolic_rate reading nor a manual override is available.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        weight_kg (float): Current weight.

    Returns:
        float | None: Estimated kcal/day, or ``None`` if height/age/sex
        aren't set in Settings.
    """
    height_cm = db.get_setting(conn, user_id, "height_cm")
    age_years = db.get_setting(conn, user_id, "age_years")
    sex = (db.get_setting(conn, user_id, "sex") or "").strip().upper()
    if not (height_cm and age_years and sex in ("M", "F")):
        return None
    base = 10 * weight_kg + 6.25 * float(height_cm) - 5 * float(age_years)
    return base + 5 if sex == "M" else base - 161


def bmr_for_date(
    conn: sqlite3.Connection, user_id: int, date: str,
    weight_kg: Optional[float],
) -> Optional[float]:
    """Best available BMR for a date: device reading, manual override,
    then formula estimate.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date.
        weight_kg (float | None): Current weight, for the formula
            fallback.

    Returns:
        float | None: kcal/day, or ``None`` if nothing is available.
    """
    row = conn.execute(
        "SELECT kcal_per_day FROM basal_metabolic_rate WHERE user_id = ? "
        "AND local_date <= ? ORDER BY local_date DESC LIMIT 1",
        (user_id, date),
    ).fetchone()
    if row:
        return row["kcal_per_day"]
    manual = db.get_setting(conn, user_id, "bmr_manual_kcal")
    if manual:
        return float(manual)
    if weight_kg is not None:
        return estimate_bmr(conn, user_id, weight_kg)
    return None


def _burn_for_date(
    conn: sqlite3.Connection, user_id: int, date: str,
    weight_kg: Optional[float],
) -> float:
    """Best available total burn (kcal) for a single day.

    Prefers the device's own total_calories_burned reading (already
    BMR + activity, no double-counting); falls back to
    active_calories + a BMR estimate when the device doesn't report a
    TDEE directly.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date.
        weight_kg (float | None): For the BMR fallback formula.

    Returns:
        float: Total burn estimate (0 if nothing is available at all).
    """
    total = conn.execute(
        "SELECT SUM(kcal) AS total FROM total_calories_burned WHERE "
        "user_id = ? AND local_date = ?", (user_id, date),
    ).fetchone()["total"]
    if total is not None:
        return total
    active = conn.execute(
        "SELECT SUM(kcal) AS total FROM active_calories WHERE user_id = ? "
        "AND local_date = ?", (user_id, date),
    ).fetchone()["total"] or 0
    return active + (bmr_for_date(conn, user_id, date, weight_kg) or 0)


def daily_calorie_balance(
    conn: sqlite3.Connection, user_id: int, end_date: str, days: int,
) -> list[dict]:
    """Per-day calorie balance (intake minus burn) for charting.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        end_date (str): ISO local date, window end.
        days (int): Window length.

    Returns:
        list[dict]: ``{"date": ..., "balance_kcal": ...}`` for each
        day that has logged nutrition -- unlogged days are skipped
        rather than assumed to be zero intake.
    """
    start = (
        dt.date.fromisoformat(end_date) - dt.timedelta(days=days - 1)
    ).isoformat()
    weight_row = conn.execute(
        "SELECT kg FROM weight WHERE user_id = ? AND local_date <= ? "
        "ORDER BY local_date DESC LIMIT 1", (user_id, end_date),
    ).fetchone()
    weight_kg = weight_row["kg"] if weight_row else None

    series = []
    day = dt.date.fromisoformat(start)
    while day.isoformat() <= end_date:
        date = day.isoformat()
        intake = conn.execute(
            "SELECT SUM(calories) AS total FROM nutrition WHERE "
            "user_id = ? AND local_date = ?", (user_id, date),
        ).fetchone()["total"]
        if intake is not None:
            burn = _burn_for_date(conn, user_id, date, weight_kg)
            series.append(
                {"date": date, "balance_kcal": round(intake - burn)}
            )
        day += dt.timedelta(days=1)
    return series


def calorie_balance_for_range(
    conn: sqlite3.Connection, user_id: int, end_date: str, days: int = 7,
) -> dict:
    """Average daily calorie balance (intake minus burn) over a window.

    Only averages over days that actually have logged nutrition --
    nutrition data is sparse right now (the user just started
    logging), so this degrades to "not enough data" rather than
    silently treating unlogged days as zero intake.

    Returns:
        dict: ``avg_balance_kcal`` (negative = deficit), ``days_logged``,
        or ``{}`` if no day in the window has nutrition data.
    """
    series = daily_calorie_balance(conn, user_id, end_date, days)
    if not series:
        return {}
    balances = [row["balance_kcal"] for row in series]
    return {
        "avg_balance_kcal": round(sum(balances) / len(balances)),
        "days_logged": len(balances),
    }


def protein_trend(
    conn: sqlite3.Connection, user_id: int, end_date: str, days: int = 7,
) -> dict:
    """Average daily protein intake vs a bodyweight-based target.

    Returns:
        dict: ``avg_protein_g``, ``target_g`` (if weight + a target
        ratio are known), ``days_logged``, or ``{}`` if nothing
        logged in the window.
    """
    start = (
        dt.date.fromisoformat(end_date) - dt.timedelta(days=days - 1)
    ).isoformat()
    rows = conn.execute(
        "SELECT protein_g FROM nutrition WHERE user_id = ? AND "
        "local_date BETWEEN ? AND ? AND protein_g IS NOT NULL",
        (user_id, start, end_date),
    ).fetchall()
    if not rows:
        return {}
    avg_protein = sum(r["protein_g"] for r in rows) / len(rows)

    result = {
        "avg_protein_g": round(avg_protein, 1), "days_logged": len(rows),
    }
    weight_row = conn.execute(
        "SELECT kg FROM weight WHERE user_id = ? AND local_date <= ? "
        "ORDER BY local_date DESC LIMIT 1", (user_id, end_date),
    ).fetchone()
    ratio = db.get_setting(conn, user_id, "protein_target_g_per_kg")
    if weight_row and ratio:
        result["target_g"] = round(weight_row["kg"] * float(ratio), 1)
    return result


def tdee_estimate(
    conn: sqlite3.Connection, user_id: int, date: str, weight_kg: float,
) -> Optional[float]:
    """Estimated total daily energy expenditure, for setting targets.

    Prefers the trailing 7-day average of the device's own
    total_calories_burned; falls back to BMR + trailing average
    active_calories when the device doesn't report a TDEE directly,
    or when Settings' ``trust_device_tdee`` is turned off (see
    :func:`bmr_for_date` and db.DEFAULT_SETTINGS for why that switch
    exists -- the device total is never decomposed to swap out just
    its basal component, so bmr_manual_kcal has no effect at all
    unless this is off). Only looks at days before ``date`` (today
    is always incomplete).

    Returns:
        float | None: kcal/day, or ``None`` if no BMR is derivable
        either (no device reading, no manual override, no
        height/age/sex in Settings).
    """
    start = (
        dt.date.fromisoformat(date) - dt.timedelta(days=7)
    ).isoformat()
    end = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    trust_device = (
        db.get_setting(conn, user_id, "trust_device_tdee") or "1"
    ) == "1"
    avg_total = conn.execute(
        "SELECT AVG(daily_total) AS avg FROM (SELECT local_date, "
        "SUM(kcal) AS daily_total FROM total_calories_burned WHERE "
        "user_id = ? AND local_date BETWEEN ? AND ? GROUP BY "
        "local_date)", (user_id, start, end),
    ).fetchone()["avg"] if trust_device else None
    if avg_total is not None:
        return avg_total
    bmr = bmr_for_date(conn, user_id, date, weight_kg)
    if bmr is None:
        return None
    avg_active = conn.execute(
        "SELECT AVG(daily_total) AS avg FROM (SELECT local_date, "
        "SUM(kcal) AS daily_total FROM active_calories WHERE "
        "user_id = ? AND local_date BETWEEN ? AND ? GROUP BY "
        "local_date)", (user_id, start, end),
    ).fetchone()["avg"] or 0
    return bmr + avg_active


KCAL_PER_KG_BODY_MASS = 7700  # standard energy-balance heuristic


def macro_targets(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Today's calorie/protein/fat/carb/hydration targets.

    Calorie target = TDEE estimate + the daily calorie delta implied
    by ``weekly_weight_change_kg`` (negative = cut, positive = bulk).
    Protein/fat are bodyweight-ratio targets from Settings; carbs fill
    whatever calories remain. All targets are omitted rather than
    guessed when an input (weight, TDEE) is missing.

    Returns:
        dict: Whichever of ``calorie_target_kcal``, ``tdee_estimate_kcal``,
        ``protein_target_g``, ``fat_target_g``, ``carb_target_g``,
        ``hydration_target_ml`` are computable.
    """
    weight_row = conn.execute(
        "SELECT kg FROM weight WHERE user_id = ? AND local_date <= ? "
        "ORDER BY local_date DESC LIMIT 1", (user_id, date),
    ).fetchone()
    if not weight_row:
        return {}
    weight_kg = weight_row["kg"]

    result: dict = {}
    tdee = tdee_estimate(conn, user_id, date, weight_kg)
    calorie_target = None
    if tdee is not None:
        weekly_change = float(
            db.get_setting(conn, user_id, "weekly_weight_change_kg") or 0
        )
        daily_delta = weekly_change * KCAL_PER_KG_BODY_MASS / 7
        calorie_target = round(tdee + daily_delta)
        result["tdee_estimate_kcal"] = round(tdee)
        result["calorie_target_kcal"] = calorie_target

    protein_ratio = db.get_setting(conn, user_id, "protein_target_g_per_kg")
    fat_ratio = db.get_setting(conn, user_id, "fat_target_g_per_kg")
    protein_target = round(weight_kg * float(protein_ratio)) if protein_ratio else None
    fat_target = round(weight_kg * float(fat_ratio)) if fat_ratio else None
    if protein_target is not None:
        result["protein_target_g"] = protein_target
    if fat_target is not None:
        result["fat_target_g"] = fat_target
    if calorie_target is not None and protein_target is not None and fat_target is not None:
        remaining_kcal = calorie_target - protein_target * 4 - fat_target * 9
        result["carb_target_g"] = max(round(remaining_kcal / 4), 0)

    hydration_ratio = db.get_setting(
        conn, user_id, "hydration_target_ml_per_kg",
    )
    if hydration_ratio:
        result["hydration_target_ml"] = round(
            weight_kg * float(hydration_ratio)
        )
    return result


def format_plan_header(targets: dict, language: str) -> str:
    """One-line day budget for the morning ntfy push.

    Deterministic (no LLM): the numbers always reach the phone even
    if the coaching message phrases around them.

    Parameters:
        targets (dict): ``macro_targets`` output.
        language (str): "fr" or "en".

    Returns:
        str: e.g. "Plan du jour: 2100 kcal - P140g L72g G210g -
        2.8L", or "" when nothing is computable.
    """
    parts = []
    if targets.get("calorie_target_kcal"):
        parts.append(f"{targets['calorie_target_kcal']} kcal")
    fat_tag, carb_tag = ("L", "G") if language == "fr" else ("F", "C")
    macros = " ".join(
        f"{tag}{targets[key]}g"
        for tag, key in (("P", "protein_target_g"),
                         (fat_tag, "fat_target_g"),
                         (carb_tag, "carb_target_g"))
        if targets.get(key)
    )
    if macros:
        parts.append(macros)
    if targets.get("hydration_target_ml"):
        parts.append(f"{targets['hydration_target_ml'] / 1000:.1f}L")
    if not parts:
        return ""
    label = "Plan du jour" if language == "fr" else "Today's plan"
    return f"{label}: " + " - ".join(parts)


def garmin_hydration_for_date(
    conn: sqlite3.Connection, user_id: int, day: str,
) -> Optional[float]:
    """Garmin Connect's own hydration total for a date, if logged.

    Kept as its own lookup rather than folded into the HC-derived
    ``hydration`` table (ingest/garmin_api.py explains why) and
    checked ahead of it wherever hydration is read: unlike Health
    Connect's MyFitnessPal-sourced total, which reaches the export a
    full day after that day's meals do, Garmin's API answers for
    ``day`` as it currently stands, no export snapshot in between --
    so when this has a value, none of the missing/fallback handling
    built for the Health Connect path applies at all.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        day (str): ISO local date.

    Returns:
        float | None: Millilitres, or None if Garmin has no reading.
    """
    row = conn.execute(
        "SELECT volume_ml FROM garmin_hydration WHERE user_id = ? "
        "AND local_date = ?", (user_id, day),
    ).fetchone()
    return row["volume_ml"] if row and row["volume_ml"] is not None else None


def intake_for_date(
    conn: sqlite3.Connection, user_id: int, day: str,
) -> dict:
    """What was logged on one local date.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        day (str): ISO local date.

    Returns:
        dict: ``calories_kcal``/``protein_g``/``carbs_g``/``fat_g``
        plus ``entries`` (only if nutrition was logged) and
        ``hydration_ml`` (only if hydration was logged from either
        source -- see :func:`garmin_hydration_for_date`).
    """
    row = conn.execute(
        "SELECT SUM(calories) AS calories, SUM(protein_g) AS protein_g, "
        "SUM(carbs_g) AS carbs_g, SUM(fat_g) AS fat_g, "
        "COUNT(*) AS entries FROM nutrition "
        "WHERE user_id = ? AND local_date = ?", (user_id, day),
    ).fetchone()

    result: dict = {}
    if row["calories"] is not None:
        result.update(
            calories_kcal=round(row["calories"]),
            protein_g=round(row["protein_g"] or 0, 1),
            carbs_g=round(row["carbs_g"] or 0, 1),
            fat_g=round(row["fat_g"] or 0, 1),
            entries=row["entries"],
        )
    garmin_hydration = garmin_hydration_for_date(conn, user_id, day)
    if garmin_hydration is not None:
        result["hydration_ml"] = round(garmin_hydration)
    else:
        hydration = conn.execute(
            "SELECT SUM(volume_ml) AS total FROM hydration WHERE "
            "user_id = ? AND local_date = ?", (user_id, day),
        ).fetchone()["total"]
        if hydration is not None:
            result["hydration_ml"] = round(hydration)
    return result


def yesterday_intake(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> dict:
    """What was actually logged the day before ``date``.

    Returns:
        dict: :func:`intake_for_date` for yesterday.
    """
    return intake_for_date(
        conn, user_id,
        (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat(),
    )


_GAP_FIELDS = [
    ("calories_kcal", "calorie_target_kcal"),
    ("protein_g", "protein_target_g"),
    ("fat_g", "fat_target_g"),
    ("carbs_g", "carb_target_g"),
    ("hydration_ml", "hydration_target_ml"),
]


def _slope_per_week(points: list[tuple[str, float]]) -> Optional[float]:
    """Least-squares change per week across dated values.

    Preferred over subtracting the first reading from the last: the
    endpoints are exactly the two most easily distorted points in a
    noisy series (one heavy dinner, one dehydrated morning), and a
    fit over every point in between is what makes "you are losing
    0.34 kg a week" a statement about the body rather than about two
    particular mornings.

    Parameters:
        points (list[tuple[str, float]]): (ISO date, value), any order.

    Returns:
        float | None: Change per week, or None with fewer than two
        distinct dates (a slope through one point is not a slope).
    """
    if len(points) < 2:
        return None
    origin = dt.date.fromisoformat(min(date for date, _ in points))
    days = [
        (dt.date.fromisoformat(date) - origin).days for date, _ in points
    ]
    values = [value for _, value in points]
    n = len(points)
    mean_day = sum(days) / n
    mean_value = sum(values) / n
    variance = sum((day - mean_day) ** 2 for day in days)
    if variance == 0:  # every reading on the same date
        return None
    covariance = sum(
        (day - mean_day) * (value - mean_value)
        for day, value in zip(days, values)
    )
    return round(covariance / variance * 7, 3)


def _daily_series(
    conn: sqlite3.Connection, user_id: int, table: str, value_col: str,
    start: str, end: str,
) -> list[tuple[str, float]]:
    """One value per date for a weight-shaped table, oldest first.

    Averaged within the date for the same reason the trend halves
    are (see :func:`_weight_like_trend`): these tables are
    multi-source and a day can carry two readings.
    """
    return [
        (row["local_date"], row["value"])
        for row in conn.execute(
            f"SELECT local_date, AVG({value_col}) AS value FROM {table} "
            "WHERE user_id = ? AND local_date BETWEEN ? AND ? "
            "GROUP BY local_date ORDER BY local_date",
            (user_id, start, end),
        )
    ]


def point_progression(
    conn: sqlite3.Connection, user_id: int, table: str, value_col: str,
    date: str, days: int = 28,
) -> dict:
    """Both readings of change a measurement series supports.

    A number can move in two senses at once, and conflating them is
    how a coach ends up saying the opposite of what is happening:
    the step from the last measurement to this one ("+0.4 kg since
    Tuesday", which on a bathroom scale is mostly water and food in
    transit) and the direction across the window ("-0.34 kg a week
    over 28 days", which is the body). Reported separately, a heavy
    morning inside a steady loss reads as exactly that.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        table (str): ``weight``, ``body_fat`` or ``lean_body_mass``.
        value_col (str): That table's value column.
        date (str): ISO local date, window end.
        days (int): Trailing window length for the slope.

    Returns:
        dict: ``latest`` and ``previous`` (each ``date``/``value``),
        ``change`` and ``days_between`` between those two, plus
        ``per_week`` and ``readings`` over the window, and
        ``recent_step`` -- latest vs the reading closest to
        ``RECENT_STEP_DAYS`` ago -- a third figure distinct from
        both: not one noisy reading like ``change``, not blended
        across the whole window like ``per_week``, so a rise across
        this week specifically stays visible even inside a slope
        that nets out flat or falling over the full window. Keys are
        omitted rather than faked when the data cannot support them:
        one reading yields ``latest`` alone, none yields ``{}``.
    """
    start = (
        dt.date.fromisoformat(date) - dt.timedelta(days=days - 1)
    ).isoformat()
    series = _daily_series(conn, user_id, table, value_col, start, date)
    if not series:
        return {}
    result: dict = {
        "latest": {"date": series[-1][0], "value": round(series[-1][1], 2)},
        "readings": len(series),
        "window_days": days,
    }
    per_week = _slope_per_week(series)
    if per_week is not None:
        result["per_week"] = per_week
    if len(series) < 2:
        return result
    previous_date, previous_value = series[-2]
    result["previous"] = {
        "date": previous_date, "value": round(previous_value, 2),
    }
    result["change"] = round(series[-1][1] - previous_value, 2)
    result["days_between"] = (
        dt.date.fromisoformat(series[-1][0])
        - dt.date.fromisoformat(previous_date)
    ).days

    latest_date = dt.date.fromisoformat(series[-1][0])
    target = latest_date - dt.timedelta(days=RECENT_STEP_DAYS)
    candidates = series[:-1]  # never compare the latest to itself
    closest_date, closest_value = min(
        candidates,
        key=lambda point: abs(
            (dt.date.fromisoformat(point[0]) - target).days
        ),
    )
    days_back = (latest_date - dt.date.fromisoformat(closest_date)).days
    if abs(days_back - RECENT_STEP_DAYS) <= RECENT_STEP_TOLERANCE_DAYS:
        result["recent_step"] = {
            "from_date": closest_date,
            "value": round(closest_value, 2),
            "change": round(series[-1][1] - closest_value, 2),
            "days": days_back,
        }
    return result


# Effort fields compared between two sessions of the same kind.
# Direction is which way counts as progress, so the coach never has
# to work out whether a smaller number is better: a shorter session
# is less work, a lower heart rate for the same work is fitter.
_EFFORT_FIELDS = {
    "duration_min": "higher_is_more_work",
    "kcal": "higher_is_more_work",
    "avg_hr": "lower_is_fitter",
    "rpe": "lower_is_easier",
}


def _session_effort(
    conn: sqlite3.Connection, user_id: int, row: sqlite3.Row,
) -> dict:
    """One session's comparable effort figures.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        row (sqlite3.Row): An ``exercise_sessions`` row.

    Returns:
        dict: ``date`` plus whichever of ``duration_min``/``avg_hr``/
        ``rpe``/``kcal`` this session actually has.
    """
    effort = {
        "date": row["local_date"],
        "duration_min": metrics.session_duration_min(row),
    }
    if row["rpe"] is not None:
        effort["rpe"] = row["rpe"]
    hr = conn.execute(
        "SELECT AVG(bpm) AS avg_hr FROM exercise_hr_samples "
        "WHERE user_id = ? AND exercise_uuid = ?",
        (user_id, row["uuid"]),
    ).fetchone()["avg_hr"]
    if hr is not None:
        effort["avg_hr"] = round(hr)
    kcal = conn.execute(
        "SELECT SUM(kcal) AS kcal FROM active_calories WHERE "
        "user_id = ? AND start_utc < ? AND end_utc > ?",
        (user_id, row["end_utc"], row["start_utc"]),
    ).fetchone()["kcal"]
    if kcal is not None:
        effort["kcal"] = round(kcal)
    return effort


def effort_progression(
    conn: sqlite3.Connection, user_id: int, date: str, days: int = 90,
) -> dict:
    """Per exercise type, the last session against the one before it,
    and the direction across the window.

    The same two senses as :func:`point_progression`, for training.
    The prompt used to ask the LLM to do this itself -- "compare
    tonight's session to the last one of the same type" -- which
    meant the one comparison progressive overload actually turns on
    was left to a model reading a list, and could be quietly wrong
    in either direction. Computing it means the message can say "12
    min de plus qu'il y a 4 jours, a frequence cardiaque egale"
    because that is what happened.

    Sessions are grouped by their displayed label, so a manual
    label_override groups with its corrected type rather than with
    whatever Garmin guessed.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date, window end.
        days (int): Trailing window length.

    Returns:
        dict: One entry per label seen in the window, each with
        ``latest``, ``sessions`` (count) and ``window_days``, plus
        ``previous``/``change``/``days_between`` once there are two,
        and ``per_week`` slopes for the numeric fields. ``change``
        carries only fields both sessions have, each with its
        ``direction`` from ``_EFFORT_FIELDS``.
    """
    start = (
        dt.date.fromisoformat(date) - dt.timedelta(days=days - 1)
    ).isoformat()
    rows = conn.execute(
        "SELECT uuid, local_date, start_utc, end_utc, exercise_type, "
        "label_override, rpe FROM exercise_sessions WHERE user_id = ? "
        "AND local_date BETWEEN ? AND ? ORDER BY start_utc",
        (user_id, start, date),
    ).fetchall()

    by_label: dict[str, list[dict]] = {}
    for row in rows:
        label = row["label_override"] or EXERCISE_TYPE_LABELS.get(
            row["exercise_type"], "other"
        )
        by_label.setdefault(label, []).append(
            _session_effort(conn, user_id, row)
        )

    progression: dict = {}
    for label, efforts in by_label.items():
        entry: dict = {
            "latest": efforts[-1],
            "sessions": len(efforts),
            "window_days": days,
        }
        slopes = {}
        for field in _EFFORT_FIELDS:
            points = [
                (effort["date"], float(effort[field]))
                for effort in efforts if field in effort
            ]
            slope = _slope_per_week(points)
            if slope is not None:
                slopes[field] = slope
        if slopes:
            entry["per_week"] = slopes
        if len(efforts) >= 2:
            previous = efforts[-2]
            entry["previous"] = previous
            entry["change"] = {
                field: {
                    "delta": round(efforts[-1][field] - previous[field], 1),
                    "direction": direction,
                }
                for field, direction in _EFFORT_FIELDS.items()
                if field in efforts[-1] and field in previous
            }
            entry["days_between"] = (
                dt.date.fromisoformat(efforts[-1]["date"])
                - dt.date.fromisoformat(previous["date"])
            ).days
        progression[label] = entry
    return progression


def remaining_today(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> dict:
    """Today's budget minus what has already been logged today.

    The prompt used to ask the LLM for this subtraction, which is
    the one thing this project deliberately never does: every figure
    the message quotes is computed here, so it cannot drift. It also
    made the advice vaguer than the data allowed -- "il te reste 1400
    kcal et 96 g de proteines" is actionable in a way that "vise 2100
    kcal" stops being the moment breakfast is logged.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today).

    Returns:
        dict: ``date``, ``targets``, ``logged`` (so far today) and
        ``remaining`` (target minus logged). Negative values are kept
        rather than floored -- an overshot budget is exactly what the
        message needs to say. ``remaining`` is empty when there are
        no targets to subtract from.
    """
    targets = macro_targets(conn, user_id, date)
    logged = intake_for_date(conn, user_id, date)
    remaining = {
        actual_key: round(targets[target_key] - logged.get(actual_key, 0), 1)
        for actual_key, target_key in _GAP_FIELDS
        if target_key in targets
    }
    return {
        "date": date, "targets": targets, "logged": logged,
        "remaining": remaining,
    }


def export_reaches(
    conn: sqlite3.Connection, user_id: int, table: str, date: str,
) -> Optional[bool]:
    """Whether the newest Health Connect export carried ``date``.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        table (str): Ingested table name, as recorded by the parser.
        date (str): ISO local date.

    Returns:
        bool | None: True/False once an export has been parsed since
        coverage tracking existed, None before that -- which is not
        the same as False and must not be read as one.
    """
    row = conn.execute(
        "SELECT first_date, last_date FROM hc_export_coverage "
        "WHERE user_id = ? AND table_name = ?", (user_id, table),
    ).fetchone()
    if not row:
        return None
    return row["first_date"] <= date <= row["last_date"]


def nutrition_gap(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Yesterday's targets vs what was actually logged, for the coach.

    This is what makes food/hydration suggestions concrete instead of
    generic: the LLM is handed an already-computed gap (e.g. "42g
    protein short of target"), not raw numbers to do arithmetic on.

    Returns:
        dict: ``targets``, ``actual``, ``gap`` (target minus actual,
        positive = still short, negative = exceeded; only for fields
        present in both), ``date`` (yesterday's date),
        ``log_looks_incomplete`` -- see below -- and, only when
        ``data_missing`` is true and the day before was itself fully
        covered by the export, ``nutrition_fallback`` (that day's own
        ``date``/``actual``/``gap``, never merged into yesterday's).

    The gap is only as good as the logging behind it, and food
    logging reaches this database through a chain (the logging app ->
    Health Connect -> the phone's nightly export) that drops entries
    routinely: on a real account only 5 of 30 days had any nutrition
    row at all, and a day the app itself showed as 1713 kcal arrived
    here as 1043 with the evening meal missing. A day like that is
    indistinguishable, in the numbers, from genuinely not eating --
    so the gap comes back saying the athlete was 1282 kcal and 149 g
    of protein short, and advice built on it tells them to eat a
    dinner they already ate. ``log_looks_incomplete`` marks the days
    where that reading is not credible, so the message can say the
    log looks partial rather than inventing a deficit.
    """
    yesterday = (
        dt.date.fromisoformat(date) - dt.timedelta(days=1)
    ).isoformat()
    targets = macro_targets(conn, user_id, yesterday)
    actual = yesterday_intake(conn, user_id, date)

    # Distinct from the heuristic below: this one is a fact. The
    # export either reached that date or it did not, and when it did
    # not, the emptiness says nothing at all about what was eaten.
    missing = export_reaches(conn, user_id, "nutrition", yesterday) is False

    # Same lag hydration has, just less consistent about which day it
    # hits: a real account had MyFitnessPal's meals fully logged and
    # visible in the app, but the export the ingest actually ran
    # against that morning was an earlier, less-complete snapshot --
    # the export coverage table said yesterday was missing even
    # though, by the time anyone looked, it plainly was not. Only
    # kicks in on the FACT (missing), never on the ratio heuristic
    # below: a day that genuinely has some food logged but reads as
    # sparse should surface as sparse, not get silently swapped for a
    # different day's numbers.
    nutrition_fallback = None
    if missing:
        day_before = (
            dt.date.fromisoformat(yesterday) - dt.timedelta(days=1)
        ).isoformat()
        if export_reaches(conn, user_id, "nutrition", day_before):
            fallback_actual = intake_for_date(conn, user_id, day_before)
            if fallback_actual.get("calories_kcal") is not None:
                fallback_targets = macro_targets(conn, user_id, day_before)
                nutrition_fallback = {
                    "date": day_before, "actual": fallback_actual,
                    "gap": {
                        actual_key: round(
                            fallback_targets[target_key]
                            - fallback_actual[actual_key], 1,
                        )
                        for actual_key, target_key in _GAP_FIELDS
                        if actual_key in fallback_actual
                        and target_key in fallback_targets
                    },
                }

    # Hydration syncs on its own schedule, independently of nutrition
    # -- a real account had MyFitnessPal's meals reach the export for
    # a day while its water total for that SAME day stayed a sync
    # behind (nutrition through the 19th, hydration only through the
    # 18th). A coverage check scoped to "nutrition" never sees that,
    # so a hydration total left over from a stale ingest -- or simply
    # absent -- got reported as yesterday's real water intake. Water
    # is dropped from actual/gap entirely when its own table hasn't
    # reached yesterday, rather than trusting a number nothing here
    # can vouch for.
    #
    # None of this applies when Garmin already answered the question:
    # it carries no export lag, so a Garmin reading for yesterday is
    # simply yesterday's real total, already sitting in ``actual``.
    garmin_has_hydration = (
        garmin_hydration_for_date(conn, user_id, yesterday) is not None
    )
    hydration_missing = (
        not garmin_has_hydration
        and export_reaches(conn, user_id, "hydration", yesterday) is False
    )
    hydration_fallback = None
    if hydration_missing:
        actual = {k: v for k, v in actual.items() if k != "hydration_ml"}
        # Confirmed structural, not a one-off: two exports four days
        # apart both had hydration's coverage exactly one day behind
        # nutrition's -- MyFitnessPal writes the day's water as a
        # single 00:00-23:59 total, finalised later than its meals,
        # so it consistently misses the export that already has that
        # day's food. Left as a flat "missing", this would go dark
        # every single morning forever, not just on an off day. The
        # day before usually HAS synced by then, so surface that
        # instead of nothing -- clearly dated, never mistaken for
        # yesterday's number.
        day_before = (
            dt.date.fromisoformat(yesterday) - dt.timedelta(days=1)
        ).isoformat()
        if export_reaches(conn, user_id, "hydration", day_before):
            fallback_ml = conn.execute(
                "SELECT SUM(volume_ml) AS total FROM hydration WHERE "
                "user_id = ? AND local_date = ?", (user_id, day_before),
            ).fetchone()["total"]
            if fallback_ml is not None:
                fallback_target = macro_targets(
                    conn, user_id, day_before,
                ).get("hydration_target_ml")
                hydration_fallback = {
                    "date": day_before, "actual_ml": round(fallback_ml),
                }
                if fallback_target is not None:
                    hydration_fallback["gap_ml"] = round(
                        fallback_target - fallback_ml, 1,
                    )

    gap = {
        actual_key: round(targets[target_key] - actual[actual_key], 1)
        for actual_key, target_key in _GAP_FIELDS
        if actual_key in actual and target_key in targets
    }
    result = {
        "date": yesterday, "targets": targets, "actual": actual, "gap": gap,
        "data_missing": missing,
        "hydration_data_missing": hydration_missing,
        "log_looks_incomplete": (
            missing or _log_looks_incomplete(actual, targets)
        ),
    }
    if hydration_fallback is not None:
        result["hydration_fallback"] = hydration_fallback
    if nutrition_fallback is not None:
        result["nutrition_fallback"] = nutrition_fallback
    return result


# Below this share of the calorie target, a day reads as "barely
# logged" rather than "barely eaten". Someone genuinely under-eating
# still lands well above it, so the flag costs little when wrong: the
# message only softens from a firm deficit to a caveated one.
_LOGGED_ENOUGH_RATIO = 0.5


def _log_looks_incomplete(actual: dict, targets: dict) -> bool:
    """Whether yesterday's food log is too sparse to draw a gap from.

    Parameters:
        actual (dict): ``yesterday_intake`` output.
        targets (dict): ``macro_targets`` output.

    Returns:
        bool: True when nothing was logged, or so little that missing
        entries explain it better than the athlete's day does.
    """
    logged = actual.get("calories_kcal")
    if not actual.get("entries") or logged is None:
        return True
    target = targets.get("calorie_target_kcal")
    if not target:
        return logged <= 0
    return logged < _LOGGED_ENOUGH_RATIO * target


# Thresholds below which a gap isn't worth nudging about on the
# calendar (the LLM message covers the full picture either way; this
# is just the short, deterministic line that rides alongside tonight's
# workout numbers, so it doesn't wait on/depend on an LLM call).
NUDGE_PROTEIN_MIN_G = 5
NUDGE_HYDRATION_MIN_ML = 200
NUDGE_CALORIE_SURPLUS_MIN_KCAL = 100


def format_nutrition_nudge(nutrition: dict, language: str = "fr") -> str:
    """One-line nutrition/hydration reminder for the calendar event.

    Deterministic (no LLM call) so the calendar update in the morning
    doesn't wait on/depend on the coaching-message call -- it reuses
    the same gap numbers the LLM message narrates around.

    Parameters:
        nutrition (dict): ``nutrition_gap(...)`` output -- the whole
            dict, not just its ``gap``. A shortfall measured against
            a day the export never carried is not a shortfall, and
            telling someone to eat 120 g of protein they already ate
            is worse than saying nothing.
        language (str): "fr" or "en".

    Returns:
        str: A short line, or "" if nothing is worth flagging (no
        data, an unusable food log, or everything within threshold).
    """
    gap = nutrition.get("gap") or {}
    # Hydration survives an unusable food log: it comes from its own
    # records, and a missing dinner says nothing about what was drunk.
    food_is_usable = not nutrition.get("log_looks_incomplete")
    parts = []
    protein_gap = gap.get("protein_g") if food_is_usable else None
    if protein_gap is not None and protein_gap > NUDGE_PROTEIN_MIN_G:
        parts.append(
            f"+{round(protein_gap)}g proteines" if language == "fr"
            else f"+{round(protein_gap)}g protein"
        )
    hydration_gap = gap.get("hydration_ml")
    if hydration_gap is not None and hydration_gap > NUDGE_HYDRATION_MIN_ML:
        liters = round(hydration_gap / 1000, 1)
        parts.append(f"{liters}L d'eau" if language == "fr" else f"{liters}L water")
    calorie_gap = gap.get("calories_kcal") if food_is_usable else None
    if calorie_gap is not None and calorie_gap < -NUDGE_CALORIE_SURPLUS_MIN_KCAL:
        over = abs(round(calorie_gap))
        parts.append(
            f"-{over}kcal (hier en surplus)" if language == "fr"
            else f"-{over}kcal (surplus yesterday)"
        )
    if not parts:
        return ""
    return "Nutrition: " + ", ".join(parts)


# How far back "the recent step" looks, distinct from both the
# immediate previous reading (mostly noise) and the full-window
# slope (mostly the past). A user who gained weight Monday-to-Friday
# is describing exactly this window, and neither of the other two
# figures speaks to it: the immediate step is one day, the slope
# blends four-plus weeks and a five-day rise inside a longer decline
# can average out to "flat".
RECENT_STEP_DAYS = 7
# How far a candidate reading may sit from that target and still
# count as "the recent step" -- weigh-ins are not on a fixed
# schedule, so this tolerates a few days' slack either way.
RECENT_STEP_TOLERANCE_DAYS = 3

# Each half of the trend window needs at least this many days
# carrying a reading before a flat delta means anything. Weigh-ins
# reached this database through Health Connect alone until Garmin
# was added, and they arrived about weekly: two readings a fortnight
# apart differing by 0.1 kg is not a plateau, it is two numbers. The
# coach called one anyway, on a real account, while the athlete's
# weight was moving.
MIN_TREND_READING_DAYS = 3


def detect_plateau(
    weight: dict, calories: dict, recent_step: Optional[dict] = None,
) -> dict:
    """Flag a weight-loss plateau despite a real logged deficit.

    Parameters:
        weight (dict): ``weight_trend`` output (14-day half-vs-half
            average -- smoothed on purpose, which is exactly why it
            can call a week "flat" while the readings inside it rose).
        calories (dict): ``calorie_balance_for_range`` output.
        recent_step (dict | None): ``point_progression(...)
            ["recent_step"]`` -- latest reading vs about a week ago.
            A real move here, in either direction, is not a plateau
            whatever the smoothed trend says, so it is checked first
            and short-circuits the rest of this function.

    Returns:
        dict: ``{"plateau": bool, "note": str | None}``, plus
        ``too_few_weigh_ins`` when the window is too sparse to judge,
        or ``recent_move`` (``direction``/``change``/``days``) when a
        clear recent move pre-empted the plateau question entirely.
        ``note`` is a plain-language flag for the LLM/dashboard to
        surface, not a prescription -- it names the situation, the
        LLM phrases advice.
    """
    if not weight or "delta" not in weight:
        return {"plateau": False, "note": None}
    if min(
        weight.get("current_days", 0), weight.get("past_days", 0)
    ) < MIN_TREND_READING_DAYS:
        return {"plateau": False, "note": None, "too_few_weigh_ins": True}
    if recent_step and abs(recent_step["change"]) >= PLATEAU_WEIGHT_DELTA_KG:
        return {
            "plateau": False, "note": None,
            "recent_move": {
                "direction": (
                    "up" if recent_step["change"] > 0 else "down"
                ),
                "change": recent_step["change"],
                "days": recent_step["days"],
            },
        }
    flat = abs(weight["delta"]) < PLATEAU_WEIGHT_DELTA_KG
    if not flat:
        return {"plateau": False, "note": None}
    if calories.get("avg_balance_kcal", 0) <= REAL_DEFICIT_KCAL:
        return {
            "plateau": True,
            "note": (
                "Poids stable malgre un deficit calorique logue -- "
                "plateau probable (adaptation metabolique ou apports "
                "sous-estimes)."
            ),
        }
    if calories:
        return {
            "plateau": True,
            "note": (
                "Poids stable, pas de vrai deficit logue -- "
                "probablement a l'entretien calorique."
            ),
        }
    return {"plateau": False, "note": None}


RECAL_ABS_THRESHOLD_KG = 0.15  # per week
RECAL_REL_THRESHOLD = 0.4  # 40% relative deviation from target


def _weekly_rate_kg(
    conn: sqlite3.Connection, user_id: int, end_date: str, days: int = 28,
) -> Optional[float]:
    """Actual weekly weight-change rate: a least-squares slope over
    the trailing window, not the two endpoints.

    Used to be exactly that subtraction, and it was the more fragile
    of the two methods this file now has for the same question: the
    earliest and latest readings in the window are the two points
    most likely to be a single odd morning, and extrapolating a
    week's rate from just those two turned a five-day rise sitting
    inside an otherwise-declining month into a confidently-reported
    "-0.18 kg/week", with no sign anything had gone up. See
    :func:`_slope_per_week`.

    Returns:
        float | None: kg/week (signed), or ``None`` if the readings
        in the window don't span at least 7 days.
    """
    start = (
        dt.date.fromisoformat(end_date) - dt.timedelta(days=days)
    ).isoformat()
    series = _daily_series(conn, user_id, "weight", "kg", start, end_date)
    if len(series) < 2:
        return None
    span_days = (
        dt.date.fromisoformat(series[-1][0])
        - dt.date.fromisoformat(series[0][0])
    ).days
    if span_days < 7:
        return None
    return _slope_per_week(series)


def recalibration_check(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> dict:
    """Flag when the actual weight trend has drifted from the goal
    rate long enough that the calorie target itself should change,
    instead of silently staying wrong.

    Returns:
        dict: ``{"flagged": bool}`` plus ``actual_weekly_kg``,
        ``target_weekly_kg``, ``suggested_daily_calorie_adjustment_kcal``
        when there's enough data to judge (empty/False otherwise, or
        if the goal is maintenance -- weekly_weight_change_kg == 0).
    """
    target = float(
        db.get_setting(conn, user_id, "weekly_weight_change_kg") or 0
    )
    if target == 0:
        return {"flagged": False}
    actual = _weekly_rate_kg(conn, user_id, date)
    if actual is None:
        return {"flagged": False}
    diff = actual - target
    threshold = max(RECAL_ABS_THRESHOLD_KG, abs(target) * RECAL_REL_THRESHOLD)
    if abs(diff) < threshold:
        return {
            "flagged": False, "actual_weekly_kg": round(actual, 2),
            "target_weekly_kg": target,
        }
    return {
        "flagged": True, "actual_weekly_kg": round(actual, 2),
        "target_weekly_kg": target,
        "suggested_daily_calorie_adjustment_kcal": round(
            -diff * KCAL_PER_KG_BODY_MASS / 7
        ),
    }


def weekly_progress(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Bundle all trend signals for the LLM payload and Progress page.

    Returns:
        dict: ``weight_trend_14d``, ``body_fat_trend_28d``,
        ``lean_mass_trend_28d``, ``calorie_balance_7d``,
        ``protein_7d``, ``plateau``, ``recalibration``, plus
        ``weight_progression``/``body_fat_progression`` (last
        reading vs the one before, and the slope across the window)
        and ``effort_progression`` (the same, per exercise type) --
        empty sub-dicts where there isn't enough data yet.
    """
    weight = weight_trend(conn, user_id, date)
    weight_progress = point_progression(conn, user_id, "weight", "kg", date)
    calories = calorie_balance_for_range(conn, user_id, date)
    return {
        "weight_trend_14d": weight,
        "weight_progression": weight_progress,
        "body_fat_progression": point_progression(
            conn, user_id, "body_fat", "percentage", date,
        ),
        "effort_progression": effort_progression(conn, user_id, date),
        "body_fat_trend_28d": body_fat_trend(conn, user_id, date),
        "lean_mass_trend_28d": lean_mass_trend(conn, user_id, date),
        "calorie_balance_7d": calories,
        "protein_7d": protein_trend(conn, user_id, date),
        "plateau": detect_plateau(
            weight, calories, weight_progress.get("recent_step"),
        ),
        "nutrition_yesterday": nutrition_gap(conn, user_id, date),
        "recalibration": recalibration_check(conn, user_id, date),
    }


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "smart_coach.db"
    conn = db.connect(tmp)
    db.init_db(conn)
    uid = db.create_user(conn, "test", "password1234")
    other_uid = db.create_user(conn, "other", "password1234")

    base = dt.date(2026, 7, 1)
    for i in range(28):
        date = (base + dt.timedelta(days=i)).isoformat()
        weight = 80.0 - (0.05 * i if i < 14 else 0.7)  # loses, then flat
        conn.execute(
            "INSERT INTO weight VALUES (?, ?, ?, ?, ?)",
            (f"w{i}", uid, f"{date}T07:00:00+00:00", date, weight),
        )
        if i >= 20:  # nutrition only logged the last week (sparse, realistic)
            conn.execute(
                "INSERT INTO nutrition VALUES "
                "(?, ?, ?, ?, ?, NULL, 1900, 140, 180, 60)",
                (f"n{i}", uid, f"{date}T12:00:00+00:00",
                 f"{date}T13:00:00+00:00", date),
            )
            conn.execute(
                "INSERT INTO active_calories VALUES (?, ?, ?, ?, ?, ?)",
                (f"a{i}", uid, f"{date}T18:00:00+00:00",
                 f"{date}T19:00:00+00:00", date, 400),
            )
    conn.execute(
        "INSERT INTO hydration VALUES (?, ?, ?, ?, ?, ?)",
        ("hyd26", uid,
         f"{(base + dt.timedelta(days=26)).isoformat()}T09:00:00+00:00",
         f"{(base + dt.timedelta(days=26)).isoformat()}T09:01:00+00:00",
         (base + dt.timedelta(days=26)).isoformat(), 2100),
    )
    # Another user's weight data -- must never leak into uid's trend.
    conn.execute(
        "INSERT INTO weight VALUES ('w-other', ?, "
        "'2026-07-27T07:00:00+00:00', '2026-07-27', 999.0)", (other_uid,),
    )
    conn.commit()

    end_date = (base + dt.timedelta(days=27)).isoformat()
    trend = weight_trend(conn, uid, end_date)
    # The last 14 days of the fixture are entirely flat: a clean
    # non-overlapping two-half window must report zero delta (the
    # losing phase is older than the window).
    assert trend["delta"] == 0.0, trend
    assert trend["current_avg"] < 900  # not contaminated by the other user
    # During the losing phase the same window shows the loss.
    losing = weight_trend(
        conn, uid, (base + dt.timedelta(days=13)).isoformat(),
    )
    assert losing["delta"] < 0, losing

    protein = protein_trend(conn, uid, end_date)
    assert protein["avg_protein_g"] == 140.0
    # target_g uses the default protein_target_g_per_kg (1.8) x latest
    # weight, since both a weight reading and the ratio setting exist
    assert db.get_setting(conn, uid, "protein_target_g_per_kg") == "1.8"
    assert protein["target_g"] == round(79.3 * 1.8, 1), protein

    calories = calorie_balance_for_range(conn, uid, end_date)
    assert calories["days_logged"] == 7
    # 1900 intake - 400 burn - BMR(unset, no formula inputs) = 1500 surplus
    assert calories["avg_balance_kcal"] == 1500

    plateau = detect_plateau(trend, calories)
    assert plateau["plateau"] is True

    # The real bug report: an athlete losing overall who gains
    # across one week is not on a plateau, and must not be told
    # they are. Fixture: -0.1 kg/day for 21 days, then +0.15 kg/day
    # for the final week -- net loss over 28 days, real rise over 7.
    rise_conn = db.connect(Path(tempfile.mkdtemp()) / "rise.db")
    db.init_db(rise_conn)
    rise_uid = db.create_user(rise_conn, "rise", "password1234")
    for i in range(28):
        day_date = (base + dt.timedelta(days=i)).isoformat()
        kg = 90.0 - 0.1 * i if i < 21 else 90.0 - 0.1 * 21 + 0.15 * (i - 21)
        rise_conn.execute(
            "INSERT INTO weight VALUES (?, ?, ?, ?, ?)",
            (f"r{i}", rise_uid, f"{day_date}T07:00:00+00:00", day_date, kg),
        )
    rise_conn.commit()
    rise_end = (base + dt.timedelta(days=27)).isoformat()
    rise_wp = point_progression(rise_conn, rise_uid, "weight", "kg", rise_end)
    assert rise_wp["recent_step"]["change"] > 0, rise_wp  # the felt rise
    assert rise_wp["per_week"] < 0, rise_wp  # still losing overall
    rise_trend = weight_trend(rise_conn, rise_uid, rise_end)
    rise_plateau = detect_plateau(
        rise_trend, {}, rise_wp.get("recent_step"),
    )
    # Must name the rise, not call it a plateau -- whatever the
    # smoothed 14-day halves happen to say.
    assert rise_plateau["plateau"] is False, rise_plateau
    assert rise_plateau["recent_move"]["direction"] == "up", rise_plateau
    assert rise_plateau["recent_move"]["change"] > 0, rise_plateau

    # Without a recent_step (old call sites, or too few readings for
    # one), behaviour is unchanged -- this override never applies to
    # data that cannot support it.
    assert detect_plateau(trend, calories, None) == plateau

    # recalibration_check's rate must be the same robust slope, not
    # the two endpoints -- on this fixture the naive subtraction
    # (first reading vs last, both potentially odd mornings) reads
    # -0.31 kg/week; the fit through all 28 readings reads -0.46.
    # Neither is "the" true answer, but only one of them is immune
    # to a single reading at either end moving it.
    slope_rate = _weekly_rate_kg(rise_conn, rise_uid, rise_end)
    rise_rows = rise_conn.execute(
        "SELECT local_date, kg FROM weight WHERE user_id = ? "
        "ORDER BY local_date", (rise_uid,),
    ).fetchall()
    naive_rate = (
        (rise_rows[-1]["kg"] - rise_rows[0]["kg"])
        / (
            dt.date.fromisoformat(rise_rows[-1]["local_date"])
            - dt.date.fromisoformat(rise_rows[0]["local_date"])
        ).days * 7
    )
    assert round(slope_rate, 2) == -0.46, slope_rate
    assert round(naive_rate, 2) == -0.31, naive_rate
    assert abs(slope_rate - naive_rate) > 0.1  # genuinely different answers

    # A fortnight carrying one weigh-in per half is two numbers, not
    # a trend: flat or not, it must not be reported as a plateau.
    sparse_conn = db.connect(Path(tempfile.mkdtemp()) / "sparse.db")
    db.init_db(sparse_conn)
    sparse_uid = db.create_user(sparse_conn, "sparse", "password1234")
    for offset, kg in ((0, 89.0), (10, 89.05)):
        date = (base + dt.timedelta(days=offset)).isoformat()
        sparse_conn.execute(
            "INSERT INTO weight VALUES (?, ?, ?, ?, ?)",
            (f"s{offset}", sparse_uid, f"{date}T07:00:00+00:00", date, kg),
        )
    sparse_conn.commit()
    sparse_end = (base + dt.timedelta(days=13)).isoformat()
    sparse_trend = weight_trend(sparse_conn, sparse_uid, sparse_end)
    assert sparse_trend["current_days"] == 1, sparse_trend
    assert sparse_trend["past_days"] == 1, sparse_trend
    assert abs(sparse_trend["delta"]) < PLATEAU_WEIGHT_DELTA_KG
    sparse_plateau = detect_plateau(sparse_trend, calories)
    assert sparse_plateau["plateau"] is False, sparse_plateau
    assert sparse_plateau["too_few_weigh_ins"] is True, sparse_plateau

    # A day can carry two readings that disagree: the scale reaches
    # Garmin and Health Connect separately, and an evening weigh-in
    # is heavier than the morning one. Counted as two samples, that
    # single day pulls its whole half; counted as one day, it
    # contributes its own mean like every other day.
    dual_conn = db.connect(Path(tempfile.mkdtemp()) / "dual.db")
    db.init_db(dual_conn)
    dual_uid = db.create_user(dual_conn, "dual", "password1234")
    for offset in range(14):
        date = (base + dt.timedelta(days=offset)).isoformat()
        dual_conn.execute(
            "INSERT INTO weight VALUES (?, ?, ?, ?, ?)",
            (f"hc{offset}", dual_uid, f"{date}T07:00:00+00:00", date,
             90.0 - 0.1 * offset),
        )
    # Second, heavier reading on the final day only.
    last_date = (base + dt.timedelta(days=13)).isoformat()
    dual_conn.execute(
        "INSERT INTO weight VALUES (?, ?, ?, ?, 90.1)",
        ("garmin-weight-13", dual_uid, f"{last_date}T19:00:00+00:00",
         last_date),
    )
    dual_conn.commit()
    dual_trend = weight_trend(dual_conn, dual_uid, last_date)
    assert dual_trend["current_days"] == 7, dual_trend
    assert dual_trend["past_days"] == 7, dual_trend
    # Recent half: six plain days plus (88.7 + 90.1)/2 = 89.4 for the
    # doubled one, averaging 89.1 against 89.7 before. Pooling the
    # eight raw readings instead gives 89.14, shrinking a real 0.6 kg
    # fall to 0.56.
    assert dual_trend["current_avg"] == 89.1, dual_trend
    assert dual_trend["delta"] == -0.6, dual_trend

    # Lean mass logged flat while weight fell -> recomposition signal.
    for i in (0, 14, 27):
        date = (base + dt.timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO lean_body_mass VALUES (?, ?, ?, ?, 61.0)",
            (f"lm{i}", uid, f"{date}T07:00:00+00:00", date),
        )
    conn.commit()
    lean = lean_mass_trend(conn, uid, end_date)
    assert lean["delta"] == 0.0, lean

    header = format_plan_header(
        {"calorie_target_kcal": 2100, "protein_target_g": 140,
         "fat_target_g": 72, "carb_target_g": 210,
         "hydration_target_ml": 2800}, "fr",
    )
    assert header == (
        "Plan du jour: 2100 kcal - P140g L72g G210g - 2.8L"
    ), header
    assert format_plan_header({}, "fr") == ""
    assert format_plan_header(
        {"protein_target_g": 140}, "en",
    ) == "Today's plan: P140g"

    bundle = weekly_progress(conn, uid, end_date)
    assert bundle["weight_trend_14d"] == trend
    assert bundle["lean_mass_trend_28d"] == lean
    # protein/fat/hydration targets need no BMR inputs (default ratio
    # settings are seeded by create_user), so they're already in the
    # gap computed above even before height/age/sex are set below.
    assert "protein_g" in bundle["nutrition_yesterday"]["gap"]
    assert "hydration_ml" in bundle["nutrition_yesterday"]["gap"]

    db.set_setting(conn, uid, "height_cm", "178")
    db.set_setting(conn, uid, "age_years", "34")
    db.set_setting(conn, uid, "sex", "M")

    # tdee_estimate: no total_calories_burned rows -> BMR formula +
    # trailing active_calories average (400 kcal/day logged since i>=20)
    weight_at_end = 79.3
    tdee = tdee_estimate(conn, uid, end_date, weight_at_end)
    expected_bmr = 10 * weight_at_end + 6.25 * 178 - 5 * 34 + 5
    assert abs(tdee - (expected_bmr + 400)) < 0.5, tdee

    # Device TDEE present and trusted by default: it wins outright
    # over BMR+active, even though it disagrees with the formula.
    for offset in range(20, 27):
        day_date = (base + dt.timedelta(days=offset)).isoformat()
        conn.execute(
            "INSERT INTO total_calories_burned VALUES (?, ?, ?, ?, ?, "
            "3000)",
            (f"tcb{offset}", uid, f"{day_date}T23:00:00+00:00",
             f"{day_date}T23:59:00+00:00", day_date),
        )
    conn.commit()
    device_tdee = tdee_estimate(conn, uid, end_date, weight_at_end)
    assert device_tdee == 3000, device_tdee

    # trust_device_tdee=0: the device average is ignored outright,
    # even though it exists, back to BMR + active -- the lever
    # someone who trusts their own known basal over the device's
    # needs, since bmr_manual_kcal alone can't reach this path while
    # a device total is present.
    db.set_setting(conn, uid, "trust_device_tdee", "0")
    distrust_tdee = tdee_estimate(conn, uid, end_date, weight_at_end)
    assert abs(distrust_tdee - (expected_bmr + 400)) < 0.5, distrust_tdee

    # Combined with a manual BMR override, the athlete's own known
    # basal (not the formula, not the device) drives TDEE.
    db.set_setting(conn, uid, "bmr_manual_kcal", "1500")
    manual_tdee = tdee_estimate(conn, uid, end_date, weight_at_end)
    assert manual_tdee == 1500 + 400, manual_tdee
    db.set_setting(conn, uid, "bmr_manual_kcal", "")
    db.set_setting(conn, uid, "trust_device_tdee", "1")
    conn.execute("DELETE FROM total_calories_burned WHERE user_id = ?", (uid,))
    conn.commit()

    targets = macro_targets(conn, uid, end_date)
    assert targets["protein_target_g"] == round(weight_at_end * 1.8)
    assert targets["fat_target_g"] == round(weight_at_end * 0.9)
    assert targets["hydration_target_ml"] == round(weight_at_end * 35)
    assert "carb_target_g" in targets
    assert macro_targets(conn, other_uid, end_date) == {} or \
        macro_targets(conn, other_uid, end_date).get("protein_target_g") \
        != targets["protein_target_g"]  # isolated (other user's weight differs)

    yesterday = yesterday_intake(conn, uid, end_date)
    assert yesterday["calories_kcal"] == 1900
    assert yesterday["hydration_ml"] == 2100

    gap = nutrition_gap(conn, uid, end_date)
    assert gap["date"] == (base + dt.timedelta(days=26)).isoformat()
    assert "protein_g" in gap["gap"]
    assert "hydration_ml" in gap["gap"]
    assert "calories_kcal" in gap["gap"]  # calorie target now derivable
    assert weekly_progress(conn, uid, end_date)["nutrition_yesterday"] == gap
    # 1900 kcal logged against a ~2400 target: a real day's eating,
    # so the gap stands as a gap.
    assert gap["log_looks_incomplete"] is False, gap

    # The failure mode this guards, with the numbers that produced it:
    # the tracking app showed 1713 kcal for the day, only 1043 of it
    # reached Health Connect, and a later day arrived with a couple of
    # stray entries. Reported as a deficit, that becomes "149g of
    # protein short" and a prescription to eat a dinner already eaten.
    sparse_day = (base + dt.timedelta(days=26)).isoformat()
    conn.execute(
        "DELETE FROM nutrition WHERE user_id = ? AND local_date = ?",
        (uid, sparse_day),
    )
    conn.execute(
        "INSERT INTO nutrition (uuid, user_id, start_utc, end_utc, "
        "local_date, calories, protein_g, carbs_g, fat_g) VALUES "
        "('sparse', ?, ?, ?, ?, 120, 11.8, 4, 3)",
        (uid, f"{sparse_day}T08:00:00+00:00",
         f"{sparse_day}T08:30:00+00:00", sparse_day),
    )
    conn.commit()
    sparse = nutrition_gap(conn, uid, end_date)
    assert sparse["log_looks_incomplete"] is True, sparse
    assert sparse["actual"]["entries"] == 1, sparse

    # A day with nothing at all is the same story, not a perfect fast.
    conn.execute(
        "DELETE FROM nutrition WHERE user_id = ? AND local_date = ?",
        (uid, sparse_day),
    )
    conn.commit()
    assert nutrition_gap(conn, uid, end_date)["log_looks_incomplete"] is True

    # Two senses of change at once. The fixture falls 0.05 kg/day
    # for a fortnight then sits flat, so across the last 28 days the
    # slope is a real loss while the step from the previous weigh-in
    # to the latest is zero -- a coach given only one of those two
    # numbers tells a different story than a coach given both.
    wprog = point_progression(conn, uid, "weight", "kg", end_date)
    assert wprog["latest"]["date"] == end_date, wprog
    assert wprog["readings"] == 28, wprog
    assert wprog["days_between"] == 1, wprog
    assert wprog["change"] == 0.0, wprog
    assert wprog["per_week"] < 0, wprog
    # Flat for the whole recent week too, in this fixture -- so
    # recent_step agrees with everything else and changes nothing.
    assert wprog["recent_step"]["days"] == 7, wprog
    assert wprog["recent_step"]["change"] == 0.0, wprog

    # One reading gives a latest and nothing to compare it against,
    # rather than a change of zero -- which would read as "stable".
    single_conn = db.connect(Path(tempfile.mkdtemp()) / "single.db")
    db.init_db(single_conn)
    single_uid = db.create_user(single_conn, "single", "password1234")
    single_conn.execute(
        "INSERT INTO weight VALUES ('only', ?, ?, ?, 88.0)",
        (single_uid, f"{end_date}T07:00:00+00:00", end_date),
    )
    single_conn.commit()
    single = point_progression(single_conn, single_uid, "weight", "kg",
                               end_date)
    assert single["latest"]["value"] == 88.0, single
    assert "change" not in single and "per_week" not in single, single
    assert point_progression(
        single_conn, single_uid, "body_fat", "percentage", end_date,
    ) == {}

    # The slope is a fit, not a subtraction of the endpoints: a
    # single distorted final morning must not flip a steady loss.
    noisy = [
        ((base + dt.timedelta(days=i)).isoformat(), 90.0 - 0.1 * i)
        for i in range(14)
    ]
    assert _slope_per_week(noisy) == -0.7, _slope_per_week(noisy)
    noisy[-1] = (noisy[-1][0], 91.0)  # one heavy morning
    spiked = _slope_per_week(noisy)
    assert spiked < 0, spiked  # still a loss, just a shallower one
    assert (91.0 - 90.0) / 13 * 7 > 0  # endpoints alone would say +0.54
    assert _slope_per_week([("2026-07-01", 90.0)]) is None
    assert _slope_per_week(
        [("2026-07-01", 90.0), ("2026-07-01", 91.0)]
    ) is None  # same date twice: no time to have a slope over

    # Efforts: the last session of a type against the one before it,
    # plus the direction across the window.
    eff_conn = db.connect(Path(tempfile.mkdtemp()) / "effort.db")
    db.init_db(eff_conn)
    eff_uid = db.create_user(eff_conn, "effort", "password1234")
    for i, (day, minutes, hr) in enumerate((
        (0, 30, 150), (7, 35, 148), (14, 42, 145),
    )):
        day_date = (base + dt.timedelta(days=day)).isoformat()
        start_utc = f"{day_date}T18:00:00+00:00"
        end_utc = (
            dt.datetime.fromisoformat(start_utc)
            + dt.timedelta(minutes=minutes)
        ).isoformat()
        eff_conn.execute(
            "INSERT INTO exercise_sessions (uuid, user_id, start_utc, "
            "end_utc, local_date, exercise_type, rpe) "
            "VALUES (?, ?, ?, ?, ?, 57, ?)",
            (f"s{i}", eff_uid, start_utc, end_utc, day_date, 7 - i * 0.5),
        )
        eff_conn.executemany(
            "INSERT INTO exercise_hr_samples VALUES (?, ?, ?, ?)",
            # Straddle the intended mean so avg_hr lands on it
            # exactly, rather than on a rounded half-beat.
            [(f"s{i}", eff_uid, f"{day_date}T18:0{n}:00+00:00",
              hr - 1 + 2 * n) for n in range(2)],
        )
    eff_conn.commit()
    efforts = effort_progression(
        eff_conn, eff_uid, (base + dt.timedelta(days=14)).isoformat(),
    )
    treadmill = efforts["running_treadmill"]
    assert treadmill["sessions"] == 3, treadmill
    assert treadmill["latest"]["duration_min"] == 42, treadmill
    assert treadmill["previous"]["duration_min"] == 35, treadmill
    assert treadmill["days_between"] == 7, treadmill
    # Longer AND easier: more work at a lower heart rate and a lower
    # RPE, which is the shape progressive overload is supposed to have.
    assert treadmill["change"]["duration_min"]["delta"] == 7, treadmill
    assert treadmill["change"]["avg_hr"]["delta"] == -3, treadmill
    assert treadmill["change"]["rpe"]["delta"] == -0.5, treadmill
    assert treadmill["change"]["avg_hr"]["direction"] == "lower_is_fitter"
    assert treadmill["per_week"]["duration_min"] == 6.0, treadmill
    assert treadmill["per_week"]["avg_hr"] == -2.5, treadmill

    # A label_override groups with the corrected type, not with
    # whatever Garmin guessed.
    eff_conn.execute(
        "UPDATE exercise_sessions SET label_override = 'natation' "
        "WHERE uuid = 's2'",
    )
    eff_conn.commit()
    relabelled = effort_progression(
        eff_conn, eff_uid, (base + dt.timedelta(days=14)).isoformat(),
    )
    assert relabelled["natation"]["sessions"] == 1, relabelled
    assert relabelled["running_treadmill"]["sessions"] == 2, relabelled
    assert "change" not in relabelled["natation"], relabelled

    # What is left to eat today, precomputed so the message never
    # has to subtract. The fixture logs 1900 kcal / 140 g protein on
    # end_date against a 1700 kcal / 143 g budget, so calories come
    # back negative -- kept, not floored, because "200 kcal over" is
    # the thing worth saying -- while protein still has 3 g to go.
    remaining = remaining_today(conn, uid, end_date)
    assert remaining["logged"]["calories_kcal"] == 1900, remaining
    assert remaining["remaining"]["calories_kcal"] == -200, remaining
    assert remaining["remaining"]["protein_g"] == 3.0, remaining
    # Nothing drunk yet today: the whole hydration budget is open.
    assert (
        remaining["remaining"]["hydration_ml"]
        == remaining["targets"]["hydration_target_ml"]
    ), remaining

    # Logging more shrinks the remainder by exactly that much.
    conn.execute(
        "INSERT INTO nutrition VALUES "
        "(?, ?, ?, ?, ?, NULL, 300, 25, 20, 10)",
        ("today-snack", uid, f"{end_date}T16:00:00+00:00",
         f"{end_date}T16:30:00+00:00", end_date),
    )
    conn.commit()
    after = remaining_today(conn, uid, end_date)
    assert after["logged"]["calories_kcal"] == 2200, after
    assert after["remaining"]["calories_kcal"] == -500, after
    assert after["remaining"]["protein_g"] == -22.0, after
    conn.execute("DELETE FROM nutrition WHERE uuid = 'today-snack'")
    conn.commit()
    assert remaining_today(conn, uid, end_date) == remaining

    # Until an export has been parsed there is no coverage on record,
    # and "we don't know" must not be reported as "the day is missing".
    assert export_reaches(conn, uid, "nutrition", end_date) is None
    assert nutrition_gap(conn, uid, end_date)["data_missing"] is False

    # Once one has, a day past its last_date is missing as a matter
    # of fact, not of heuristic -- the athlete's empty log here is
    # the export stopping short, not a day without food.
    yesterday = (
        dt.date.fromisoformat(end_date) - dt.timedelta(days=1)
    ).isoformat()
    conn.execute(
        "INSERT INTO hc_export_coverage (user_id, table_name, "
        "first_date, last_date, observed_at) VALUES (?, 'nutrition', "
        "?, ?, '2026-07-28T04:00:00+00:00')",
        (uid, base.isoformat(),
         (dt.date.fromisoformat(yesterday) - dt.timedelta(days=2)).isoformat()),
    )
    conn.commit()
    assert export_reaches(conn, uid, "nutrition", yesterday) is False
    stale = nutrition_gap(conn, uid, end_date)
    assert stale["data_missing"] is True, stale
    assert stale["log_looks_incomplete"] is True, stale
    # Coverage stops two days short here (last_date = yesterday - 2),
    # so the day before yesterday isn't covered either -- no fallback
    # to offer.
    assert "nutrition_fallback" not in stale, stale

    # The real bug report: export coverage said yesterday was missing
    # even though the athlete's food was fully logged and visible in
    # the app -- an earlier, less-complete snapshot got ingested than
    # what became available later. day_before (base+25, already
    # carrying the fixture's normal 1900 kcal/140g-protein row from
    # the base loop) IS covered once coverage reaches one day less
    # than "yesterday" needs -- surface it, clearly dated.
    day_before = (
        dt.date.fromisoformat(yesterday) - dt.timedelta(days=1)
    ).isoformat()
    conn.execute(
        "UPDATE hc_export_coverage SET last_date = ? WHERE user_id = ? "
        "AND table_name = 'nutrition'",
        (day_before, uid),
    )
    conn.commit()
    assert export_reaches(conn, uid, "nutrition", yesterday) is False
    assert export_reaches(conn, uid, "nutrition", day_before) is True
    with_nutrition_fallback = nutrition_gap(conn, uid, end_date)
    assert with_nutrition_fallback["data_missing"] is True
    # Food is gone (yesterday's export coverage is missing), but
    # hydration is untouched -- it is its own, separate signal.
    assert "calories_kcal" not in with_nutrition_fallback["actual"]
    assert with_nutrition_fallback["actual"]["hydration_ml"] == 2100
    fb = with_nutrition_fallback["nutrition_fallback"]
    assert fb["date"] == day_before, fb
    assert fb["actual"]["calories_kcal"] == 1900, fb
    assert fb["actual"]["protein_g"] == 140.0, fb
    day_before_target = macro_targets(conn, uid, day_before)
    assert fb["gap"]["calories_kcal"] == round(
        day_before_target["calorie_target_kcal"] - 1900, 1,
    ), fb

    # A day the export did reach is judged on its contents as before.
    assert export_reaches(conn, uid, "nutrition", base.isoformat()) is True
    conn.execute(
        "UPDATE hc_export_coverage SET last_date = ? WHERE user_id = ? "
        "AND table_name = 'nutrition'",
        (end_date, uid),
    )
    conn.commit()
    assert nutrition_gap(conn, uid, end_date)["data_missing"] is False

    # Hydration syncs independently of nutrition and lags behind it
    # routinely -- the bug report this guards: MyFitnessPal's meals
    # reached the export through yesterday while its water total for
    # that SAME day stayed a sync behind. Nutrition being current
    # must not vouch for hydration.
    conn.execute(
        "INSERT INTO hc_export_coverage (user_id, table_name, "
        "first_date, last_date, observed_at) VALUES (?, 'hydration', "
        "?, ?, '2026-07-28T04:00:00+00:00')",
        (uid, base.isoformat(),
         (dt.date.fromisoformat(yesterday) - dt.timedelta(days=1)).isoformat()),
    )
    conn.commit()
    lagging = nutrition_gap(conn, uid, end_date)
    assert lagging["data_missing"] is False, lagging  # nutrition is current
    assert lagging["hydration_data_missing"] is True, lagging
    assert "hydration_ml" not in lagging["actual"], lagging
    assert "hydration_ml" not in lagging["gap"], lagging
    # A stale row for yesterday already sitting in the table (from an
    # earlier, now-superseded ingest) must not leak through either.
    conn.execute(
        "INSERT INTO hydration (uuid, user_id, start_utc, end_utc, "
        "local_date, volume_ml) VALUES ('stale-hyd', ?, ?, ?, ?, 1126)",
        (uid, f"{yesterday}T08:00:00+00:00",
         f"{yesterday}T08:05:00+00:00", yesterday),
    )
    conn.commit()
    still_lagging = nutrition_gap(conn, uid, end_date)
    assert "hydration_ml" not in still_lagging["actual"], still_lagging
    # No fallback yet: coverage reaches day_before (set up above) but
    # there is no actual reading there in this fixture.
    assert "hydration_fallback" not in still_lagging, still_lagging

    # The structural case this exists for: yesterday's hydration is
    # never going to show up in time (confirmed against two real
    # exports four days apart, both exactly one day behind on
    # hydration specifically), but the day before it has a real,
    # covered reading -- surface that, clearly dated, rather than
    # going dark on hydration every single morning.
    day_before = (
        dt.date.fromisoformat(yesterday) - dt.timedelta(days=1)
    ).isoformat()
    conn.execute(
        "INSERT INTO hydration (uuid, user_id, start_utc, end_utc, "
        "local_date, volume_ml) VALUES ('fallback-hyd', ?, ?, ?, ?, "
        "2400)",
        (uid, f"{day_before}T08:00:00+00:00",
         f"{day_before}T08:05:00+00:00", day_before),
    )
    conn.commit()
    with_fallback = nutrition_gap(conn, uid, end_date)
    assert "hydration_ml" not in with_fallback["actual"], with_fallback
    fallback = with_fallback["hydration_fallback"]
    assert fallback["date"] == day_before, fallback
    assert fallback["actual_ml"] == 2400, fallback
    assert fallback["gap_ml"] == round(
        with_fallback["targets"]["hydration_target_ml"] - 2400, 1,
    ), fallback
    conn.execute("DELETE FROM hydration WHERE uuid = 'fallback-hyd'")
    conn.commit()

    # A Garmin reading for yesterday bypasses all of the above: it
    # carries no export lag, so it settles the question outright,
    # even while Health Connect's own coverage is still a day behind
    # (still true at this point in the fixture) and even with a
    # stale HC row still sitting in the table for the same date.
    conn.execute(
        "INSERT INTO garmin_hydration (user_id, local_date, volume_ml) "
        "VALUES (?, ?, 2750.0)", (uid, yesterday),
    )
    conn.commit()
    garmin_wins = nutrition_gap(conn, uid, end_date)
    assert garmin_wins["hydration_data_missing"] is False, garmin_wins
    assert "hydration_fallback" not in garmin_wins, garmin_wins
    assert garmin_wins["actual"]["hydration_ml"] == 2750, garmin_wins
    conn.execute("DELETE FROM garmin_hydration WHERE user_id = ?", (uid,))
    conn.commit()

    # Once hydration's own coverage catches up, it is trusted again.
    conn.execute(
        "UPDATE hc_export_coverage SET last_date = ? WHERE user_id = ? "
        "AND table_name = 'hydration'",
        (end_date, uid),
    )
    conn.commit()
    caught_up = nutrition_gap(conn, uid, end_date)
    assert caught_up["hydration_data_missing"] is False, caught_up
    # 2100 from the base fixture's own hyd26 row (same date) + this
    # test's 1126 -- summed, since both are now-legitimate readings
    # once the table's coverage says the day is trustworthy.
    assert caught_up["actual"]["hydration_ml"] == 3226, caught_up
    conn.execute("DELETE FROM hydration WHERE uuid = 'stale-hyd'")
    conn.commit()

    # This scenario's protein gap (~2.7g) is below the nudge threshold
    # (by design -- not every tiny miss is worth a calendar nudge);
    # hydration and calories are the ones that should surface here.
    nudge_fr = format_nutrition_nudge(gap, "fr")
    assert "eau" in nudge_fr and "kcal" in nudge_fr, nudge_fr
    nudge_en = format_nutrition_nudge(gap, "en")
    assert "water" in nudge_en and "kcal" in nudge_en, nudge_en
    assert format_nutrition_nudge({}, "fr") == ""
    # Under threshold.
    assert format_nutrition_nudge({"gap": {"protein_g": 2}}, "fr") == ""

    # An unusable food log silences the food half of the nudge -- a
    # calendar entry telling the athlete to eat 120g of protein they
    # already ate is worse than no entry -- while hydration, which
    # comes from its own records, still surfaces.
    unusable = {
        "gap": {"protein_g": 120, "calories_kcal": -800, "hydration_ml": 900},
        "log_looks_incomplete": True,
    }
    unusable_nudge = format_nutrition_nudge(unusable, "fr")
    assert "proteines" not in unusable_nudge, unusable_nudge
    assert "kcal" not in unusable_nudge, unusable_nudge
    assert "eau" in unusable_nudge, unusable_nudge

    # Recalibration: actual rate (~-0.18kg/wk, weight barely moved
    # after day 14) is well short of the -0.4kg/wk target -> flagged,
    # with a suggested tightening of the calorie target.
    recal = recalibration_check(conn, uid, end_date)
    assert recal["flagged"] is True, recal
    assert -0.25 < recal["actual_weekly_kg"] < -0.1, recal
    assert recal["suggested_daily_calorie_adjustment_kcal"] < 0, recal
    assert bundle["recalibration"] == recalibration_check(conn, uid, end_date)
    # Maintenance goal (0) never flags, regardless of actual trend.
    db.set_setting(conn, uid, "weekly_weight_change_kg", "0")
    assert recalibration_check(conn, uid, end_date) == {"flagged": False}
    db.set_setting(conn, uid, "weekly_weight_change_kg", "-0.4")

    print("progress.py: all checks passed")
