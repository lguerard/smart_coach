#!/usr/bin/env python3
"""A photo of a meal (or of a menu) -> calories and protein.

Lunch at a restaurant or a canteen is exactly the meal that never gets
logged, and the one that decides whether the day's protein target is
reached. A photo of the plate gives an estimate per food and in total;
a photo of the menu or the canteen board gives the best choice for what
is left of today's targets.

The photo is only sent to the model and never stored; only the
estimate is kept (``meal_estimates``). "Ajouter à ma journée" writes the
estimate into the day's nutrition (uuid ``smartcoach-meal-<id>``, which
the Health Connect reconciliation leaves alone), so the dashboard, the
afternoon check-in and tomorrow's message count it.

MyFitnessPal has no API to write into, and reads no food from Health
Connect, so the estimate goes there by hand ("Copier pour
MyFitnessPal", for its quick add). Once the meal comes back from the
diary through Health Connect, ``match_logged`` takes the estimate's
own row out (``added = 2``): the meal counts once, whichever arrived
first.
"""

import datetime as dt
import json
import os
import sqlite3

import body

MEAL_MODEL = os.environ.get("MEAL_ANALYSIS_MODEL", "claude-opus-5-5")
KINDS = ("plate", "menu")
# added: not in the day / in it (our row) / found in the food diary.
NOT_ADDED, ADDED, IN_DIARY = 0, 1, 2
# Health Connect meal types, and the local hour each one ends.
BREAKFAST, LUNCH, DINNER, SNACK = 1, 2, 3, 4
_MEAL_ENDS = ((10.5, BREAKFAST), (15.0, LUNCH), (18.0, SNACK), (24.0, DINNER))
# A diary entry is the same meal when its calories are this close.
MATCH_KCAL_RATIO, MATCH_KCAL_MIN = 0.25, 120
MATCH_DAYS = 7

_MACROS = {
    "kcal": {"type": "integer"}, "protein_g": {"type": "integer"},
    "carbs_g": {"type": "integer"}, "fat_g": {"type": "integer"},
}
MEAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["usable", "items", "total", "confidence", "best_choice",
                 "advice"],
    "properties": {
        "usable": {"type": "boolean"},
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "grams", *_MACROS],
            "properties": {"name": {"type": "string"},
                           "grams": {"type": "integer"}, **_MACROS},
        }},
        "total": {"type": "object", "additionalProperties": False,
                  "required": list(_MACROS), "properties": _MACROS},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "best_choice": {"type": "string"},
        "advice": {"type": "string"},
    },
}


def _prompt(kind: str, remaining: dict) -> str:
    left = []
    if remaining.get("calories_kcal") is not None:
        left.append(f"{remaining['calories_kcal']} kcal")
    if remaining.get("protein_g") is not None:
        left.append(f"{remaining['protein_g']} g de proteines")
    budget = (f"Il lui reste aujourd'hui environ {', '.join(left)}. "
              if left else "")
    common = (
        "Tu aides un homme sedentaire qui veut perdre du gras et prendre "
        "du muscle, et qui mange souvent au restaurant ou a la cantine. "
        f"{budget}Reponds en francais, avec des portions realistes de "
        "restaurant ou de cantine en France. Ne fais aucune remarque sur "
        "son corps ou son poids, pas de morale. Si l'image ne montre ni "
        "repas ni menu, usable=false et le reste vide ou a 0. "
    )
    if kind == "menu":
        return common + (
            "La photo montre un menu, une carte ou le tableau d'une "
            "cantine. items : les 2 ou 3 plats les plus interessants pour "
            "lui, avec une estimation par portion habituelle. total : le "
            "plat que tu recommandes. best_choice : ce plat et pourquoi, "
            "en une phrase (proteines d'abord, sans depasser ce qui lui "
            "reste). advice : un ajustement simple a demander (sauce a "
            "part, legumes en plus, pas de pain...), une phrase."
        )
    return common + (
        "La photo montre son assiette. items : chaque aliment visible, "
        "son poids estime en grammes et ses calories et macros. total : "
        "la somme. confidence : ta confiance dans l'estimation. "
        "best_choice : chaine vide. advice : une phrase concrete pour le "
        "reste de la journee au vu de ce qui lui reste (ex. 'il te "
        "manque ~40 g de proteines : skyr et whey ce soir')."
    )


def analyze(
    conn: sqlite3.Connection, user_id: int, raw: bytes, kind: str,
    date: str, analyzer=None,
) -> dict:
    """Estimate a meal (or pick from a menu) and store the estimate.

    Parameters:
        raw (bytes): The uploaded image (any format Pillow reads).
        kind (str): ``plate`` or ``menu``.
        date (str): Today's ISO local date.
        analyzer: ``(jpeg, prompt) -> dict``; the configured provider by
            default (``body.vision_json``).

    Returns:
        dict: The stored row: ``id``, ``kind``, ``analysis``.

    Raises:
        body.PhotoError: Not an image, too large, unknown kind.
        RuntimeError: The model call failed.
    """
    import progress

    if kind not in KINDS:
        raise body.PhotoError("Type de photo inconnu.")
    jpeg = body.clean_image(raw)  # EXIF (place, time) stripped
    prompt = _prompt(kind, progress.remaining_today(conn, user_id, date).get(
        "remaining") or {})
    analysis = (analyzer or (lambda image, text: body.vision_json(
        image, text, MEAL_SCHEMA, MEAL_MODEL, effort="medium",
    )))(jpeg, prompt)
    cursor = conn.execute(
        "INSERT INTO meal_estimates (user_id, local_date, kind, analysis, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, date, kind, json.dumps(analysis, ensure_ascii=False),
         dt.datetime.now(dt.timezone.utc).isoformat()),
    )
    conn.commit()
    return {"id": cursor.lastrowid, "kind": kind, "analysis": analysis}


def recent(conn: sqlite3.Connection, user_id: int, limit: int = 5) -> list[dict]:
    """The latest estimates, newest first, analysis decoded."""
    return [
        {**dict(row), "analysis": json.loads(row["analysis"])}
        for row in conn.execute(
            "SELECT * FROM meal_estimates WHERE user_id = ? ORDER BY id DESC "
            "LIMIT ?", (user_id, limit),
        )
    ]


def add_to_day(conn: sqlite3.Connection, user_id: int, meal_id: int) -> bool:
    """Write an estimate's total into that day's nutrition (once).

    Returns:
        bool: Whether it was added (False: not found, not usable, or
        already added).
    """
    row = conn.execute(
        "SELECT * FROM meal_estimates WHERE id = ? AND user_id = ?",
        (meal_id, user_id),
    ).fetchone()
    if row is None or row["added"]:
        return False
    analysis = json.loads(row["analysis"])
    total = analysis.get("total") or {}
    if not analysis.get("usable") or not total.get("kcal"):
        return False
    conn.execute(
        "INSERT OR REPLACE INTO nutrition (uuid, user_id, start_utc, end_utc, "
        "local_date, meal_type, calories, protein_g, carbs_g, fat_g) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"smartcoach-meal-{meal_id}", user_id, row["created_at"],
         row["created_at"], row["local_date"],
         meal_type(conn, user_id, row["created_at"]),
         total["kcal"], total.get("protein_g", 0), total.get("carbs_g", 0),
         total.get("fat_g", 0)),
    )
    conn.execute("UPDATE meal_estimates SET added = ? WHERE id = ?",
                 (ADDED, meal_id))
    conn.commit()
    match_logged(conn, user_id, row["local_date"])
    return True


def meal_type(conn: sqlite3.Connection, user_id: int, created_at: str) -> int:
    """The meal a photo was taken at, from its local time."""
    import metrics

    local = dt.datetime.fromisoformat(created_at).astimezone(
        metrics.local_tz(conn, user_id))
    hour = local.hour + local.minute / 60
    return next(kind for end, kind in _MEAL_ENDS if hour < end)


def _close(kcal: float, logged: float) -> bool:
    return abs(logged - kcal) <= max(MATCH_KCAL_MIN, MATCH_KCAL_RATIO * kcal)


def match_logged(
    conn: sqlite3.Connection, user_id: int, date: str,
    days: int = MATCH_DAYS,
) -> list[int]:
    """Take out the estimates the food diary now has too.

    An added estimate matches when that day's diary (any nutrition row
    not written here: MyFitnessPal through Health Connect) has the same
    meal -- same meal type, or none given -- with close calories, as
    one entry (a quick add) or as the sum of that meal's entries. The
    estimate's own row is deleted and it is marked ``IN_DIARY``. Each
    diary meal matches one estimate at most.

    Returns:
        list[int]: The ids matched now.
    """
    start = (dt.date.fromisoformat(date) - dt.timedelta(days=days - 1)).isoformat()
    estimates = conn.execute(
        "SELECT * FROM meal_estimates WHERE user_id = ? AND added = ? AND "
        "local_date BETWEEN ? AND ? ORDER BY id", (user_id, ADDED, start, date),
    ).fetchall()
    used = {
        uuid for r in conn.execute(
            "SELECT diary_uuids FROM meal_estimates WHERE user_id = ? AND "
            "added = ? AND diary_uuids IS NOT NULL", (user_id, IN_DIARY),
        ) for uuid in json.loads(r["diary_uuids"])
    }
    matched = []
    for row in estimates:
        kcal = (json.loads(row["analysis"]).get("total") or {}).get("kcal")
        if not kcal:
            continue
        kind = meal_type(conn, user_id, row["created_at"])
        refused = set(json.loads(row["diary_uuids"] or "[]"))
        diary = conn.execute(
            "SELECT uuid, meal_type, calories FROM nutrition WHERE user_id = ? "
            "AND local_date = ? AND uuid NOT LIKE 'smartcoach-%' AND "
            "calories IS NOT NULL AND (meal_type IS NULL OR meal_type IN (0, ?))",
            (user_id, row["local_date"], kind),
        ).fetchall()
        groups = [((r["uuid"],), r["calories"]) for r in diary]
        same_meal = [r for r in diary if r["meal_type"] == kind]
        if len(same_meal) > 1:
            groups.append((tuple(r["uuid"] for r in same_meal),
                           sum(r["calories"] for r in same_meal)))
        found = next(
            (uuids for uuids, logged in sorted(
                groups, key=lambda g: abs(g[1] - kcal))
             if _close(kcal, logged) and not (used | refused) & set(uuids)),
            None,
        )
        if found is None:
            continue
        used |= set(found)
        conn.execute("DELETE FROM nutrition WHERE uuid = ? AND user_id = ?",
                     (f"smartcoach-meal-{row['id']}", user_id))
        conn.execute(
            "UPDATE meal_estimates SET added = ?, diary_uuids = ? WHERE id = ?",
            (IN_DIARY, json.dumps(list(found)), row["id"]))
        matched.append(row["id"])
    if matched:
        conn.commit()
    return matched


def mfp_text(analysis: dict) -> str:
    """The estimate as typed in MyFitnessPal's quick add."""
    total = analysis.get("total") or {}
    return (f"{total.get('kcal', 0)} kcal · glucides {total.get('carbs_g', 0)} g"
            f" · lipides {total.get('fat_g', 0)} g · protéines "
            f"{total.get('protein_g', 0)} g")


def remove_from_day(conn: sqlite3.Connection, user_id: int, meal_id: int) -> bool:
    """Take an added estimate back out of the day's nutrition. From
    ``IN_DIARY`` ("not the same meal"), the diary entries it matched are
    kept as refused, so adding it again does not match them again."""
    deleted = conn.execute(
        "DELETE FROM nutrition WHERE uuid = ? AND user_id = ?",
        (f"smartcoach-meal-{meal_id}", user_id),
    ).rowcount
    conn.execute(
        "UPDATE meal_estimates SET added = ? WHERE id = ? AND user_id = ?",
        (NOT_ADDED, meal_id, user_id),
    )
    conn.commit()
    return bool(deleted)


def delete(conn: sqlite3.Connection, user_id: int, meal_id: int) -> None:
    """Forget an estimate (and its nutrition row, if added)."""
    remove_from_day(conn, user_id, meal_id)
    conn.execute("DELETE FROM meal_estimates WHERE id = ? AND user_id = ?",
                 (meal_id, user_id))
    conn.commit()


if __name__ == "__main__":
    import io
    import tempfile
    from pathlib import Path

    from PIL import Image

    import db
    import metrics

    conn = db.connect(Path(tempfile.mkdtemp()) / "meals.db")
    db.init_db(conn)
    uid = db.create_user(conn, "m", "pw-123456")
    other = db.create_user(conn, "o", "pw-123456")
    day = "2026-10-17"
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buf, "JPEG")
    photo = buf.getvalue()
    seen = {}

    def fake(jpeg, prompt):
        seen["prompt"] = prompt
        return {"usable": True, "items": [
            {"name": "poulet roti", "grams": 150, "kcal": 280, "protein_g": 42,
             "carbs_g": 0, "fat_g": 12},
            {"name": "riz", "grams": 180, "kcal": 230, "protein_g": 5,
             "carbs_g": 50, "fat_g": 1}],
            "total": {"kcal": 510, "protein_g": 47, "carbs_g": 50, "fat_g": 13},
            "confidence": "medium", "best_choice": "", "advice": "Skyr ce soir."}

    meal = analyze(conn, uid, photo, "plate", day, analyzer=fake)
    assert "assiette" in seen["prompt"] and "corps" in seen["prompt"]
    menu = analyze(conn, uid, photo, "menu", day, analyzer=fake)
    assert "menu" in seen["prompt"] and "best_choice" in seen["prompt"]
    assert [m["id"] for m in recent(conn, uid)] == [menu["id"], meal["id"]]
    assert recent(conn, other) == []
    # Into the day once, back out, owner only.
    assert not add_to_day(conn, other, meal["id"])
    assert add_to_day(conn, uid, meal["id"])
    assert not add_to_day(conn, uid, meal["id"])  # already added
    assert metrics.nutrition_for_date(conn, uid, day)["protein_g"] == 47
    assert remove_from_day(conn, uid, meal["id"])
    assert metrics.nutrition_for_date(conn, uid, day) == {}
    delete(conn, uid, menu["id"])
    assert [m["id"] for m in recent(conn, uid)] == [meal["id"]]
    # Not an image, unknown kind: refused before any model call.
    for raw, kind in ((b"not an image", "plate"), (photo, "selfie")):
        try:
            analyze(conn, uid, raw, kind, day, analyzer=fake)
        except body.PhotoError:
            continue
        raise AssertionError(kind)
    # The diary has it too (MyFitnessPal, through Health Connect): our
    # row goes, the meal counts once. Breakfast at 600 kcal is not the
    # lunch photo at 510; a quick add at 540 at lunch is.
    def diary(uuid, kind, kcal, protein):
        conn.execute(
            "INSERT INTO nutrition (uuid, user_id, start_utc, end_utc, "
            "local_date, meal_type, calories, protein_g) VALUES "
            "(?, ?, 'x', 'x', ?, ?, ?, ?)", (uuid, uid, day, kind, kcal, protein))

    conn.execute("UPDATE meal_estimates SET created_at = ? WHERE id = ?",
                 ("2026-10-17T10:40:00+00:00", meal["id"]))  # 12:40 in Paris
    assert meal_type(conn, uid, "2026-10-17T10:40:00+00:00") == LUNCH
    assert meal_type(conn, uid, "2026-10-17T05:30:00+00:00") == BREAKFAST
    assert meal_type(conn, uid, "2026-10-17T18:30:00+00:00") == DINNER
    diary("mfp-breakfast", BREAKFAST, 520, 20)
    assert add_to_day(conn, uid, meal["id"])
    assert recent(conn, uid)[0]["added"] == ADDED
    assert metrics.nutrition_for_date(conn, uid, day)["calories_kcal"] == 1030
    diary("mfp-lunch", LUNCH, 540, 44)
    assert match_logged(conn, uid, day) == [meal["id"]]
    assert recent(conn, uid)[0]["added"] == IN_DIARY
    assert metrics.nutrition_for_date(conn, uid, day)["calories_kcal"] == 1060
    assert match_logged(conn, uid, day) == []  # once
    assert not add_to_day(conn, uid, meal["id"])  # already counted
    # Logged food by food: the lunch's entries add up to the photo. One
    # diary meal never takes two estimates.
    second = analyze(conn, uid, photo, "plate", day, analyzer=fake)
    conn.execute("UPDATE meal_estimates SET created_at = ? WHERE id = ?",
                 ("2026-10-17T11:00:00+00:00", second["id"]))
    assert add_to_day(conn, uid, second["id"])
    assert recent(conn, uid)[0]["added"] == ADDED  # mfp-lunch is taken
    diary("mfp-lunch-2a", LUNCH, 300, 30)
    diary("mfp-lunch-2b", LUNCH, 200, 10)
    conn.execute("DELETE FROM nutrition WHERE uuid = 'mfp-lunch'")
    conn.commit()
    assert match_logged(conn, uid, day) == [second["id"]]
    # "Not the same meal": added again, it does not match that one.
    remove_from_day(conn, uid, second["id"])
    assert add_to_day(conn, uid, second["id"])
    assert recent(conn, uid)[0]["added"] == ADDED
    assert match_logged(conn, uid, day) == []
    assert "510 kcal" in mfp_text(second["analysis"])
    assert "protéines 47 g" in mfp_text(second["analysis"])
    unusable = analyze(conn, uid, photo, "plate", day, analyzer=lambda j, p: {
        "usable": False, "items": [], "total": {"kcal": 0, "protein_g": 0,
        "carbs_g": 0, "fat_g": 0}, "confidence": "low", "best_choice": "",
        "advice": ""})
    assert not add_to_day(conn, uid, unusable["id"])
    print("meals.py: all checks passed (no live model call made)")
