"""Grant Smart Coach access to one account's Google Calendar.

One command, run once per account:

    docker compose run --rm -it -p 8765:8765 \\
        smart_coach-worker python setup_calendar.py alice

It prints a Google URL, waits for you to approve it in a browser, and
writes the token where the containers already look for it -- no file to
copy afterwards.

Why the fixed port: Google's consent redirects back to a local address,
so that address has to be reachable. A random port could not be
published out of the container, and could not be forwarded over SSH
either. 8765 is used unless --port says otherwise.
"""

import argparse
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CONFIG_DIR = Path.home() / ".config/smart_coach"
CLIENT_SECRET = CONFIG_DIR / "calendar_client_secret.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grant Calendar access for one Smart Coach account."
    )
    parser.add_argument(
        "username",
        help="the Smart Coach account this calendar belongs to "
        "(the name you signed up with)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="local port Google redirects back to (default: 8765)",
    )
    args = parser.parse_args()

    if not CLIENT_SECRET.exists():
        print(
            f"Missing {CLIENT_SECRET}.\n\n"
            "Create it once in the Google Cloud Console:\n"
            "  1. console.cloud.google.com -> create or pick a project\n"
            "  2. APIs & Services -> Library -> enable 'Google Calendar API'\n"
            "  3. APIs & Services -> Credentials -> Create credentials\n"
            "     -> OAuth client ID -> User data\n"
            "  4. Application type: Desktop app  <- must be Desktop\n"
            "  5. Download the JSON, then on the server:\n"
            "       mkdir -p data/gcal-config\n"
            "       cp ~/Downloads/client_secret_*.json \\\n"
            "          data/gcal-config/calendar_client_secret.json\n\n"
            "If the consent screen is in Testing mode, add your own address\n"
            "under 'Test users' or Google will refuse the approval.",
            file=sys.stderr,
        )
        return 1

    token_file = CONFIG_DIR / f"calendar_token_{args.username}.json"
    if token_file.exists():
        print(f"{token_file} already exists — delete it to start over.")
        return 0

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    print(
        "\nOpen the URL below in a browser and approve the access.\n"
        "The browser must be able to reach "
        f"http://localhost:{args.port} on this machine.\n"
        "Over SSH, open the session with:\n"
        f"    ssh -L {args.port}:localhost:{args.port} you@your-server\n"
    )
    # open_browser=False: there is no browser inside the container, and a
    # server reached over SSH has no display either. Printing the URL works
    # in every case.
    creds = flow.run_local_server(port=args.port, open_browser=False)

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    print(f"\nDone. Token written to {token_file}")
    print("Nothing else to copy — run_coach.py will pick it up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
