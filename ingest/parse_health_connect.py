#!/usr/bin/env python3
"""Parse a Health Connect export (raw internal SQLite backup) and
upsert its records into smart_coach's own database.

Each export is a full on-device snapshot, not an incremental diff, so
ingestion is idempotent by design: HC records are upserted by their
stable ``uuid`` (``INSERT ... ON CONFLICT DO UPDATE``), and the one
derived/aggregate table (``heart_rate_daily``) is fully recomputed
from the export each run rather than incrementally accumulated, which
would double-count across runs.

Exercise type / sleep stage constants below are taken verbatim from
androidx.health.connect.client.records (ExerciseSessionRecord,
SleepSessionRecord) -- Health Connect itself stores only the int, no
lookup table, so smart_coach ships its own copy for display purposes.
"""

import datetime as dt
import sqlite3
from pathlib import Path

SLEEP_STAGE_LABELS = {
    1: "awake",
    2: "sleeping",
    3: "out_of_bed",
    4: "light",
    5: "deep",
    6: "rem",
    7: "awake_in_bed",
}

# Health Connect's Room db normalizes each physical-quantity type to
# one internal unit, independent of what unit the public API exposes
# a field as -- empirically confirmed against a real export (weight
# ~89kg person stored as raw 89250; a plausible ~2200kcal/day TDEE
# stored as raw 2221000; a plausible ~1780kcal/day BMR stored as raw
# 86.05; distance/elevation/floors matched real-world values with NO
# scaling). Volume (hydration) and nutrition's Mass macros are
# unverified -- currently 0 rows in the source export -- but follow
# the same Mass=grams / Volume=milliliters convention as everything
# else; re-verify once nutrition/hydration logging actually has data.
GRAMS_TO_KG = 1 / 1000  # Mass -> grams internally
CALORIES_TO_KCAL = 1 / 1000  # Energy -> (small) calories internally
WATTS_TO_KCAL_PER_DAY = 86400 / 4184  # Power -> watts internally

EXERCISE_TYPE_LABELS = {
    0: "other_workout", 2: "badminton", 4: "baseball", 5: "basketball",
    8: "biking", 9: "biking_stationary", 10: "boot_camp", 11: "boxing",
    13: "calisthenics", 14: "cricket", 16: "dancing", 25: "elliptical",
    26: "exercise_class", 27: "fencing", 28: "football_american",
    29: "football_australian", 31: "frisbee_disc", 32: "golf",
    33: "guided_breathing", 34: "gymnastics", 35: "handball",
    36: "hiit", 37: "hiking", 38: "ice_hockey", 39: "ice_skating",
    44: "martial_arts", 46: "paddling", 47: "paragliding",
    48: "pilates", 50: "racquetball", 51: "rock_climbing",
    52: "roller_hockey", 53: "rowing", 54: "rowing_machine",
    55: "rugby", 56: "running", 57: "running_treadmill", 58: "sailing",
    59: "scuba_diving", 60: "skating", 61: "skiing", 62: "snowboarding",
    63: "snowshoeing", 64: "soccer", 65: "softball", 66: "squash",
    68: "stair_climbing", 69: "stair_climbing_machine",
    70: "strength_training", 71: "stretching", 72: "surfing",
    73: "swimming_open_water", 74: "swimming_pool", 75: "table_tennis",
    76: "tennis", 78: "volleyball", 79: "walking", 80: "water_polo",
    81: "weightlifting", 82: "wheelchair", 83: "yoga",
}


def _local_date(time_ms: int, zone_offset_s: int) -> str:
    """ISO local date for an HC epoch-millis + zone-offset pair.

    Parameters:
        time_ms (int): Epoch millis (UTC).
        zone_offset_s (int): Zone offset in seconds, as stored by HC.

    Returns:
        str: ``YYYY-MM-DD`` in the record's local time.
    """
    local = dt.datetime.fromtimestamp(
        (time_ms + zone_offset_s * 1000) / 1000, tz=dt.timezone.utc
    )
    return local.date().isoformat()


def _iso_utc(time_ms: int) -> str:
    """ISO 8601 UTC timestamp from HC epoch millis.

    Parameters:
        time_ms (int): Epoch millis (UTC).

    Returns:
        str: ISO 8601 string with a ``+00:00`` offset.
    """
    return dt.datetime.fromtimestamp(
        time_ms / 1000, tz=dt.timezone.utc
    ).isoformat()


def _upsert(
    conn: sqlite3.Connection, user_id: int, table: str,
    columns: list[str], rows: list[tuple], conflict_col: str = "uuid",
) -> int:
    """Insert rows (tagged with ``user_id``), updating on a
    conflicting key.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user -- every ingested row belongs to
            whoever's Health Connect export this is.
        table (str): Target table name.
        columns (list[str]): Data column names (NOT including
            ``user_id``), matching each row tuple's order; the first
            entry must be ``conflict_col``.
        rows (list[tuple]): Rows to upsert (uuid first, matching
            ``columns``' order -- ``user_id`` is injected here).
        conflict_col (str): Unique column to upsert on.

    Returns:
        int: Number of rows upserted.
    """
    if not rows:
        return 0
    full_columns = [columns[0], "user_id", *columns[1:]]
    full_rows = [(row[0], user_id, *row[1:]) for row in rows]
    placeholders = ",".join("?" * len(full_columns))
    collist = ",".join(full_columns)
    updates = ",".join(
        f"{c}=excluded.{c}" for c in full_columns
        if c != conflict_col and c != "user_id"
    )
    conn.executemany(
        f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_col}) DO UPDATE SET {updates}",
        full_rows,
    )
    return len(full_rows)


def _simple_point_table(
    hc: sqlite3.Connection, hc_table: str, value_col: str,
    scale: float = 1.0,
) -> list[tuple]:
    """Pull a ``time``/``zone_offset``/single-value HC record table.

    Parameters:
        hc (sqlite3.Connection): Read-only HC export connection.
        hc_table (str): Source table name.
        value_col (str): Name of the single value column to read.
        scale (float): Multiplier applied to the raw value (HC's Room
            db normalizes each physical-quantity type to its own
            internal unit -- see UNIT_SCALES below).

    Returns:
        list[tuple]: ``(uuid_hex, time_utc, local_date, value)`` rows.
    """
    rows = hc.execute(
        f"SELECT uuid, time, zone_offset, {value_col} FROM {hc_table}"
    ).fetchall()
    return [
        (
            uuid.hex(), _iso_utc(time_ms), _local_date(time_ms, offset),
            value * scale,
        )
        for uuid, time_ms, offset, value in rows
    ]


def _simple_interval_table(
    hc: sqlite3.Connection, hc_table: str, value_col: str,
    scale: float = 1.0,
) -> list[tuple]:
    """Pull a ``start_time``/``end_time`` interval HC record table.

    Parameters:
        hc (sqlite3.Connection): Read-only HC export connection.
        hc_table (str): Source table name.
        value_col (str): Name of the single value column to read.
        scale (float): Multiplier applied to the raw value, see
            UNIT_SCALES below.

    Returns:
        list[tuple]: ``(uuid_hex, start_utc, end_utc, local_date,
        value)`` rows, local_date derived from the start time.
    """
    rows = hc.execute(
        f"SELECT uuid, start_time, start_zone_offset, end_time, "
        f"{value_col} FROM {hc_table}"
    ).fetchall()
    return [
        (
            uuid.hex(), _iso_utc(start_ms), _iso_utc(end_ms),
            _local_date(start_ms, offset), value * scale,
        )
        for uuid, start_ms, offset, end_ms, value in rows
    ]


def _parse_steps(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    rows = hc.execute(
        "SELECT uuid, start_time, start_zone_offset, end_time, count "
        "FROM steps_record_table"
    ).fetchall()
    data = [
        (
            uuid.hex(), _iso_utc(start_ms), _iso_utc(end_ms),
            _local_date(start_ms, offset), count,
        )
        for uuid, start_ms, offset, end_ms, count in rows
    ]
    return _upsert(
        conn, user_id, "steps",
        ["uuid", "start_utc", "end_utc", "local_date", "count"], data,
    )


def _parse_resting_heart_rate(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_point_table(hc, "resting_heart_rate_record_table",
                                "beats_per_minute")
    return _upsert(
        conn, user_id, "resting_heart_rate",
        ["uuid", "time_utc", "local_date", "bpm"], data,
    )


def _parse_weight(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_point_table(
        hc, "weight_record_table", "weight", scale=GRAMS_TO_KG,
    )
    return _upsert(
        conn, user_id, "weight",
        ["uuid", "time_utc", "local_date", "kg"], data,
    )


def _parse_body_fat(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_point_table(hc, "body_fat_record_table", "percentage")
    return _upsert(
        conn, user_id, "body_fat",
        ["uuid", "time_utc", "local_date", "percentage"], data,
    )


def _parse_lean_body_mass(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_point_table(
        hc, "lean_body_mass_record_table", "mass", scale=GRAMS_TO_KG,
    )
    return _upsert(
        conn, user_id, "lean_body_mass",
        ["uuid", "time_utc", "local_date", "kg"], data,
    )


def _parse_basal_metabolic_rate(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_point_table(
        hc, "basal_metabolic_rate_record_table", "basal_metabolic_rate",
        scale=WATTS_TO_KCAL_PER_DAY,
    )
    return _upsert(
        conn, user_id, "basal_metabolic_rate",
        ["uuid", "time_utc", "local_date", "kcal_per_day"], data,
    )


def _parse_active_calories(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_interval_table(
        hc, "active_calories_burned_record_table", "energy",
        scale=CALORIES_TO_KCAL,
    )
    return _upsert(
        conn, user_id, "active_calories",
        ["uuid", "start_utc", "end_utc", "local_date", "kcal"], data,
    )


def _parse_total_calories_burned(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_interval_table(
        hc, "total_calories_burned_record_table", "energy",
        scale=CALORIES_TO_KCAL,
    )
    return _upsert(
        conn, user_id, "total_calories_burned",
        ["uuid", "start_utc", "end_utc", "local_date", "kcal"], data,
    )


def _parse_hydration(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_interval_table(hc, "hydration_record_table", "volume")
    return _upsert(
        conn, user_id, "hydration",
        ["uuid", "start_utc", "end_utc", "local_date", "volume_ml"], data,
    )


def _parse_distance(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_interval_table(hc, "distance_record_table", "distance")
    return _upsert(
        conn, user_id, "distance",
        ["uuid", "start_utc", "end_utc", "local_date", "meters"], data,
    )


def _parse_floors_climbed(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_interval_table(hc, "floors_climbed_record_table", "floors")
    return _upsert(
        conn, user_id, "floors_climbed",
        ["uuid", "start_utc", "end_utc", "local_date", "floors"], data,
    )


def _parse_elevation_gained(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    data = _simple_interval_table(
        hc, "elevation_gained_record_table", "elevation",
    )
    return _upsert(
        conn, user_id, "elevation_gained",
        ["uuid", "start_utc", "end_utc", "local_date", "meters"], data,
    )


def _parse_nutrition(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    rows = hc.execute(
        "SELECT uuid, start_time, start_zone_offset, end_time, "
        "meal_type, energy, protein, total_carbohydrate, total_fat "
        "FROM nutrition_record_table"
    ).fetchall()
    data = [
        (
            uuid.hex(), _iso_utc(start_ms), _iso_utc(end_ms),
            _local_date(start_ms, offset), meal_type,
            energy * CALORIES_TO_KCAL if energy is not None else None,
            protein, carbs, fat,  # Mass macros: already grams, no scale
        )
        for uuid, start_ms, offset, end_ms, meal_type, energy, protein,
        carbs, fat in rows
    ]
    return _upsert(
        conn, user_id, "nutrition",
        ["uuid", "start_utc", "end_utc", "local_date", "meal_type",
         "calories", "protein_g", "carbs_g", "fat_g"], data,
    )


def _parse_heart_rate_daily(
    hc: sqlite3.Connection, conn: sqlite3.Connection, user_id: int,
) -> int:
    """Aggregate HC's per-beat heart rate series into daily stats.

    Recomputed from scratch each run (delete + reinsert) since this is
    a derived table, not a 1:1 uuid passthrough -- incrementally
    accumulating it across runs would double-count.
    """
    rows = hc.execute(
        "SELECT p.start_zone_offset, s.epoch_millis, s.beats_per_minute "
        "FROM heart_rate_record_series_table s "
        "JOIN heart_rate_record_table p ON p.row_id = s.parent_key"
    ).fetchall()
    by_date: dict[str, list[int]] = {}
    for offset, epoch_ms, bpm in rows:
        if bpm is None:
            continue
        by_date.setdefault(_local_date(epoch_ms, offset), []).append(bpm)

    conn.execute("DELETE FROM heart_rate_daily WHERE user_id = ?", (user_id,))
    data = [
        (
            user_id, date, sum(bpms) / len(bpms), min(bpms), max(bpms),
            len(bpms),
        )
        for date, bpms in by_date.items()
    ]
    if data:
        conn.executemany(
            "INSERT INTO heart_rate_daily "
            "(user_id, local_date, avg_bpm, min_bpm, max_bpm, "
            "sample_count) VALUES (?, ?, ?, ?, ?, ?)",
            data,
        )
    return len(data)


def parse_and_upsert(
    hc_export_path: Path, conn: sqlite3.Connection, user_id: int,
) -> dict[str, int]:
    """Parse a Health Connect export and upsert it into smart_coach's db.

    Parameters:
        hc_export_path (Path): Path to the extracted
            ``health_connect_export.db`` file.
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): The account this export belongs to -- every
            ingested row is tagged with it.

    Returns:
        dict[str, int]: Row counts ingested per table, also logged to
        the ``ingest_runs`` table for the dashboard's status page.
    """
    hc = sqlite3.connect(f"file:{hc_export_path}?mode=ro", uri=True)
    try:
        # Exercise sessions and sleep deliberately NOT parsed here:
        # those two domains come straight from the Garmin API
        # (ingest/garmin_api.py) -- Garmin's HC writer mislabels
        # activity types and carries no per-session HR series.
        counts = {
            "steps": _parse_steps(hc, conn, user_id),
            "heart_rate_daily": _parse_heart_rate_daily(hc, conn, user_id),
            "resting_heart_rate": _parse_resting_heart_rate(hc, conn, user_id),
            "weight": _parse_weight(hc, conn, user_id),
            "body_fat": _parse_body_fat(hc, conn, user_id),
            "lean_body_mass": _parse_lean_body_mass(hc, conn, user_id),
            "active_calories": _parse_active_calories(hc, conn, user_id),
            "total_calories_burned": _parse_total_calories_burned(hc, conn, user_id),
            "basal_metabolic_rate": _parse_basal_metabolic_rate(hc, conn, user_id),
            "nutrition": _parse_nutrition(hc, conn, user_id),
            "hydration": _parse_hydration(hc, conn, user_id),
            "distance": _parse_distance(hc, conn, user_id),
            "floors_climbed": _parse_floors_climbed(hc, conn, user_id),
            "elevation_gained": _parse_elevation_gained(hc, conn, user_id),
        }
    finally:
        hc.close()

    ran_at = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO ingest_runs (user_id, ran_at, table_name, row_count) "
        "VALUES (?, ?, ?, ?)",
        [(user_id, ran_at, table, count) for table, count in counts.items()],
    )
    conn.commit()
    return counts


# Minimal synthetic HC schema (self-check only) -- module-level so the
# multi-user isolation check can spin up a second export without
# duplicating the whole CREATE TABLE block.
_SELFCHECK_SCHEMA = (
    "CREATE TABLE steps_record_table (uuid BLOB, start_time "
    "INTEGER, start_zone_offset INTEGER, end_time INTEGER, "
    "count INTEGER);"
    "CREATE TABLE resting_heart_rate_record_table (uuid BLOB, "
    "time INTEGER, zone_offset INTEGER, beats_per_minute "
    "INTEGER);"
    "CREATE TABLE weight_record_table (uuid BLOB, time INTEGER,"
    " zone_offset INTEGER, weight REAL);"
    "CREATE TABLE body_fat_record_table (uuid BLOB, time "
    "INTEGER, zone_offset INTEGER, percentage REAL);"
    "CREATE TABLE lean_body_mass_record_table (uuid BLOB, time"
    " INTEGER, zone_offset INTEGER, mass REAL);"
    "CREATE TABLE basal_metabolic_rate_record_table (uuid BLOB,"
    " time INTEGER, zone_offset INTEGER, "
    "basal_metabolic_rate REAL);"
    "CREATE TABLE active_calories_burned_record_table (uuid "
    "BLOB, start_time INTEGER, start_zone_offset INTEGER, "
    "end_time INTEGER, energy REAL);"
    "CREATE TABLE total_calories_burned_record_table (uuid "
    "BLOB, start_time INTEGER, start_zone_offset INTEGER, "
    "end_time INTEGER, energy REAL);"
    "CREATE TABLE hydration_record_table (uuid BLOB, start_time"
    " INTEGER, start_zone_offset INTEGER, end_time INTEGER, "
    "volume REAL);"
    "CREATE TABLE distance_record_table (uuid BLOB, start_time"
    " INTEGER, start_zone_offset INTEGER, end_time INTEGER, "
    "distance REAL);"
    "CREATE TABLE floors_climbed_record_table (uuid BLOB, "
    "start_time INTEGER, start_zone_offset INTEGER, end_time "
    "INTEGER, floors REAL);"
    "CREATE TABLE elevation_gained_record_table (uuid BLOB, "
    "start_time INTEGER, start_zone_offset INTEGER, end_time "
    "INTEGER, elevation REAL);"
    "CREATE TABLE sleep_session_record_table (row_id INTEGER "
    "PRIMARY KEY, uuid BLOB, start_time INTEGER, "
    "start_zone_offset INTEGER, end_time INTEGER, title TEXT, "
    "notes TEXT);"
    "CREATE TABLE sleep_stages_table (parent_key INTEGER, "
    "stage_start_time INTEGER, stage_end_time INTEGER, "
    "stage_type INTEGER);"
    "CREATE TABLE exercise_session_record_table (uuid BLOB, "
    "start_time INTEGER, start_zone_offset INTEGER, end_time "
    "INTEGER, exercise_type INTEGER, title TEXT, notes TEXT, "
    "session_rate_of_perceived_exertion REAL);"
    "CREATE TABLE nutrition_record_table (uuid BLOB, start_time"
    " INTEGER, start_zone_offset INTEGER, end_time INTEGER, "
    "meal_type INTEGER, energy REAL, protein REAL, "
    "total_carbohydrate REAL, total_fat REAL);"
    "CREATE TABLE heart_rate_record_table (row_id INTEGER "
    "PRIMARY KEY, start_zone_offset INTEGER);"
    "CREATE TABLE heart_rate_record_series_table (parent_key "
    "INTEGER, epoch_millis INTEGER, beats_per_minute INTEGER);"
)


if __name__ == "__main__":
    import sys
    import tempfile

    # Allow `python ingest/parse_health_connect.py` (script dir on
    # sys.path, not the repo root) as well as `-m ingest...`.
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import db

    if len(sys.argv) >= 2:
        smart_coach_conn = db.connect()
        db.init_db(smart_coach_conn)
        if len(sys.argv) >= 3:
            cli_user_id = int(sys.argv[2])
        else:
            users = db.all_users(smart_coach_conn)
            if not users:
                sys.exit(
                    "No users exist yet -- run manage_users.py first, or "
                    "pass a user id: parse_health_connect.py <path> <user_id>"
                )
            cli_user_id = users[0]["id"]
        counts = parse_and_upsert(
            Path(sys.argv[1]), smart_coach_conn, cli_user_id,
        )
        for table, count in sorted(counts.items()):
            print(f"{table}: {count}")
    else:
        # Self-check against a tiny synthetic HC export.
        tmp = Path(tempfile.mkdtemp())
        hc_path = tmp / "health_connect_export.db"
        hc = sqlite3.connect(hc_path)
        hc.executescript(_SELFCHECK_SCHEMA)
        u1, u2 = b"\x01" * 16, b"\x02" * 16
        t0 = 1_700_000_000_000  # fixed reference instant
        hc.execute(
            "INSERT INTO steps_record_table VALUES (?, ?, 3600, ?, ?)",
            (u1, t0, t0 + 60_000, 500),
        )
        hc.execute(
            "INSERT INTO resting_heart_rate_record_table VALUES "
            "(?, ?, 3600, ?)", (u1, t0, 55),
        )
        # Sleep/exercise rows present in the export (as in real life)
        # but deliberately ignored -- those domains are Garmin API
        # territory now (ingest/garmin_api.py).
        hc.execute(
            "INSERT INTO sleep_session_record_table VALUES "
            "(1, ?, ?, 3600, ?, 'Sleep', NULL)",
            (u2, t0, t0 + 8 * 3600 * 1000),
        )
        hc.execute(
            "INSERT INTO sleep_stages_table VALUES (1, ?, ?, 4)",
            (t0, t0 + 3600 * 1000),
        )
        hc.execute(
            "INSERT INTO heart_rate_record_table VALUES (1, 3600)",
        )
        hc.execute(
            "INSERT INTO heart_rate_record_series_table VALUES "
            "(1, ?, 60)", (t0,),
        )
        hc.execute(
            "INSERT INTO heart_rate_record_series_table VALUES "
            "(1, ?, 80)", (t0 + 1000,),
        )
        hc.execute(
            "INSERT INTO exercise_session_record_table VALUES "
            "(?, ?, 3600, ?, 70, NULL, NULL, 7.5)",
            (u1, t0, t0 + 1800_000),
        )
        # Raw values as actually observed in a real HC export: Mass in
        # grams, Energy in calories, Power in watts -- regression
        # coverage for the unit-scaling bug (weight was off by 1000x
        # before UNIT_SCALES existed).
        hc.execute(
            "INSERT INTO weight_record_table VALUES "
            "(?, ?, 3600, 89250.0)", (u1, t0),
        )
        hc.execute(
            "INSERT INTO active_calories_burned_record_table VALUES "
            "(?, ?, 3600, ?, 400000.0)", (u1, t0, t0 + 3600_000),
        )
        hc.execute(
            "INSERT INTO basal_metabolic_rate_record_table VALUES "
            "(?, ?, 3600, 86.05)", (u1, t0),
        )
        hc.commit()
        hc.close()

        test_db_path = tmp / "smart_coach.db"
        conn = db.connect(test_db_path)
        db.init_db(conn)
        uid = db.create_user(conn, "test", "password1234")
        other_uid = db.create_user(conn, "other", "password1234")
        counts = parse_and_upsert(hc_path, conn, uid)
        assert counts["steps"] == 1
        assert counts["resting_heart_rate"] == 1
        assert counts["heart_rate_daily"] == 1
        # Sleep and exercise stay untouched by the HC parse.
        assert "sleep_sessions" not in counts
        assert "exercise_sessions" not in counts
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM sleep_sessions",
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM exercise_sessions",
        ).fetchone()["n"] == 0

        # Re-running (same export, same user) must not duplicate or
        # double-count.
        counts2 = parse_and_upsert(hc_path, conn, uid)
        assert counts2 == counts
        row = conn.execute(
            "SELECT avg_bpm, sample_count FROM heart_rate_daily WHERE "
            "user_id = ?", (uid,),
        ).fetchone()
        assert row["sample_count"] == 2
        assert row["avg_bpm"] == 70.0
        steps_row = conn.execute(
            "SELECT count FROM steps WHERE user_id = ?", (uid,),
        ).fetchone()
        assert steps_row["count"] == 500

        weight_row = conn.execute(
            "SELECT kg FROM weight WHERE user_id = ?", (uid,),
        ).fetchone()
        assert weight_row["kg"] == 89.25, weight_row["kg"]

        active_row = conn.execute(
            "SELECT kcal FROM active_calories WHERE user_id = ?", (uid,),
        ).fetchone()
        assert active_row["kcal"] == 400.0, active_row["kcal"]

        bmr_row = conn.execute(
            "SELECT kcal_per_day FROM basal_metabolic_rate WHERE "
            "user_id = ?", (uid,),
        ).fetchone()
        # 86.05 W -> ~1777 kcal/day (Power's internal unit is watts)
        assert 1770 < bmr_row["kcal_per_day"] < 1785, bmr_row["kcal_per_day"]

        # Isolation check: a second user's export (real Android UUIDs
        # are per-device cryptographically random, so two real phones
        # never produce the same uuid -- this uses a distinct uuid to
        # model that, rather than the same export twice) must not
        # affect the first user's rows at all.
        hc2_path = tmp / "health_connect_export_2.db"
        hc2 = sqlite3.connect(hc2_path)
        hc2.executescript(_SELFCHECK_SCHEMA)
        u3 = b"\x03" * 16
        hc2.execute(
            "INSERT INTO steps_record_table VALUES (?, ?, 3600, ?, ?)",
            (u3, t0, t0 + 60_000, 250),
        )
        hc2.commit()
        hc2.close()
        other_counts = parse_and_upsert(hc2_path, conn, other_uid)
        assert other_counts["steps"] == 1
        other_steps_row = conn.execute(
            "SELECT count FROM steps WHERE user_id = ?", (other_uid,),
        ).fetchone()
        assert other_steps_row["count"] == 250
        # uid's own row is untouched by the other user's ingest.
        assert conn.execute(
            "SELECT count FROM steps WHERE user_id = ?", (uid,),
        ).fetchone()["count"] == 500

        print("parse_health_connect.py: all checks passed")
