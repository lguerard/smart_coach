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
"""

import datetime as dt
import json
import os
import sqlite3

import body

MEAL_MODEL = os.environ.get("MEAL_ANALYSIS_MODEL", "claude-opus-5-5")
KINDS = ("plate", "menu")

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
    noon = f"{row['local_date']}T12:00:00+00:00"
    conn.execute(
        "INSERT OR REPLACE INTO nutrition (uuid, user_id, start_utc, end_utc, "
        "local_date, meal_type, calories, protein_g, carbs_g, fat_g) VALUES "
        "(?, ?, ?, ?, ?, 2, ?, ?, ?, ?)",
        (f"smartcoach-meal-{meal_id}", user_id, noon, noon, row["local_date"],
         total["kcal"], total.get("protein_g", 0), total.get("carbs_g", 0),
         total.get("fat_g", 0)),
    )
    conn.execute("UPDATE meal_estimates SET added = 1 WHERE id = ?", (meal_id,))
    conn.commit()
    return True


def remove_from_day(conn: sqlite3.Connection, user_id: int, meal_id: int) -> bool:
    """Take an added estimate back out of the day's nutrition."""
    deleted = conn.execute(
        "DELETE FROM nutrition WHERE uuid = ? AND user_id = ?",
        (f"smartcoach-meal-{meal_id}", user_id),
    ).rowcount
    conn.execute(
        "UPDATE meal_estimates SET added = 0 WHERE id = ? AND user_id = ?",
        (meal_id, user_id),
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
    unusable = analyze(conn, uid, photo, "plate", day, analyzer=lambda j, p: {
        "usable": False, "items": [], "total": {"kcal": 0, "protein_g": 0,
        "carbs_g": 0, "fat_g": 0}, "confidence": "low", "best_choice": "",
        "advice": ""})
    assert not add_to_day(conn, uid, unusable["id"])
    print("meals.py: all checks passed (no live model call made)")
