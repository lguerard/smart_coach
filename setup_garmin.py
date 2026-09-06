"""Log one Smart Coach account into Garmin, once.

    docker compose run --rm -it smart_coach-worker python setup_garmin.py alice

Asks for the Garmin e-mail and password (plus the MFA code if the
account has it) and caches the resulting tokens under
data/garmin-tokens/<username>. They stay valid about a year; after that
this same command renews them.

The tokens are per Smart Coach account, so two people sharing one
deployment never share a Garmin login.
"""

import argparse
import sys

from ingest import garmin_api


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Log one Smart Coach account into Garmin."
    )
    parser.add_argument(
        "username",
        help="the Smart Coach account these Garmin tokens belong to "
        "(the name you signed up with)",
    )
    args = parser.parse_args()

    try:
        garmin_api.get_client(args.username)
    except Exception as exc:  # noqa: BLE001 -- surfaced to a human, not handled
        print(f"Garmin login failed: {exc}", file=sys.stderr)
        return 1

    print(f"\nDone. Garmin tokens cached for {args.username!r}.")
    print("They last about a year; rerun this command when they expire.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
