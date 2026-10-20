#!/usr/bin/env python3
"""A small moving figure for each exercise, drawn here (no image rights).

The exercise datasets with pictures either own them (Gym visual) or
took them from elsewhere without a clear licence, so the moves are
drawn: a stick figure going from the start position to the end one and
back, in a loop (SVG with SMIL animation, no JavaScript), in the
page's own colours. The kettlebell follows the hands.

Each drawing is two poses -- joint coordinates in a 100 x 100 box,
ground at y = 93 -- plus the props the move needs (chair, wall, step).
``ART`` holds the base drawings, ``VARIANT_ART`` says which one shows
each variant of the library, and ``svg(key)`` renders one.
"""

from typing import Optional

from markupsafe import Markup

GROUND = 93
HEAD_R = 5.5
BELL_R = 4.2
DURATION_S = 2.6

# Standing, side view, facing right.
_STAND = {
    "H": (50, 13), "S": (50, 24), "P": (50, 50), "K": (51, 71),
    "A": (50, 92), "T": (57, 93), "E": (50, 37), "W": (51, 50),
}
# Standing, front view: both legs and both arms, feet apart.
_FRONT = {
    "H": (50, 13), "S": (50, 24), "P": (50, 50),
    "K": (45, 71), "A": (43, 92), "K2": (55, 71), "A2": (57, 92),
    "E": (42, 37), "W": (40, 50), "E2": (58, 37), "W2": (60, 50),
}
# Lying on the back, head left, knees bent.
_SUPINE = {
    "H": (14, 85), "S": (22, 88), "P": (48, 90), "K": (62, 74),
    "A": (70, 92), "T": (76, 92), "E": (30, 92), "W": (40, 93),
}
# Top of a push-up, head right.
_PUSHUP_UP = {
    "H": (80, 52), "S": (72, 58), "P": (44, 70), "K": (28, 80),
    "A": (12, 88), "T": (9, 92), "E": (72, 75), "W": (72, 92),
}


def _with(base: dict, **changes) -> dict:
    return {**base, **changes}


def _shift(pose: dict, dx: float = 0, dy: float = 0, keep=()) -> dict:
    return {k: (v if k in keep else (v[0] + dx, v[1] + dy)) for k, v in pose.items()}


ART: dict[str, dict] = {
    # --- legs ---------------------------------------------------------
    "squat": {
        "a": _with(_STAND, E=(54, 36), W=(60, 44)),
        "b": {"H": (49, 33), "S": (46, 44), "P": (36, 68), "K": (55, 70),
              "A": (50, 92), "T": (57, 93), "E": (58, 46), "W": (69, 45)},
    },
    "goblet_squat": {
        "a": _with(_STAND, E=(53, 38), W=(56, 30)),
        "b": {"H": (49, 33), "S": (46, 44), "P": (36, 68), "K": (55, 70),
              "A": (50, 92), "T": (57, 93), "E": (52, 56), "W": (55, 48)},
        "bell": "W",
    },
    "chair_squat": {
        "a": _with(_STAND, E=(54, 36), W=(60, 44)),
        "b": {"H": (51, 37), "S": (47, 47), "P": (33, 66), "K": (54, 68),
              "A": (50, 92), "T": (57, 93), "E": (58, 50), "W": (69, 49)},
        "props": ["chair_behind"],
    },
    "prisoner_squat": {
        "a": _with(_STAND, E=(58, 16), W=(52, 11)),
        "b": {"H": (49, 33), "S": (46, 44), "P": (36, 68), "K": (55, 70),
              "A": (50, 92), "T": (57, 93), "E": (55, 35), "W": (50, 30)},
    },
    "jump_squat": {
        "a": _shift(_with(_STAND, E=(46, 36), W=(42, 46)), dy=-7),
        "b": {"H": (49, 33), "S": (46, 44), "P": (36, 68), "K": (55, 70),
              "A": (50, 92), "T": (57, 93), "E": (58, 46), "W": (69, 45)},
    },
    "lunge": {
        "a": _with(_STAND, K2=(49, 71), A2=(48, 92), T2=(55, 93)),
        "b": {"H": (48, 27), "S": (48, 38), "P": (48, 64), "K": (64, 66),
              "A": (64, 92), "T": (71, 93), "K2": (40, 86), "A2": (28, 88),
              "T2": (25, 93), "E": (48, 51), "W": (49, 63)},
    },
    "goblet_lunge": {
        "a": _with(_STAND, K2=(49, 71), A2=(48, 92), T2=(55, 93),
                   E=(53, 38), W=(56, 30)),
        "b": {"H": (48, 27), "S": (48, 38), "P": (48, 64), "K": (64, 66),
              "A": (64, 92), "T": (71, 93), "K2": (40, 86), "A2": (28, 88),
              "T2": (25, 93), "E": (51, 50), "W": (54, 42)},
        "bell": "W",
    },
    "supported_lunge": {
        "a": _with(_STAND, K2=(49, 71), A2=(48, 92), T2=(55, 93),
                   E=(62, 36), W=(74, 44)),
        "b": {"H": (48, 27), "S": (48, 38), "P": (48, 64), "K": (64, 66),
              "A": (64, 92), "T": (71, 93), "K2": (40, 86), "A2": (28, 88),
              "T2": (25, 93), "E": (62, 46), "W": (74, 52)},
        "props": ["chair_front"],
    },
    "wall_sit": {
        "a": {"H": (35, 29), "S": (35, 40), "P": (35, 66), "K": (56, 66),
              "A": (56, 92), "T": (63, 93), "E": (42, 52), "W": (50, 62)},
        "b": {"H": (35, 28.5), "S": (35, 40), "P": (35, 66), "K": (56, 66),
              "A": (56, 92), "T": (63, 93), "E": (42, 52), "W": (50, 62)},
        "props": ["wall"],
    },
    "calf_raise": {
        "a": _STAND,
        "b": _shift(_STAND, dy=-6, keep=("T",)),
    },
    "step_calf_raise": {
        "a": _shift(_with(_STAND, T=(57, 87)), dy=-6, keep=("T",)),
        "b": _shift(_with(_STAND, A=(50, 86), T=(57, 87)), dy=-12, keep=("T",)),
        "props": ["step"],
    },
    "glute_bridge": {
        "a": _SUPINE,
        "b": _with(_SUPINE, P=(48, 75), K=(64, 72)),
    },
    "swing": {
        "a": {"H": (68, 35), "S": (61, 41), "P": (38, 54), "K": (46, 72),
              "A": (46, 92), "T": (53, 93), "E": (54, 56), "W": (45, 66)},
        "b": _with(_STAND, E=(62, 30), W=(76, 30)),
        "bell": "W",
    },
    # --- push ---------------------------------------------------------
    "pushup": {
        "a": _PUSHUP_UP,
        "b": {"H": (80, 72), "S": (72, 77), "P": (44, 83), "K": (28, 87),
              "A": (12, 90), "T": (9, 92), "E": (62, 80), "W": (72, 92)},
    },
    "knee_pushup": {
        "a": {"H": (80, 56), "S": (72, 62), "P": (46, 76), "K": (32, 92),
              "A": (20, 86), "T": (16, 84), "E": (72, 77), "W": (72, 92)},
        "b": {"H": (80, 74), "S": (72, 79), "P": (46, 86), "K": (32, 92),
              "A": (20, 86), "T": (16, 84), "E": (62, 82), "W": (72, 92)},
    },
    "incline_pushup": {
        "a": {"H": (80, 40), "S": (72, 46), "P": (44, 64), "K": (28, 78),
              "A": (14, 90), "T": (10, 93), "E": (73, 60), "W": (74, 72)},
        "b": {"H": (82, 56), "S": (74, 60), "P": (46, 72), "K": (29, 82),
              "A": (14, 90), "T": (10, 93), "E": (64, 64), "W": (74, 72)},
        "props": ["box_hands"],
    },
    "plank": {
        "a": {"H": (79, 70), "S": (70, 75), "P": (44, 76), "K": (28, 82),
              "A": (12, 88), "T": (9, 92), "E": (70, 92), "W": (82, 92)},
        "b": {"H": (79, 69), "S": (70, 74.5), "P": (44, 75.5), "K": (28, 82),
              "A": (12, 88), "T": (9, 92), "E": (70, 92), "W": (82, 92)},
    },
    "dips": {
        "a": {"H": (46, 28), "S": (44, 40), "P": (46, 64), "K": (66, 70),
              "A": (82, 90), "T": (86, 86), "E": (38, 52), "W": (36, 64)},
        "b": {"H": (46, 41), "S": (44, 53), "P": (46, 78), "K": (66, 78),
              "A": (82, 90), "T": (86, 86), "E": (30, 54), "W": (36, 64)},
        "props": ["chair_behind_low"],
    },
    "floor_press": {
        "a": _with(_SUPINE, E=(30, 92), W=(30, 80)),
        "b": _with(_SUPINE, E=(25, 74), W=(25, 62)),
        "bell": "W",
    },
    "overhead_press": {
        "a": _with(_STAND, E=(58, 34), W=(56, 25)),
        "b": _with(_STAND, E=(53, 10), W=(52, -4)),
        "bell": "W",
    },
    "pullover": {
        "a": _with(_SUPINE, E=(8, 82), W=(2, 88)),
        "b": _with(_SUPINE, E=(22, 72), W=(25, 62)),
        "bell": "W",
    },
    "triceps_extension": {
        "a": _with(_STAND, E=(53, 7), W=(42, 15)),
        "b": _with(_STAND, E=(53, 7), W=(54, -7)),
        "bell": "W",
    },
    # --- pull and arms ------------------------------------------------
    "row": {
        "a": {"H": (72, 41), "S": (64, 46), "P": (40, 54), "K": (42, 73),
              "A": (40, 92), "T": (47, 93), "E": (64, 60), "W": (64, 74)},
        "b": {"H": (72, 41), "S": (64, 46), "P": (40, 54), "K": (42, 73),
              "A": (40, 92), "T": (47, 93), "E": (52, 44), "W": (56, 57)},
        "bell": "W",
    },
    "deadlift": {
        "a": {"H": (70, 37), "S": (62, 43), "P": (36, 54), "K": (48, 72),
              "A": (48, 92), "T": (55, 93), "E": (60, 59), "W": (58, 75)},
        "b": _with(_STAND, E=(51, 37), W=(52, 52)),
        "bell": "W",
    },
    "curl": {
        "a": _with(_STAND, E=(51, 38), W=(53, 52)),
        "b": _with(_STAND, E=(51, 38), W=(59, 28)),
        "bell": "W",
    },
    "upright_row": {
        "a": _with(_FRONT, E=(45, 40), W=(50, 55), E2=(55, 40), W2=(50, 55)),
        "b": _with(_FRONT, E=(33, 25), W=(48, 31), E2=(67, 25), W2=(52, 31)),
        "bell": "W",
    },
    "woodchop": {
        "a": _with(_FRONT, H=(48, 18), S=(48, 29), P=(50, 54), K=(42, 74),
                   K2=(58, 74), E=(40, 46), W=(33, 62), E2=(46, 48), W2=(34, 62)),
        "b": _with(_FRONT, E=(60, 16), W=(70, 6), E2=(64, 20), W2=(70, 7)),
        "bell": "W",
    },
    "around_world": {
        "a": _with(_FRONT, E=(40, 40), W=(44, 52), E2=(60, 40), W2=(56, 52)),
        "b": _with(_FRONT, E=(40, 40), W=(44, 52), E2=(60, 40), W2=(56, 52)),
        "orbit": 1,
    },
    "around_world_rev": {
        "a": _with(_FRONT, E=(40, 40), W=(44, 52), E2=(60, 40), W2=(56, 52)),
        "b": _with(_FRONT, E=(40, 40), W=(44, 52), E2=(60, 40), W2=(56, 52)),
        "orbit": -1,
    },
    # --- core and floor -----------------------------------------------
    "superman": {
        "a": {"H": (82, 87), "S": (74, 90), "P": (46, 90), "K": (30, 90),
              "A": (14, 90), "T": (10, 92), "E": (86, 90), "W": (96, 90)},
        "b": {"H": (82, 81), "S": (74, 86), "P": (46, 90), "K": (30, 88),
              "A": (14, 83), "T": (10, 83), "E": (86, 81), "W": (96, 77)},
    },
    "situp_press": {
        "a": _with(_SUPINE, E=(22, 76), W=(23, 66)),
        "b": {"H": (44, 52), "S": (42, 62), "P": (48, 90), "K": (62, 74),
              "A": (70, 92), "T": (76, 92), "E": (45, 50), "W": (47, 38)},
        "bell": "W",
    },
    "crunch": {
        "a": _with(_SUPINE, E=(26, 80), W=(30, 86)),
        "b": {"H": (22, 72), "S": (28, 80), "P": (48, 90), "K": (62, 74),
              "A": (70, 92), "T": (76, 92), "E": (32, 72), "W": (36, 78)},
    },
    "russian_twist": {
        "a": {"H": (30, 52), "S": (32, 62), "P": (42, 86), "K": (60, 72),
              "A": (72, 84), "T": (77, 82), "E": (44, 72), "W": (52, 66)},
        "b": {"H": (30, 52), "S": (32, 62), "P": (42, 86), "K": (60, 72),
              "A": (72, 84), "T": (77, 82), "E": (36, 76), "W": (40, 82)},
        "bell": "W",
    },
    "mountain_climber": {
        "a": _with(_PUSHUP_UP, K=(54, 80), A=(44, 90), T=(41, 92),
                   K2=(28, 80), A2=(12, 88), T2=(9, 92)),
        "b": _with(_PUSHUP_UP, K=(28, 80), A=(12, 88), T=(9, 92),
                   K2=(54, 80), A2=(44, 90), T2=(41, 92)),
    },
    "jumping_jack": {
        "a": _with(_FRONT, K=(48, 71), A=(47, 92), K2=(52, 71), A2=(53, 92),
                   E=(46, 37), W=(45, 50), E2=(54, 37), W2=(55, 50)),
        "b": _with(_FRONT, K=(42, 71), A=(35, 92), K2=(58, 71), A2=(65, 92),
                   E=(38, 16), W=(32, 4), E2=(62, 16), W2=(68, 4)),
    },
}

# Each library variant (exercise_library keys) -> the drawing showing it.
VARIANT_ART = {
    "air_squat": "squat", "pause_squat": "squat", "sumo_squat": "squat",
    "chair_squat": "chair_squat", "prisoner_squat": "prisoner_squat",
    "jump_squat": "jump_squat", "goblet_squat": "goblet_squat",
    "pause_goblet_squat": "goblet_squat", "front_squat_kb": "goblet_squat",
    "kb_goblet_squat_int": "goblet_squat",
    "forward_lunge": "lunge", "walking_lunge": "lunge", "split_squat": "lunge",
    "side_lunge": "lunge", "reverse_lunge": "lunge", "curtsy_lunge": "lunge",
    "reverse_lunge_knee": "lunge", "jump_lunge": "lunge",
    "supported_lunge": "supported_lunge",
    "supported_reverse_lunge": "supported_lunge",
    "goblet_forward_lunge": "goblet_lunge", "goblet_reverse_lunge": "goblet_lunge",
    "lunge_pass_through": "goblet_lunge",
    "wall_sit": "wall_sit",
    "calf_raise": "calf_raise", "single_calf_raise": "calf_raise",
    "step_calf_raise": "step_calf_raise",
    "glute_bridge": "glute_bridge", "marching_bridge": "glute_bridge",
    "single_leg_bridge": "glute_bridge", "feet_up_bridge": "glute_bridge",
    "kb_swing": "swing", "single_arm_swing": "swing",
    "pushup": "pushup", "wide_pushup": "pushup", "diamond_pushup": "pushup",
    "decline_pushup": "pushup", "knee_pushup": "knee_pushup",
    "incline_pushup": "incline_pushup",
    "plank": "plank", "knee_plank": "plank", "shoulder_tap_plank": "plank",
    "side_plank": "plank", "knee_side_plank": "plank",
    "bent_knee_dip": "dips", "straight_leg_dip": "dips", "feet_up_dip": "dips",
    "kb_floor_press": "floor_press", "kb_floor_press_2h": "floor_press",
    "kb_squeeze_press": "floor_press",
    "kneeling_press": "overhead_press", "standing_press": "overhead_press",
    "kb_pullover": "pullover", "kb_overhead_ext": "triceps_extension",
    "two_arm_row": "row", "one_arm_row": "row", "kb_one_arm_row": "row",
    "renegade_row": "row",
    "kb_deadlift": "deadlift", "kb_curl": "curl", "kb_hammer_curl": "curl",
    "kb_upright_row": "upright_row", "kb_woodchop": "woodchop",
    "kb_around_world": "around_world", "kb_around_world_rev": "around_world_rev",
    "superman": "superman", "bird_superman": "superman",
    "hold_superman": "superman", "swimmer": "superman",
    "crunch": "crunch", "kb_situp_press": "situp_press", "kb_russian_twist": "russian_twist",
    "mountain_climber": "mountain_climber", "slow_climber": "mountain_climber",
    "jumping_jack": "jumping_jack", "step_jack": "jumping_jack",
}

_BONES = (("S", "P"), ("P", "K"), ("K", "A"), ("A", "T"), ("S", "E"),
          ("E", "W"))
_FAR_BONES = (("P", "K2"), ("K2", "A2"), ("A2", "T2"), ("S", "E2"),
              ("E2", "W2"))

_PROPS = {
    "chair_behind": '<path d="M18 66 H34 M20 66 V93 M32 66 V93 M18 66 V40" />',
    "chair_behind_low": '<path d="M22 64 H40 M24 64 V93 M38 64 V93 M22 64 V40" />',
    "chair_front": '<path d="M70 52 H86 M72 52 V93 M84 52 V93 M86 52 V26" />',
    "wall": '<path d="M29 4 V93" />',
    "step": '<path d="M40 87 H66 V93" />',
    "box_hands": '<path d="M64 72 H88 V93 M64 72 V93" />',
}


def _anim(attr: str, a: float, b: float) -> str:
    if a == b:
        return ""
    return (f'<animate attributeName="{attr}" values="{a};{b};{a}" '
            f'dur="{DURATION_S}s" repeatCount="indefinite" calcMode="spline" '
            'keyTimes="0;0.5;1" keySplines=".45 0 .55 1;.45 0 .55 1"/>')


def _line(a: dict, b: dict, p: str, q: str, cls: str) -> str:
    (x1, y1), (x2, y2) = a[p], a[q]
    (u1, v1), (u2, v2) = b[p], b[q]
    return (f'<line class="{cls}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">'
            + _anim("x1", x1, u1) + _anim("y1", y1, v1)
            + _anim("x2", x2, u2) + _anim("y2", y2, v2) + "</line>")


def _circle(cx: float, cy: float, r: float, cls: str, to=None) -> str:
    to = to or (cx, cy)
    return (f'<circle class="{cls}" cx="{cx}" cy="{cy}" r="{r}">'
            + _anim("cx", cx, to[0]) + _anim("cy", cy, to[1]) + "</circle>")


def svg(art_key: Optional[str], size: int = 72, label: str = "") -> Markup:
    """One drawing, or an empty string when there is none."""
    art = ART.get(art_key or "")
    if art is None:
        return Markup("")
    a, b = art["a"], art["b"]
    parts = [f'<line class="ground" x1="0" y1="{GROUND}" x2="100" y2="{GROUND}"/>']
    parts += [f'<g class="prop">{_PROPS[p]}</g>' for p in art.get("props", [])]
    parts += [_line(a, b, p, q, "far") for p, q in _FAR_BONES if p in a and q in a]
    parts += [_line(a, b, p, q, "bone") for p, q in _BONES if p in a and q in a]
    parts.append(_circle(*a["H"], HEAD_R, "head", b["H"]))
    if art.get("bell"):
        (x, y), (u, v) = a[art["bell"]], b[art["bell"]]
        parts.append(_circle(x, y + BELL_R - 1, BELL_R, "bell", (u, v + BELL_R - 1)))
    if art.get("orbit"):
        # Around the waist: the bell circles the hips, smaller behind.
        sweep = 1 if art["orbit"] > 0 else 0
        parts.append(
            '<circle class="bell" r="4.2"><animateMotion dur="2.2s" '
            'repeatCount="indefinite" path="M50 58 a15 5 0 1 '
            f'{sweep} 0.01 0"/></circle>')
    title = f"<title>{label}</title>" if label else ""
    return Markup(
        f'<svg class="move-art" viewBox="0 -12 100 108" width="{size}" '
        f'height="{size}" role="img" aria-label="{label}">{title}'
        + "".join(parts) + "</svg>")


def for_variant(key: Optional[str], size: int = 72, label: str = "") -> Markup:
    """The drawing for one exercise_library variant key."""
    return svg(VARIANT_ART.get(key or ""), size, label)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))
    import exercise_library

    keys = {v["key"] for src in (exercise_library.LADDERS,
                                 exercise_library.KETTLEBELL_LADDERS)
            for lad in src.values() for rung in lad["rungs"] for v in rung}
    missing = sorted(keys - set(VARIANT_ART))
    assert not missing, f"no drawing for: {missing}"
    assert set(VARIANT_ART.values()) <= set(ART), set(VARIANT_ART.values()) - set(ART)
    for name, art in ART.items():
        assert set(art["a"]) == set(art["b"]), name  # same joints both ends
    drawn = str(for_variant("kb_goblet_squat_int", label="goblet squat"))
    assert drawn.startswith('<svg class="move-art"') and "<animate" in drawn
    assert 'class="bell"' in drawn and "goblet squat" in drawn
    assert for_variant("unknown") == ""
    assert "animateMotion" in str(for_variant("kb_around_world"))
    print("move_art.py: all checks passed")
