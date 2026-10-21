#!/usr/bin/env python3
"""Signed one-tap links, for the buttons in a phone notification.

A notification button is pressed outside any browser session, so it
cannot log in. Each link instead carries a signature: an HMAC of what
it does (rate the session of that day, with that rating) under a
random secret kept per user in the database. A link can do exactly
that one thing and nothing else, cannot be altered, and stops working
after ``MAX_AGE_DAYS``. Three kinds: "rate" (the evening's session),
"sleep" (the morning's "how did you sleep") and "progress" (after a
session too easy: heavier or longer next time, progression_choice).

The web app (POST /quick/rate/...) checks it; the worker, which sends
the notifications, signs it -- both read the same secret from the
shared database, so no extra file needs mounting.
"""

import datetime as dt
import hashlib
import hmac
import secrets
import sqlite3
from typing import Optional

import db

RATINGS = ("easy", "right", "hard")
SLEEP_RATINGS = ("good", "ok", "bad")
PROGRESS_CHOICES = ("longer", "heavier")
VALUES = {"rate": RATINGS, "sleep": SLEEP_RATINGS, "progress": PROGRESS_CHOICES}
MAX_AGE_DAYS = 3


def _secret(conn: sqlite3.Connection, user_id: int) -> str:
    secret = db.get_setting(conn, user_id, "link_secret")
    if not secret:
        secret = secrets.token_hex(32)
        db.set_setting(conn, user_id, "link_secret", secret)
    return secret


def sign(
    conn: sqlite3.Connection, user_id: int, date: str, rating: str,
    action: str = "rate",
) -> str:
    """The signature for "``action`` ``date`` as ``rating``" (rate the
    session, or the night)."""
    message = f"{action}:{user_id}:{date}:{rating}".encode()
    return hmac.new(_secret(conn, user_id).encode(), message,
                    hashlib.sha256).hexdigest()[:32]


def rate_url(
    conn: sqlite3.Connection, user_id: int, base_url: str, date: str,
    rating: str,
) -> str:
    """Absolute URL of the one-tap rating link."""
    return (f"{base_url.rstrip('/')}/quick/rate/{user_id}/{date}/{rating}/"
            f"{sign(conn, user_id, date, rating)}")


def sleep_url(
    conn: sqlite3.Connection, user_id: int, base_url: str, date: str,
    rating: str,
) -> str:
    """Absolute URL of the one-tap "how did you sleep" link."""
    return (f"{base_url.rstrip('/')}/quick/sleep/{user_id}/{date}/{rating}/"
            f"{sign(conn, user_id, date, rating, 'sleep')}")


def progress_url(
    conn: sqlite3.Connection, user_id: int, base_url: str, date: str,
    choice: str,
) -> str:
    """Absolute URL of the one-tap "heavier or longer" link."""
    return (f"{base_url.rstrip('/')}/quick/progress/{user_id}/{date}/{choice}/"
            f"{sign(conn, user_id, date, choice, 'progress')}")


def verify(
    conn: sqlite3.Connection, user_id: int, date: str, rating: str,
    signature: str, today: Optional[str] = None, action: str = "rate",
) -> bool:
    """Whether a rating link is genuine and still fresh."""
    if rating not in VALUES.get(action, ()):
        return False
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        return False
    now = dt.date.fromisoformat(today) if today else dt.date.today()
    if not 0 <= (now - day).days <= MAX_AGE_DAYS:
        return False
    if not db.get_setting(conn, user_id, "link_secret"):
        return False  # no link was ever signed for this account
    return hmac.compare_digest(
        sign(conn, user_id, date, rating, action), signature)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    conn = db.connect(Path(tempfile.mkdtemp()) / "links.db")
    db.init_db(conn)
    uid = db.create_user(conn, "q", "pw-123456")
    other = db.create_user(conn, "o", "pw-123456")
    assert not verify(conn, uid, "2026-10-17", "easy", "0" * 32, "2026-10-17")
    url = rate_url(conn, uid, "https://coach.example/", "2026-10-17", "easy")
    assert url.startswith(f"https://coach.example/quick/rate/{uid}/2026-10-17/easy/"), url
    sig = url.rsplit("/", 1)[1]
    assert verify(conn, uid, "2026-10-17", "easy", sig, "2026-10-17")
    assert verify(conn, uid, "2026-10-17", "easy", sig, "2026-10-20")
    # Altered rating, date, user, or too old: refused.
    assert not verify(conn, uid, "2026-10-17", "hard", sig, "2026-10-17")
    assert not verify(conn, uid, "2026-10-16", "easy", sig, "2026-10-17")
    assert not verify(conn, other, "2026-10-17", "easy", sig, "2026-10-17")
    assert not verify(conn, uid, "2026-10-17", "easy", sig, "2026-10-21")
    assert not verify(conn, uid, "2026-10-17", "meh", sig, "2026-10-17")
    assert not verify(conn, uid, "bad-date", "easy", sig, "2026-10-17")
    # A sleep link is its own kind: not usable to rate a session, nor
    # the other way round.
    night = sleep_url(conn, uid, "https://coach.example", "2026-10-17", "bad")
    assert night.startswith(f"https://coach.example/quick/sleep/{uid}/2026-10-17/bad/")
    night_sig = night.rsplit("/", 1)[1]
    assert verify(conn, uid, "2026-10-17", "bad", night_sig, "2026-10-17", "sleep")
    assert not verify(conn, uid, "2026-10-17", "bad", night_sig, "2026-10-17")
    assert not verify(conn, uid, "2026-10-17", "easy", sig, "2026-10-17", "sleep")
    assert not verify(conn, uid, "2026-10-17", "good", night_sig, "2026-10-17", "sleep")
    # The "heavier or longer" link: its own kind too.
    up = progress_url(conn, uid, "https://coach.example", "2026-10-17", "heavier")
    assert f"/quick/progress/{uid}/2026-10-17/heavier/" in up, up
    assert verify(conn, uid, "2026-10-17", "heavier", up.rsplit("/", 1)[1],
                  "2026-10-17", "progress")
    assert not verify(conn, uid, "2026-10-17", "longer", up.rsplit("/", 1)[1],
                      "2026-10-17", "progress")
    # The secret stays the same once made.
    assert url == rate_url(conn, uid, "https://coach.example", "2026-10-17", "easy")
    print("quick_links.py: all checks passed")
