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

If nothing can reach http://localhost:<port> on this machine at all
(e.g. the server is only reachable through a jump host that won't do
port forwarding), use --manual instead: it skips the local callback
server entirely and has you paste the redirect URL back in by hand.
"""

import argparse
import os
import sys
from pathlib import Path

# oauthlib refuses to parse a non-https redirect URL by default. The
# redirect here is always http://localhost -- the loopback address RFC
# 8252 exempts from that rule -- and the actual token exchange with
# Google still goes over HTTPS regardless; this only lifts the local
# scheme check on the redirect URL itself.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

from google_auth_oauthlib.flow import Flow, InstalledAppFlow  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CONFIG_DIR = Path.home() / ".config/smart_coach"
CLIENT_SECRET = CONFIG_DIR / "calendar_client_secret.json"


def existing_token_decision(
    username: str, token_file: Path, replace: bool,
    health=None,
) -> tuple[bool, str]:
    """Whether to run the consent flow when a token file already exists.

    This used to stop at "already exists" whenever the file was there --
    including when Google had revoked it, which is exactly when the
    morning notification tells you to run this command. So the stored
    token is now actually tried: a dead or unreadable one is set aside
    (kept as ``.bak``) and replaced; a working one is left alone unless
    ``--replace`` says otherwise.

    Parameters:
        username (str): Account name.
        token_file (Path): Where the token lives.
        replace (bool): Re-authorize even if the token works.
        health: ``gcal.token_health``-like callable, injectable for
            tests.

    Returns:
        tuple[bool, str]: ``(run the consent flow, message to print)``.
    """
    if not token_file.exists():
        return True, ""
    if replace:
        status, detail = "replace", ""
    else:
        if health is None:
            import gcal

            health = gcal.token_health
        status, detail = health(username)
    if status == "ok":
        return False, (
            f"{token_file} works: Google accepts it, nothing to do.\n"
            "Rerun with --replace to re-authorize anyway."
        )
    if status == "unreachable":
        return False, (
            f"Could not reach Google to test {token_file} ({detail}).\n"
            "Check the network, or rerun with --replace to re-authorize."
        )
    backup = token_file.with_suffix(token_file.suffix + ".bak")
    token_file.replace(backup)
    why = "as asked (--replace)" if status == "replace" else (
        "Google no longer accepts it"
    )
    return True, (
        f"Replacing {token_file}: {why}. The old one is kept as {backup}."
    )


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
    parser.add_argument(
        "--manual",
        action="store_true",
        help="paste the redirect URL yourself instead of running a local "
        "callback server -- use this when nothing on this machine can "
        "reach http://localhost:<port> (e.g. only reachable through a "
        "jump host with no port forwarding)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="use this name even though no account is called that",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="re-authorize even if the current token still works",
    )
    args = parser.parse_args()

    if not args.force:
        import db

        conn = db.connect()
        db.init_db(conn)
        problem = db.unknown_account_message(conn, args.username)
        conn.close()
        if problem:
            print(problem, file=sys.stderr)
            return 2

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
    proceed, message = existing_token_decision(
        args.username, token_file, args.replace,
    )
    if message:
        print(message)
    if not proceed:
        return 0

    if args.manual:
        # No local server at all: Google still redirects the browser to
        # this address whether or not anything is listening there. The
        # code lands in the browser's address bar regardless, so it can
        # be copied out by hand -- this needs no port published, forwarded
        # or reachable anywhere.
        redirect_uri = f"http://localhost:{args.port}/"
        flow = Flow.from_client_secrets_file(
            str(CLIENT_SECRET), scopes=SCOPES, redirect_uri=redirect_uri
        )
        auth_url, _ = flow.authorization_url()
        print(
            "\nOpen the URL below in a browser and approve the access.\n"
            "The page it redirects to afterwards will fail to load --\n"
            "ignore that error and copy the full URL from the address bar\n"
            f"(it starts with {redirect_uri}?...).\n\n{auth_url}\n"
        )
        redirect_response = input("Paste that URL here: ").strip()
        flow.fetch_token(authorization_response=redirect_response)
        creds = flow.credentials
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        print(
            "\nOpen the URL below in a browser and approve the access.\n"
            "The browser must be able to reach "
            f"http://localhost:{args.port} on this machine.\n"
            "Over SSH, open the session with:\n"
            f"    ssh -L {args.port}:localhost:{args.port} you@your-server\n"
            "If nothing can reach that address at all, rerun this command\n"
            "with --manual instead.\n"
        )
        # open_browser=False: there is no browser inside the container, and a
        # server reached over SSH has no display either. Printing the URL
        # works in every case.
        # bind_addr="0.0.0.0": the callback server must listen on every
        # interface, not just the container's own loopback, or Docker's
        # published port (-p 8765:8765) has nothing to forward to. host
        # stays "localhost" so the redirect_uri sent to Google still
        # matches what the OAuth client has registered and what the
        # browser connects to.
        creds = flow.run_local_server(
            host="localhost", bind_addr="0.0.0.0", port=args.port, open_browser=False
        )

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    print(f"\nDone. Token written to {token_file}")
    print("Nothing else to copy — run_coach.py will pick it up.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        token = tmp / "calendar_token_bob.json"

        # No file: go ahead, nothing to say.
        assert existing_token_decision("bob", token, False) == (True, "")
        # A working token is left alone.
        token.write_text("{}")
        go, msg = existing_token_decision(
            "bob", token, False, health=lambda u: ("ok", ""))
        assert not go and "works" in msg and token.exists()
        # Google unreachable proves nothing: keep it, say so.
        go, msg = existing_token_decision(
            "bob", token, False, health=lambda u: ("unreachable", "dns"))
        assert not go and "dns" in msg and token.exists()
        # A refused token (the morning's invalid_grant) is set aside and
        # the consent flow runs -- the case that used to dead-end.
        go, msg = existing_token_decision(
            "bob", token, False, health=lambda u: ("refused", "x"))
        assert go and not token.exists(), msg
        assert (tmp / "calendar_token_bob.json.bak").exists()
        # --replace re-authorizes even a working token, without testing.
        token.write_text("{}")
        go, msg = existing_token_decision(
            "bob", token, True, health=lambda u: 1 / 0)
        assert go and "--replace" in msg and not token.exists()
        print("setup_calendar.py: all checks passed")
        raise SystemExit(0)
    raise SystemExit(main())
