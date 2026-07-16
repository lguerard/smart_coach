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
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        table (str): ``weight`` or ``body_fat``.
        value_col (str): ``kg`` or ``percentage``.
        end_date (str): ISO local date, window end.
        days (int): Total window length.

    Returns:
        dict: ``current_avg``, ``past_avg``, ``delta`` (current minus
        past), or ``{}`` if not enough readings in either half.
    """
    end = dt.date.fromisoformat(end_date)
    mid = (end - dt.timedelta(days=days // 2)).isoformat()
    start = (end - dt.timedelta(days=days)).isoformat()

    def avg(lo: str, hi: str) -> Optional[float]:
        row = conn.execute(
            f"SELECT AVG({value_col}) AS avg, COUNT(*) AS n FROM {table} "
            "WHERE user_id = ? AND local_date BETWEEN ? AND ?",
            (user_id, lo, hi),
        ).fetchone()
        return row["avg"] if row["n"] else None

    current_avg = avg(mid, end_date)
    past_avg = avg(start, mid)
    if current_avg is None or past_avg is None:
        return {}
    return {
        "current_avg": round(current_avg, 2),
        "past_avg": round(past_avg, 2),
        "delta": round(current_avg - past_avg, 2),
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


def estimate_bmr(
    conn: sqlite3.Connection, user_id: int, weight_kg: float,
) -> Optional[float]:
    """Mifflin-St Jeor BMR estimate from settings + current weight.

    Only used as a last-resort fallback when neither a device-reported
    basal_metabolic_rate reading nor a manual override is available.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
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
        conn (sqlite3.Connection): smart_sport db connection.
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
        conn (sqlite3.Connection): smart_sport db connection.
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
        conn (sqlite3.Connection): smart_sport db connection.
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
    active_calories when the device doesn't report a TDEE directly.
    Only looks at days before ``date`` (today is always incomplete).

    Returns:
        float | None: kcal/day, or ``None`` if no BMR is derivable
        either (no device reading, no manual override, no
        height/age/sex in Settings).
    """
    start = (
        dt.date.fromisoformat(date) - dt.timedelta(days=7)
    ).isoformat()
    end = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    avg_total = conn.execute(
        "SELECT AVG(daily_total) AS avg FROM (SELECT local_date, "
        "SUM(kcal) AS daily_total FROM total_calories_burned WHERE "
        "user_id = ? AND local_date BETWEEN ? AND ? GROUP BY "
        "local_date)", (user_id, start, end),
    ).fetchone()["avg"]
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


def yesterday_intake(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> dict:
    """What was actually logged the day before ``date``.

    Returns:
        dict: ``calories_kcal``/``protein_g``/``carbs_g``/``fat_g``
        (only if nutrition was logged) and ``hydration_ml`` (only if
        hydration was logged), for yesterday.
    """
    yesterday = (
        dt.date.fromisoformat(date) - dt.timedelta(days=1)
    ).isoformat()
    row = conn.execute(
        "SELECT SUM(calories) AS calories, SUM(protein_g) AS protein_g, "
        "SUM(carbs_g) AS carbs_g, SUM(fat_g) AS fat_g FROM nutrition "
        "WHERE user_id = ? AND local_date = ?", (user_id, yesterday),
    ).fetchone()
    hydration = conn.execute(
        "SELECT SUM(volume_ml) AS total FROM hydration WHERE "
        "user_id = ? AND local_date = ?", (user_id, yesterday),
    ).fetchone()["total"]

    result: dict = {}
    if row["calories"] is not None:
        result.update(
            calories_kcal=round(row["calories"]),
            protein_g=round(row["protein_g"] or 0, 1),
            carbs_g=round(row["carbs_g"] or 0, 1),
            fat_g=round(row["fat_g"] or 0, 1),
        )
    if hydration is not None:
        result["hydration_ml"] = round(hydration)
    return result


_GAP_FIELDS = [
    ("calories_kcal", "calorie_target_kcal"),
    ("protein_g", "protein_target_g"),
    ("fat_g", "fat_target_g"),
    ("carbs_g", "carb_target_g"),
    ("hydration_ml", "hydration_target_ml"),
]


def nutrition_gap(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Yesterday's targets vs what was actually logged, for the coach.

    This is what makes food/hydration suggestions concrete instead of
    generic: the LLM is handed an already-computed gap (e.g. "42g
    protein short of target"), not raw numbers to do arithmetic on.

    Returns:
        dict: ``targets``, ``actual``, ``gap`` (target minus actual,
        positive = still short, negative = exceeded; only for fields
        present in both), ``date`` (yesterday's date).
    """
    yesterday = (
        dt.date.fromisoformat(date) - dt.timedelta(days=1)
    ).isoformat()
    targets = macro_targets(conn, user_id, yesterday)
    actual = yesterday_intake(conn, user_id, date)
    gap = {
        actual_key: round(targets[target_key] - actual[actual_key], 1)
        for actual_key, target_key in _GAP_FIELDS
        if actual_key in actual and target_key in targets
    }
    return {
        "date": yesterday, "targets": targets, "actual": actual, "gap": gap,
    }


# Thresholds below which a gap isn't worth nudging about on the
# calendar (the LLM message covers the full picture either way; this
# is just the short, deterministic line that rides alongside tonight's
# workout numbers, so it doesn't wait on/depend on an LLM call).
NUDGE_PROTEIN_MIN_G = 5
NUDGE_HYDRATION_MIN_ML = 200
NUDGE_CALORIE_SURPLUS_MIN_KCAL = 100


def format_nutrition_nudge(gap: dict, language: str = "fr") -> str:
    """One-line nutrition/hydration reminder for the calendar event.

    Deterministic (no LLM call) so the calendar update in the morning
    doesn't wait on/depend on the coaching-message call -- it reuses
    the same gap numbers the LLM message narrates around.

    Parameters:
        gap (dict): ``nutrition_gap(...)["gap"]``.
        language (str): "fr" or "en".

    Returns:
        str: A short line, or "" if nothing is worth flagging (no
        data, or everything's within threshold).
    """
    parts = []
    protein_gap = gap.get("protein_g")
    if protein_gap is not None and protein_gap > NUDGE_PROTEIN_MIN_G:
        parts.append(
            f"+{round(protein_gap)}g proteines" if language == "fr"
            else f"+{round(protein_gap)}g protein"
        )
    hydration_gap = gap.get("hydration_ml")
    if hydration_gap is not None and hydration_gap > NUDGE_HYDRATION_MIN_ML:
        liters = round(hydration_gap / 1000, 1)
        parts.append(f"{liters}L d'eau" if language == "fr" else f"{liters}L water")
    calorie_gap = gap.get("calories_kcal")
    if calorie_gap is not None and calorie_gap < -NUDGE_CALORIE_SURPLUS_MIN_KCAL:
        over = abs(round(calorie_gap))
        parts.append(
            f"-{over}kcal (hier en surplus)" if language == "fr"
            else f"-{over}kcal (surplus yesterday)"
        )
    if not parts:
        return ""
    return "Nutrition: " + ", ".join(parts)


def detect_plateau(weight: dict, calories: dict) -> dict:
    """Flag a weight-loss plateau despite a real logged deficit.

    Parameters:
        weight (dict): ``weight_trend`` output.
        calories (dict): ``calorie_balance_for_range`` output.

    Returns:
        dict: ``{"plateau": bool, "note": str | None}``. ``note`` is a
        plain-language flag for the LLM/dashboard to surface, not a
        prescription -- it names the situation, the LLM phrases advice.
    """
    if not weight or "delta" not in weight:
        return {"plateau": False, "note": None}
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
    """Actual weekly weight-change rate from the earliest/latest
    readings in a trailing window (more stable than day-to-day noise).

    Returns:
        float | None: kg/week (signed), or ``None`` if fewer than 2
        readings span at least 7 days in the window.
    """
    start = (
        dt.date.fromisoformat(end_date) - dt.timedelta(days=days)
    ).isoformat()
    rows = conn.execute(
        "SELECT local_date, kg FROM weight WHERE user_id = ? AND "
        "local_date BETWEEN ? AND ? ORDER BY local_date",
        (user_id, start, end_date),
    ).fetchall()
    if len(rows) < 2:
        return None
    first, last = rows[0], rows[-1]
    span_days = (
        dt.date.fromisoformat(last["local_date"])
        - dt.date.fromisoformat(first["local_date"])
    ).days
    if span_days < 7:
        return None
    return (last["kg"] - first["kg"]) / span_days * 7


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
        ``calorie_balance_7d``, ``protein_7d``, ``plateau``,
        ``recalibration`` -- empty sub-dicts where there isn't enough
        data yet.
    """
    weight = weight_trend(conn, user_id, date)
    calories = calorie_balance_for_range(conn, user_id, date)
    return {
        "weight_trend_14d": weight,
        "body_fat_trend_28d": body_fat_trend(conn, user_id, date),
        "calorie_balance_7d": calories,
        "protein_7d": protein_trend(conn, user_id, date),
        "plateau": detect_plateau(weight, calories),
        "nutrition_yesterday": nutrition_gap(conn, user_id, date),
        "recalibration": recalibration_check(conn, user_id, date),
    }


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "smart_sport.db"
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
    assert trend["delta"] < 0, trend
    assert trend["current_avg"] < 900  # not contaminated by the other user

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

    bundle = weekly_progress(conn, uid, end_date)
    assert bundle["weight_trend_14d"] == trend
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

    # This scenario's protein gap (~2.7g) is below the nudge threshold
    # (by design -- not every tiny miss is worth a calendar nudge);
    # hydration and calories are the ones that should surface here.
    nudge_fr = format_nutrition_nudge(gap["gap"], "fr")
    assert "eau" in nudge_fr and "kcal" in nudge_fr, nudge_fr
    nudge_en = format_nutrition_nudge(gap["gap"], "en")
    assert "water" in nudge_en and "kcal" in nudge_en, nudge_en
    assert format_nutrition_nudge({}, "fr") == ""
    assert format_nutrition_nudge({"protein_g": 2}, "fr") == ""  # under threshold

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
