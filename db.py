#!/usr/bin/env python3
"""smart_sport's own SQLite store.

Multi-user: every table carries a ``user_id``, one row per person
sharing this deployment (e.g. a family). uuid-keyed HC tables (steps,
weight, ...) keep uuid as the sole primary key -- ponytail: real
Android-generated UUIDs are effectively globally unique, so a
cross-user collision is not a realistic concern; the upgrade path
(composite (user_id, uuid) PK) is straightforward if that assumption
ever breaks. Tables keyed by short strings that legitimately repeat
across users (session_type, a settings key, an achievement key) use a
composite (user_id, ...) primary key instead, since those WOULD
collide.
"""

import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path

# Relative to cwd by default, matching the docker-compose bind mount
# (./data/db/ on the host -> /app/data/db/ in the container). Override
# with SMART_SPORT_DB for tests/dev.
DB_PATH = Path(os.environ.get("SMART_SPORT_DB", "data/db/smart_sport.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- First account ever created is the admin; self-signed-up
    -- accounts start unapproved and can't log in until the admin
    -- approves them from Settings.
    is_admin INTEGER NOT NULL DEFAULT 0,
    approved INTEGER NOT NULL DEFAULT 0
);

-- WebAuthn passkeys: one row per registered credential. credential_id
-- and public_key are stored as the base64url/raw bytes the webauthn
-- library emits; sign_count backs clone detection.
CREATE TABLE IF NOT EXISTS passkeys (
    credential_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    label TEXT
);
CREATE INDEX IF NOT EXISTS idx_passkeys_user ON passkeys(user_id);

CREATE TABLE IF NOT EXISTS steps (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_user_date ON steps(user_id, local_date);

-- ponytail: raw heart_rate_record_series_table is ~170k rows/year of
-- beat samples; a dashboard/coach only needs the daily shape, so
-- ingestion aggregates to one row/day instead of storing every beat.
-- Per-exercise-session samples are kept separately for HR-zone
-- analysis, see exercise_hr_samples below.
CREATE TABLE IF NOT EXISTS heart_rate_daily (
    user_id INTEGER NOT NULL REFERENCES users(id),
    local_date TEXT NOT NULL,
    avg_bpm REAL,
    min_bpm INTEGER,
    max_bpm INTEGER,
    sample_count INTEGER,
    PRIMARY KEY (user_id, local_date)
);

CREATE TABLE IF NOT EXISTS resting_heart_rate (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    time_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    bpm INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rhr_user_date ON resting_heart_rate(user_id, local_date);

CREATE TABLE IF NOT EXISTS sleep_sessions (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    title TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_sleep_user_date ON sleep_sessions(user_id, local_date);

CREATE TABLE IF NOT EXISTS sleep_stages (
    parent_uuid TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    stage_start_utc TEXT NOT NULL,
    stage_end_utc TEXT NOT NULL,
    stage_type INTEGER NOT NULL,
    PRIMARY KEY (parent_uuid, stage_start_utc)
);
CREATE INDEX IF NOT EXISTS idx_sleep_stages_parent ON sleep_stages(parent_uuid);

CREATE TABLE IF NOT EXISTS exercise_sessions (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    exercise_type INTEGER,
    title TEXT,
    notes TEXT,
    rpe REAL,
    -- Manual correction from the Sessions page: Garmin's HC-written
    -- exercise_type is unreliable (observed mislabeling treadmill/
    -- calisthenics as Baseball/Gymnastics on real data). Never
    -- touched by ingestion upserts (not in parse_health_connect's
    -- column list), so it survives every re-ingest.
    label_override TEXT
);
CREATE INDEX IF NOT EXISTS idx_exercise_user_date ON exercise_sessions(user_id, local_date);

-- Per-exercise-session HR samples (bounded to workout windows only,
-- not the full-day firehose) -- backs HR-zone analysis on the
-- Sessions page.
CREATE TABLE IF NOT EXISTS exercise_hr_samples (
    exercise_uuid TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    epoch_utc TEXT NOT NULL,
    bpm INTEGER NOT NULL,
    PRIMARY KEY (exercise_uuid, epoch_utc)
);
CREATE INDEX IF NOT EXISTS idx_exercise_hr_parent ON exercise_hr_samples(exercise_uuid);

-- GPS route points per exercise session (Health Connect's
-- ExerciseRoute). Empty until/unless the source app is granted route
-- permission -- ingested proactively (same "sparse now, ready when
-- logged" posture as hydration/nutrition), but unlike those, whether
-- this ever populates depends on a permission this project doesn't
-- control.
CREATE TABLE IF NOT EXISTS exercise_route_points (
    exercise_uuid TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    epoch_utc TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    altitude_m REAL,
    PRIMARY KEY (exercise_uuid, epoch_utc)
);
CREATE INDEX IF NOT EXISTS idx_route_parent ON exercise_route_points(exercise_uuid);

CREATE TABLE IF NOT EXISTS weight (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    time_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    kg REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weight_user_date ON weight(user_id, local_date);

CREATE TABLE IF NOT EXISTS body_fat (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    time_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    percentage REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_body_fat_user_date ON body_fat(user_id, local_date);

CREATE TABLE IF NOT EXISTS lean_body_mass (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    time_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    kg REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lean_mass_user_date ON lean_body_mass(user_id, local_date);

CREATE TABLE IF NOT EXISTS active_calories (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    kcal REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_active_cal_user_date ON active_calories(user_id, local_date);

CREATE TABLE IF NOT EXISTS basal_metabolic_rate (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    time_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    kcal_per_day REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bmr_user_date ON basal_metabolic_rate(user_id, local_date);

CREATE TABLE IF NOT EXISTS nutrition (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    meal_type INTEGER,
    calories REAL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL
);
CREATE INDEX IF NOT EXISTS idx_nutrition_user_date ON nutrition(user_id, local_date);

CREATE TABLE IF NOT EXISTS hydration (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    volume_ml REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hydration_user_date ON hydration(user_id, local_date);

CREATE TABLE IF NOT EXISTS distance (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    meters REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_distance_user_date ON distance(user_id, local_date);

CREATE TABLE IF NOT EXISTS floors_climbed (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    floors REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_floors_user_date ON floors_climbed(user_id, local_date);

CREATE TABLE IF NOT EXISTS elevation_gained (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    meters REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_elevation_user_date ON elevation_gained(user_id, local_date);

-- Device-computed total daily energy expenditure (BMR + activity).
-- Preferred over active_calories + a BMR formula for calorie-balance
-- math whenever the device actually reports it.
CREATE TABLE IF NOT EXISTS total_calories_burned (
    uuid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    kcal REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_total_cal_user_date ON total_calories_burned(user_id, local_date);

-- Daily training-load model (Fitness/Fatigue/Form, intervals.icu
-- style). Per-session duration * max(1 + level/10, rpe/5): the
-- prescribed level drives planned sessions, a cleaned RPE (see
-- parse_health_connect.py's _clean_rpe) raises unplanned efforts.
CREATE TABLE IF NOT EXISTS training_load (
    user_id INTEGER NOT NULL REFERENCES users(id),
    local_date TEXT NOT NULL,
    daily_load REAL NOT NULL,
    ctl REAL NOT NULL,
    atl REAL NOT NULL,
    tsb REAL NOT NULL,
    PRIMARY KEY (user_id, local_date)
);

-- Ingestion health check: one row per table per run, shown on the
-- dashboard's Settings/Status page.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    ran_at TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_user ON ingest_runs(user_id);

-- Rule-engine state, replacing garmin-coach's levels.json.
-- red_streak/deload_until back the deload guardrail (training.py):
-- 3 reds in a row forces a deload week, tracked per session type.
CREATE TABLE IF NOT EXISTS levels (
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_type TEXT NOT NULL,
    level INTEGER NOT NULL,
    red_streak INTEGER NOT NULL DEFAULT 0,
    deload_until TEXT,
    PRIMARY KEY (user_id, session_type)
);

-- History of triggered deload weeks, one row per event. Used by the
-- "Deload Warrior" achievement (survived a deload = its window ended)
-- and available for a future deload-history view.
CREATE TABLE IF NOT EXISTS deload_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_type TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    ends_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deload_events_user ON deload_events(user_id);

-- Xbox-Gamerscore-style achievement system. ACHIEVEMENTS in
-- achievements.py is the static registry (name/tier/points); this
-- table only records which keys have been unlocked and when.
CREATE TABLE IF NOT EXISTS achievements (
    user_id INTEGER NOT NULL REFERENCES users(id),
    key TEXT NOT NULL,
    unlocked_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- Auditable XP transaction log backing the Player Level system
-- (achievements.py: xp_total/player_level). Every grant is one row
-- with a human-readable reason, so the level shown on the dashboard
-- is never an opaque number -- it's the sum of a visible, browsable
-- ledger (surfaced on the Achievements page).
CREATE TABLE IF NOT EXISTS xp_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    amount INTEGER NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xp_ledger_user_date ON xp_ledger(user_id, date);

-- Tunable goal/config values, editable from the Settings page.
CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER NOT NULL REFERENCES users(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- Replaces garmin-coach's append-only history.log.
CREATE TABLE IF NOT EXISTS coach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    local_date TEXT NOT NULL,
    status TEXT,
    session_type TEXT,
    level INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coach_log_user_date ON coach_log(user_id, local_date);
"""

# Per-user weekly plan: weekday ("0"=Monday ... "6"=Sunday) -> session
# template. session_type null = day outside the leveling system (rest,
# free activity); title/start/duration still drive the calendar event.
# Default mirrors the original hardcoded week; each user edits their
# own copy in Settings.
DEFAULT_SCHEDULE = {
    "0": {"session_type": "treadmill",
          "title": "Tapis - marche rapide inclinee",
          "start": "20:00", "duration_min": 30},
    "1": {"session_type": "lower_body", "title": "Muscu bas du corps",
          "start": "20:00", "duration_min": 30},
    "2": {"session_type": "treadmill",
          "title": "Tapis - marche rapide inclinee",
          "start": "20:00", "duration_min": 30},
    "3": {"session_type": "upper_body",
          "title": "Muscu haut du corps + gainage",
          "start": "20:00", "duration_min": 30},
    "4": {"session_type": "treadmill",
          "title": "Tapis - marche rapide inclinee",
          "start": "20:00", "duration_min": 30},
    "5": {"session_type": "calisthenics",
          "title": "Calisthenie full body",
          "start": "20:00", "duration_min": 30},
    "6": {"session_type": None, "title": "Velo en famille",
          "start": "09:00", "duration_min": 90},
}

DEFAULT_SETTINGS = {
    # Local calendar-day boundaries for sleep/steps/nutrition are
    # computed in this timezone (HC's per-record zone_offset isn't
    # persisted since the user has a single home timezone).
    "timezone": "Europe/Paris",
    "language": "fr",  # "fr" or "en" -- coaching message language
    "step_goal": "10000",
    "protein_target_g_per_kg": "1.8",
    "fat_target_g_per_kg": "0.9",
    "hydration_target_ml_per_kg": "35",
    "target_weight_kg": "",
    "weekly_weight_change_kg": "-0.4",
    "height_cm": "",
    "age_years": "",
    "sex": "",
    "bmr_manual_kcal": "",
    "rclone_remote": "",  # this user's Drive folder (multi-user: one export per person)
    "calendar_name": "",  # this user's target Google Calendar display name
    "ntfy_topic": "",  # this user's own ntfy topic (falls back to env NTFY_TOPIC)
    "schedule": json.dumps(DEFAULT_SCHEDULE),
    # Max session duration: sessions stay dense and short by default;
    # raise this to let high levels extend the session instead.
    "session_cap_min": "30",
}


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the smart_sport SQLite database.

    Parameters:
        path (Path): Database file location.

    Returns:
        sqlite3.Connection: Connection with row access by column name.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if missing, migrate old schemas.

    Parameters:
        conn (sqlite3.Connection): Open connection.
    """
    conn.executescript(SCHEMA)
    # Migration for databases created before the admin/approval
    # columns existed: everyone already on board is grandfathered as
    # approved, and the oldest account becomes the admin.
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(users)")
    }
    if "approved" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN approved "
            "INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute("UPDATE users SET approved = 1")
    if "is_admin" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN is_admin "
            "INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE users SET is_admin = 1 "
            "WHERE id = (SELECT MIN(id) FROM users)"
        )
    conn.commit()


# --- Users ---

def _hash_password(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256, stdlib only (no bcrypt/argon2 dependency --
    100k iterations is adequate for a self-hosted, low-QPS login).
    """
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 100_000
    ).hex()


def create_user(
    conn: sqlite3.Connection, username: str, password: str,
    approved: bool = True,
) -> int:
    """Create a new user account.

    The very first account ever created becomes the admin and is
    always approved, regardless of ``approved`` -- someone has to be
    able to approve the others. Self-signup (the web form) passes
    ``approved=False``; the admin CLI keeps the approved default.

    Parameters:
        conn (sqlite3.Connection): Open connection.
        username (str): Unique login name.
        password (str): Plain-text password (hashed before storing).
        approved (bool): Whether the account can log in immediately.

    Returns:
        int: New user's id.

    Raises:
        ValueError: Username already taken.
    """
    import datetime as dt

    first = conn.execute(
        "SELECT COUNT(*) AS n FROM users"
    ).fetchone()["n"] == 0
    is_admin = first
    if first:
        approved = True

    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, password_salt, "
            "created_at, is_admin, approved) VALUES (?, ?, ?, ?, ?, ?)",
            (
                username, password_hash, salt,
                dt.datetime.now(dt.timezone.utc).isoformat(),
                int(is_admin), int(approved),
            ),
        )
    except sqlite3.IntegrityError:
        # Roll back the implicit transaction the failed INSERT opened
        # -- a leaked request connection stuck in a transaction locks
        # every other writer out of the database.
        conn.rollback()
        raise ValueError(f"Username {username!r} is already taken.")
    conn.commit()
    user_id = cursor.lastrowid
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (user_id, key, value) "
            "VALUES (?, ?, ?)", (user_id, key, value),
        )
    conn.commit()
    return user_id


def verify_login(
    conn: sqlite3.Connection, username: str, password: str,
) -> sqlite3.Row | None:
    """Check credentials.

    Parameters:
        conn (sqlite3.Connection): Open connection.
        username (str): Login name.
        password (str): Plain-text password to check.

    Returns:
        sqlite3.Row | None: ``id``/``approved``/``is_admin`` if the
        credentials are valid, else ``None``. Approval is the
        caller's check -- valid-but-pending gets a different message
        than wrong credentials.
    """
    row = conn.execute(
        "SELECT id, password_hash, password_salt, approved, is_admin "
        "FROM users WHERE username = ?", (username,),
    ).fetchone()
    if not row:
        return None
    candidate = _hash_password(password, row["password_salt"])
    if secrets.compare_digest(candidate, row["password_hash"]):
        return row
    return None


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """Look up a user by id (for the session -> username display)."""
    return conn.execute(
        "SELECT id, username FROM users WHERE id = ?", (user_id,),
    ).fetchone()


def all_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every approved user account -- used by run_ingest.py /
    run_coach.py to loop the daily pipeline once per person. Pending
    accounts get no pipeline until the admin approves them.
    """
    return conn.execute(
        "SELECT id, username FROM users WHERE approved = 1 ORDER BY id"
    ).fetchall()


def pending_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Self-signed-up accounts awaiting admin approval."""
    return conn.execute(
        "SELECT id, username, created_at FROM users WHERE approved = 0 "
        "ORDER BY id"
    ).fetchall()


def approve_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Let a pending account log in."""
    conn.execute(
        "UPDATE users SET approved = 1 WHERE id = ?", (user_id,),
    )
    conn.commit()


def reject_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Delete a PENDING account (guard in the WHERE clause: an
    approved account with data is never deletable this way).
    """
    conn.execute(
        "DELETE FROM settings WHERE user_id = ? AND EXISTS (SELECT 1 "
        "FROM users WHERE id = ? AND approved = 0)", (user_id, user_id),
    )
    conn.execute(
        "DELETE FROM users WHERE id = ? AND approved = 0", (user_id,),
    )
    conn.commit()


def is_admin(conn: sqlite3.Connection, user_id: int) -> bool:
    """Whether this account may approve/reject signups."""
    row = conn.execute(
        "SELECT is_admin FROM users WHERE id = ?", (user_id,),
    ).fetchone()
    return bool(row and row["is_admin"])


# --- Passkeys (WebAuthn) ---

def add_passkey(
    conn: sqlite3.Connection, user_id: int, credential_id: str,
    public_key: bytes, sign_count: int, label: str = "",
) -> None:
    """Store a newly registered WebAuthn credential."""
    import datetime as dt

    conn.execute(
        "INSERT INTO passkeys (credential_id, user_id, public_key, "
        "sign_count, created_at, label) VALUES (?, ?, ?, ?, ?, ?)",
        (
            credential_id, user_id, public_key, sign_count,
            dt.datetime.now(dt.timezone.utc).isoformat(), label,
        ),
    )
    conn.commit()


def passkeys_for_user(
    conn: sqlite3.Connection, user_id: int,
) -> list[sqlite3.Row]:
    """This user's registered passkeys (Settings list + login allow)."""
    return conn.execute(
        "SELECT credential_id, sign_count, created_at, label "
        "FROM passkeys WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    ).fetchall()


def passkey_by_credential(
    conn: sqlite3.Connection, credential_id: str,
) -> sqlite3.Row | None:
    """Look up a credential during authentication."""
    return conn.execute(
        "SELECT credential_id, user_id, public_key, sign_count "
        "FROM passkeys WHERE credential_id = ?", (credential_id,),
    ).fetchone()


def set_passkey_sign_count(
    conn: sqlite3.Connection, credential_id: str, sign_count: int,
) -> None:
    """Persist the authenticator's counter after a successful login."""
    conn.execute(
        "UPDATE passkeys SET sign_count = ? WHERE credential_id = ?",
        (sign_count, credential_id),
    )
    conn.commit()


def delete_passkey(
    conn: sqlite3.Connection, user_id: int, credential_id: str,
) -> None:
    """Remove one of the user's own passkeys (user-scoped WHERE)."""
    conn.execute(
        "DELETE FROM passkeys WHERE credential_id = ? AND user_id = ?",
        (credential_id, user_id),
    )
    conn.commit()


# --- Per-user settings ---

def get_setting(conn: sqlite3.Connection, user_id: int, key: str) -> str:
    """Read a setting value, empty string if unset/blank.

    Parameters:
        conn (sqlite3.Connection): Open connection.
        user_id (int): Owning user.
        key (str): Setting key.

    Returns:
        str: Stored value, or "" if the key doesn't exist.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE user_id = ? AND key = ?",
        (user_id, key),
    ).fetchone()
    return row["value"] if row else ""


def set_setting(
    conn: sqlite3.Connection, user_id: int, key: str, value: str,
) -> None:
    """Upsert a setting value.

    Parameters:
        conn (sqlite3.Connection): Open connection.
        user_id (int): Owning user.
        key (str): Setting key.
        value (str): New value (stored as text).
    """
    conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
        (user_id, key, value),
    )
    conn.commit()


if __name__ == "__main__":
    import tempfile

    connection = connect()
    init_db(connection)
    tables = [
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name"
        )
    ]
    assert "steps" in tables and "settings" in tables and "users" in tables
    print(f"db.py: schema OK ({len(tables)} tables) at {DB_PATH}")

    # Self-check against an isolated scratch db (never the real one).
    tmp = Path(tempfile.mkdtemp()) / "smart_sport_selfcheck.db"
    test_conn = connect(tmp)
    init_db(test_conn)

    alice = create_user(test_conn, "alice", "correct horse battery staple")
    bob = create_user(test_conn, "bob", "hunter2")
    assert alice != bob
    login = verify_login(test_conn, "alice", "correct horse battery staple")
    assert login["id"] == alice
    assert verify_login(test_conn, "alice", "wrong password") is None
    assert verify_login(test_conn, "nobody", "x") is None
    try:
        create_user(test_conn, "alice", "another password")
        raise AssertionError("expected ValueError for duplicate username")
    except ValueError:
        pass

    assert get_user(test_conn, alice)["username"] == "alice"
    assert {u["username"] for u in all_users(test_conn)} == {"alice", "bob"}

    # First user is the admin and always approved; a self-signup is
    # pending (invisible to the pipeline loop) until approved.
    assert login["is_admin"] == 1 and login["approved"] == 1
    assert is_admin(test_conn, alice) and not is_admin(test_conn, bob)
    carol = create_user(test_conn, "carol", "password1234", approved=False)
    assert verify_login(test_conn, "carol", "password1234")["approved"] == 0
    assert {u["username"] for u in all_users(test_conn)} == {"alice", "bob"}
    assert [u["id"] for u in pending_users(test_conn)] == [carol]
    approve_user(test_conn, carol)
    assert "carol" in {u["username"] for u in all_users(test_conn)}
    # reject only touches PENDING accounts -- approved carol survives.
    reject_user(test_conn, carol)
    assert get_user(test_conn, carol) is not None
    dave = create_user(test_conn, "dave", "password1234", approved=False)
    reject_user(test_conn, dave)
    assert get_user(test_conn, dave) is None
    assert test_conn.execute(
        "SELECT COUNT(*) AS n FROM settings WHERE user_id = ?", (dave,),
    ).fetchone()["n"] == 0

    # Passkeys: register, look up, count update, user-scoped delete.
    add_passkey(test_conn, alice, "cred-a", b"pk-bytes", 0, "phone")
    add_passkey(test_conn, bob, "cred-b", b"pk-bytes-2", 5)
    assert len(passkeys_for_user(test_conn, alice)) == 1
    found = passkey_by_credential(test_conn, "cred-a")
    assert found["user_id"] == alice and found["public_key"] == b"pk-bytes"
    set_passkey_sign_count(test_conn, "cred-a", 7)
    assert passkey_by_credential(test_conn, "cred-a")["sign_count"] == 7
    delete_passkey(test_conn, bob, "cred-a")  # wrong owner: no-op
    assert passkey_by_credential(test_conn, "cred-a") is not None
    delete_passkey(test_conn, alice, "cred-a")
    assert passkey_by_credential(test_conn, "cred-a") is None

    # Migration path: a pre-admin-era users table gains the columns,
    # everyone grandfathered approved, oldest account is admin.
    old = connect(Path(tempfile.mkdtemp()) / "old.db")
    old.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, "
        "password_salt TEXT NOT NULL, created_at TEXT NOT NULL);"
        "INSERT INTO users (username, password_hash, password_salt, "
        "created_at) VALUES ('legacy1', 'h', 's', 't'), "
        "('legacy2', 'h', 's', 't');"
    )
    init_db(old)
    assert is_admin(old, 1) and not is_admin(old, 2)
    assert {u["username"] for u in all_users(old)} == {"legacy1", "legacy2"}

    # Settings are per-user from creation (seeded by create_user) and
    # independently editable -- this IS the data-isolation contract.
    assert get_setting(test_conn, alice, "step_goal") == "10000"
    set_setting(test_conn, alice, "step_goal", "12000")
    assert get_setting(test_conn, alice, "step_goal") == "12000"
    assert get_setting(test_conn, bob, "step_goal") == "10000"  # untouched

    print("db.py: self-check passed (isolated scratch db, 2 users)")
