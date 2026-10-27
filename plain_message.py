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
FELT_FR = {"good": "bien dormi", "ok": "nuit normale", "bad": "mal dormi"}
FELT_EN = {"good": "slept well", "ok": "normal night", "bad": "slept badly"}
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
    kg = kg or session.get("bell_kg")
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


WIN_FR = {"seance": "la séance", "repos": "le repos", "proteines": "les protéines",
          "calories": "les calories", "eau": "l'eau", "pas": "les pas"}


def _num(value) -> str:
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else str(value)


def _review_block(brief: dict, fr: bool) -> str:
    """Yesterday reviewed, with a conclusion -- not a list."""
    if not fr:
        return ""
    review = brief.get("review") or {}
    sentences = []
    training = review.get("training") or {}
    planned, done = training.get("planned"), training.get("done")
    if done == "rien":
        if training.get("planned_tier") == "train" and planned:
            sentences.append(f"Hier, la séance {planned.lower()} n'a pas été faite.")
    elif done and planned and done != planned:
        sentences.append(f"Hier, {done.lower()} à la place de {planned.lower()}.")
    elif done:
        detail = ", ".join(x for x in (
            f"{training['minutes']} min" if training.get("minutes") else "",
            f"notée « {training['rating']} »" if training.get("rating") else "",
        ) if x)
        sentences.append(f"Hier, séance {done.lower()} faite"
                         + (f" ({detail})." if detail else "."))
    elif training.get("planned_tier") in ("rest", "recovery"):
        sentences.append("Hier, journée de récupération.")
    elif planned:
        sentences.append(f"Hier, aucune séance enregistrée ({planned.lower()} "
                         "prévue).")
    food = review.get("food") or {}
    eaten = [f"{food['protein_g']['eaten']} g de protéines sur "
             f"{food['protein_g']['target']}" if food.get("protein_g") else "",
             f"{_num(food['calories_kcal']['eaten'])} kcal sur "
             f"{_num(food['calories_kcal']['target'])}"
             if food.get("calories_kcal") else ""]
    eaten = [x for x in eaten if x]
    if eaten:
        sentences.append("Côté assiette : " + ", ".join(eaten) + ".")
    energy = review.get("energy") or {}
    if energy.get("eaten_kcal") is not None and energy.get("burned_kcal"):
        balance = energy["balance_kcal"]
        sentences.append(
            f"Calories : {_num(energy['eaten_kcal'])} mangées, "
            f"{_num(energy['burned_kcal'])} brûlées selon la montre, soit "
            + (f"{_num(-balance)} de moins que dépensé." if balance < 0
               else f"{_num(balance)} de plus que dépensé." if balance > 0
               else "autant que dépensé."))
    elif energy.get("burned_kcal"):
        sentences.append(f"{_num(energy['burned_kcal'])} kcal brûlées selon "
                         "la montre.")
    elif food.get("kind") in ("not_synced", "log_partial"):
        sentences.append("Tes repas d'hier ne sont pas encore tous arrivés : "
                         "pas de bilan alimentaire.")
    steps = review.get("steps") or {}
    if steps.get("value") is not None:
        sentences.append(f"{_num(steps['value'])} pas"
                         + (f" sur {_num(steps['goal'])}." if steps.get("goal") else "."))
    wins = [WIN_FR.get(w, w) for w in review.get("wins") or []]
    misses = [WIN_FR.get(m, m) for m in review.get("misses") or []]
    verdict = review.get("verdict")
    if verdict == "good":
        sentences.append("Bilan : une bonne journée, tout a suivi.")
    elif verdict == "mixed":
        sentences.append(f"Bilan : {_and(wins)}, c'est fait ; {_and(misses)}, "
                         "à rattraper aujourd'hui.")
    elif verdict == "off":
        sentences.append(f"Bilan : journée plus difficile ({_and(misses)}), "
                         "on repart sur de bonnes bases aujourd'hui.")
    if not sentences:
        # An older brief without a review: its one point about yesterday.
        return _yesterday_line(brief.get("yesterday") or {},
                               brief.get("help") or {}, fr)
    food_help = ((brief.get("help") or {}).get("food") or {}).get("items")
    if food_help:
        items = " + ".join(f"{i['qty']} {i['name']}".replace("x1 ", "")
                           for i in food_help)
        sentences.append(f"Pour rattraper : {items} à midi.")
    return " ".join(sentences)


def _changes_block(brief: dict, fr: bool) -> str:
    """What is different tonight from the last time, and why."""
    if not fr:
        return ""
    changes = brief.get("changes") or {}
    parts = []
    for key, fmt in (("level", "niveau {a} → {b}"), ("rounds", "{a} → {b} tours"),
                     ("work_sec", "{a} → {b} s d'effort"),
                     ("bell_kg", "kettlebell {a} → {b} kg"),
                     ("speed_kmh", "{a} → {b} km/h"),
                     ("incline_pct", "pente {a} → {b} %"),
                     ("duration_min", "{a} → {b} min")):
        if changes.get(key):
            parts.append(fmt.format(a=changes[key]["from"], b=changes[key]["to"]))
    parts += [f"{h['from']} → {h['to']}" for h in changes.get("harder") or []]
    if changes.get("added"):
        parts.append("en plus : " + ", ".join(changes["added"]))
    if changes.get("removed"):
        parts.append("en moins : " + ", ".join(changes["removed"]))
    text = changes.get("level_text") or ""
    if parts:
        days = changes.get("days")
        since = ("Par rapport à hier" if days == 1 else
                 f"Par rapport à ta dernière séance (il y a {days} jours)"
                 if days else "Par rapport à ta dernière séance")
        return f"{since} : " + ", ".join(parts) + ". " + text
    return text


def card(brief: dict) -> str:
    """The morning message, without a model, from a ``coach_brief`` brief:
    yesterday reviewed, what changes tonight and why, then tonight.

    Returns:
        str: Three short blocks separated by blank lines.
    """
    fr = brief.get("language", "fr") != "en"
    body = brief.get("body") or {}
    session = brief.get("session") or {}
    blocks = []

    # Yesterday, then the night: what the person said first.
    felt = (FELT_FR if fr else FELT_EN).get(body.get("sleep_felt") or "")
    score = body.get("sleep_score")
    night = felt or (f"score {score:.0f}/100" if score is not None else "")
    if felt and score is not None:
        night += f" (montre : {score:.0f})" if fr else f" (watch: {score:.0f})"
    review = _review_block(brief, fr)
    read = body.get("night") or {}
    if read and fr:
        minutes = read["sleep_min"]
        wakes = read.get("wakes") or []
        night = ", ".join(x for x in (
            night,
            f"environ {minutes // 60} h {minutes % 60:02d} de sommeil",
            (f"réveillé vers {_and(wakes)}" if wakes else ""),
            (f"sans te rendormir après {read['final_wake']}"
             if read.get("final_wake") else ""),
        ) if x)
    night_line = (f"Cette nuit : {night}." if fr else f"Last night: {night}.") if night else ""
    trend = " ".join((brief.get("sleep_trend") or {}).get("points") or []) if fr else ""
    if review or night_line or trend:
        blocks.append(" ".join(x for x in (review, night_line, trend) if x))
    if body.get("illness_signs"):
        blocks.append(("⚠️ À surveiller : " if fr else "⚠️ Watch for: ")
                      + ", ".join(body["illness_signs"])
                      + (". Si de vrais symptômes arrivent, consulte."
                         if fr else ". If real symptoms show up, see a doctor."))

    # What changes, and why.
    changes = _changes_block(brief, fr) or _level_line(session, body, fr)
    if changes:
        blocks.append(changes)

    # Tonight.
    session_lines = _session_card(session, fr)
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

    care = (brief.get("help") or {}).get("care")
    if care:
        blocks.append(
            f"💧 Bois environ {care['drink_ml'] / 1000:.1f} L. Consulte si tu "
            f"as {care['see_doctor_if']}." if fr else
            f"💧 Drink about {care['drink_ml'] / 1000:.1f} L. See a doctor "
            f"if you have {care['see_doctor_if']}.")
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
    morning["review"] = {
        "training": {"planned": "Kettlebell a la maison",
                     "planned_tier": "train", "done": "Kettlebell a la maison",
                     "recorded": True, "minutes": 26, "rating": "bien"},
        "food": {"kind": "protein_short",
                 "protein_g": {"eaten": 57, "target": 95},
                 "calories_kcal": {"eaten": 1650, "target": 2100}},
        "steps": {"value": 3900, "goal": 8000},
        "wins": ["seance"], "misses": ["proteines", "pas"], "verdict": "mixed",
    }
    morning["changes"] = {
        "last_date": "2026-10-17", "days": 2,
        "level": {"from": 4, "to": 5}, "work_sec": {"from": 25, "to": 30},
        "harder": [{"from": "fente avant", "to": "fente marchee"}],
        "level_text": "On monte d'un cran ce soir : tu as réussi les "
                      "dernières à ce niveau.",
    }
    text = card(morning)
    review, changed, tonight = text.split("\n\n")
    # Yesterday reviewed with a conclusion, then the night as said.
    assert review.startswith("Hier, séance kettlebell a la maison faite "
                             "(26 min, notée « bien »)."), review
    assert "Côté assiette : 57 g de protéines sur 95, 1 650 kcal sur 2 100." in review
    assert "3 900 pas sur 8 000." in review, review
    assert "Bilan : la séance, c'est fait ; les protéines et les pas, à " \
        "rattraper aujourd'hui." in review, review
    assert "Pour rattraper : 2 oeufs à midi." in review, review
    assert review.endswith("Cette nuit : mal dormi (montre : 78)."), review
    # What changes and why.
    assert changed.startswith("Par rapport à ta dernière séance (il y a 2 "
                              "jours) : niveau 4 → 5, 25 → 30 s d'effort, "
                              "fente avant → fente marchee."), changed
    assert changed.endswith("tu as réussi les dernières à ce niveau."), changed
    # Tonight.
    assert tonight.startswith("🏋️ Ce soir : Kettlebell — 4 tours"), tonight
    assert "25 s d'effort, 10 s de repos : goblet squat · rowing, 1 bras" in text
    assert "· à la fin : étirement triceps 30 s." in text, text
    assert "après 2 séances à ce niveau" in text, text
    assert "tableau de bord" not in text and "assis" not in text, text
    assert "bureau" not in text, text
    # Nothing changed: the reason alone, from the level decision.
    same = card({**morning, "changes": {"level_text": "Même séance que la "
                                         "dernière fois : nuit un peu juste."}})
    assert "\n\nMême séance que la dernière fois : nuit un peu juste.\n\n" in same
    # Without a review (older brief): the one point about yesterday.
    old_style = card({k: v for k, v in morning.items()
                      if k not in ("review", "changes")})
    assert old_style.startswith("Il te manquait 38 g de protéines hier."), old_style
    assert "nuit et récupération un peu justes" in old_style, old_style
    bare = {k: v for k, v in morning.items() if k not in ("review", "changes")}
    up = card({**bare, "session": {**morning["session"],
        "level_reason": "ta derniere seance de ce type etait trop facile : "
                        "niveau 3 -> 4", "progress_hint": ""}})
    assert "On monte d'un cran ce soir : ta dernière séance était trop facile." in up
    lighter = card({**bare, "session": {**morning["session"],
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
    assert tread == ("Cette nuit : score 81/100.\n\n🏃 Ce soir : Tapis, 6.3 "
                     "km/h, 12 % de pente, 20 min."), tread
    weighed = card({"language": "fr", "angle": "train", "session": {
        "type": "kettlebell", "values": {"rounds": 4, "rest_sec": 10},
        "moves": ["goblet squat (6 kg) 25s", "curl kettlebell (6 kg) 25s"]}})
    assert "Kettlebell 6 kg — 4 tours" in weighed, weighed
    assert "goblet squat · curl kettlebell." in weighed, weighed
    # Last night read from the heart rate: length and awakenings.
    read = card({"language": "fr", "body": {"sleep_felt": "bad", "night": {
        "sleep_min": 320, "watch_sleep_min": 350, "wakes": ["02:41", "04:40"],
        "final_wake": "04:40"}}})
    assert read == ("Cette nuit : mal dormi, environ 5 h 20 de sommeil, "
                    "réveillé vers 02:41 et 04:40, sans te rendormir après "
                    "04:40."), read
    trended = card({"language": "fr", "body": {"sleep_felt": "ok"},
                    "sleep_trend": {"points": ["Sur les 11 dernières nuits, tu "
                                               "dors en moyenne 5 h 52 : couche-toi "
                                               "vers 22:33 ce soir.", "Autre point."]}})
    assert trended == ("Cette nuit : nuit normale. Sur les 11 dernières nuits, "
                       "tu dors en moyenne 5 h 52 : couche-toi vers 22:33 ce "
                       "soir. Autre point."), trended
    assert card_or_none(None) is not None
    # Calories eaten against burned, said as where the day stands.
    stood = card({"language": "fr", "review": {
        "food": {"kind": "on_track"},
        "energy": {"eaten_kcal": 1850, "burned_kcal": 2400,
                   "balance_kcal": -550}}})
    assert ("Calories : 1 850 mangées, 2 400 brûlées selon la montre, soit "
            "550 de moins que dépensé.") in stood, stood
    assert "Pour rattraper" not in stood, stood
    print("plain_message.py: all checks passed")
