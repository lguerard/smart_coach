#!/usr/bin/env python3
"""Per-muscle fatigue: what the last days actually asked of each muscle.

The level system knows one number per session type, so a three-hour hike
on Saturday and a leg circuit on Sunday looked like a fresh leg day. This
reads every logged activity (Garmin and Health Connect sessions, plus the
coach's own circuits) as a stimulus on the muscles it works, and lets it
fade with a half-life:

    fatigue(muscle) = sum(stimulus_i * 0.5 ** (hours_since_i / 36))

A normal hard session for a muscle is about 1.0 of stimulus right after
it: "fatigued" for a day or so, "recovering" on day two, "ready" by day
three -- two hard days in a row on the same muscles stay fatigued longer.
The idea comes from openGym's recovery model; this is an independent,
simpler reimplementation for activity-level data (no sets or loads here).

It is used as a capped vote: fatigued main muscles for tonight's session
hold the level (yellow), they never make a day red on their own -- a
sore muscle is not a sick body. The same numbers drive the dashboard's
body map.
"""

import datetime as dt
import sqlite3
import unicodedata
from typing import Optional

import exercise_library
from ingest.parse_health_connect import EXERCISE_TYPE_LABELS

HALF_LIFE_H = 36.0
WINDOW_DAYS = 10  # beyond this a session is below 1 % of its stimulus
FATIGUED_AT = 0.75
RECOVERING_AT = 0.35
REFERENCE_MIN = 30.0  # duration of a "normal" session
MAX_DURATION_FACTOR = 3.0  # a very long day counts, but not without bound
MAIN_MUSCLE_WEIGHT = 0.8  # in a session's own weights: a main muscle

MUSCLES = dict(exercise_library.MUSCLE_LABEL_FR)

# Coach treadmill session: a steep incline walk.
TREADMILL_MUSCLES = {"calves": 0.8, "glutes": 0.7, "hamstrings": 0.5,
                     "quads": 0.4}

# label -> (intensity of a 30-min bout, muscle weights). Intensity 1.0
# is a hard session for its main muscles; walking is a fraction of it.
# fmt: off
ACTIVITY_MUSCLES = {
    "walking": (0.35, {"calves": 1.0, "quads": 0.6, "glutes": 0.6,
                       "hamstrings": 0.4}),
    "running_treadmill": (0.8, TREADMILL_MUSCLES),
    "running": (1.0, {"quads": 0.8, "hamstrings": 0.7, "calves": 1.0,
                      "glutes": 0.7}),
    "hiking": (0.8, {"quads": 1.0, "glutes": 1.0, "calves": 0.8,
                     "hamstrings": 0.6}),
    "snowshoeing": (0.9, {"quads": 1.0, "glutes": 1.0, "calves": 0.8,
                          "hamstrings": 0.6}),
    "stair_climbing": (0.9, {"quads": 1.0, "glutes": 1.0, "calves": 0.8}),
    "stair_climbing_machine": (0.9, {"quads": 1.0, "glutes": 1.0,
                                     "calves": 0.8}),
    "biking": (0.8, {"quads": 1.0, "glutes": 0.6, "calves": 0.4,
                     "hamstrings": 0.3}),
    "biking_stationary": (0.8, {"quads": 1.0, "glutes": 0.6,
                                "calves": 0.4, "hamstrings": 0.3}),
    "elliptical": (0.7, {"quads": 0.8, "glutes": 0.7, "calves": 0.4,
                         "hamstrings": 0.4, "shoulders": 0.2}),
    "rowing": (0.9, {"back": 1.0, "biceps": 0.6, "quads": 0.7,
                     "glutes": 0.6, "hamstrings": 0.4,
                     "lower_back": 0.4}),
    "rowing_machine": (0.9, {"back": 1.0, "biceps": 0.6, "quads": 0.7,
                             "glutes": 0.6, "hamstrings": 0.4,
                             "lower_back": 0.4}),
    "swimming_pool": (0.9, {"back": 1.0, "shoulders": 1.0, "chest": 0.5,
                            "triceps": 0.5, "abs": 0.3}),
    "swimming_open_water": (0.9, {"back": 1.0, "shoulders": 1.0,
                                  "chest": 0.5, "triceps": 0.5,
                                  "abs": 0.3}),
    "tennis": (0.7, {"quads": 0.7, "calves": 0.7, "shoulders": 0.6,
                     "abs": 0.4}),
    "badminton": (0.7, {"quads": 0.7, "calves": 0.7, "shoulders": 0.5}),
    "squash": (0.8, {"quads": 0.8, "calves": 0.8, "shoulders": 0.5}),
    "soccer": (0.9, {"quads": 1.0, "hamstrings": 0.8, "calves": 0.8,
                     "glutes": 0.6}),
    "basketball": (0.8, {"quads": 0.8, "calves": 0.9, "shoulders": 0.4}),
    "volleyball": (0.6, {"quads": 0.7, "calves": 0.8, "shoulders": 0.6}),
    "skiing": (0.8, {"quads": 1.0, "glutes": 0.7, "abs": 0.4}),
    "dancing": (0.5, {"calves": 0.7, "quads": 0.6, "glutes": 0.5}),
    "rock_climbing": (0.9, {"back": 1.0, "biceps": 0.9, "shoulders": 0.6,
                            "abs": 0.4}),
    "yoga": (0.3, {"abs": 0.6, "shoulders": 0.4, "glutes": 0.3,
                   "lower_back": 0.4}),
    "pilates": (0.4, {"abs": 1.0, "glutes": 0.5, "lower_back": 0.4}),
    "stretching": (0.1, {}),
    "guided_breathing": (0.0, {}),
}
# fmt: on
# Generic strength work when no coach circuit explains it.
GENERIC_STRENGTH = (0.7, {muscle: 0.6 for muscle in MUSCLES})
STRENGTH_LABELS = {"strength_training", "calisthenics", "weightlifting",
                   "boot_camp", "hiit", "exercise_class", "other_workout",
                   "gymnastics"}

ACTIVITY_LABEL_FR = {
    "walking": "marche", "running_treadmill": "tapis",
    "running": "course", "hiking": "randonnee", "biking": "velo",
    "biking_stationary": "velo d'appartement", "elliptical": "elliptique",
    "rowing": "aviron", "rowing_machine": "rameur",
    "swimming_pool": "natation", "swimming_open_water": "natation",
    "strength_training": "renforcement", "calisthenics": "circuit",
    "yoga": "yoga", "pilates": "pilates", "tennis": "tennis",
    "stair_climbing": "escaliers", "soccer": "foot", "skiing": "ski",
}

# Free-text zone names (the Corps analysis writes them) -> muscle keys.
ZONE_KEYWORDS = [
    ("lower_back", ("lombaire", "bas du dos")),
    ("chest", ("pector", "poitrine", "pecs")),
    ("shoulders", ("epaule", "deltoid")),
    ("triceps", ("tricep",)),
    ("biceps", ("bicep",)),
    ("back", ("dos", "dorsa", "trapez", "rhombo", "lats")),
    ("abs", ("abdo", "sangle", "oblique", "ventre", "gainage", "core")),
    ("glutes", ("fess", "glute")),
    ("quads", ("quadri", "cuisse")),
    ("hamstrings", ("ischio",)),
    ("calves", ("mollet",)),
]


def _plain(text: str) -> str:
    """Lowercase, accents removed."""
    return "".join(
        char for char in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(char) != "Mn"
    )


def muscles_from_zone(text: str) -> list[str]:
    """Muscle keys a free-text zone name refers to.

    "bas du dos" is the lower back, not the whole back; "cuisses" is
    the quads (the hamstrings have their own word).
    """
    plain = _plain(text or "")
    found = []
    for key, words in ZONE_KEYWORDS:
        if any(word in plain for word in words):
            if key == "back" and "lower_back" in found and (
                "bas du dos" in plain and plain.count("dos") == 1
            ):
                continue
            found.append(key)
    return found


def session_muscle_weights(
    session_type: Optional[str], level: int, date: Optional[str] = None,
    equipment: Optional[dict] = None,
) -> dict:
    """Muscle weights of one coach session type at a level."""
    import training  # local: training imports this module

    if session_type is None:
        return {}
    if session_type == "treadmill":
        return dict(TREADMILL_MUSCLES)
    return exercise_library.session_muscles(
        training.session_values(
            session_type, level, date=date, equipment=equipment,
        ),
    )


def _coach_day(conn: sqlite3.Connection, user_id: int, day: str):
    """That day's coach session (type, level) when it was a training day."""
    row = conn.execute(
        "SELECT session_type, level, tier FROM coach_log WHERE user_id = ? "
        "AND local_date = ? ORDER BY id DESC LIMIT 1", (user_id, day),
    ).fetchone()
    if not row or not row["session_type"]:
        return None
    if (row["tier"] or "train") != "train":
        return None
    return row["session_type"], row["level"] or 0


def _stimulus(conn, user_id, row, coach) -> tuple[float, dict, str]:
    """(intensity, weights, label) for one logged activity."""
    label = (
        row["label_override"]
        or EXERCISE_TYPE_LABELS.get(row["exercise_type"], "other_workout")
    )
    key = _plain(label).replace(" ", "_")
    if coach and key in STRENGTH_LABELS | {"running_treadmill", "walking"}:
        session_type, level = coach
        circuit = session_type != "treadmill" and key in STRENGTH_LABELS
        treadmill = session_type == "treadmill" and key in (
            "running_treadmill", "walking",
        )
        if circuit or treadmill:
            return 1.0, session_muscle_weights(
                session_type, level, row["local_date"],
                exercise_library.equipment_for(conn, user_id),
            ), session_type
    if key in ACTIVITY_MUSCLES:
        intensity, weights = ACTIVITY_MUSCLES[key]
        return intensity, weights, key
    if key in STRENGTH_LABELS:
        intensity, weights = GENERIC_STRENGTH
        return intensity, weights, key
    # A corrected free-text label ("tapis", "velo"...): best effort.
    for word, mapped in (("tapis", "running_treadmill"),
                         ("marche", "walking"), ("velo", "biking"),
                         ("course", "running"), ("rando", "hiking"),
                         ("natation", "swimming_pool"),
                         ("circuit", "calisthenics")):
        if word in key:
            return _stimulus(
                conn, user_id,
                {**dict(row), "label_override": mapped}, coach,
            )
    return 0.0, {}, key


def muscle_fatigue(
    conn: sqlite3.Connection, user_id: int, date: str,
    at: Optional[dt.datetime] = None,
) -> dict:
    """Fatigue, state and last-7-days volume per muscle.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date of the reading.
        at (datetime | None): Reading time (aware). Defaults to 07:00
            local on ``date`` -- the morning decision -- so only what
            happened before it counts.

    Returns:
        dict: ``muscles`` -> {key: {fatigue, state, week_volume,
        cause}} for every muscle (``cause`` = the activity label that
        contributes most now, when any), and ``activities`` counted.
    """
    import metrics

    tz = metrics.local_tz(conn, user_id)
    if at is None:
        at = dt.datetime.combine(
            dt.date.fromisoformat(date), dt.time(7, 0), tzinfo=tz,
        )
    start = (
        dt.date.fromisoformat(date) - dt.timedelta(days=WINDOW_DAYS)
    ).isoformat()
    rows = conn.execute(
        "SELECT uuid, local_date, start_utc, end_utc, exercise_type, "
        "label_override, rpe FROM exercise_sessions WHERE user_id = ? "
        "AND local_date BETWEEN ? AND ? ORDER BY start_utc",
        (user_id, start, date),
    ).fetchall()
    week_start = at - dt.timedelta(days=7)
    state = {m: {"fatigue": 0.0, "week_volume": 0.0, "_cause": {}}
             for m in MUSCLES}
    counted = 0
    for row in rows:
        end = dt.datetime.fromisoformat(row["end_utc"])
        if end.tzinfo is None:
            end = end.replace(tzinfo=dt.timezone.utc)
        if end > at:
            continue
        begin = dt.datetime.fromisoformat(row["start_utc"])
        if begin.tzinfo is None:
            begin = begin.replace(tzinfo=dt.timezone.utc)
        minutes = max(0.0, (end - begin).total_seconds() / 60)
        coach = _coach_day(conn, user_id, row["local_date"])
        intensity, weights, label = _stimulus(conn, user_id, row, coach)
        if not weights or intensity <= 0 or minutes <= 0:
            continue
        counted += 1
        amount = intensity * min(minutes / REFERENCE_MIN,
                                 MAX_DURATION_FACTOR)
        if row["rpe"] is not None:
            amount *= max(0.5, min(1.6, row["rpe"] / 6.0))
        decay = 0.5 ** (((at - end).total_seconds() / 3600) / HALF_LIFE_H)
        for muscle, weight in weights.items():
            if muscle not in state:
                continue
            now = amount * weight * decay
            state[muscle]["fatigue"] += now
            state[muscle]["_cause"][label] = (
                state[muscle]["_cause"].get(label, 0.0) + now
            )
            if end >= week_start:
                state[muscle]["week_volume"] += amount * weight
    result = {}
    for muscle, values in state.items():
        fatigue = values["fatigue"]
        causes = values.pop("_cause")
        result[muscle] = {
            "fatigue": round(fatigue, 2),
            "week_volume": round(values["week_volume"], 2),
            "state": (
                "fatigued" if fatigue >= FATIGUED_AT
                else "recovering" if fatigue >= RECOVERING_AT
                else "ready"
            ),
            **({"cause": max(causes, key=causes.get)} if causes else {}),
        }
    return {"muscles": result, "activities": counted}


def session_vote(
    fatigue: dict, weights: dict,
) -> Optional[tuple[str, str, str]]:
    """A status vote when tonight's main muscles are still fatigued.

    Capped at yellow, and never green: being recovered is the normal
    case and says nothing about readiness, but legs still cooked from
    yesterday's hike are a reason not to push the level up tonight.

    Parameters:
        fatigue (dict): ``muscle_fatigue`` output.
        weights (dict): Tonight's session muscle weights.

    Returns:
        tuple | None: ``("Fatigue musculaire", "yellow", detail)``.
    """
    tired = [
        muscle for muscle, weight in weights.items()
        if weight >= MAIN_MUSCLE_WEIGHT
        and fatigue["muscles"].get(muscle, {}).get("state") == "fatigued"
    ]
    if not tired:
        return None
    causes = {
        fatigue["muscles"][m].get("cause") for m in tired
    } - {None}
    because = ", ".join(
        sorted(ACTIVITY_LABEL_FR.get(c, c.replace("_", " ")) for c in causes)
    )
    detail = ", ".join(MUSCLES[m] for m in tired) + " encore fatigues"
    if because:
        detail += f" ({because})"
    return ("Fatigue musculaire", "yellow", detail)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db

    assert muscles_from_zone("Épaules") == ["shoulders"]
    assert muscles_from_zone("bas du dos") == ["lower_back"]
    assert set(muscles_from_zone("dos et lombaires")) == {"back",
                                                         "lower_back"}
    assert muscles_from_zone("Cuisses (quadriceps)") == ["quads"]
    assert muscles_from_zone("abdominaux / obliques") == ["abs"]
    assert muscles_from_zone("tête") == []

    conn = db.connect(Path(tempfile.mkdtemp()) / "muscles.db")
    db.init_db(conn)
    uid = db.create_user(conn, "mover", "password1234")

    def _session(uuid, day, start_h, minutes, exercise_type=None,
                 label=None, rpe=None):
        start = dt.datetime.fromisoformat(f"{day}T{start_h:02d}:00:00+00:00")
        end = start + dt.timedelta(minutes=minutes)
        conn.execute(
            "INSERT INTO exercise_sessions (uuid, user_id, start_utc, "
            "end_utc, local_date, exercise_type, label_override, rpe) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid, uid, start.isoformat(), end.isoformat(), day,
             exercise_type, label, rpe),
        )
        conn.commit()

    # Nothing logged: every muscle ready, no volume.
    empty = muscle_fatigue(conn, uid, "2026-10-05")
    assert all(m["state"] == "ready" and m["week_volume"] == 0
               for m in empty["muscles"].values())
    assert set(empty["muscles"]) == set(MUSCLES)

    # A three-hour hike on Sunday -> Monday morning legs are fatigued,
    # upper body is not.
    _session("hike", "2026-10-04", 9, 180, label="hiking")
    monday = muscle_fatigue(conn, uid, "2026-10-05")
    legs = monday["muscles"]
    assert legs["quads"]["state"] == "fatigued", legs["quads"]
    assert legs["glutes"]["cause"] == "hiking"
    assert legs["chest"]["state"] == "ready"
    assert monday["activities"] == 1
    # ... and it fades: by Thursday the quads are ready again.
    thursday = muscle_fatigue(conn, uid, "2026-10-08")["muscles"]
    assert thursday["quads"]["fatigue"] < legs["quads"]["fatigue"] / 4
    assert thursday["quads"]["state"] in ("ready", "recovering")
    # Week volume is not decayed: it still counts the hike.
    assert thursday["quads"]["week_volume"] == legs["quads"]["week_volume"]

    # The vote: a leg circuit on Monday is held (yellow), with the cause;
    # an upper-body circuit is not touched.
    lower = session_muscle_weights("lower_body", 4, "2026-10-05")
    vote = session_vote(monday, lower)
    assert vote and vote[1] == "yellow", vote
    assert "quadriceps" in vote[2] and "randonnee" in vote[2], vote
    upper = session_muscle_weights("upper_body", 4, "2026-10-05")
    assert session_vote(monday, upper) is None
    assert session_vote(empty, lower) is None

    # An activity after the reading time is not counted yet.
    _session("evening", "2026-10-05", 18, 60, label="running")
    morning = muscle_fatigue(conn, uid, "2026-10-05")
    assert morning["activities"] == 1
    later = muscle_fatigue(
        conn, uid, "2026-10-05",
        at=dt.datetime(2026, 10, 5, 21, 0, tzinfo=dt.timezone.utc),
    )
    assert later["activities"] == 2
    assert later["muscles"]["calves"]["fatigue"] > (
        morning["muscles"]["calves"]["fatigue"]
    )

    # A strength session on a coach circuit day takes that circuit's
    # muscles (upper body), not the generic full-body guess.
    conn.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message, tier) VALUES (?, '2026-10-06T06:00', "
        "'2026-10-06', 'green', 'upper_body', 4, 'm', 'train')", (uid,),
    )
    conn.commit()
    _session("circuit", "2026-10-06", 18, 30, exercise_type=70, rpe=8)
    after = muscle_fatigue(
        conn, uid, "2026-10-06",
        at=dt.datetime(2026, 10, 6, 20, 0, tzinfo=dt.timezone.utc),
    )["muscles"]
    assert after["chest"]["cause"] == "upper_body", after["chest"]
    assert after["chest"]["state"] == "fatigued", after["chest"]
    # Harder sessions weigh more: RPE 8 counts above a plain one.
    assert after["chest"]["fatigue"] > 1.0
    # A rest day's log row does not turn a strength session into it.
    assert _coach_day(conn, uid, "2026-10-07") is None

    print("muscles.py: all checks passed")
