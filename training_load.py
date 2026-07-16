#!/usr/bin/env python3
"""Fitness/Fatigue/Form (CTL/ATL/TSB), intervals.icu/TrainingPeaks-style.

Daily training stress has no power-meter or reliable-RPE data behind
it (see training.py's docstring -- Health Connect carries neither for
these bodyweight/treadmill sessions), so load is approximated as
total exercise minutes that day weighted by smart_sport's own
prescribed level for that date (0-10, from ``coach_log`` -- the same
number already driving the workout itself, level 0 -> factor 1.0,
level 10 -> factor 2.0). This is a deliberate proxy, not a physiological
measurement; it is internally consistent (harder prescribed sessions
count for more) which is what CTL/ATL/TSB need to be directionally
useful.

CTL (Fitness) is a 42-day exponentially-weighted moving average of
daily load, ATL (Fatigue) the same over 7 days, TSB (Form) = yesterday's
CTL minus yesterday's ATL -- the accumulated fitness-minus-fatigue
going into today, before today's own session lands.
"""

import datetime as dt
import math
import sqlite3

CTL_DAYS = 42
ATL_DAYS = 7
DEFAULT_WINDOW_DAYS = 180  # ample for the EWMA seed-zero bias to decay

_CTL_ALPHA = 1 - math.exp(-1 / CTL_DAYS)
_ATL_ALPHA = 1 - math.exp(-1 / ATL_DAYS)


def _daily_load(
    conn: sqlite3.Connection, user_id: int, local_date: str,
) -> float:
    """Training stress for one day: exercise minutes * level factor.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        local_date (str): ISO local date.

    Returns:
        float: Daily load (0 on rest days).
    """
    row = conn.execute(
        "SELECT level FROM coach_log WHERE user_id = ? AND local_date = ? "
        "ORDER BY created_at DESC LIMIT 1", (user_id, local_date),
    ).fetchone()
    level = row["level"] if row and row["level"] is not None else 0
    factor = 1 + level / 10

    rows = conn.execute(
        "SELECT start_utc, end_utc FROM exercise_sessions WHERE "
        "user_id = ? AND local_date = ?", (user_id, local_date),
    ).fetchall()
    total_minutes = sum(
        (
            dt.datetime.fromisoformat(r["end_utc"])
            - dt.datetime.fromisoformat(r["start_utc"])
        ).total_seconds() / 60
        for r in rows
    )
    return total_minutes * factor


def compute_training_load(
    conn: sqlite3.Connection, user_id: int, through_date: str,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> None:
    """Recompute and persist CTL/ATL/TSB for a trailing window.

    Recomputes from scratch every call (no incremental state) --
    cheap at one row/day, and avoids a migration story if the EWMA
    formula ever changes.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        through_date (str): ISO local date, last day to compute.
        window_days (int): Lookback length feeding the EWMA seed.
    """
    start = dt.date.fromisoformat(through_date) - dt.timedelta(
        days=window_days,
    )
    end_date = dt.date.fromisoformat(through_date)
    conn.execute(
        "DELETE FROM training_load WHERE user_id = ? AND local_date >= ?",
        (user_id, start.isoformat()),
    )

    ctl = atl = 0.0
    current = start
    rows = []
    while current <= end_date:
        iso = current.isoformat()
        tsb = ctl - atl
        load = _daily_load(conn, user_id, iso)
        ctl += (load - ctl) * _CTL_ALPHA
        atl += (load - atl) * _ATL_ALPHA
        rows.append((user_id, iso, load, ctl, atl, tsb))
        current += dt.timedelta(days=1)

    conn.executemany(
        "INSERT INTO training_load (user_id, local_date, daily_load, "
        "ctl, atl, tsb) VALUES (?, ?, ?, ?, ?, ?)", rows,
    )
    conn.commit()


def training_load_history(
    conn: sqlite3.Connection, user_id: int, days: int = 90,
) -> list[dict]:
    """Chart-ready CTL/ATL/TSB series for the last ``days`` days.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        days (int): Lookback length.

    Returns:
        list[dict]: Oldest-first rows with ``local_date``,
        ``daily_load``, ``ctl``, ``atl``, ``tsb``.
    """
    rows = conn.execute(
        "SELECT local_date, daily_load, ctl, atl, tsb FROM training_load "
        "WHERE user_id = ? ORDER BY local_date DESC LIMIT ?",
        (user_id, days),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def latest_training_load(
    conn: sqlite3.Connection, user_id: int,
) -> dict | None:
    """Most recent CTL/ATL/TSB snapshot, or ``None`` if none computed.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.

    Returns:
        dict | None: ``local_date``, ``ctl``, ``atl``, ``tsb``.
    """
    row = conn.execute(
        "SELECT local_date, daily_load, ctl, atl, tsb FROM training_load "
        "WHERE user_id = ? ORDER BY local_date DESC LIMIT 1", (user_id,),
    ).fetchone()
    return dict(row) if row else None


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db as db_module

    tmp = Path(tempfile.mkdtemp()) / "smart_sport.db"
    conn = db_module.connect(tmp)
    db_module.init_db(conn)
    uid = db_module.create_user(conn, "test", "password1234")
    other_uid = db_module.create_user(conn, "other", "password1234")

    # 10 days of steady 30-minute level-5 sessions -> load should ramp
    # CTL/ATL up from zero, ATL faster than CTL (short vs long EWMA).
    for i in range(10):
        d = (dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, "
            "status, session_type, level, message) VALUES "
            "(?, ?, ?, 'green', 'treadmill', 5, 'x')",
            (uid, f"{d}T08:00:00+00:00", d),
        )
        conn.execute(
            "INSERT INTO exercise_sessions (uuid, user_id, start_utc, "
            "end_utc, local_date, exercise_type, title, notes, rpe) "
            "VALUES (?, ?, ?, ?, ?, 1, 't', NULL, NULL)",
            (
                f"ex{i}", uid, f"{d}T08:00:00+00:00",
                f"{d}T08:30:00+00:00", d,
            ),
        )
    conn.commit()

    compute_training_load(conn, uid, "2026-01-10", window_days=30)
    history = training_load_history(conn, uid, days=31)
    assert len(history) == 31  # window_days + through_date, inclusive
    assert history[0]["ctl"] == 0.0  # first day, EWMA hasn't moved yet
    last = history[-1]
    assert last["local_date"] == "2026-01-10"
    # 30min * (1 + 5/10) = 45 load/day once sessions start
    assert abs(last["daily_load"] - 45.0) < 0.01
    assert last["atl"] > last["ctl"] > 0  # short EWMA reacts faster
    assert last["tsb"] < 0  # accumulating fatigue faster than fitness

    latest = latest_training_load(conn, uid)
    assert latest["local_date"] == "2026-01-10"

    # Isolation: other_uid has no sessions/coach_log at all.
    compute_training_load(conn, other_uid, "2026-01-10", window_days=30)
    other_history = training_load_history(conn, other_uid, days=31)
    assert all(r["daily_load"] == 0.0 for r in other_history)
    assert other_history[-1]["ctl"] == 0.0
    assert latest_training_load(conn, other_uid)["ctl"] == 0.0

    print("training_load.py: all checks passed")
