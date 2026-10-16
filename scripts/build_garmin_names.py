#!/usr/bin/env python3
"""Check every Garmin exercise name the code sends against Garmin's own
catalog, and record the checked ones in library/garmin_exercises_used.json.

A name Garmin does not know shows on the watch as a bare category (or
not at all), so each one is checked once against the official FIT SDK
(scripts/garmin_catalog.mjs) and the self-check in ingest/garmin_api.py
then refuses any name that was never checked.

    node scripts/garmin_catalog.mjs > /tmp/garmin_catalog.json
    python scripts/build_garmin_names.py /tmp/garmin_catalog.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest import garmin_api  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "library" / "garmin_exercises_used.json"


def main(catalog_path: str) -> int:
    catalog = json.loads(Path(catalog_path).read_text())
    used: dict = {}
    unknown = []
    for category, name in garmin_api.exercise_names_used():
        if category not in catalog or (name and name not in catalog[category]):
            unknown.append((category, name))
        used.setdefault(category, set())
        if name:
            used[category].add(name)
    if unknown:
        for category, name in unknown:
            print(f"unknown to Garmin: {category} / {name}")
        return 1
    OUT.write_text(json.dumps(
        {c: sorted(n) for c, n in sorted(used.items())}, indent=1,
    ) + "\n")
    print(f"{sum(len(n) for n in used.values())} names checked -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
