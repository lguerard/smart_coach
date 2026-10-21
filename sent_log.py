#!/usr/bin/env python3
"""Every notification sent to a phone, kept for the Messages page.

A push is gone once swiped away, and the coach's reasons go with it:
the morning message explains a level, the evening one a goal to retype.
Each send goes through :func:`send`, which pushes it (``notify``) and
keeps a copy, failed sends included, so the page shows what reached the
phone and what did not. Mornings from before this log existed still
have their message in ``coach_log``; :func:`history` falls back to it.
"""

import datetime as dt
import sqlite3
from typing import Optional

import notify

KINDS_FR = {
    "morning": "Message du matin", "replan": "Séance changée",
    "achievement": "Succès", "sleep": "Question du matin",
    "afternoon": "Point de 16 h", "evening": "Soir", "rating": "Note de séance",
    "error": "Erreur",
}


def send(
    conn: sqlite3.Connection, user_id: int, kind: str, text: str,
    title: str = "Smart Coach", topic: Optional[str] = None,
    actions: Optional[list] = None, click: Optional[str] = None,
) -> None:
    """Push one notification and keep it; a failed push is kept too
    (``ok`` = 0) and the error raised as before."""
    try:
        if actions:
            notify.notify_with_actions(text, actions, title=title,
                                       topic=topic, click=click)
        else:
            notify.notify(text, title=title, topic=topic)
    except Exception:
        _record(conn, user_id, kind, title, text, False)
        raise
    _record(conn, user_id, kind, title, text, True)


def _record(conn, user_id, kind, title, text, ok) -> None:
    import metrics

    now = dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(metrics.local_tz(conn, user_id))
    conn.execute(
        "INSERT INTO sent_messages (user_id, sent_utc, local_date, "
        "local_time, kind, title, text, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, now.isoformat(timespec="seconds"), local.date().isoformat(),
         local.strftime("%H:%M"), kind, title, text, int(ok)))
    conn.commit()


def history(conn: sqlite3.Connection, user_id: int, days: int = 30,
            today: Optional[str] = None) -> list[dict]:
    """The last ``days`` days, newest first: ``{date, messages}`` where
    each message is ``{time, kind, label, title, text, ok}``."""
    import metrics

    today = today or dt.datetime.now(metrics.local_tz(conn, user_id)).date().isoformat()
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=days - 1)).isoformat()
    by_day: dict[str, list] = {}
    for row in conn.execute(
        "SELECT local_date, local_time, kind, title, text, ok FROM "
        "sent_messages WHERE user_id = ? AND local_date >= ? ORDER BY id",
        (user_id, start),
    ):
        by_day.setdefault(row["local_date"], []).append({
            "time": row["local_time"], "kind": row["kind"],
            "label": KINDS_FR.get(row["kind"], row["kind"]),
            "title": row["title"], "text": row["text"], "ok": bool(row["ok"]),
        })
    first = conn.execute(
        "SELECT MIN(local_date) AS d FROM sent_messages WHERE user_id = ?",
        (user_id,)).fetchone()["d"]
    # Before the log: the morning message the coach wrote that day.
    for row in conn.execute(
        "SELECT local_date, created_at, message FROM coach_log WHERE "
        "id IN (SELECT MAX(id) FROM coach_log WHERE user_id = ? GROUP BY "
        "local_date) AND local_date >= ? AND local_date < ? AND message "
        "IS NOT NULL AND message != ''",
        (user_id, start, first or "9999"),
    ):
        by_day.setdefault(row["local_date"], []).append({
            "time": None, "kind": "morning", "label": KINDS_FR["morning"],
            "title": "Smart Coach", "text": row["message"], "ok": True,
        })
    return [{"date": d, "messages": by_day[d]}
            for d in sorted(by_day, reverse=True)]


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db

    conn = db.connect(Path(tempfile.mkdtemp()) / "sent.db")
    db.init_db(conn)
    uid = db.create_user(conn, "s", "pw-123456")
    pushed = []
    notify.notify = lambda text, title="Smart Coach", topic=None: pushed.append(text)
    notify.notify_with_actions = (
        lambda text, actions, title="Smart Coach", topic=None, click=None:
        pushed.append((text, len(actions))))

    import metrics

    today = dt.datetime.now(metrics.local_tz(conn, uid)).date()
    old = (today - dt.timedelta(days=3)).isoformat()
    # A morning from before the log: shown from coach_log.
    conn.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "message) VALUES (?, 'x', ?, 'green', 'Ancien message du matin')",
        (uid, old))
    conn.commit()

    send(conn, uid, "morning", "Plan du jour : 1973 kcal")
    send(conn, uid, "rating", "Comment était ta séance ?",
         actions=[{"label": "Bien"}])
    assert pushed == ["Plan du jour : 1973 kcal", ("Comment était ta séance ?", 1)]

    # A failed push is kept, and still raises.
    def down(*args, **kwargs):
        raise OSError("ntfy down")
    notify.notify = down
    try:
        send(conn, uid, "evening", "Ouvre MyFitnessPal")
        raise AssertionError("the failure should propagate")
    except OSError:
        pass

    days = history(conn, uid)
    assert [d["date"] for d in days] == [today.isoformat(), old], days
    tonight = days[0]["messages"]
    assert [m["kind"] for m in tonight] == ["morning", "rating", "evening"]
    assert tonight[0]["label"] == "Message du matin" and tonight[0]["time"]
    assert not tonight[2]["ok"] and tonight[1]["ok"], tonight
    assert days[1]["messages"][0]["text"] == "Ancien message du matin"
    # Older than the window: left out.
    assert history(conn, uid, days=2) == days[:1]
    print("sent_log.py: all checks passed")
