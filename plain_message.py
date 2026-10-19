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
    "protein_short": "Il te manquait {gap_g} g de protéines hier.",
    "calories_over": "Tu as dépassé ta cible d'environ {over_kcal} kcal hier.",
    "water_short": "Il te manquait environ {gap_ml} ml d'eau hier.",
    "on_track": "Hier, côté alimentation, tu étais dans tes objectifs.",
    "older_day": "Le repas d'hier n'est pas encore synchronisé ; on regarde "
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
    "steps_low": "Hier, {value} pas sur {goal} : une petite marche ce midi "
                 "suffit à rattraper.",
    "floors_low": "Hier, peu d'étages : prends l'escalier une fois ou deux.",
    "intensity_low": "Cette semaine, peu de minutes d'effort : la séance du "
                     "jour compte.",
    "stress_high": "Hier a ete stressant : prends deux minutes pour "
                   "respirer.",
    "battery_drained": "Hier t'a vide : garde de l'energie pour ce soir.",
}
MOVEMENT_EN = {
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
            f"Aujourd'hui, repos : pas de séance ({replaces} annulé). De "
            "l'eau, du sommeil, et une marche tranquille si l'envie vient."
            if fr else
            f"Today is a rest day: no session ({replaces} cancelled). "
            "Water, sleep, and an easy walk if you feel like it."
        )
    if kind == "recovery":
        replaces = session.get("replaces")
        instead = (
            (f" La séance {replaces} est annulée ce soir : c'est la seule "
             "chose à faire aujourd'hui." if fr else
             f" {replaces} is cancelled tonight: it is the only thing to "
             "do today.") if replaces else ""
        )
        incline = values.get("incline_pct")
        if incline:
            return (
                f"Aujourd'hui, marche active en pente : "
                f"{values.get('speed_kmh')} km/h, {incline} % de pente, "
                f"{values.get('duration_min')} min.{instead}"
                if fr else
                f"Today, a brisk uphill walk: {values.get('speed_kmh')} "
                f"km/h, {incline}% incline, {values.get('duration_min')} "
                f"min.{instead}"
            )
        return (
            f"Aujourd'hui, marche tranquille : {values.get('speed_kmh')} "
            f"km/h, a plat, {values.get('duration_min')} min.{instead}"
            if fr else
            f"Today, an easy walk: {values.get('speed_kmh')} km/h, flat, "
            f"{values.get('duration_min')} min.{instead}"
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
        minutes = values.get("duration_min")
        length = (f" (environ {minutes} min)" if fr else
                  f" (about {minutes} min)") if minutes else ""
        line = (
            f"Ce soir, {rounds} tours{length} : {moves}." if fr else
            f"Tonight, {rounds} rounds{length}: {moves}."
        )
        if session.get("progress_hint"):
            line += (f" Pour monter : {session['progress_hint']}." if fr
                     else f" To step up: {session['progress_hint']}.")
        return line
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
            line += (f" Par exemple à midi : {items}." if fr
                     else f" For example at lunch: {items}.")
        water = help_.get("water")
        if water:
            line += (f" {water['glasses']} grands verres dans la journée."
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

    changed = brief.get("yesterday_session")
    if changed:
        paragraphs.append(
            f"Hier, {changed['done']} a la place de {changed['instead_of']} : "
            "c'est note, et compte comme fait." if fr else
            f"Yesterday, {changed['done']} instead of {changed['instead_of']}: "
            "noted, and counted as done."
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

    if brief.get("next_step"):
        paragraphs.append(brief["next_step"])
    return "\n\n".join(p.strip() for p in paragraphs if p.strip())


# --- The card: the default morning message ------------------------------
# Short, always the same shape, read in ten seconds: the night, tonight's
# session and why it is at that level, one thing about yesterday. Built
# straight from the decisions, so nothing in it can be misread or made up.

SESSION_FR = {
    "treadmill": "Tapis", "lower_body": "Muscu bas du corps",
    "upper_body": "Muscu haut du corps", "calisthenics": "Calisthénie",
    "kettlebell": "Kettlebell",
}
SESSION_EN = {
    "treadmill": "Treadmill", "lower_body": "Lower body",
    "upper_body": "Upper body", "calisthenics": "Calisthenics",
    "kettlebell": "Kettlebell",
}
FELT_FR = {"good": "bien dormi", "ok": "nuit moyenne", "bad": "mal dormi"}
FELT_EN = {"good": "slept well", "ok": "average night", "bad": "slept badly"}
# Signal labels (training._all_votes) as the card says them.
SIGNAL_FR = {
    "sommeil": "nuit", "recuperation": "récupération",
    "vfc": "variabilité cardiaque", "fc de repos": "cœur au repos",
    "batterie": "batterie", "stress (veille)": "stress d'hier",
    "charge recente": "semaine chargée", "ressenti": "dernières notes",
    "fatigue musculaire": "muscles encore fatigués",
}


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else (
        ", ".join(names[:-1]) + " et " + names[-1])


def _level_line(session: dict, body: dict, fr: bool) -> str:
    """Up, same or lighter tonight, and why, in plain words."""
    import re

    reason = session.get("level_reason") or ""
    if not reason:
        return ""
    moved = re.search(r"niveau (\d+) -> (\d+)", reason)
    causes = re.search(r"\(([^)]*)\)", reason)
    names = [SIGNAL_FR.get(n.strip(), n.strip())
             for n in causes.group(1).split(",")] if causes else []
    if body.get("tired_muscles"):
        names = [f"muscles encore fatigués ({body['tired_muscles']})"]
    if not fr:
        if moved and int(moved.group(2)) > int(moved.group(1)):
            return "Stepping up tonight."
        if moved:
            return "A lighter session tonight."
        return "Same session as last time."
    if "semaine allegee" in reason:
        return "Semaine plus légère : c'est voulu, pour bien récupérer."
    if moved and int(moved.group(2)) > int(moved.group(1)):
        why = ("ta dernière séance était trop facile" if "trop facile" in reason
               else "tu as réussi les dernières à ce niveau"
               if "seances" in reason or "séances" in reason
               else "bonne forme ce matin")
        return f"On monte d'un cran ce soir : {why}."
    if moved:
        return ("Séance allégée ce soir"
                + (f" : {_and(names)} pas au top." if names else "."))
    if "maximum" in reason:
        return "Tu es au plus haut niveau de cette séance."
    if "minimum" in reason:
        return "La version la plus douce de la séance ce soir."
    if "trop dure" in reason:
        return "Même séance que la dernière fois : tu l'as trouvée dure."
    return ("Même séance que la dernière fois"
            + (f" : {_and(names)} un peu juste{'s' if len(names) > 1 else ''}."
               if names else "."))


def _bell_kg(moves: list[str]) -> tuple[list[str], str]:
    """The moves without their "(6 kg)", and the weight, said once."""
    import re

    weights = {m.group(1) for m in (
        re.search(r" \((\d+(?:[.,]\d+)?) kg\)", x) for x in moves) if m}
    if len(weights) != 1:
        return moves, ""
    kg = weights.pop()
    return [x.replace(f" ({kg} kg)", "") for x in moves], kg


def _moves_block(session: dict, fr: bool) -> str:
    """The moves, with a shared effort/rest time said once."""
    import re

    moves, _ = _bell_kg(list(session.get("moves") or []))
    finisher = [m for m in moves if m.startswith("à la fin")]
    moves = [m for m in moves if m not in finisher]
    timed = [re.match(r"(.*) (\d+)s$", m) for m in moves]
    rest = (session.get("values") or {}).get("rest_sec")
    if moves and all(timed) and len({t.group(2) for t in timed}) == 1:
        work = timed[0].group(2)
        head = (f"{work} s d'effort" + (f", {rest} s de repos" if rest else "")
                if fr else
                f"{work} s on" + (f", {rest} s rest" if rest else ""))
        line = f"{head} : " + " · ".join(t.group(1) for t in timed)
    else:
        line = " · ".join(moves)
    if finisher:
        line += " · " + finisher[0]
    return line + "." if line else ""


def _session_card(session: dict, fr: bool) -> list[str]:
    kind = session.get("type")
    values = session.get("values") or {}
    if kind in ("rest", "recovery") or not kind:
        line = _session_line(session, fr)
        icon = "🛌" if kind == "rest" else "🚶"
        return [f"{icon} {line}"] if line else []
    labels = SESSION_FR if fr else SESSION_EN
    label = labels.get(kind, kind)
    if kind == "treadmill":
        head = (f"🏃 Ce soir : {label}, {values.get('speed_kmh')} km/h, "
                f"{values.get('incline_pct')} % de pente, "
                f"{values.get('duration_min')} min." if fr else
                f"🏃 Tonight: {label}, {values.get('speed_kmh')} km/h, "
                f"{values.get('incline_pct')}% incline, "
                f"{values.get('duration_min')} min.")
        return [head]
    if not session.get("moves"):
        return [f"🏋️ {session['note']}."] if session.get("note") else []
    _, kg = _bell_kg(list(session.get("moves") or []))
    if kg:
        label += f" {kg} kg"
    rounds, minutes = values.get("rounds"), values.get("duration_min")
    size = ", ".join(x for x in (
        f"{rounds} tours" if fr and rounds else f"{rounds} rounds" if rounds else "",
        (f"environ {minutes} min" if fr else f"about {minutes} min") if minutes else "",
    ) if x)
    head = (f"🏋️ Ce soir : {label}" if fr else f"🏋️ Tonight: {label}") + (
        f" — {size}" if size else "")
    return [head, _moves_block(session, fr)]


def card(brief: dict) -> str:
    """The morning message as a short card, from a ``coach_brief`` brief.

    Returns:
        str: A few lines, one per point, blank lines between blocks.
    """
    fr = brief.get("language", "fr") != "en"
    body = brief.get("body") or {}
    session = brief.get("session") or {}
    blocks = []

    # The night: what the person said first, the watch in brackets.
    felt = (FELT_FR if fr else FELT_EN).get(body.get("sleep_felt") or "")
    score = body.get("sleep_score")
    night = felt or (f"score {score:.0f}/100" if score is not None else "")
    if felt and score is not None:
        night += f" (montre : {score:.0f})" if fr else f" (watch: {score:.0f})"
    parts = [f"{'Nuit' if fr else 'Night'} : {night}" if night else ""]
    readiness = body.get("training_readiness_score")
    if readiness is not None:
        parts.append(f"{'récupération' if fr else 'readiness'} {readiness:.0f}/100")
    line = " · ".join(p for p in parts if p)
    if line:
        blocks.append(f"🌙 {line[0].upper()}{line[1:]}")
    if body.get("illness_signs"):
        blocks.append(("⚠️ À surveiller : " if fr else "⚠️ Watch for: ")
                      + ", ".join(body["illness_signs"])
                      + (". Si de vrais symptômes arrivent, consulte."
                         if fr else ". If real symptoms show up, see a doctor."))

    # Tonight, and why at that level.
    session_lines = _session_card(session, fr)
    level = _level_line(session, body, fr)
    if level:
        session_lines.append(f"➡️ {level}")
    hint = session.get("progress_hint") or ""
    if hint and fr:
        import re

        steady = re.search(r"apres (\d+)", hint)
        session_lines.append(
            "La prochaine séance de ce type montera d'un cran."
            if hint.startswith("la prochaine") else
            "Pour monter : note-la « trop facile » ce soir"
            + (f", sinon elle monte après {steady.group(1)} séances à ce niveau."
               if steady else "."))
    elif hint:
        session_lines.append(f"To step up: {hint}.")
    if brief.get("heavier_bell"):
        bell = brief["heavier_bell"]
        session_lines.append(
            f"Ta kettlebell de {bell['current_kg']:g} kg devient légère : "
            f"passe à {bell['next_kg']:g} kg (à indiquer dans Réglages)."
            if fr else
            f"Your {bell['current_kg']:g} kg bell is getting light: move to "
            f"{bell['next_kg']:g} kg (set it in Settings).")
    if session_lines:
        blocks.append("\n".join(session_lines))

    # Yesterday: one point at most.
    changed = brief.get("yesterday_session")
    if changed:
        blocks.append(
            f"✅ Hier : {changed['done']} à la place de "
            f"{changed['instead_of']}, c'est noté." if fr else
            f"✅ Yesterday: {changed['done']} instead of "
            f"{changed['instead_of']}, noted.")
    elif brief.get("skipped") and brief.get("angle") == "skipped_yesterday":
        blocks.append("Hier, pas de séance : inutile de rattraper, celle du "
                      "jour suffit." if fr else
                      "No session yesterday: no need to make up for it.")
    point = brief.get("yesterday") or {}
    help_ = brief.get("help") or {}
    yesterday = _yesterday_line(point, help_, fr)
    if yesterday:
        icon = "🍽️" if point.get("topic") == "food" else "👟"
        blocks.append(f"{icon} {yesterday}")

    care = help_.get("care")
    if care:
        blocks.append(
            f"💧 Bois environ {care['drink_ml'] / 1000:.1f} L. Consulte si tu "
            f"as {care['see_doctor_if']}." if fr else
            f"💧 Drink about {care['drink_ml'] / 1000:.1f} L. See a doctor "
            f"if you have {care['see_doctor_if']}.")
    # The closing step, except on a training day: the rating buttons
    # tonight already ask for it.
    if brief.get("next_step") and brief.get("angle") != "train":
        blocks.append(brief["next_step"])
    return "\n\n".join(b.strip() for b in blocks if b.strip())


def card_or_none(brief: Optional[dict]) -> Optional[str]:
    """``card`` that never raises; the older prose layout as a last try."""
    try:
        return card(brief or {}) or render(brief or {})
    except Exception:  # noqa: BLE001 -- the morning must still go out
        return render_or_none(brief)


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
        "next_step": "Demain on regarde comment tu te sens.",
    })
    assert "repos" in rest and "Tapis annulé" in rest, rest
    assert "2.5 L" in rest and "fievre" in rest, rest
    assert rest.endswith("te sens."), rest

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
    assert "38 g de protéines" in circuit and "150 g yaourt grec" in circuit
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
    walk = render({"language": "fr", "angle": "recovery", "session": {
        "type": "recovery", "replaces": "Kettlebell a la maison",
        "values": {"speed_kmh": 4.5, "duration_min": 20}}})
    assert "La séance Kettlebell a la maison est annulée ce soir" in walk, walk
    assert "seule chose" in walk, walk
    uphill = render({"language": "fr", "angle": "recovery", "session": {
        "type": "recovery", "replaces": "Tapis",
        "values": {"speed_kmh": 5.0, "incline_pct": 5, "duration_min": 35}}})
    assert "marche active en pente : 5.0 km/h, 5 % de pente, 35 min" in uphill
    swapped = render({"language": "fr", "angle": "train", "yesterday_session": {
        "done": "Kettlebell a la maison", "instead_of": "marche de recuperation"}})
    assert "Hier, Kettlebell a la maison a la place de marche de recuperation" in swapped

    # The card, the default morning message.
    morning = {
        "language": "fr", "angle": "train",
        "body": {"sleep_felt": "bad", "sleep_score": 78,
                 "training_readiness_score": 62},
        "session": {
            "type": "kettlebell",
            "values": {"rounds": 4, "duration_min": 24, "rest_sec": 10},
            "moves": ["goblet squat 25s", "rowing, 1 bras 25s",
                      "à la fin : étirement triceps 30 s"],
            "level_reason": "jour jaune (sommeil, recuperation) : niveau "
                            "maintenu a 5",
            "progress_hint": "fais la seance et note-la : 'trop facile' la "
                             "fait monter des la prochaine fois, sinon elle "
                             "monte apres 2 seances faites a ce niveau"},
        "yesterday": {"topic": "food", "kind": "protein_short", "gap_g": 38},
        "help": {"food": {"items": [{"qty": "2", "name": "oeufs"}]}},
        "next_step": "Ce soir, dis-moi sur le tableau de bord...",
    }
    text = card(morning)
    # The night as said first, the watch in brackets; never "assis".
    assert text.startswith("🌙 Nuit : mal dormi (montre : 78)"), text
    assert "25 s d'effort, 10 s de repos : goblet squat · rowing, 1 bras" in text
    assert "· à la fin : étirement triceps 30 s." in text, text
    assert "nuit et récupération un peu justes" in text, text
    assert "après 2 séances à ce niveau" in text, text
    assert "protéines" in text and "tableau de bord" not in text, text
    assert "assis" not in text and "bureau" not in text, text
    up = card({**morning, "session": {**morning["session"],
        "level_reason": "ta derniere seance de ce type etait trop facile : "
                        "niveau 3 -> 4", "progress_hint": ""}})
    assert "On monte d'un cran ce soir : ta dernière séance était trop facile." in up
    lighter = card({**morning, "session": {**morning["session"],
        "level_reason": "jour rouge (vfc, sommeil) : niveau 5 -> 4"}})
    assert "Séance allégée ce soir : variabilité cardiaque et nuit pas au top." in lighter
    walk = card({"language": "fr", "angle": "recovery", "body": {},
                 "session": {"type": "recovery", "replaces": "Kettlebell",
                             "values": {"speed_kmh": 5, "incline_pct": 5,
                                        "duration_min": 35}},
                 "next_step": "Si ton corps suit, on remonte demain."})
    assert walk.startswith("🚶") and "La séance Kettlebell est annulée" in walk
    assert walk.endswith("on remonte demain."), walk
    tread = card({"language": "fr", "angle": "train", "body": {"sleep_score": 81},
                  "session": {"type": "treadmill", "values": {
                      "speed_kmh": 6.3, "incline_pct": 12, "duration_min": 20}}})
    assert tread == ("🌙 Nuit : score 81/100\n\n🏃 Ce soir : Tapis, 6.3 km/h, "
                     "12 % de pente, 20 min."), tread
    weighed = card({"language": "fr", "angle": "train", "session": {
        "type": "kettlebell", "values": {"rounds": 4, "rest_sec": 10},
        "moves": ["goblet squat (6 kg) 25s", "curl kettlebell (6 kg) 25s"]}})
    assert "Kettlebell 6 kg — 4 tours" in weighed, weighed
    assert "goblet squat · curl kettlebell." in weighed, weighed
    assert card_or_none(None) is not None
    print("plain_message.py: all checks passed")
