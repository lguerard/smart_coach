#!/usr/bin/env python3
"""Run every module's self-check. No framework, matching garmin-coach's
test_coach.py posture -- this is the one-command "run the tests" entry.
"""

from _helpers import run_module_selfcheck

MODULES = [
    "db.py", "training.py", "training_load.py", "metrics.py", "progress.py",
    "achievements.py", "llm.py", "notify.py", "gcal.py", "weather.py",
    "run_checkin.py",
    "ingest/sync_drive.py", "ingest/parse_health_connect.py",
    "ingest/garmin_api.py",
]

if __name__ == "__main__":
    for module in MODULES:
        print(run_module_selfcheck(module))
    print("\nrun_all.py: every module self-check passed")
