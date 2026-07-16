#!/usr/bin/env python3
"""Cron entrypoint: pull each user's Health Connect export and ingest it.

Run before run_coach.py so "today" reflects the latest sync. Loops
over every account (each person has their own rclone remote and their
own staging subdirectory, so one user's sync failure doesn't block
another's).
"""

import os
from pathlib import Path

import db
from ingest import parse_health_connect, sync_drive

STAGING_ROOT = Path(os.environ.get("INGEST_STAGING_DIR", "data/inbox"))


def main() -> None:
    """Sync + ingest the Drive export for every user account."""
    conn = db.connect()
    db.init_db(conn)
    for user in db.all_users(conn):
        remote = db.get_setting(conn, user["id"], "rclone_remote")
        if not remote:
            print(f"{user['username']}: no rclone_remote configured, skipping")
            continue
        print(f"{user['username']}:")
        staging_dir = STAGING_ROOT / user["username"]
        export_path = sync_drive.sync_and_extract(staging_dir, remote)
        counts = parse_health_connect.parse_and_upsert(
            export_path, conn, user["id"],
        )
        for table, count in sorted(counts.items()):
            print(f"  {table}: {count}")


if __name__ == "__main__":
    main()
