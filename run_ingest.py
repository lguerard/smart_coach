#!/usr/bin/env python3
"""Cron entrypoint: ingest each user's data (Garmin API + HC export).

Exercise sessions and sleep come straight from the Garmin API;
everything else (steps, weight, nutrition, ...) from the Health
Connect export synced off Google Drive. Run before run_coach.py so
"today" reflects the latest sync. Loops over every account (each
person has their own rclone remote, staging subdirectory and Garmin
tokens, so one user's failure doesn't block another's).
"""

import os
from pathlib import Path

import db
from ingest import garmin_api, parse_health_connect, sync_drive

STAGING_ROOT = Path(os.environ.get("INGEST_STAGING_DIR", "data/inbox"))


def main() -> None:
    """Sync + ingest the Drive export for every user account."""
    conn = db.connect()
    db.init_db(conn)
    for user in db.all_users(conn):
        print(f"{user['username']}:")
        # Garmin first, HC second -- each guarded so one source (or
        # one user) failing doesn't block the other.
        try:
            counts = garmin_api.fetch_and_upsert(
                conn, user["id"], user["username"],
            )
            for table, count in sorted(counts.items()):
                print(f"  garmin:{table}: {count}")
        except Exception as exc:
            print(f"  garmin: FAILED: {exc}")

        remote = db.get_setting(conn, user["id"], "rclone_remote")
        if not remote:
            print("  no rclone_remote configured, skipping HC export")
            continue
        staging_dir = STAGING_ROOT / user["username"]
        export_path = sync_drive.sync_and_extract(staging_dir, remote)
        counts = parse_health_connect.parse_and_upsert(
            export_path, conn, user["id"],
        )
        for table, count in sorted(counts.items()):
            print(f"  {table}: {count}")


if __name__ == "__main__":
    main()
