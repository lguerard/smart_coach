#!/usr/bin/env python3
"""Build library/body_paths.json from MuscleMap's Swift path data.

MuscleMap by Melih Colpan (MIT; github.com/melihcolpan/MuscleMap) ships
its body outlines as SVG path strings inside Swift source. This extracts
the base muscle groups (not the finer sub-groups, which overlap them)
for the male and female, front and back views, into one JSON file the
dashboard renders as inline SVG.

    git clone --depth 1 https://github.com/melihcolpan/MuscleMap
    python scripts/build_body_map.py MuscleMap
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "library" / "body_paths.json"
KEEP = {
    "abs", "biceps", "calves", "chest", "deltoids", "feet", "forearm",
    "gluteal", "hamstring", "hands", "head", "hair", "knees", "lowerBack",
    "obliques", "quadriceps", "tibialis", "trapezius", "triceps",
    "upperBack", "adductors", "neck", "ankles",
}
FILES = {
    ("male", "front"): "MaleFrontPaths.swift",
    ("male", "back"): "MaleBackPaths.swift",
    ("female", "front"): "FemaleFrontPaths.swift",
    ("female", "back"): "FemaleBackPaths.swift",
}
VIEWBOX = re.compile(
    r"static let (\w+) = BodyViewBox\(\s*origin: CGPoint\(x: ([\d.]+), "
    r"y: ([\d.]+)\),\s*size: CGSize\(width: ([\d.]+), height: ([\d.]+)\)",
)


def parse(swift: str) -> list[dict]:
    """``[{slug, paths}]`` from one *Paths.swift file."""
    parts = []
    for block in swift.split("BodyPartPathData(")[1:]:
        slug = re.search(r"slug:\s*\.(\w+)", block)
        if not slug or slug.group(1) not in KEEP:
            continue
        body = block.split("BodyPartPathData(")[0]
        paths = re.findall(r'"([Mm][^"]+)"', body)
        if paths:
            parts.append({"slug": slug.group(1), "paths": paths})
    return parts


def main(repo: str) -> None:
    data_dir = Path(repo) / "Sources" / "MuscleMap" / "Data"
    boxes = {
        name: [float(x), float(y), float(w), float(h)]
        for name, x, y, w, h in VIEWBOX.findall(
            (data_dir / "BodyPathData.swift").read_text()
        )
    }
    out: dict = {}
    for (gender, side), file_name in FILES.items():
        out.setdefault(gender, {})[side] = {
            "viewBox": boxes[f"{gender}{side.capitalize()}"],
            "parts": parse((data_dir / file_name).read_text()),
        }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    print(f"-> {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
