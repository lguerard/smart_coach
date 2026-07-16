#!/usr/bin/env python3
"""Create a smart_sport user account.

No public signup form -- accounts are created from the command line,
same operational posture as this project's other one-off admin steps
(rclone config, claude setup-token): run once per person via
``docker compose run --rm -it smart_sport-worker python manage_users.py``.
"""

import getpass
import sys

import db


def main() -> None:
    """Prompt for a username/password and create the account."""
    if len(sys.argv) > 1:
        username = sys.argv[1]
    else:
        username = input("Username: ").strip()
    if not username:
        print("Username cannot be empty.", file=sys.stderr)
        sys.exit(1)

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords don't match.", file=sys.stderr)
        sys.exit(1)
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    conn = db.connect()
    db.init_db(conn)
    try:
        user_id = db.create_user(conn, username, password)
    except ValueError as error:
        print(error, file=sys.stderr)
        sys.exit(1)
    suffix = " -- admin" if db.is_admin(conn, user_id) else ""
    print(f"Created user {username!r} (id={user_id}){suffix}.")


if __name__ == "__main__":
    main()
