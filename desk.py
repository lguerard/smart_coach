#!/usr/bin/env python3
"""Silent, invisible movement for a desk job in a shared office.

The coach's sessions happen after work; the other eight hours are spent
sitting, which the watch shows as 13 sedentary hours, a handful of floors
and a stress average above 50. This picks a short break from exercises
that need no space, no equipment, no floor, make no sound and look like
someone adjusting in their chair -- and it picks them from the day's
signals rather than from a fixed list, so a drained or ill day gets
breathing and gentle mobility, never a strength set.

Deterministic on purpose, like the rest of the plan: the same inputs give
the same break, so a regenerated message or the dashboard card shows what
the morning message said, and the LLM only phrases it.
"""

import datetime as dt
import sqlite3
from typing import Optional

import db

# Mon-Fri. A desk break on a Saturday is just a stretch, not an office
# problem to solve.
DESK_WEEKDAYS = range(5)
BREAK_ITEMS = 3
STAIRS_NOTE = {
    "fr": "Prends l'escalier plutot que l'ascenseur au moins une fois "
          "aujourd'hui (monter vaut des etages, et c'est invisible).",
    "en": "Take the stairs instead of the lift at least once today "
          "(it counts as floors, and nobody notices).",
}
WALK_NOTE = {
    "fr": "Une marche de 5 minutes toutes les heures -- un tour de "
          "plateau, un verre d'eau -- vaut mieux qu'un seul grand effort.",
    "en": "A 5-minute walk every hour -- a lap of the floor, a glass "
          "of water -- beats one big effort.",
}

# When in the day each kind of break does the most good, in plain words.
WHEN = {
    "breath": {
        "fr": "avant ta prochaine reunion, ou quand les epaules montent",
        "en": "before your next meeting, or when your shoulders creep up",
    },
    "stretch": {
        "fr": "en milieu de matinee, quand le dos commence a tirer",
        "en": "mid-morning, when your back starts to pull",
    },
    "circulation": {
        "fr": "toutes les heures, en finissant un mail",
        "en": "every hour, while finishing an email",
    },
    "strength": {
        "fr": "apres le dejeuner, quand l'energie retombe",
        "en": "after lunch, when energy dips",
    },
}

# kind: stretch | breath | circulation | strength. intensity: gentle
# exercises are fine on any day; light and moderate are held back when
# the day asks for rest.
# fmt: off
EXERCISES = [
    {"key": "neck_tilt", "kind": "stretch", "posture": "seated",
     "intensity": "gentle", "seconds": 40, "zone": "neck",
     "name": {"fr": "Etirement du cou", "en": "Neck stretch"},
     "how": {
         "fr": "Assis droit, penche l'oreille vers l'epaule, main "
               "opposee posee sur la cuisse. 20 s de chaque cote, "
               "sans forcer.",
         "en": "Sit tall, tip your ear to your shoulder, opposite hand "
               "resting on your thigh. 20 s each side, no forcing."}},
    {"key": "shoulder_rolls", "kind": "stretch", "posture": "seated",
     "intensity": "gentle", "seconds": 30, "zone": "shoulders",
     "name": {"fr": "Rotations d'epaules", "en": "Shoulder rolls"},
     "how": {
         "fr": "Remonte les epaules vers les oreilles, recule-les, "
               "descends. 10 grands cercles lents, puis 10 dans "
               "l'autre sens.",
         "en": "Lift shoulders to ears, roll them back and down. 10 "
               "slow circles, then 10 the other way."}},
    {"key": "chest_opener", "kind": "stretch", "posture": "seated",
     "intensity": "gentle", "seconds": 30, "zone": "chest",
     "name": {"fr": "Ouverture de poitrine", "en": "Chest opener"},
     "how": {
         "fr": "Assis au bord de la chaise, mains jointes derriere le "
               "dos, ouvre la poitrine et regarde droit devant. "
               "Tiens 20-30 s en respirant.",
         "en": "Sit at the chair's edge, hands clasped behind your "
               "back, open the chest and look straight ahead. Hold "
               "20-30 s, breathing."}},
    {"key": "wrist_stretch", "kind": "stretch", "posture": "seated",
     "intensity": "gentle", "seconds": 40, "zone": "wrists",
     "name": {"fr": "Poignets et avant-bras", "en": "Wrists and forearms"},
     "how": {
         "fr": "Bras tendu, paume vers l'avant, tire doucement les "
               "doigts vers toi 15 s ; puis paume vers le sol, doigts "
               "vers toi 15 s. Change de bras. (Le geste de la souris.)",
         "en": "Arm out, palm forward, gently pull the fingers back "
               "15 s; then palm down, fingers toward you 15 s. Switch "
               "arms. (The mouse-hand fix.)"}},
    {"key": "seated_twist", "kind": "stretch", "posture": "seated",
     "intensity": "gentle", "seconds": 40, "zone": "back",
     "name": {"fr": "Torsion assise", "en": "Seated twist"},
     "how": {
         "fr": "Assis grand, une main sur le dossier, tourne le haut "
               "du corps en regardant derriere toi. 20 s de chaque "
               "cote, bassin face a l'ecran.",
         "en": "Sit tall, one hand on the backrest, rotate your upper "
               "body to look behind you. 20 s each side, hips facing "
               "the screen."}},
    {"key": "figure_four", "kind": "stretch", "posture": "seated",
     "intensity": "gentle", "seconds": 60, "zone": "hips",
     "name": {"fr": "Etirement de la fesse (4)", "en": "Figure-4 stretch"},
     "how": {
         "fr": "Cheville droite posee sur le genou gauche, dos droit, "
               "penche-toi legerement en avant jusqu'a sentir la "
               "fesse. 30 s, puis l'autre jambe.",
         "en": "Right ankle on left knee, back straight, lean slightly "
               "forward until you feel the glute. 30 s, then switch."}},
    {"key": "hip_flexor", "kind": "stretch", "posture": "standing",
     "intensity": "gentle", "seconds": 60, "zone": "hips",
     "name": {"fr": "Flechisseurs de hanche", "en": "Hip flexor stretch"},
     "how": {
         "fr": "Debout, un pas en arriere, genou arriere flechi, "
               "bassin pousse vers l'avant, fesse serree. 30 s par "
               "cote : ce que la position assise raccourcit.",
         "en": "Stand, step one foot back, back knee soft, tuck the "
               "pelvis forward and squeeze the glute. 30 s a side: "
               "what sitting shortens."}},
    {"key": "box_breathing", "kind": "breath", "posture": "seated",
     "intensity": "gentle", "seconds": 90, "zone": "stress",
     "name": {"fr": "Respiration carree", "en": "Box breathing"},
     "how": {
         "fr": "Inspire 4 s, retiens 4 s, expire 4 s, retiens 4 s. "
               "6 cycles, yeux sur l'ecran : personne ne voit rien.",
         "en": "In 4 s, hold 4 s, out 4 s, hold 4 s. 6 cycles, eyes "
               "on the screen: nobody can tell."}},
    {"key": "long_exhale", "kind": "breath", "posture": "seated",
     "intensity": "gentle", "seconds": 90, "zone": "stress",
     "name": {"fr": "Expiration longue", "en": "Long exhale"},
     "how": {
         "fr": "Inspire par le nez 4 s, expire lentement par la "
               "bouche 6-8 s. 8 cycles. L'expiration longue calme le "
               "stress plus vite qu'une pause cafe.",
         "en": "Breathe in through the nose 4 s, out slowly through "
               "the mouth 6-8 s. 8 cycles. A long exhale settles "
               "stress faster than a coffee break."}},
    {"key": "eye_break", "kind": "breath", "posture": "seated",
     "intensity": "gentle", "seconds": 30, "zone": "eyes",
     "name": {"fr": "Pause des yeux 20-20-20", "en": "20-20-20 eye break"},
     "how": {
         "fr": "Regarde un point a ~6 m (fenetre, bout du couloir) "
               "pendant 20 s, cligne des yeux plusieurs fois.",
         "en": "Look at something ~6 m away (window, end of the "
               "corridor) for 20 s, blinking several times."}},
    {"key": "ankle_pumps", "kind": "circulation", "posture": "seated",
     "intensity": "gentle", "seconds": 45, "zone": "legs",
     "name": {"fr": "Pompes de cheville", "en": "Ankle pumps"},
     "how": {
         "fr": "Sous le bureau, pointes de pieds vers toi puis loin "
               "de toi, 20 fois, puis 10 cercles de chaque cheville. "
               "Relance la circulation des jambes.",
         "en": "Under the desk, toes toward you then away, 20 times, "
               "then 10 circles each ankle. Restarts leg circulation."}},
    {"key": "seated_marching", "kind": "circulation", "posture": "seated",
     "intensity": "gentle", "seconds": 60, "zone": "legs",
     "name": {"fr": "Marche assise", "en": "Seated marching"},
     "how": {
         "fr": "Assis, souleve un genou puis l'autre, lentement, 30 "
               "fois en tout. Invisible sous un bureau.",
         "en": "Seated, lift one knee then the other, slowly, 30 "
               "times in all. Invisible under a desk."}},
    {"key": "calf_raises", "kind": "strength", "posture": "standing",
     "intensity": "light", "seconds": 60, "zone": "legs",
     "name": {"fr": "Montees sur pointes", "en": "Calf raises"},
     "how": {
         "fr": "Debout, une main sur le bureau, monte sur les pointes "
               "en 2 s, descends en 2 s. 20 repetitions, sans bruit.",
         "en": "Standing, one hand on the desk, rise onto your toes in "
               "2 s, lower in 2 s. 20 reps, silent."}},
    {"key": "slow_sit_stand", "kind": "strength", "posture": "standing",
     "intensity": "moderate", "seconds": 90, "zone": "legs",
     "name": {"fr": "Assis-debout lent", "en": "Slow sit-to-stand"},
     "how": {
         "fr": "Leve-toi de la chaise sans les mains en 3 s, "
               "redescends en 3 s sans te laisser tomber. 10 a 12 "
               "repetitions : des squats que personne ne remarque.",
         "en": "Stand up from the chair hands-free in 3 s, sit back "
               "down in 3 s without dropping. 10-12 reps: squats "
               "nobody notices."}},
    {"key": "desk_pushup", "kind": "strength", "posture": "standing",
     "intensity": "moderate", "seconds": 60, "zone": "upper",
     "name": {"fr": "Pompes contre le bureau", "en": "Desk push-ups"},
     "how": {
         "fr": "Mains sur le bord d'un bureau stable, corps droit, "
               "descends la poitrine vers le bord en 2 s, remonte. "
               "10 a 15 repetitions. Seulement sur un bureau fixe.",
         "en": "Hands on the edge of a sturdy desk, body straight, "
               "lower your chest to the edge in 2 s, press back. "
               "10-15 reps. Only on a fixed desk."}},
    {"key": "glute_squeeze", "kind": "strength", "posture": "seated",
     "intensity": "light", "seconds": 60, "zone": "glutes",
     "name": {"fr": "Contraction des fessiers", "en": "Glute squeezes"},
     "how": {
         "fr": "Assis, serre les fessiers 5 s, relache. 10 fois. "
               "Totalement invisible, et ca reveille ce que la chaise "
               "endort.",
         "en": "Seated, squeeze the glutes 5 s, release. 10 times. "
               "Completely invisible, and it wakes up what the chair "
               "puts to sleep."}},
    {"key": "belly_brace", "kind": "strength", "posture": "seated",
     "intensity": "light", "seconds": 60, "zone": "core",
     "name": {"fr": "Gainage assis", "en": "Seated core brace"},
     "how": {
         "fr": "Assis grand, rentre le ventre comme pour fermer un "
               "pantalon serre, tiens 10 s en respirant, relache. 6 "
               "fois.",
         "en": "Sit tall, draw the belly in as if zipping tight "
               "trousers, hold 10 s breathing, release. 6 times."}},
    # Second batch, after the exercise library: posture work for the
    # desk hunch (head forward, shoulders rounded, hips flexed all day).
    {"key": "chin_tuck", "kind": "stretch", "posture": "seated",
     "intensity": "gentle", "seconds": 40, "zone": "neck",
     "name": {"fr": "Rentrer le menton", "en": "Chin tucks"},
     "how": {
         "fr": "Regard droit devant, recule la tete comme pour faire un "
               "double menton, tiens 3 s. 10 fois : l'antidote a la tete "
               "en avant devant l'ecran.",
         "en": "Eyes level, slide your head back into a double chin, "
               "hold 3 s. 10 times: the fix for screen head."}},
    {"key": "scapula_squeeze", "kind": "strength", "posture": "seated",
     "intensity": "light", "seconds": 45, "zone": "back",
     "name": {"fr": "Serrer les omoplates", "en": "Shoulder blade squeeze"},
     "how": {
         "fr": "Assis droit, rapproche les omoplates et descends-les "
               "vers les poches arriere, tiens 5 s. 10 fois.",
         "en": "Sit tall, pull your shoulder blades together and down "
               "towards your back pockets, hold 5 s. 10 times."}},
    {"key": "wrist_circles", "kind": "circulation", "posture": "seated",
     "intensity": "gentle", "seconds": 30, "zone": "wrists",
     "name": {"fr": "Cercles de poignets", "en": "Wrist circles"},
     "how": {
         "fr": "Poings fermes, 10 cercles lents dans un sens, 10 dans "
               "l'autre, sous le bureau.",
         "en": "Fists closed, 10 slow circles each way, under the desk."}},
    {"key": "upper_back_hug", "kind": "stretch", "posture": "seated",
     "intensity": "gentle", "seconds": 30, "zone": "back",
     "name": {"fr": "Etirement du haut du dos", "en": "Upper back stretch"},
     "how": {
         "fr": "Croise les bras comme pour te serrer dans tes bras, "
               "arrondis le haut du dos et respire dedans 20-30 s.",
         "en": "Hug yourself, round your upper back and breathe into it "
               "for 20-30 s."}},
    {"key": "overhead_triceps", "kind": "stretch", "posture": "seated",
     "intensity": "gentle", "seconds": 40, "zone": "shoulders",
     "name": {"fr": "Etirement du triceps", "en": "Overhead triceps stretch"},
     "how": {
         "fr": "Une main dans le dos par-dessus l'epaule, l'autre pousse "
               "doucement le coude. 20 s par bras.",
         "en": "One hand down your back over the shoulder, the other "
               "gently pushes the elbow. 20 s per arm."}},
    {"key": "seated_hamstring", "kind": "stretch", "posture": "seated",
     "intensity": "gentle", "seconds": 60, "zone": "hips",
     "name": {"fr": "Ischios assis", "en": "Seated hamstring stretch"},
     "how": {
         "fr": "Au bord de la chaise, une jambe tendue talon au sol, "
               "penche-toi dos droit jusqu'a sentir l'arriere de la "
               "cuisse. 30 s par jambe.",
         "en": "At the chair's edge, one leg straight heel down, lean "
               "forward with a straight back until the back of the thigh "
               "pulls. 30 s per leg."}},
    {"key": "seated_pelvic_tilt", "kind": "circulation",
     "posture": "seated", "intensity": "gentle", "seconds": 45,
     "zone": "back",
     "name": {"fr": "Bascule du bassin assis", "en": "Seated pelvic tilt"},
     "how": {
         "fr": "Assis, arrondis doucement le bas du dos puis creuse-le, "
               "lentement, 10 fois. Les lombaires respirent.",
         "en": "Seated, slowly round then arch your lower back, 10 times. "
               "Your lower back gets moving again."}},
    {"key": "standing_side_bend", "kind": "stretch", "posture": "standing",
     "intensity": "gentle", "seconds": 40, "zone": "back",
     "name": {"fr": "Inclinaison laterale debout",
              "en": "Standing side bend"},
     "how": {
         "fr": "Debout pres du bureau, un bras au-dessus de la tete, "
               "penche-toi doucement de cote. 20 s par cote.",
         "en": "Standing by the desk, one arm overhead, lean gently to "
               "the side. 20 s each side."}},
]
# fmt: on
BY_KEY = {exercise["key"]: exercise for exercise in EXERCISES}

# Zones the typing posture loads, in the order they are worth fixing.
DESK_ZONES = ("neck", "wrists", "chest", "hips", "back", "shoulders")


def _pick(
    pool: list[dict], zones: tuple[str, ...], seed: int, used: set[str],
) -> Optional[dict]:
    """The seed-rotated first exercise of ``pool`` hitting one of ``zones``.

    Rotating by the date's ordinal keeps a week from repeating the same
    three gestures while staying identical for every render of one day.
    """
    candidates = [
        exercise for exercise in pool
        if exercise["zone"] in zones and exercise["key"] not in used
    ]
    if not candidates:
        return None
    return candidates[seed % len(candidates)]


def build_break(
    date: str, flags: list[str], tier: str = "train",
    status: Optional[str] = None, language: str = "fr",
    items: int = BREAK_ITEMS,
) -> dict:
    """The day's desk break, chosen from the day's signals.

    Rules, in order: a rest day or a red status allows only gentle
    exercises; stress or a drained battery put a breathing exercise
    first; sitting all day (``sedentary_high``) adds the hip and chest
    stretch the chair shortens; low steps/floors/intensity add one
    silent strength or circulation item (when the day allows) and a
    stairs or walk note. Whatever room is left is filled with the
    typing-posture stretches, rotated by date.

    Parameters:
        date (str): ISO local date; seeds the rotation.
        flags (list[str]): ``metrics.movement_summary(...)["flags"]``.
        tier (str): ``train`` / ``recovery`` / ``rest``.
        status (str | None): Today's green/yellow/red.
        language (str): ``fr`` or ``en``.
        items (int): How many exercises.

    Returns:
        dict: ``gentle_only`` (bool), ``items`` (each ``name``, ``how``,
        ``seconds``, ``posture``, ``kind``), ``total_min`` and
        ``notes`` (stairs/walk lines, only when the flags call for them
        and the day is not a rest day).
    """
    language = language if language in ("fr", "en") else "fr"
    seed = dt.date.fromisoformat(date).toordinal()
    gentle_only = tier == "rest" or status == "red"
    pool = [
        exercise for exercise in EXERCISES
        if not gentle_only or exercise["intensity"] == "gentle"
    ]
    chosen: list[dict] = []
    used: set[str] = set()

    def add(zones: tuple[str, ...], pick_pool: Optional[list] = None):
        if len(chosen) >= items:
            return
        exercise = _pick(pick_pool or pool, zones, seed + len(chosen), used)
        if exercise:
            chosen.append(exercise)
            used.add(exercise["key"])

    if "stress_high" in flags or "battery_drained" in flags:
        add(("stress",))
    if not gentle_only and (
        "steps_low" in flags or "floors_low" in flags
        or "intensity_low" in flags
    ):
        add(("legs", "glutes", "core", "upper"),
            [e for e in pool if e["kind"] in ("strength", "circulation")])
    elif "sedentary_high" in flags:
        add(("legs",), [e for e in pool if e["kind"] == "circulation"])
    if "sedentary_high" in flags:
        add(("hips", "chest"))
    for zone in DESK_ZONES:
        add((zone,))
    chosen = chosen[:items]

    notes = []
    if tier != "rest":
        if "floors_low" in flags:
            notes.append(STAIRS_NOTE[language])
        if "steps_low" in flags or "sedentary_high" in flags:
            notes.append(WALK_NOTE[language])
    return {
        "gentle_only": gentle_only,
        "items": [
            {
                "name": exercise["name"][language],
                "how": exercise["how"][language],
                "seconds": exercise["seconds"],
                "posture": exercise["posture"], "kind": exercise["kind"],
                "when": WHEN[exercise["kind"]][language],
            }
            for exercise in chosen
        ],
        "total_min": max(1, round(sum(e["seconds"] for e in chosen) / 60)),
        "notes": notes,
    }


def desk_mode(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    """Whether to suggest desk breaks today: the setting is on and it is
    a weekday.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date.

    Returns:
        bool: True for a Monday-to-Friday with ``desk_job`` on.
    """
    if (db.get_setting(conn, user_id, "desk_job") or "1") != "1":
        return False
    return dt.date.fromisoformat(date).weekday() in DESK_WEEKDAYS


def break_for_day(
    conn: sqlite3.Connection, user_id: int, date: str, tier: str,
    status: Optional[str], language: str = "fr",
) -> dict:
    """Today's break from yesterday's movement flags, or ``{}``.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owning user.
        date (str): ISO local date (today).
        tier (str): ``train`` / ``recovery`` / ``rest``.
        status (str | None): Today's green/yellow/red.
        language (str): ``fr`` or ``en``.

    Returns:
        dict: ``build_break`` output, ``{}`` when desk mode is off.
    """
    if not desk_mode(conn, user_id, date):
        return {}
    import metrics  # local: metrics imports db, desk imports db too

    flags = metrics.movement_summary(conn, user_id, date)["flags"]
    return build_break(date, flags, tier, status, language)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    # Every exercise is complete in both languages, with a real
    # duration and a known kind -- a missing translation would only
    # show up as a KeyError in the middle of a morning run.
    kinds = {"stretch", "breath", "circulation", "strength"}
    assert len({e["key"] for e in EXERCISES}) == len(EXERCISES)
    for exercise in EXERCISES:
        assert exercise["kind"] in kinds, exercise["key"]
        assert exercise["posture"] in ("seated", "standing")
        assert exercise["intensity"] in ("gentle", "light", "moderate")
        assert exercise["seconds"] > 0
        assert exercise["kind"] in WHEN
        for lang in ("fr", "en"):
            assert exercise["name"][lang] and exercise["how"][lang]

    monday = "2026-10-05"
    # The reported day: sat 13 h, 4 floors, 2765 steps, stressed,
    # battery drained -> breathing first, the hips the chair shortens,
    # something that moves the legs, and the stairs/walk notes.
    flags = ["steps_low", "floors_low", "sedentary_high", "stress_high",
             "battery_drained"]
    plan = build_break(monday, flags)
    assert len(plan["items"]) == BREAK_ITEMS, plan
    assert plan["items"][0]["kind"] == "breath", plan
    assert any(i["kind"] in ("strength", "circulation")
               for i in plan["items"]), plan
    assert all(i["when"] for i in plan["items"]), plan
    assert len(plan["notes"]) == 2, plan
    assert 1 <= plan["total_min"] <= 8, plan
    assert plan == build_break(monday, flags), "must be deterministic"

    # Same day, different date: the rotation changes the stretches.
    seen = {
        tuple(i["name"] for i in build_break(
            (dt.date(2026, 10, 5) + dt.timedelta(days=n)).isoformat(),
            ["sedentary_high"],
        )["items"])
        for n in range(7)
    }
    assert len(seen) > 1, seen

    # A rest day or a red status: gentle only -- no strength, no stairs.
    for tier, status in (("rest", None), ("train", "red")):
        gentle = build_break(monday, flags, tier, status)
        assert gentle["gentle_only"], gentle
        used_keys = {
            e["key"] for e in EXERCISES if e["name"]["fr"] in
            {i["name"] for i in gentle["items"]}
        }
        assert all(BY_KEY[k]["intensity"] == "gentle" for k in used_keys)
        assert all(
            i["kind"] != "strength" for i in gentle["items"]
        ), gentle
    assert build_break(monday, flags, "rest")["notes"] == []  # no stairs
    assert build_break(monday, flags, "train", "red")["notes"]  # walk ok

    # No signal still gives the typing-posture stretches, no notes.
    quiet = build_break(monday, [])
    assert len(quiet["items"]) == BREAK_ITEMS and quiet["notes"] == []
    # ...mobility and posture work, never a real strength set.
    by_name = {e["name"]["fr"]: e for e in EXERCISES}
    assert all(
        by_name[i["name"]]["intensity"] in ("gentle", "light")
        for i in quiet["items"]
    ), quiet

    # English comes back in English; an unknown language falls back.
    assert build_break(monday, flags, language="en")["items"][0][
        "name"
    ] != plan["items"][0]["name"]
    assert build_break(monday, flags, language="de") == plan

    # Standing exercises keep to the silent ones: nothing here jumps.
    assert all(
        e["intensity"] != "high" and "saut" not in e["how"]["fr"].lower()
        for e in EXERCISES
    )

    # desk_mode: setting and weekday.
    conn = db.connect(Path(tempfile.mkdtemp()) / "desk.db")
    db.init_db(conn)
    uid = db.create_user(conn, "desker", "password1234")
    assert desk_mode(conn, uid, monday)
    assert not desk_mode(conn, uid, "2026-10-04")  # Sunday
    db.set_setting(conn, uid, "desk_job", "0")
    assert not desk_mode(conn, uid, monday)
    assert break_for_day(conn, uid, monday, "train", "green") == {}
    db.set_setting(conn, uid, "desk_job", "1")
    day = break_for_day(conn, uid, monday, "train", "green")
    assert len(day["items"]) == BREAK_ITEMS, day

    print("desk.py: all checks passed")
