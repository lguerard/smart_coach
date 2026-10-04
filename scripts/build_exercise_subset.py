#!/usr/bin/env python3
"""Build library/exercises_subset.json from the exercises dataset.

Keeps only the records the ladders in exercise_library.py point at, and
only their text metadata (name, muscles, French instructions) -- never
the images or GIFs, which belong to Gym visual and are not covered by
the dataset's MIT licence.

    git clone --depth 1 https://github.com/hasaneyldrm/exercises-dataset
    python scripts/build_exercise_subset.py exercises-dataset
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import exercise_library  # noqa: E402


def wanted_ids() -> set[str]:
    """Every dataset id a ladder variant references."""
    return {
        variant["dataset"]
        for ladder in exercise_library.LADDERS.values()
        for rung in ladder["rungs"] for variant in rung
        if variant["dataset"]
    }


def main(dataset_dir: str) -> None:
    records = json.loads(
        (Path(dataset_dir) / "data" / "exercises.json").read_text()
    )
    ids = wanted_ids()
    subset = {
        record["id"]: {
            "name_en": record["name"],
            "target": record.get("target"),
            "secondary_muscles": record.get("secondary_muscles", []),
            "instructions_fr": (record.get("instructions") or {}).get("fr"),
        }
        for record in records if record["id"] in ids
    }
    missing = ids - set(subset)
    if missing:
        sys.exit(f"ids not found in the dataset: {sorted(missing)}")
    out = exercise_library.SUBSET_FILE
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(subset, ensure_ascii=False, indent=1,
                              sort_keys=True) + "\n")
    print(f"{len(subset)} exercises -> {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
