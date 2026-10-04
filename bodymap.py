#!/usr/bin/env python3
"""Front/back body figure, coloured per muscle, for the dashboard.

Outlines are MuscleMap's (Melih Colpan, MIT; see library/NOTICE.md),
extracted into ``library/body_paths.json`` by scripts/build_body_map.py.
This module only decides which CSS class each outline gets: the
template draws the paths and the stylesheet owns the colours, so the
figure follows the light/dark theme like everything else.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

import muscles

PATHS_FILE = Path(__file__).parent / "library" / "body_paths.json"

# MuscleMap outline slug -> this project's muscle key (None: drawn as
# body, never coloured -- head, hands, knees...).
SLUG_TO_MUSCLE = {
    "chest": "chest", "deltoids": "shoulders", "triceps": "triceps",
    "biceps": "biceps", "trapezius": "back", "upperBack": "back",
    "lowerBack": "lower_back", "abs": "abs", "obliques": "abs",
    "gluteal": "glutes", "quadriceps": "quads", "adductors": "quads",
    "hamstring": "hamstrings", "calves": "calves", "tibialis": "calves",
}

# Recovery view: fatigue state -> class; a muscle not used in 7 days is
# "idle", so the figure also shows what has gone untrained.
STATE_CLASS = {"fatigued": "bm-hot", "recovering": "bm-warm",
               "ready": "bm-ok"}
STATE_LABEL_FR = {"fatigued": "fatigue", "recovering": "recupere",
                  "ready": "pret"}


@lru_cache(maxsize=1)
def _paths() -> dict:
    try:
        return json.loads(PATHS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def gender_for(sex: Optional[str]) -> str:
    """``female`` for an F/female setting, ``male`` otherwise."""
    return "female" if (sex or "").strip().lower()[:1] == "f" else "male"


def figure(gender: str, classes: dict, titles: dict) -> dict:
    """Both views, each outline with its class and tooltip.

    Parameters:
        gender (str): ``male`` / ``female``.
        classes (dict): Muscle key -> CSS class.
        titles (dict): Muscle key -> tooltip text.

    Returns:
        dict: ``{"front"|"back": {"viewBox": str, "parts": [{d, cls,
        title}]}}``, or ``{}`` when the path file is missing (the
        template then shows nothing).
    """
    views = _paths().get(gender) or {}
    out = {}
    for side, view in views.items():
        parts = []
        for part in view["parts"]:
            muscle = SLUG_TO_MUSCLE.get(part["slug"])
            cls = classes.get(muscle, "bm-base") if muscle else "bm-body"
            title = titles.get(muscle, "") if muscle else ""
            for d in part["paths"]:
                parts.append({"d": d, "cls": cls, "title": title})
        out[side] = {"viewBox": " ".join(f"{v:g}" for v in view["viewBox"]),
                     "parts": parts}
    return out


def recovery_figure(fatigue: dict, gender: str) -> dict:
    """The figure for ``muscles.muscle_fatigue`` output."""
    classes, titles = {}, {}
    for muscle, info in fatigue["muscles"].items():
        idle = info["week_volume"] == 0 and info["state"] == "ready"
        classes[muscle] = "bm-idle" if idle else STATE_CLASS[info["state"]]
        titles[muscle] = (
            f"{muscles.MUSCLES[muscle]} : "
            + ("pas travaille cette semaine" if idle
               else STATE_LABEL_FR[info["state"]])
        )
    return figure(gender, classes, titles)


def focus_figure(focus: list[str], gender: str) -> dict:
    """The figure highlighting muscles to develop (Corps tab)."""
    return figure(
        gender, {m: "bm-focus" for m in focus},
        {m: f"{muscles.MUSCLES[m]} : a developper" for m in focus},
    )


if __name__ == "__main__":
    assert gender_for("F") == "female" and gender_for("femme") == "female"
    assert gender_for("M") == "male" and gender_for("") == "male"
    data = _paths()
    assert set(data) == {"male", "female"}, list(data)
    for gender in data:
        assert set(data[gender]) == {"front", "back"}
        for view in data[gender].values():
            assert len(view["viewBox"]) == 4 and view["parts"]
    # Every coloured slug maps to a real muscle key.
    assert set(SLUG_TO_MUSCLE.values()) <= set(muscles.MUSCLES)
    # Each muscle appears on at least one view of each body.
    for gender in data:
        drawn = {SLUG_TO_MUSCLE.get(p["slug"])
                 for view in data[gender].values() for p in view["parts"]}
        assert set(muscles.MUSCLES) <= drawn, (gender,
                                               set(muscles.MUSCLES) - drawn)
    fatigue = {"muscles": {m: {"fatigue": 0.0, "week_volume": 0.0,
                               "state": "ready"} for m in muscles.MUSCLES}}
    fatigue["muscles"]["quads"] = {"fatigue": 1.2, "week_volume": 2.0,
                                   "state": "fatigued", "cause": "hiking"}
    fig = recovery_figure(fatigue, "male")
    front = fig["front"]["parts"]
    quads = [p for p in front if p["cls"] == "bm-hot"]
    assert quads and "quadriceps" in quads[0]["title"], quads[:1]
    assert any(p["cls"] == "bm-idle" for p in front)  # untrained shown
    assert any(p["cls"] == "bm-body" for p in front)  # head, hands...
    focus = focus_figure(["shoulders"], "female")
    assert any(p["cls"] == "bm-focus" for v in focus.values()
               for p in v["parts"])
    print("bodymap.py: all checks passed")
