#!/usr/bin/env python3
"""The person's own changes to their circuits, kept across mornings.

The morning run rebuilds every session from the ladders, so anything
edited on the watch or in the calendar would be gone the next day.
Changes made here are stored instead and applied every time a session
is built (``training.user_session_values``): on the watch, in the
calendar, on the dashboard and in the message alike.

Two kinds of change, per session type -- and per kettlebell circuit
(A, B, or both):

- a library move taken out of the circuit;
- a move of their own: a name, how to do it, reps or seconds, done in
  every tour or once at the end (a stretch), with an optional Garmin
  exercise so the watch shows its name and picture.

Own moves keep a fixed count: they are not on a ladder, so the level
does not move them.
"""

import datetime as dt
import sqlite3

import exercise_library

EDITABLE_TYPES = ("lower_body", "upper_body", "calisthenics", "kettlebell")
CIRCUITS = ("", "A", "B")  # "" = every circuit of the type
UNITS = ("reps", "sec")
PLACEMENTS = ("round", "end")  # in every tour / once, after the last tour
MAX_NAME, MAX_CUE, MAX_AMOUNT = 60, 300, 600
REP_SECONDS = 3  # rough time per rep, for the session length

# Garmin exercises a move of one's own can be shown as (checked against
# Garmin's catalog with the others, scripts/build_garmin_names.py).
GARMIN_PICKER = {
    "": ("Aucun (nom générique)", None),
    "triceps_stretch": ("Étirement triceps", ("WARM_UP", "TRICEPS_STRETCH")),
    "biceps_stretch": ("Étirement biceps", ("WARM_UP", "BICEPS_STRETCH")),
    "chest_stretch": ("Étirement pectoraux", ("WARM_UP", "STRETCH_PECTORAL")),
    "shoulder_stretch": ("Étirement épaules", ("WARM_UP", "STRETCH_SHOULDER")),
    "lat_stretch": ("Étirement dos (grand dorsal)", ("WARM_UP", "STRETCH_LAT")),
    "forearm_stretch": ("Étirement avant-bras", ("WARM_UP", "STRETCH_FOREARMS")),
    "abs_stretch": ("Étirement abdos", ("WARM_UP", "STRETCH_ABS")),
    "hamstring_stretch": ("Étirement ischios", ("WARM_UP", "STRETCH_HAMSTRING")),
    "quad_stretch": ("Étirement quadriceps", ("WARM_UP", "STRETCH_QUAD")),
    "calf_stretch": ("Étirement mollets", ("WARM_UP", "STRETCH_CALF")),
    "triceps_extension": ("Extension triceps",
                          ("TRICEPS_EXTENSION",
                           "OVERHEAD_DUMBBELL_TRICEPS_EXTENSION")),
    "kb_curl": ("Curl biceps kettlebell", ("CURL", "KETTLEBELL_BICEPS_CURL")),
    "kb_press": ("Développé kettlebell",
                 ("BENCH_PRESS", "KETTLEBELL_CHEST_PRESS")),
    "shoulder_press": ("Développé épaules",
                       ("SHOULDER_PRESS", "OVERHEAD_DUMBBELL_PRESS")),
    "kb_row": ("Rowing kettlebell", ("ROW", "KETTLEBELL_ROW")),
    "kb_swing": ("Swing kettlebell", ("HIP_RAISE", "KETTLEBELL_SWING")),
    "kb_deadlift": ("Soulevé de terre kettlebell",
                    ("DEADLIFT", "KETTLEBELL_DEADLIFT")),
    "goblet_squat": ("Goblet squat", ("SQUAT", "GOBLET_SQUAT")),
    "lunge": ("Fente", ("LUNGE", "LUNGE")),
    "push_up": ("Pompe", ("PUSH_UP", "PUSH_UP")),
    "plank": ("Gainage", ("PLANK", "PLANK")),
    "crunch": ("Crunch", ("CRUNCH", "CRUNCH")),
    "russian_twist": ("Rotation russe", ("CORE", "RUSSIAN_TWIST")),
    "jumping_jacks": ("Jumping jacks", ("CARDIO", "JUMPING_JACKS")),
}

_NON_STEP = {"rounds", "duration_min", "variants", "rest_sec", "tours",
             "finisher"}


def add_move(
    conn: sqlite3.Connection, user_id: int, session_type: str, circuit: str,
    name: str, cue: str, unit: str, amount, placement: str,
    garmin_key: str = "",
) -> int:
    """Store a move of the person's own, after validating every field.

    Raises:
        ValueError: Any field out of range, in French (shown as is).

    Returns:
        int: The new move's id.
    """
    name, cue = (name or "").strip(), (cue or "").strip()
    if session_type not in EDITABLE_TYPES:
        raise ValueError("Type de séance inconnu.")
    if circuit not in CIRCUITS or (circuit and session_type != "kettlebell"):
        raise ValueError("Circuit inconnu.")
    if not name or len(name) > MAX_NAME:
        raise ValueError(f"Le nom doit faire entre 1 et {MAX_NAME} caractères.")
    if len(cue) > MAX_CUE:
        raise ValueError(f"La consigne doit faire au plus {MAX_CUE} caractères.")
    if unit not in UNITS or placement not in PLACEMENTS:
        raise ValueError("Unité ou placement inconnu.")
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise ValueError("La quantité doit être un nombre.") from None
    if not 1 <= amount <= MAX_AMOUNT:
        raise ValueError(f"La quantité doit être entre 1 et {MAX_AMOUNT}.")
    if garmin_key not in GARMIN_PICKER:
        raise ValueError("Exercice Garmin inconnu.")
    cursor = conn.execute(
        "INSERT INTO custom_moves (user_id, session_type, circuit, name, cue, "
        "unit, amount, placement, garmin_key, created_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, session_type, circuit, name, cue, unit, amount, placement,
         garmin_key, dt.datetime.now(dt.timezone.utc).isoformat()),
    )
    conn.commit()
    return cursor.lastrowid


def delete_move(conn: sqlite3.Connection, user_id: int, move_id: int) -> bool:
    """Delete one of the person's own moves (theirs only)."""
    deleted = conn.execute(
        "DELETE FROM custom_moves WHERE id = ? AND user_id = ?",
        (move_id, user_id),
    ).rowcount
    conn.commit()
    return bool(deleted)


def set_removed(
    conn: sqlite3.Connection, user_id: int, session_type: str, circuit: str,
    slot: str, removed: bool,
) -> None:
    """Take a library move out of a circuit, or put it back."""
    if session_type not in EDITABLE_TYPES or circuit not in CIRCUITS:
        raise ValueError("Séance inconnue.")
    if slot not in exercise_library.LADDERS:
        raise ValueError("Mouvement inconnu.")
    if removed:
        conn.execute(
            "INSERT OR IGNORE INTO removed_moves VALUES (?, ?, ?, ?)",
            (user_id, session_type, circuit, slot),
        )
    else:
        conn.execute(
            "DELETE FROM removed_moves WHERE user_id = ? AND session_type = ? "
            "AND circuit = ? AND slot = ?",
            (user_id, session_type, circuit, slot),
        )
    conn.commit()


def moves_for(
    conn: sqlite3.Connection, user_id: int, session_type: str,
    circuit: str = "",
) -> list[sqlite3.Row]:
    """Own moves that apply to this type and circuit, oldest first."""
    return conn.execute(
        "SELECT * FROM custom_moves WHERE user_id = ? AND session_type = ? "
        "AND circuit IN ('', ?) ORDER BY id", (user_id, session_type, circuit),
    ).fetchall()


def removed_for(
    conn: sqlite3.Connection, user_id: int, session_type: str,
    circuit: str = "",
) -> set:
    """Library slots taken out of this type and circuit."""
    return {
        row["slot"] for row in conn.execute(
            "SELECT slot FROM removed_moves WHERE user_id = ? AND "
            "session_type = ? AND circuit IN ('', ?)",
            (user_id, session_type, circuit),
        )
    }


def _move_seconds(unit: str, amount: int) -> int:
    return amount if unit == "sec" else amount * REP_SECONDS


def _variant(row: sqlite3.Row) -> dict:
    garmin = GARMIN_PICKER[row["garmin_key"]][1] if row["garmin_key"] in GARMIN_PICKER else None
    return {"key": f"custom_{row['id']}", "name": row["name"],
            "cue": row["cue"], "garmin": list(garmin) if garmin else [],
            "muscles": {}, "custom": True}


def apply(
    conn: sqlite3.Connection, user_id: int, session_type: str, values: dict,
    circuit: str = "",
) -> dict:
    """``values`` with the person's changes: library moves taken out,
    own moves added in every tour (keys ``custom_<id>``, ``_sec`` for
    timed ones) or once at the end (``finisher``), and the length
    adjusted. A change that would leave no move at all is ignored, so a
    session is never empty.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        session_type (str): The session type.
        values (dict): ``training.session_values`` output (not changed).
        circuit (str): Kettlebell circuit of the day, "" otherwise.

    Returns:
        dict: New values (the same dict object when nothing applies).
    """
    if session_type not in EDITABLE_TYPES:
        return values
    removed = removed_for(conn, user_id, session_type, circuit)
    own = moves_for(conn, user_id, session_type, circuit)
    if not removed and not own:
        return values
    out = {k: v for k, v in values.items() if k not in removed}
    variants = {k: v for k, v in (values.get("variants") or {}).items()
                if k not in removed}
    rounds = values.get("rounds") or 1
    rest = values.get("rest_sec") or 0
    seconds = 0
    for slot in removed & set(values):
        unit = exercise_library.LADDERS[slot]["unit"]
        seconds -= rounds * (_move_seconds(unit, values[slot]) + rest)
    finisher = []
    for row in own:
        key = f"custom_{row['id']}{'_sec' if row['unit'] == 'sec' else ''}"
        if row["placement"] == "round":
            out[key] = row["amount"]
            variants[key] = _variant(row)
            seconds += rounds * (_move_seconds(row["unit"], row["amount"]) + rest)
        else:
            finisher.append({**_variant(row), "slot": key,
                             "amount": row["amount"], "unit": row["unit"]})
            seconds += _move_seconds(row["unit"], row["amount"])
    if not any(k not in _NON_STEP for k in out) and not finisher:
        return values  # everything taken out: keep the session as built
    if "variants" in values or variants:
        out["variants"] = variants
    if finisher:
        out["finisher"] = finisher
    if "duration_min" in values:
        out["duration_min"] = max(1, round(values["duration_min"] + seconds / 60))
    return out


def finisher_text(values: dict) -> str:
    """'' or "a la fin : etirement triceps 30 s, ..." for descriptions."""
    items = values.get("finisher") or []
    if not items:
        return ""
    return "à la fin : " + ", ".join(
        f"{item['name']} {item['amount']}{' s' if item['unit'] == 'sec' else ''}"
        for item in items
    )


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db
    import training

    conn = db.connect(Path(tempfile.mkdtemp()) / "custom.db")
    db.init_db(conn)
    uid = db.create_user(conn, "c", "pw-123456")
    other = db.create_user(conn, "o", "pw-123456")
    friday, saturday = "2026-10-16", "2026-10-17"
    base = training.session_values("kettlebell", 0, date=friday)
    circuit = training.kettlebell_circuit(friday)

    # Nothing stored: the very same values come back.
    assert apply(conn, uid, "kettlebell", base, circuit) is base

    # The reported wish: a kettlebell triceps stretch, once at the end.
    stretch = add_move(conn, uid, "kettlebell", "", "Étirement triceps",
                       "Coude plié derrière la tête, tire doucement.", "sec",
                       30, "end", "triceps_stretch")
    out = apply(conn, uid, "kettlebell", base, circuit)
    assert out["finisher"][0]["name"] == "Étirement triceps", out
    assert out["finisher"][0]["garmin"] == ["WARM_UP", "TRICEPS_STRETCH"]
    assert out["duration_min"] == round(base["duration_min"] + 0.5), out
    assert "à la fin : Étirement triceps 30 s" == finisher_text(out)
    assert base.get("finisher") is None  # input untouched
    # Applies to both circuits ("" = all) but not to another user.
    assert apply(conn, uid, "kettlebell", training.session_values(
        "kettlebell", 0, date=saturday), training.kettlebell_circuit(saturday),
    ).get("finisher")
    assert apply(conn, other, "kettlebell", base, circuit) is base

    # A move in every tour, only on circuit B; a library move removed.
    b_only = add_move(conn, uid, "kettlebell", "B", "Pompes diamant", "",
                      "reps", 8, "round", "push_up")
    set_removed(conn, uid, "kettlebell", "B", "kb_situp_press_sec", True)
    b_day = friday if circuit == "B" else saturday
    b_values = training.session_values("kettlebell", 0, date=b_day)
    b_out = apply(conn, uid, "kettlebell", b_values, "B")
    assert f"custom_{b_only}" in b_out and b_out[f"custom_{b_only}"] == 8
    assert "kb_situp_press_sec" not in b_out, b_out
    assert "kb_situp_press_sec" not in b_out["variants"]
    assert b_out["variants"][f"custom_{b_only}"]["garmin"] == ["PUSH_UP", "PUSH_UP"]
    a_day = saturday if b_day == friday else friday
    a_out = apply(conn, uid, "kettlebell",
                  training.session_values("kettlebell", 0, date=a_day), "A")
    assert not any(k.startswith("custom_") and not k.endswith("_sec")
                   for k in a_out), a_out  # B's own move stays on B
    # Length: +5 tours x (8 reps x 3 s + 10 s) -5 x (20 + 10) + 30 s.
    expected = b_values["duration_min"] + (5 * 34 - 5 * 30 + 30) / 60
    assert b_out["duration_min"] == max(1, round(expected)), b_out

    # Putting a move back, deleting an own move (owner only).
    set_removed(conn, uid, "kettlebell", "B", "kb_situp_press_sec", False)
    assert "kb_situp_press_sec" in apply(conn, uid, "kettlebell", b_values, "B")
    assert not delete_move(conn, other, stretch)
    assert delete_move(conn, uid, stretch)

    # Never an empty session: taking every move out is ignored.
    upper = training.session_values("upper_body", 2, date=friday)
    for slot in [k for k in upper if k not in _NON_STEP]:
        set_removed(conn, uid, "upper_body", "", slot, True)
    assert apply(conn, uid, "upper_body", upper) is upper

    # Validation, in French.
    for bad in (
        dict(session_type="treadmill"), dict(circuit="B", session_type="upper_body"),
        dict(name=""), dict(name="x" * 61), dict(unit="kg"),
        dict(amount="abc"), dict(amount=0), dict(amount=601),
        dict(placement="middle"), dict(garmin_key="nope"),
    ):
        args = dict(session_type="kettlebell", circuit="", name="ok", cue="",
                    unit="reps", amount=10, placement="round", garmin_key="")
        args.update(bad)
        try:
            add_move(conn, uid, **args)
        except ValueError:
            continue
        raise AssertionError(bad)
    try:
        set_removed(conn, uid, "kettlebell", "", "not_a_slot", True)
        raise AssertionError("unknown slot accepted")
    except ValueError:
        pass
    print("custom_moves.py: all checks passed")
