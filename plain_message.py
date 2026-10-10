#!/usr/bin/env python3
"""The morning message without any language model.

Used when every configured model is unavailable (subscription quota
spent, API down, no network): the brief already holds every decision
and every number, so a plain, friendly message can be assembled from it
directly. Less varied than a written one, never wrong, and the morning
notification still arrives -- which matters more than its prose.
"""

from typing import Optional

TIP_KIND_FR = {
    "protein_short": "Il te manquait {gap_g} g de proteines hier.",
    "calories_over": "Tu as depasse ta cible d'environ {over_kcal} kcal hier.",
    "water_short": "Il te manquait environ {gap_ml} ml d'eau hier.",
    "on_track": "Hier, cote alimentation, tu etais dans tes objectifs.",
    "older_day": "Le repas d'hier n'est pas encore synchronise ; on regarde "
                 "le {date}.",
}
TIP_KIND_EN = {
    "protein_short": "You were {gap_g} g short on protein yesterday.",
    "calories_over": "You went about {over_kcal} kcal over yesterday.",
    "water_short": "You were about {gap_ml} ml short on water yesterday.",
    "on_track": "Yesterday your food was on target.",
    "older_day": "Yesterday's food has not synced yet; looking at {date}.",
}
MOVEMENT_FR = {
    "sedentary_high": "Hier, beaucoup d'heures assis : leve-toi un peu "
                      "toutes les heures aujourd'hui.",
    "steps_low": "Hier, {value} pas sur {goal} : une petite marche ce midi "
                 "suffit a rattraper.",
    "floors_low": "Hier, peu d'etages : prends l'escalier une fois ou deux.",
    "intensity_low": "Cette semaine, peu de minutes d'effort : la seance du "
                     "jour compte.",
    "stress_high": "Hier a ete stressant : prends deux minutes pour "
                   "respirer.",
    "battery_drained": "Hier t'a vide : garde de l'energie pour ce soir.",
}
MOVEMENT_EN = {
    "sedentary_high": "Lots of sitting yesterday: stand up a little every "
                      "hour today.",
    "steps_low": "Yesterday, {value} steps of {goal}: a short walk at lunch "
                 "makes up for it.",
    "floors_low": "Few floors yesterday: take the stairs once or twice.",
    "intensity_low": "Few active minutes this week: today's session counts.",
    "stress_high": "Yesterday was stressful: take two minutes to breathe.",
    "battery_drained": "Yesterday drained you: keep some energy for tonight.",
}


def _session_line(session: dict, fr: bool) -> str:
    """What to do today, from the brief's session block."""
    kind = session.get("type")
    values = session.get("values") or {}
    if kind == "rest":
        replaces = session.get("replaces") or ""
        return (
            f"Aujourd'hui, repos : pas de seance ({replaces} annule). De "
            "l'eau, du sommeil, et une marche tranquille si l'envie vient."
            if fr else
            f"Today is a rest day: no session ({replaces} cancelled). "
            "Water, sleep, and an easy walk if you feel like it."
        )
    if kind == "recovery":
        return (
            f"Aujourd'hui, marche tranquille : {values.get('speed_kmh')} "
            f"km/h, a plat, {values.get('duration_min')} min."
            if fr else
            f"Today, an easy walk: {values.get('speed_kmh')} km/h, flat, "
            f"{values.get('duration_min')} min."
        )
    if kind == "treadmill":
        return (
            f"Ce soir, tapis : {values.get('speed_kmh')} km/h, "
            f"{values.get('incline_pct')} % de pente, "
            f"{values.get('duration_min')} min."
            if fr else
            f"Tonight, treadmill: {values.get('speed_kmh')} km/h, "
            f"{values.get('incline_pct')}% incline, "
            f"{values.get('duration_min')} min."
        )
    if session.get("moves"):
        moves = ", ".join(session["moves"][:4])
        rounds = values.get("rounds")
        return (
            f"Ce soir, {rounds} tours : {moves}." if fr else
            f"Tonight, {rounds} rounds: {moves}."
        )
    if session.get("note"):
        return session["note"] + "."
    return ""


def _yesterday_line(point: dict, help_: dict, fr: bool) -> str:
    """The single point about yesterday, with its sized help."""
    topic = point.get("topic")
    if topic == "food":
        template = (TIP_KIND_FR if fr else TIP_KIND_EN).get(point.get("kind"))
        if not template:
            return ""
        line = template.format(**{k: point.get(k, "") for k in (
            "gap_g", "over_kcal", "gap_ml", "date")})
        food = help_.get("food")
        if food and food.get("items"):
            items = " + ".join(
                f"{item['qty']} {item['name']}".replace("x1 ", "")
                for item in food["items"]
            )
            line += (f" Par exemple a midi : {items}." if fr
                     else f" For example at lunch: {items}.")
        water = help_.get("water")
        if water:
            line += (f" {water['glasses']} grands verres dans la journee."
                     if fr else
                     f" {water['glasses']} big glasses through the day.")
        dinner = help_.get("lighter_dinner")
        if dinner:
            line += f" {dinner['how']}"
        return line
    if topic == "movement":
        template = (MOVEMENT_FR if fr else MOVEMENT_EN).get(point.get("flag"))
        if template:
            return template.format(value=point.get("value", ""),
                                   goal=point.get("goal", ""))
    return ""


def render(brief: dict) -> str:
    """A complete, friendly message from a ``coach_brief`` brief.

    Parameters:
        brief (dict): ``coach_brief.build_brief`` output.

    Returns:
        str: Plain text, short paragraphs separated by blank lines.
    """
    fr = brief.get("language", "fr") != "en"
    angle = brief.get("angle")
    body = brief.get("body") or {}
    session = brief.get("session") or {}
    paragraphs = []

    opener = {
        "rest": ("Salut ! Ton corps a besoin de calme aujourd'hui, et c'est "
                 "la bonne decision de l'ecouter."
                 if fr else
                 "Hi! Your body needs calm today, and listening to it is "
                 "the right call."),
        "recovery": ("Salut ! Journee en douceur aujourd'hui : on recupere."
                     if fr else "Hi! An easy day today: we recover."),
        "green_streak": (f"Bonjour ! Encore un bon matin, ca fait "
                         f"{brief.get('green_streak')} d'affilee."
                         if fr else
                         f"Good morning! Another good morning, "
                         f"{brief.get('green_streak')} in a row."),
    }.get(angle, "Bonjour !" if fr else "Good morning!")
    if body.get("tired_muscles"):
        opener += (f" Note : {body['tired_muscles']}, la seance reste au meme "
                   "niveau." if fr else
                   f" Note: {body['tired_muscles']}, the session stays at "
                   "the same level.")
    paragraphs.append(opener + " " + _session_line(session, fr))

    if brief.get("skipped") and angle == "skipped_yesterday":
        paragraphs.append(
            "La seance d'hier n'a pas eu lieu : pas besoin de rattraper, "
            "celle du jour suffit." if fr else
            "Yesterday's session did not happen: no need to make up for "
            "it, today's is enough."
        )

    care = (brief.get("help") or {}).get("care")
    if care:
        paragraphs.append(
            f"Bois environ {care['drink_ml'] / 1000:.1f} L. Consulte si tu as "
            f"{care['see_doctor_if']}." if fr else
            f"Drink about {care['drink_ml'] / 1000:.1f} L. See a doctor if "
            f"you have {care['see_doctor_if']}."
        )

    yesterday = _yesterday_line(brief.get("yesterday") or {},
                                brief.get("help") or {}, fr)
    if yesterday:
        paragraphs.append(yesterday)

    desk = brief.get("desk") or {}
    if desk.get("items"):
        item = desk["items"][0]
        when = f" ({item['when']})" if item.get("when") else ""
        paragraphs.append(
            f"Au bureau{when} : {item['name']}. {item['how']}" if fr else
            f"At your desk{when}: {item['name']}. {item['how']}"
        )

    if brief.get("next_step"):
        paragraphs.append(brief["next_step"])
    return "\n\n".join(p.strip() for p in paragraphs if p.strip())


def render_or_none(brief: Optional[dict]) -> Optional[str]:
    """``render`` that never raises -- the last line of defence."""
    try:
        return render(brief or {})
    except Exception:  # noqa: BLE001 -- the morning must still go out
        return None


if __name__ == "__main__":
    rest = render({
        "language": "fr", "angle": "rest", "body": {},
        "session": {"type": "rest", "replaces": "Tapis"},
        "yesterday": {"topic": "none"},
        "help": {"care": {"drink_ml": 2500, "see_doctor_if": "de la fievre"}},
        "desk": {"items": [{"name": "Respiration carree", "how": "4-4-4-4.",
                            "seconds": 90, "when": "avant ta reunion"}]},
        "next_step": "Demain on regarde comment tu te sens.",
    })
    assert "repos" in rest and "Tapis annule" in rest, rest
    assert "2.5 L" in rest and "fievre" in rest, rest
    assert "Respiration carree" in rest and rest.endswith("te sens."), rest

    circuit = render({
        "language": "fr", "angle": "train",
        "body": {"tired_muscles": "pectoraux encore fatigues (natation)"},
        "session": {"type": "upper_body", "values": {"rounds": 3},
                    "moves": ["pompes 9", "dips 10"]},
        "yesterday": {"topic": "food", "kind": "protein_short", "gap_g": 38},
        "help": {"food": {"items": [
            {"name": "yaourt grec", "qty": "150 g", "protein_g": 15},
            {"name": "oeuf dur", "qty": "x2", "protein_g": 12}]}},
        "next_step": "Ce soir, dis-moi comment c'etait.",
    })
    assert "3 tours : pompes 9, dips 10" in circuit, circuit
    assert "38 g de proteines" in circuit and "150 g yaourt grec" in circuit
    assert "pectoraux encore fatigues" in circuit, circuit

    english = render({"language": "en", "angle": "train",
                      "session": {"type": "treadmill", "values": {
                          "speed_kmh": 6.5, "incline_pct": 12,
                          "duration_min": 25}},
                      "yesterday": {"topic": "movement", "flag": "steps_low",
                                    "value": 2765, "goal": 10000}})
    assert "Tonight, treadmill: 6.5 km/h, 12% incline, 25 min." in english
    assert "2765 steps of 10000" in english, english
    # Nothing in the brief still gives a greeting, never an exception.
    assert render({}) == "Bonjour !"
    assert render_or_none({"session": "broken"}) is None
    print("plain_message.py: all checks passed")
