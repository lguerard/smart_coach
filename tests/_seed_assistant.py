"""A month of believable data for one account, for the assistant's
self-check (and for trying the chat by hand).

``scale`` shifts every number so two seeded accounts never share a
value: the scoping checks look for the other account's figures in a
tool's output, so they must be unmistakable.
"""

import datetime as dt
import json

import db
import training


def seed(conn, uid: int, today: str, scale: float = 1.0, tag: str = "a") -> None:
    """Fill one account with 30 days of data ending ``today``."""
    base = dt.date.fromisoformat(today)
    schedule = training.schedule_for_user(conn, uid)
    for k in range(30, -1, -1):
        day = (base - dt.timedelta(days=k)).isoformat()
        noon = f"{day}T11:00:00+00:00"
        u = f"{tag}-{day}"
        conn.execute("INSERT INTO weight VALUES (?, ?, ?, ?, ?)",
                     (f"w-{u}", uid, noon, day, round((80.0 - 0.04 * (30 - k)) * scale, 2)))
        conn.execute("INSERT INTO resting_heart_rate VALUES (?, ?, ?, ?, ?)",
                     (f"r-{u}", uid, noon, day, round((54 + (k % 4)) * scale)))
        conn.execute("INSERT INTO garmin_hrv VALUES (?, ?, ?, ?, ?)",
                     (uid, day, round((48 + (k % 5)) * scale, 1), 48 * scale, "BALANCED"))
        conn.execute("INSERT INTO garmin_training_readiness VALUES (?, ?, ?, ?, ?)",
                     (uid, day, round((62 + k % 7) * scale) % 100, "MODERATE", ""))
        conn.execute("INSERT INTO garmin_body_battery VALUES (?, ?, ?, ?, ?, ?)",
                     (uid, day, 60, 55, round(70 * scale) % 100, 20))
        conn.execute(
            "INSERT INTO garmin_daily_summary (user_id, local_date, total_steps, "
            "resting_hr, total_kcal, moderate_intensity_min, vigorous_intensity_min) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, day, round((7000 + 150 * (k % 9)) * scale), round(55 * scale),
             2400 * scale, 20, 5))
        conn.execute("INSERT INTO training_load VALUES (?, ?, ?, ?, ?, ?)",
                     (uid, day, 40.0, 35.0 * scale, 42.0 * scale, -7.0 * scale))
        if k > 0:  # nutrition lags a day, like the real export
            for meal, kcal, protein in ((1, 450, 25), (2, 750, 40), (3, 700, 35)):
                conn.execute(
                    "INSERT INTO nutrition VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"n{meal}-{u}", uid, noon, noon, day, meal,
                     kcal * scale, protein * scale, 60.0, 20.0))
        # The night that ended this morning: in bed 23:00 -> 06:45 UTC.
        start = dt.datetime.combine(base - dt.timedelta(days=k + 1), dt.time(22, 0),
                                    tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(hours=7, minutes=45 - (k % 3) * 20)
        conn.execute(
            "INSERT INTO sleep_sessions (uuid, user_id, start_utc, end_utc, "
            "local_date, sleep_score) VALUES (?, ?, ?, ?, ?, ?)",
            (f"s-{u}", uid, start.isoformat(), end.isoformat(), day,
             round((72 + k % 9) * scale) % 101))
        mid = start + dt.timedelta(hours=3)
        for a, b, stage in ((start, mid, 4), (mid, mid + dt.timedelta(minutes=50), 5),
                            (mid + dt.timedelta(minutes=50), end, 6)):
            conn.execute("INSERT INTO sleep_stages VALUES (?, ?, ?, ?, ?)",
                         (f"s-{u}", uid, a.isoformat(), b.isoformat(), stage))
        planned = schedule[dt.date.fromisoformat(day).weekday()].get("session_type")
        if planned and k <= 14:
            conn.execute(
                "INSERT INTO coach_log (user_id, created_at, local_date, status, "
                "session_type, level, message, tier, level_reason) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, 'train', ?)",
                (uid, noon, day, "yellow" if k % 3 else "green", planned,
                 3 + (k % 2), f"Message du matin {tag} {day}",
                 "meme niveau : la nuit a ete courte"))
            if k > 0 and k % 4:  # most sessions done, a few skipped
                s0 = dt.datetime.fromisoformat(f"{day}T18:00:00+00:00")
                conn.execute(
                    "INSERT INTO exercise_sessions (uuid, user_id, start_utc, "
                    "end_utc, local_date, exercise_type, title) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?)",
                    (f"e-{u}", uid, s0.isoformat(),
                     (s0 + dt.timedelta(minutes=round(30 * scale))).isoformat(),
                     day, 79, training.SESSION_LABEL_FR[planned]))
                conn.execute(
                    "INSERT INTO session_feedback VALUES (?, ?, ?, ?, 'x')",
                    (uid, day, planned, "right"))
    for days_ago, cm in ((30, 92.0), (2, 90.5)):
        day = (base - dt.timedelta(days=days_ago)).isoformat()
        conn.execute("INSERT INTO waist_measurements VALUES (?, ?, ?, 'x')",
                     (uid, day, cm * scale))
    yesterday = (base - dt.timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO sleep_feedback VALUES (?, ?, 'bad', 'x')", (uid, yesterday))
    conn.execute(
        "INSERT INTO sent_messages (user_id, sent_utc, local_date, local_time, "
        "kind, title, text) VALUES (?, 'x', ?, '07:45', 'morning', 'Smart Coach', ?)",
        (uid, yesterday, f"Bonjour {tag}, message envoye hier"))
    conn.execute("INSERT INTO achievements VALUES (?, 'first_sync', ?)", (uid, yesterday))
    conn.execute("INSERT INTO ingest_runs (user_id, ran_at, table_name, row_count) "
                 "VALUES (?, ?, 'garmin:sleep', 3)",
                 (uid, dt.datetime.now(dt.timezone.utc).isoformat()))
    conn.execute(
        "INSERT INTO meal_estimates (user_id, local_date, kind, analysis, added, "
        "created_at) VALUES (?, ?, 'plate', ?, 1, ?)",
        (uid, today, json.dumps({"items": [{"name": f"poulet {tag}"}],
                                 "total": {"kcal": 500, "protein_g": 40}}),
         f"{today}T11:00:00+00:00"))
    for key, value in (("height_cm", "180"), ("age_years", "40"), ("sex", "male"),
                       ("target_weight_kg", str(round(74 * scale, 1)))):
        db.set_setting(conn, uid, key, value)
    conn.commit()
