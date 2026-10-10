#!/usr/bin/env python3
"""Bodyweight exercise ladders: variety, and a progression you can read.

Every circuit slot (``squats``, ``pushups``...) used to be one fixed
movement whose rep count grew by one per level, forever: 22 squats at
level 10 is just a longer set of the same thing, and the session looked
identical week after week. Here each slot is a **ladder** of variants
from easier to harder, with alternatives of equal difficulty:

- **Double progression** (rep slots): inside a variant, reps climb from
  the bottom to the top of a range as the level rises; at the top, the
  next level moves to the harder variant and reps go back to the bottom
  -- the bodyweight equivalent of adding load once every rep is earned.
  Time-based slots (planks, wall sit) keep adding seconds instead.
- **Variety without noise in the data**: within a rung, the alternative
  used rotates with the ISO week, so the movement changes from one week
  to the next while every session of the same week stays comparable.
- **Muscles**: every variant names the muscles it works, which feeds the
  per-muscle fatigue model (muscles.py) and the body map.
- **Garmin**: every variant carries its Garmin exercise category/name,
  so the watch shows the real movement and its muscle diagram.

French instructions for the variants that exist in the exercises
dataset by Hasan Emir Yildirim (MIT; github.com/hasaneyldrm/
exercises-dataset) are read from ``library/exercises_subset.json``,
built by ``scripts/build_exercise_subset.py``; only text metadata is
used, never the dataset's images or GIFs (those belong to Gym visual).
Names, cues and ladders here are this project's own.
"""

import datetime as dt
import json
from pathlib import Path
from typing import Optional

LEVEL_MIN, LEVEL_MAX = 0, 10
SUBSET_FILE = Path(__file__).parent / "library" / "exercises_subset.json"

MUSCLE_LABEL_FR = {
    "chest": "pectoraux", "shoulders": "epaules", "triceps": "triceps",
    "biceps": "biceps", "back": "dos", "lower_back": "lombaires",
    "abs": "abdos", "glutes": "fessiers", "quads": "quadriceps",
    "hamstrings": "ischios", "calves": "mollets",
}


def _v(key, name, cue, garmin, muscles=None, dataset=None, noisy=False):
    """One variant. ``muscles`` overrides the slot's when given."""
    return {"key": key, "name": name, "cue": cue, "garmin": garmin,
            "muscles": muscles, "dataset": dataset, "noisy": noisy}


# slot -> unit, rep range (reps slots only), default muscles, and rungs
# from easiest to hardest; each rung is a list of equal alternatives.
# fmt: off
LADDERS = {
    "squats": {
        "unit": "reps", "range": (10, 20),
        "muscles": {"quads": 1.0, "glutes": 0.8, "hamstrings": 0.3},
        "rungs": [
            [_v("chair_squat", "squat sur chaise",
                "Effleure la chaise avec les fesses et releve-toi sans les "
                "mains.", ("SQUAT", "SQUAT"))],
            [_v("air_squat", "squat",
                "Pieds largeur d'epaules, descends cuisses paralleles, "
                "talons au sol.", ("SQUAT", "AIR_SQUAT")),
             _v("sumo_squat", "squat sumo",
                "Pieds tres ecartes, pointes vers l'exterieur, genoux dans "
                "l'axe des pieds.", ("SQUAT", "PLIE_SQUAT"),
                muscles={"quads": 0.8, "glutes": 1.0, "hamstrings": 0.3}),
             _v("prisoner_squat", "squat prisonnier",
                "Mains derriere la tete, coudes ouverts, buste bien droit.",
                ("SQUAT", "PRISONER_SQUAT"))],
            [_v("pause_squat", "squat pause 2 s",
                "Tiens 2 secondes en bas avant de remonter, sans rebond.",
                ("SQUAT", "SQUAT")),
             _v("jump_squat", "squat saute",
                "Descends, puis saute et reception souple, genoux flechis.",
                ("SQUAT", "JUMP_SQUAT"), dataset="0514", noisy=True)],
        ],
    },
    "lunges_per_leg": {
        "unit": "reps", "range": (8, 14),
        "muscles": {"quads": 1.0, "glutes": 1.0, "hamstrings": 0.4,
                    "calves": 0.2},
        "rungs": [
            [_v("supported_lunge", "fente avant avec appui",
                "Une main sur un mur ou une chaise pour l'equilibre.",
                ("LUNGE", "LUNGE"))],
            [_v("forward_lunge", "fente avant",
                "Grand pas en avant, genou arriere pres du sol, buste "
                "droit.", ("LUNGE", "LUNGE"), dataset="3470"),
             _v("side_lunge", "fente laterale",
                "Grand pas sur le cote, fesses en arriere, l'autre jambe "
                "tendue.", ("LUNGE", "SIDE_LUNGE"),
                muscles={"quads": 0.9, "glutes": 1.0, "hamstrings": 0.3})],
            [_v("walking_lunge", "fente marchee",
                "Enchaine les fentes en avancant, sans t'arreter debout.",
                ("LUNGE", "WALKING_LUNGE"), dataset="1460"),
             _v("split_squat", "fente bulgare",
                "Pied arriere pose sur une chaise, descends droit.",
                ("SQUAT", "ELEVATED_SINGLE_LEG_SQUAT"), dataset="2368")],
        ],
    },
    "reverse_lunges_per_leg": {
        "unit": "reps", "range": (8, 14),
        "muscles": {"quads": 0.9, "glutes": 1.0, "hamstrings": 0.5},
        "rungs": [
            [_v("supported_reverse_lunge", "fente arriere avec appui",
                "Recule une jambe, une main sur un appui.", ("LUNGE", ""))],
            [_v("reverse_lunge", "fente arriere",
                "Recule une jambe, genou arriere vers le sol, poids sur le "
                "talon avant.", ("LUNGE", "")),
             _v("curtsy_lunge", "fente croisee",
                "Recule la jambe en diagonale derriere l'autre, comme une "
                "reverence.", ("LUNGE", "CURTSY_LUNGE"), dataset="3769")],
            [_v("reverse_lunge_knee", "fente arriere + montee de genou",
                "En remontant, monte le genou arriere a la hauteur de la "
                "hanche.", ("LUNGE", "")),
             _v("jump_lunge", "fente sautee",
                "Change de jambe en sautant, reception souple.",
                ("LUNGE", "ALTERNATING_JUMP_LUNGE"), dataset="3582",
                noisy=True)],
        ],
    },
    "wall_sit_sec": {
        "unit": "sec", "muscles": {"quads": 1.0, "glutes": 0.4},
        "rungs": [
            [_v("wall_sit", "chaise contre mur",
                "Dos au mur, cuisses paralleles au sol, genoux au-dessus "
                "des chevilles.", ("SQUAT", "BODY_WEIGHT_WALL_SQUAT"))],
        ],
    },
    "calf_raises": {
        "unit": "reps", "range": (15, 25), "muscles": {"calves": 1.0},
        "rungs": [
            [_v("calf_raise", "mollets debout",
                "Monte sur les pointes en 2 s, redescends en 2 s.",
                ("CALF_RAISE", "STANDING_CALF_RAISE"), dataset="1373")],
            [_v("step_calf_raise", "mollets sur une marche",
                "Avant du pied sur une marche, talons qui descendent sous "
                "la marche.", ("CALF_RAISE", "STANDING_CALF_RAISE"),
                dataset="1490")],
            [_v("single_calf_raise", "mollets sur une jambe",
                "Une jambe a la fois, une main sur le mur.",
                ("CALF_RAISE", "SINGLE_LEG_STANDING_CALF_RAISE"),
                dataset="1387")],
        ],
    },
    "glute_bridge": {
        "unit": "reps", "range": (12, 20),
        "muscles": {"glutes": 1.0, "hamstrings": 0.6, "lower_back": 0.3},
        "rungs": [
            [_v("glute_bridge", "pont fessier",
                "Allonge, pieds a plat, monte le bassin en serrant les "
                "fessiers.", ("HIP_RAISE", "HIP_RAISE"), dataset="3013")],
            [_v("marching_bridge", "pont fessier marche",
                "En haut du pont, leve un genou puis l'autre sans que le "
                "bassin tombe.", ("HIP_RAISE", "MARCHING_HIP_RAISE"),
                dataset="3561")],
            [_v("single_leg_bridge", "pont sur une jambe",
                "Une jambe tendue en l'air, monte sur l'autre talon.",
                ("HIP_RAISE", "SINGLE_LEG_HIP_RAISE"), dataset="3645"),
             _v("feet_up_bridge", "pont pieds sur une chaise",
                "Talons sur l'assise d'une chaise, monte le bassin.",
                ("HIP_RAISE", "HIP_RAISE"), dataset="3523")],
        ],
    },
    "pushups": {
        "unit": "reps", "range": (6, 15),
        "muscles": {"chest": 1.0, "triceps": 0.7, "shoulders": 0.6,
                    "abs": 0.3},
        "rungs": [
            [_v("incline_pushup", "pompes inclinees",
                "Mains sur une table ou le bord du canape, corps gaine.",
                ("PUSH_UP", "INCLINE_PUSH_UP"), dataset="0493"),
             _v("knee_pushup", "pompes sur les genoux",
                "Genoux au sol, ligne droite genoux-epaules.",
                ("PUSH_UP", "KNEELING_PUSH_UP"), dataset="3211")],
            [_v("pushup", "pompes",
                "Mains sous les epaules, corps en planche, poitrine au ras "
                "du sol.", ("PUSH_UP", "PUSH_UP"), dataset="0662"),
             _v("wide_pushup", "pompes mains larges",
                "Mains plus larges que les epaules.",
                ("PUSH_UP", "PUSH_UP"), dataset="1311",
                muscles={"chest": 1.0, "shoulders": 0.6, "triceps": 0.5,
                         "abs": 0.3})],
            [_v("decline_pushup", "pompes declinees",
                "Pieds sur une chaise, mains au sol.",
                ("PUSH_UP", "DECLINE_PUSH_UP"), dataset="0279",
                muscles={"chest": 1.0, "shoulders": 0.8, "triceps": 0.7,
                         "abs": 0.3}),
             _v("diamond_pushup", "pompes diamant",
                "Mains rapprochees sous la poitrine, coudes le long du "
                "corps.", ("PUSH_UP", "DIAMOND_PUSH_UP"), dataset="0283",
                muscles={"triceps": 1.0, "chest": 0.8, "shoulders": 0.5,
                         "abs": 0.3})],
        ],
    },
    "dips": {
        "unit": "reps", "range": (8, 15),
        "muscles": {"triceps": 1.0, "shoulders": 0.5, "chest": 0.4},
        "rungs": [
            [_v("bent_knee_dip", "dips sur chaise, genoux plies",
                "Mains au bord d'une chaise stable, pieds proches.",
                ("TRICEPS_EXTENSION", "BENCH_DIP"), dataset="0129")],
            [_v("straight_leg_dip", "dips sur chaise, jambes tendues",
                "Jambes tendues, talons au sol, descends coudes a 90 deg.",
                ("TRICEPS_EXTENSION", "BENCH_DIP"), dataset="0814")],
            [_v("feet_up_dip", "dips pieds sureleves",
                "Pieds sur une deuxieme chaise.",
                ("TRICEPS_EXTENSION", "BENCH_DIP"), dataset="1753")],
        ],
    },
    "superman": {
        "unit": "reps", "range": (10, 20),
        "muscles": {"lower_back": 1.0, "back": 0.5, "glutes": 0.4,
                    "shoulders": 0.2},
        "rungs": [
            [_v("bird_superman", "superman alterne",
                "A plat ventre, leve bras droit et jambe gauche, puis "
                "l'inverse.", ("HYPEREXTENSION", "SUPERMAN_FROM_FLOOR"))],
            [_v("superman", "superman",
                "A plat ventre, leve bras et jambes ensemble, regard au "
                "sol.", ("HYPEREXTENSION", "SUPERMAN_FROM_FLOOR"))],
            [_v("hold_superman", "superman tenu 2 s",
                "Comme le superman, tiens 2 s en haut a chaque repetition.",
                ("HYPEREXTENSION", "SUPERMAN_FROM_FLOOR")),
             _v("swimmer", "nageur",
                "Bras et jambes leves, battements alternes comme en "
                "crawl.", ("HYPEREXTENSION", "SUPERMAN_FROM_FLOOR"),
                dataset="3433")],
        ],
    },
    "plank_sec": {
        "unit": "sec", "muscles": {"abs": 1.0, "shoulders": 0.4,
                                   "lower_back": 0.2},
        "rungs": [
            [_v("knee_plank", "planche sur les genoux",
                "Avant-bras et genoux au sol, ventre rentre.",
                ("PLANK", "KNEELING_PLANK"))],
            [_v("plank", "planche",
                "Avant-bras au sol, corps droit des talons a la tete.",
                ("PLANK", "PLANK"))],
            [_v("shoulder_tap_plank", "planche avec touches d'epaules",
                "En planche bras tendus, touche une epaule puis l'autre "
                "sans tourner le bassin.",
                ("PLANK", "STRAIGHT_ARM_PLANK_WITH_SHOULDER_TOUCH"),
                dataset="3699")],
        ],
    },
    "side_plank_sec": {
        "unit": "sec", "muscles": {"abs": 1.0, "shoulders": 0.3,
                                   "glutes": 0.2},
        "rungs": [
            [_v("knee_side_plank", "gainage lateral sur genoux",
                "Sur l'avant-bras et le genou, bassin haut.",
                ("PLANK", "SIDE_PLANK"))],
            [_v("side_plank", "gainage lateral",
                "Sur l'avant-bras et le bord du pied, corps aligne.",
                ("PLANK", "SIDE_PLANK"))],
        ],
    },
    "mountain_climbers": {
        "unit": "reps", "muscles": {"abs": 0.8, "shoulders": 0.5,
                                    "quads": 0.4},
        "rungs": [
            [_v("slow_climber", "mountain climbers lents",
                "Ramene un genou puis l'autre, sans sauter.",
                ("PLANK", "MOUNTAIN_CLIMBER"))],
            [_v("mountain_climber", "mountain climbers",
                "Genoux vers la poitrine en alternant vite, bassin bas.",
                ("PLANK", "MOUNTAIN_CLIMBER"), dataset="0630")],
        ],
    },
    "jumping_jacks": {
        "unit": "reps", "muscles": {"calves": 0.6, "shoulders": 0.3,
                                    "quads": 0.3},
        "rungs": [
            [_v("step_jack", "jumping jacks sans saut",
                "Ecarte une jambe a la fois en levant les bras.",
                ("CARDIO", "JUMPING_JACKS"))],
            [_v("jumping_jack", "jumping jacks",
                "Saute en ecartant bras et jambes, reception souple.",
                ("CARDIO", "JUMPING_JACKS"), noisy=True)],
        ],
    },
}
# fmt: on

# With one kettlebell, these slots swap to loaded ladders. The hinge
# (swing, deadlift) replaces the glute bridge, and a row and a press
# replace the superman and the chair dips -- the bodyweight circuits had
# no real pulling movement at all, which is what one kettlebell fixes
# best. Same double progression: with a single weight, reps climb a
# range and the next rung is a harder way to move the same bell.
# fmt: off
KETTLEBELL_LADDERS = {
    "squats": {
        "unit": "reps", "range": (8, 15),
        "muscles": {"quads": 1.0, "glutes": 0.9, "abs": 0.3},
        "rungs": [
            [_v("goblet_squat", "goblet squat",
                "Kettlebell tenue contre la poitrine, coudes entre les "
                "genoux en bas.", ("SQUAT", "GOBLET_SQUAT"),
                dataset="0534")],
            [_v("pause_goblet_squat", "goblet squat pause 2 s",
                "Tiens 2 s en bas, buste droit, puis remonte.",
                ("SQUAT", "GOBLET_SQUAT"))],
            [_v("front_squat_kb", "squat kettlebell en rack",
                "Kettlebell posee sur l'avant-bras, contre l'epaule ; "
                "change de cote a chaque serie.",
                ("SQUAT", "KETTLEBELL_SQUAT"), dataset="0533")],
        ],
    },
    "lunges_per_leg": {
        "unit": "reps", "range": (6, 12),
        "muscles": {"quads": 1.0, "glutes": 1.0, "hamstrings": 0.4},
        "rungs": [
            [_v("goblet_reverse_lunge", "fente arriere goblet",
                "Kettlebell contre la poitrine, recule une jambe.",
                ("LUNGE", ""))],
            [_v("goblet_forward_lunge", "fente avant goblet",
                "Kettlebell contre la poitrine, grand pas en avant.",
                ("LUNGE", "LUNGE"))],
            [_v("lunge_pass_through", "fente avec passage sous la jambe",
                "En bas de chaque fente, passe la kettlebell sous la "
                "cuisse avant.", ("LUNGE", ""), dataset="0536")],
        ],
    },
    "glute_bridge": {
        "unit": "reps", "range": (10, 20),
        "muscles": {"glutes": 1.0, "hamstrings": 1.0, "lower_back": 0.7},
        "rungs": [
            [_v("kb_deadlift", "souleve de terre kettlebell",
                "Kettlebell entre les pieds, fesses en arriere, dos plat, "
                "pousse le sol.", ("DEADLIFT", "KETTLEBELL_SUMO_DEADLIFT"))],
            [_v("kb_swing", "swing kettlebell",
                "Le mouvement part des hanches, pas des bras : la cloche "
                "monte a hauteur de poitrine.",
                ("HIP_RAISE", "KETTLEBELL_SWING"), dataset="0549")],
            [_v("single_arm_swing", "swing a une main",
                "Comme le swing, une main a la fois ; change a chaque "
                "serie.", ("HIP_SWING", "SINGLE_ARM_KETTLEBELL_SWING"))],
        ],
    },
    "dips": {
        "unit": "reps", "range": (6, 12), "per": "/bras",
        "muscles": {"shoulders": 1.0, "triceps": 0.7, "chest": 0.4},
        "rungs": [
            [_v("kb_floor_press", "developpe au sol, 1 bras",
                "Allonge, coude au sol, pousse la kettlebell vers le "
                "plafond.", ("BENCH_PRESS", "KETTLEBELL_CHEST_PRESS"),
                dataset="1298",
                muscles={"chest": 1.0, "triceps": 0.7, "shoulders": 0.5})],
            [_v("kneeling_press", "developpe a genou, 1 bras",
                "Un genou au sol, fessiers serres, pousse au-dessus de la "
                "tete.", ("SHOULDER_PRESS", ""))],
            [_v("standing_press", "developpe debout, 1 bras",
                "Debout, gaine, sans cambrer.", ("SHOULDER_PRESS", ""),
                dataset="0539")],
        ],
    },
    "superman": {
        "unit": "reps", "range": (8, 15),
        "muscles": {"back": 1.0, "biceps": 0.6, "shoulders": 0.3,
                    "lower_back": 0.3},
        "rungs": [
            [_v("two_arm_row", "rowing kettlebell a deux mains",
                "Buste penche dos plat, tire la kettlebell vers le "
                "nombril.", ("ROW", "KETTLEBELL_ROW"), dataset="1345")],
            [_v("one_arm_row", "rowing kettlebell, 1 bras",
                "Une main sur une chaise, tire le coude vers la hanche ; "
                "reps par bras.", ("ROW", "KETTLEBELL_ROW"),
                dataset="0541")],
            [_v("renegade_row", "rowing en planche",
                "En planche, une main sur la poignee, tire sans tourner "
                "le bassin ; reps par bras.", ("ROW", "KETTLEBELL_ROW"),
                dataset="0521",
                muscles={"back": 1.0, "abs": 0.8, "biceps": 0.5,
                         "shoulders": 0.4})],
        ],
    },
}
# fmt: on


def equipment_for(conn, user_id: int) -> dict:
    """The person's equipment from Settings (``{}`` = bodyweight only).

    Returns:
        dict: ``{"kettlebell_kg": float}`` when a kettlebell weight is
        set, else ``{}``.
    """
    import db

    raw = (db.get_setting(conn, user_id, "kettlebell_kg") or "").strip()
    try:
        weight = float(raw.replace(",", "."))
    except ValueError:
        return {}
    return {"kettlebell_kg": weight} if weight > 0 else {}


def ladder(slot: str, equipment: Optional[dict] = None) -> dict:
    """The ladder a slot follows with this equipment."""
    if equipment and equipment.get("kettlebell_kg") and (
        slot in KETTLEBELL_LADDERS
    ):
        return KETTLEBELL_LADDERS[slot]
    return LADDERS[slot]


def _subset() -> dict:
    """The MIT dataset excerpt, by dataset id ({} when not built)."""
    try:
        return json.loads(SUBSET_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _bands(rungs: int) -> list[range]:
    """Split levels 0..10 into ``rungs`` contiguous bands."""
    levels = LEVEL_MAX - LEVEL_MIN + 1
    size, extra = divmod(levels, rungs)
    bands, start = [], LEVEL_MIN
    for index in range(rungs):
        width = size + (1 if index < extra else 0)
        bands.append(range(start, start + width))
        start += width
    return bands


def _clamp(level: int) -> int:
    return max(LEVEL_MIN, min(LEVEL_MAX, int(level)))


def rung_for(
    slot: str, level: int, equipment: Optional[dict] = None,
) -> tuple[int, range]:
    """Which rung a level falls on, and that rung's level band."""
    bands = _bands(len(ladder(slot, equipment)["rungs"]))
    level = _clamp(level)
    for index, band in enumerate(bands):
        if level in band:
            return index, band
    return len(bands) - 1, bands[-1]


def reps_for(
    slot: str, level: int, equipment: Optional[dict] = None,
) -> Optional[int]:
    """Double progression: reps climb the range inside a rung.

    Returns:
        int | None: Reps for a rep slot with a range; None for time
        slots and rep slots without a range (their count stays the
        session's own linear formula).
    """
    lad = ladder(slot, equipment)
    if lad["unit"] != "reps" or "range" not in lad:
        return None
    low, high = lad["range"]
    _, band = rung_for(slot, level, equipment)
    if len(band) == 1:
        return high
    position = (_clamp(level) - band.start) / (len(band) - 1)
    return round(low + (high - low) * position)


def variant_for(
    slot: str, level: int, date: Optional[str] = None,
    equipment: Optional[dict] = None,
) -> dict:
    """The variant for this level, rotated by ISO week among equals.

    Parameters:
        slot (str): Circuit slot key.
        level (int): Session level.
        date (str | None): ISO date; picks the weekly alternative.
            None always picks the first, so callers without a date
            (tests, a level edit) stay deterministic.

    Returns:
        dict: The variant, with ``muscles`` resolved and ``rung``,
        ``rungs`` and the French ``instructions`` when known.
    """
    lad = ladder(slot, equipment)
    index, _ = rung_for(slot, level, equipment)
    rung = lad["rungs"][index]
    week = dt.date.fromisoformat(date).isocalendar()[1] if date else 0
    variant = dict(rung[week % len(rung)])
    variant["muscles"] = variant["muscles"] or lad["muscles"]
    variant["rung"], variant["rungs"] = index, len(lad["rungs"])
    variant["per"] = lad.get("per", "")
    if lad is not LADDERS.get(slot) and equipment:
        # A loaded variant names its weight: "goblet squat (12 kg)".
        variant["name"] = (
            f"{variant['name']} ({equipment['kettlebell_kg']:g} kg)"
        )
    if variant["dataset"]:
        record = _subset().get(variant["dataset"])
        if record and record.get("instructions_fr"):
            variant["instructions"] = record["instructions_fr"]
    return variant


def next_step(
    slot: str, level: int, equipment: Optional[dict] = None,
) -> Optional[str]:
    """What the next rung is, for the 'why this number' line."""
    lad = ladder(slot, equipment)
    index, band = rung_for(slot, level, equipment)
    if index + 1 >= len(lad["rungs"]):
        return None
    following = lad["rungs"][index + 1][0]["name"]
    if lad["unit"] == "reps" and "range" in lad:
        return (
            f"a {lad['range'][1]} reps, passage a : {following} "
            f"(niveau {band.stop})"
        )
    return f"des le niveau {band.stop} : {following}"


def describe_slot(
    slot: str, level: int, value, date: Optional[str] = None,
    equipment: Optional[dict] = None,
):
    """``(variant, plain-language line)`` for one slot at one level."""
    variant = variant_for(slot, level, date, equipment)
    lad = ladder(slot, equipment)
    unit = "s" if lad["unit"] == "sec" else ""
    per_leg = "/jambe" if slot.endswith("_per_leg") else ""
    per_side = "/cote" if slot == "side_plank_sec" else ""
    line = (
        f"{variant['name']} {value}{unit}{per_leg}{per_side}"
        f"{variant['per']}"
    )
    return variant, line


def session_variants(
    values: dict, level: int, date: Optional[str] = None,
    equipment: Optional[dict] = None,
) -> dict:
    """Variant summary per slot present in ``values``."""
    out = {}
    for slot in values:
        if slot not in LADDERS:
            continue
        variant = variant_for(slot, level, date, equipment)
        lad = ladder(slot, equipment)
        following = next_step(slot, level, equipment)
        out[slot] = {
            "key": variant["key"], "name": variant["name"],
            "cue": variant["cue"], "garmin": list(variant["garmin"]),
            "muscles": variant["muscles"],
            **({"per": variant["per"]} if variant["per"] else {}),
            **({"next": following} if following else {}),
            **({"range": list(lad["range"])} if "range" in lad else {}),
        }
    return out


def session_muscles(values: dict) -> dict:
    """Muscle weights of a whole circuit, max over its slots (0-1).

    Max, not sum: squats and lunges both hitting the quads make the
    quads the session's main muscle, not 'twice' a main muscle.
    """
    weights: dict = {}
    for slot, info in (values.get("variants") or {}).items():
        for muscle, weight in info["muscles"].items():
            weights[muscle] = max(weights.get(muscle, 0.0), weight)
    return weights


def for_muscles(
    muscles: list[str], levels: dict, minimum: float = 0.8,
    equipment: Optional[dict] = None,
) -> list[dict]:
    """Library moves that work ``muscles``, at the person's level.

    For the Corps tab: "shoulders to develop" becomes the variant of
    the shoulder-heavy slots the person can actually do now.

    Parameters:
        muscles (list[str]): Muscle keys.
        levels (dict): Session type -> level (the slot's level is the
            one of the session type using it).
        minimum (float): Weight a muscle needs in a variant to count.

    Returns:
        list[dict]: ``slot``, ``name``, ``cue``, ``muscles`` matched.
    """
    import training  # local: training imports this module

    slot_level: dict = {}
    for session_type, level in levels.items():
        if session_type == "treadmill":
            continue
        for slot in training.session_values(
            session_type, level, equipment=equipment,
        ):
            if slot in LADDERS:
                slot_level[slot] = max(slot_level.get(slot, 0), level)
    found = []
    for slot, level in sorted(slot_level.items()):
        variant = variant_for(slot, level, equipment=equipment)
        hit = [m for m in muscles if variant["muscles"].get(m, 0) >= minimum]
        if hit:
            found.append({"slot": slot, "name": variant["name"],
                          "cue": variant["cue"], "muscles": hit})
    return found


if __name__ == "__main__":
    # Bands cover every level exactly once, in order.
    for rungs in (1, 2, 3, 4):
        bands = _bands(rungs)
        assert [lvl for band in bands for lvl in band] == list(range(0, 11))
    assert reps_for("pushups", 0) == 6  # bottom of the range, rung 0
    assert reps_for("pushups", 3) == 15  # top of rung 0 (band 0-3)
    assert reps_for("pushups", 4) == 6  # next rung, back to the bottom
    assert variant_for("pushups", 3)["key"] == "incline_pushup"
    assert variant_for("pushups", 4)["key"] == "pushup"
    assert variant_for("pushups", 10)["rung"] == 2
    # Reps never go down inside a rung, and always reset at a new one.
    for slot, lad in LADDERS.items():
        if lad["unit"] == "reps" and "range" in lad:
            for lvl in range(0, 10):
                r0, r1 = rung_for(slot, lvl)[0], rung_for(slot, lvl + 1)[0]
                if r0 == r1:
                    assert reps_for(slot, lvl + 1) >= reps_for(slot, lvl)
                else:
                    assert reps_for(slot, lvl + 1) == lad["range"][0]
        else:
            assert reps_for(slot, 5) is None
    # Every variant is complete: name, cue, a Garmin category, muscles.
    for slot, lad in [*LADDERS.items(), *KETTLEBELL_LADDERS.items()]:
        assert lad["muscles"], slot
        for rung in lad["rungs"]:
            assert rung
            for variant in rung:
                assert variant["name"] and variant["cue"], variant
                assert variant["garmin"][0], variant
                for muscle in (variant["muscles"] or lad["muscles"]):
                    assert muscle in MUSCLE_LABEL_FR, muscle
    assert set(KETTLEBELL_LADDERS) <= set(LADDERS)
    # With a kettlebell: loaded ladders, the weight in the name, a row
    # instead of the superman -- and nothing changes without one.
    kb = {"kettlebell_kg": 12.0}
    assert variant_for("squats", 0, equipment=kb)["name"] == (
        "goblet squat (12 kg)")
    assert reps_for("squats", 0, kb) == 8
    assert variant_for("superman", 5, equipment=kb)["key"] == "one_arm_row"
    assert variant_for("superman", 5)["key"] == "superman"
    assert variant_for("dips", 0, equipment=kb)["per"] == "/bras"
    assert variant_for("pushups", 4, equipment=kb)["key"] == "pushup"
    assert next_step("glute_bridge", 0, kb).endswith("(niveau 4)")
    assert "swing" in next_step("glute_bridge", 0, kb)
    # Weekly rotation: same week -> same variant; another week can differ.
    monday, next_monday = "2026-10-05", "2026-10-12"
    assert variant_for("squats", 5, monday) == variant_for(
        "squats", 5, "2026-10-09")
    names = {variant_for("squats", 5, d)["name"]
             for d in ("2026-10-05", "2026-10-12", "2026-10-19")}
    assert len(names) == 3, names  # three alternatives on rung 1
    assert variant_for("squats", 5)["name"] == "squat"  # no date: first
    # The 'next' line names the harder variant, then nothing at the top.
    assert "pompes" in next_step("pushups", 2)
    assert next_step("pushups", 10) is None
    # Session muscles: max over slots.
    import training
    values = training.session_values("lower_body", 4, date=monday)
    weights = session_muscles(values)
    assert weights["quads"] == 1.0 and weights["glutes"] == 1.0, weights
    assert "chest" not in weights
    upper = session_muscles(training.session_values("upper_body", 4))
    assert upper["chest"] == 1.0 and upper["triceps"] == 1.0, upper
    assert "back" not in upper or upper["back"] < 0.8  # no real pull
    kb_upper = session_muscles(
        training.session_values("upper_body", 4, equipment=kb))
    assert kb_upper["back"] == 1.0, kb_upper  # the row fills the gap
    # Corps tab: shoulders/chest to develop -> concrete moves at level.
    moves = for_muscles(["chest"], {"upper_body": 2, "lower_body": 0})
    assert any(m["name"] == "pompes inclinees" for m in moves), moves
    assert for_muscles(["chest"], {}) == []
    print("exercise_library.py: all checks passed")
