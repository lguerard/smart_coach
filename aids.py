#!/usr/bin/env python3
"""The concrete help behind the morning message, worked out in code.

"Eat more protein" and "see how you feel" are conclusions the person
cannot act on. This module turns a gap into a short, sized list of real
foods, a water shortfall into a number of glasses, an ill day into a few
practical care points, and every kind of day into one honest next step.
It is deterministic -- the same gap on the same date gives the same
suggestion, rotated daily so the advice does not become wallpaper -- and
the model is told to repeat it, not to improvise amounts.

Food values are common portion averages, deliberately round: this is a
nudge to cover a gap, not a nutrition label.
"""

import datetime as dt
import math

# (name_fr, name_en, protein_g per portion, kcal per portion,
#  portion label fr/en, portions allowed, easy to eat at a desk)
FOODS = [
    ("oeuf dur", "hard-boiled egg", 6, 70, "", "", 3, True),
    ("yaourt grec ou skyr", "Greek yoghurt or skyr", 15, 90,
     "150 g", "150 g", 2, True),
    ("fromage blanc 0 %", "0% fromage frais", 14, 80,
     "200 g", "200 g", 1, True),
    ("petite boite de thon au naturel", "small tin of tuna in water", 25,
     110, "", "", 1, True),
    ("jambon blanc", "lean ham", 16, 80, "2 tranches", "2 slices", 1, True),
    ("blanc de poulet cuit", "cooked chicken breast", 30, 150,
     "100 g", "100 g", 1, True),
    ("shaker de whey", "whey shake", 24, 120, "1 dose", "1 scoop", 1, True),
]
GLASS_ML = 250
MAX_GLASSES = 4
MAX_FOOD_ITEMS = 3

CARE_DOCTOR_FR = (
    "de la fievre, un essoufflement inhabituel, une douleur ou une gene "
    "dans la poitrine, ou si ca ne s'ameliore pas apres 3 jours"
)
CARE_DOCTOR_EN = (
    "a fever, unusual breathlessness, pain or tightness in the chest, "
    "or no improvement after 3 days"
)

NEXT_STEP = {
    "rest": {
        "fr": "Demain on regarde comment tu te sens ; la reprise se fera "
              "en douceur, par une marche, pas directement par le tapis.",
        "en": "Tomorrow we check how you feel; the restart will be "
              "gentle, a walk first, not straight back on the treadmill.",
    },
    "recovery": {
        "fr": "Si ton corps suit, on remonte tranquillement d'un cran "
              "demain.",
        "en": "If your body follows, we step back up gently tomorrow.",
    },
    "skipped_yesterday": {
        "fr": "Inutile de rattraper : la seance du jour suffit, on ne "
              "double pas.",
        "en": "No need to make up for it: today's session is enough, "
              "we do not double up.",
    },
    "green_streak": {
        "fr": "Le but maintenant : garder ce rythme regulier plutot que "
              "chercher un gros effort isole.",
        "en": "The aim now: keep this steady rhythm rather than chase "
              "one big isolated effort.",
    },
    "train": {
        "fr": "Ce soir, dis-moi sur le tableau de bord si c'etait trop "
              "facile, juste bien ou trop dur : c'est comme ca que "
              "j'ajuste la suite.",
        "en": "Tonight, tell me on the dashboard whether it was too "
              "easy, just right or too hard: that is how I adjust what "
              "comes next.",
    },
}


def _name(food: tuple, language: str) -> str:
    return food[1] if language == "en" else food[0]


def food_help(
    gap_protein_g: float, date: str, language: str = "fr",
    office: bool = True,
) -> dict:
    """A short, sized list of foods that covers a protein gap.

    Rotated by date so a week of the same gap does not get the same
    answer, and limited to desk-friendly foods when ``office`` is set
    (the lunch happens at work).

    Parameters:
        gap_protein_g (float): Grams still missing.
        date (str): ISO local date; seeds the rotation.
        language (str): ``fr`` or ``en``.
        office (bool): Only foods that need no cooking at a desk.

    Returns:
        dict: ``items`` (``name``, ``qty``, ``protein_g``, ``kcal``),
        ``total_protein_g``, ``total_kcal`` and ``covers_gap`` (False
        when even the maximum list falls short).
    """
    language = language if language in ("fr", "en") else "fr"
    pool = [food for food in FOODS if food[7] or not office]
    seed = dt.date.fromisoformat(date).toordinal()
    start = seed % len(pool)
    pool = pool[start:] + pool[:start]

    remaining = float(gap_protein_g)
    items: list[dict] = []
    used: set[str] = set()
    while remaining > 4 and len(items) < MAX_FOOD_ITEMS:
        choice = next(
            (
                food for food in pool
                if food[0] not in used and food[2] <= remaining + 10
            ),
            None,
        )
        if choice is None:
            break
        used.add(choice[0])
        quantity = 1
        if choice[6] > 1:
            quantity = max(1, min(choice[6], round(remaining / choice[2])))
        label = choice[5] if language == "en" else choice[4]
        if label:
            qty = f"{quantity} x {label}" if quantity > 1 else label
        else:
            qty = f"x{quantity}"
        protein = choice[2] * quantity
        items.append({
            "name": _name(choice, language), "qty": qty,
            "protein_g": protein, "kcal": choice[3] * quantity,
        })
        remaining -= protein
    total = sum(item["protein_g"] for item in items)
    return {
        "items": items, "total_protein_g": total,
        "total_kcal": sum(item["kcal"] for item in items),
        "covers_gap": total >= gap_protein_g - 4,
    }


def water_help(gap_ml: float, language: str = "fr") -> dict:
    """The water shortfall as a number of glasses and a habit.

    Parameters:
        gap_ml (float): Millilitres still missing.
        language (str): ``fr`` or ``en``.

    Returns:
        dict: ``glasses`` (capped, so the advice stays doable), ``ml``
        and ``habit`` -- a bottle left on the desk.
    """
    glasses = min(MAX_GLASSES, max(1, math.ceil(gap_ml / GLASS_ML)))
    return {
        "glasses": glasses, "ml": round(gap_ml),
        "habit": (
            "Une gourde pleine sur le bureau, a finir avant la fin de "
            "l'apres-midi." if language == "fr"
            else "A full bottle on the desk, finished before the end of "
                 "the afternoon."
        ),
    }


def lighter_dinner(over_kcal: float, language: str = "fr") -> dict:
    """What to do about a calorie overshoot, without a lecture.

    Parameters:
        over_kcal (float): How far over the target yesterday was.
        language (str): ``fr`` or ``en``.

    Returns:
        dict: ``over_kcal`` and ``how``: the one simple lever.
    """
    return {
        "over_kcal": round(over_kcal),
        "how": (
            "Au diner, moitie moins de feculents, plus de legumes, et "
            "on garde les proteines." if language == "fr"
            else "At dinner, half the starch, more vegetables, and keep "
                 "the protein."
        ),
    }


def care_help(
    hydration_target_ml: float | None, language: str = "fr",
) -> dict:
    """Practical care for a rest day, with when to see a doctor.

    Conservative on purpose: the coach reads a watch, not a patient, so
    it names the usual reasons to get checked and stops there.

    Parameters:
        hydration_target_ml (float | None): Today's water target.
        language (str): ``fr`` or ``en``.

    Returns:
        dict: ``drink_ml``, ``resume_when`` and ``see_doctor_if``.
    """
    fr = language != "en"
    return {
        "drink_ml": round(hydration_target_ml or 2000),
        "resume_when": (
            "deux matins d'affilee sans signe de fatigue anormale"
            if fr else "two mornings in a row without unusual fatigue"
        ),
        "see_doctor_if": CARE_DOCTOR_FR if fr else CARE_DOCTOR_EN,
    }


def next_step(angle: str, language: str = "fr") -> str:
    """The honest next step for this kind of day, or ``""``.

    Parameters:
        angle (str): ``coach_brief.angle`` value.
        language (str): ``fr`` or ``en``.

    Returns:
        str: One sentence; the closing line is built on it so it says
        something specific instead of a stock encouragement.
    """
    options = NEXT_STEP.get(angle)
    if not options:
        return ""
    return options["en" if language == "en" else "fr"]


if __name__ == "__main__":
    # Every gap gets a real, sized answer, and the list is honest about
    # whether it covers the gap.
    for gap in (8, 15, 25, 38, 55, 80):
        for day in range(7):
            date = (dt.date(2026, 10, 5) + dt.timedelta(days=day)).isoformat()
            plan = food_help(gap, date)
            assert plan["items"], (gap, date)
            assert len(plan["items"]) <= MAX_FOOD_ITEMS
            assert plan["total_protein_g"] == sum(
                i["protein_g"] for i in plan["items"]
            )
            if gap <= 55:
                assert plan["covers_gap"], (gap, date, plan)
            # Never a repeated food in one suggestion.
            names = [i["name"] for i in plan["items"]]
            assert len(set(names)) == len(names), plan
    # The reported case: 38 g short, at a desk.
    plan = food_help(38, "2026-10-06")
    assert 30 <= plan["total_protein_g"] <= 60, plan
    # Rotates across days, stable within one.
    assert food_help(38, "2026-10-06") == food_help(38, "2026-10-06")
    assert len({
        tuple(i["name"] for i in food_help(38, f"2026-10-{d:02d}")["items"])
        for d in range(5, 12)
    }) > 2
    # English names, and the very small gap needs nothing.
    assert all(
        i["name"] != j[0]
        for i in food_help(20, "2026-10-06", "en")["items"] for j in FOODS
        if j[0] != j[1]
    )
    assert food_help(3, "2026-10-06")["items"] == []

    assert water_help(900)["glasses"] == 4
    assert water_help(300)["glasses"] == 2
    assert water_help(50)["glasses"] == 1
    assert water_help(5000)["glasses"] == MAX_GLASSES  # stays doable
    assert lighter_dinner(349.6)["over_kcal"] == 350

    care = care_help(2400)
    assert care["drink_ml"] == 2400 and "fievre" in care["see_doctor_if"]
    assert care_help(None)["drink_ml"] == 2000
    assert "fever" in care_help(None, "en")["see_doctor_if"]

    for angle in ("rest", "recovery", "skipped_yesterday", "green_streak",
                  "train"):
        assert next_step(angle) and next_step(angle, "en"), angle
    assert next_step("nonsense") == ""
    # The training-day close points at a feedback loop that exists.
    assert "tableau de bord" in next_step("train")

    print("aids.py: all checks passed")
