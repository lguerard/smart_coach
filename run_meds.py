#!/usr/bin/env python3
"""Cron entrypoint (every 5 minutes): medication and supplement reminders.

For each account, every dose whose time has come and that is not marked
yet gets a notification with "Pris" and "Sauter" buttons, again every
``repeat_min`` minutes until one is pressed (meds.py says which are due).
Silent when nothing is due, so the log stays readable.
"""

import datetime as dt
import sqlite3
import sys

import db
import meds
import metrics
import sent_log


def remind(conn: sqlite3.Connection, user: dict,
           now: dt.datetime | None = None) -> int:
    """Send the reminders due for one account; how many went out."""
    import quick_links
    import run_checkin

    user_id = user["id"]
    now = now or dt.datetime.now(metrics.local_tz(conn, user_id)).replace(tzinfo=None)
    date = now.date().isoformat()
    topic = db.get_setting(conn, user_id, "ntfy_topic") or None
    base = run_checkin.public_url(conn, user_id)
    sent = 0
    for dose in meds.due(conn, user_id, now):
        what = dose["name"] + (f" ({dose['dose']})" if dose["dose"] else "")
        text = (f"💊 {what} : prise de {dose['slot']}."
                + (f" Rappel {dose['count'] + 1}." if dose["count"] else "")
                + f" Je te le redis dans {dose['repeat_min']} min tant que ce "
                  "n'est pas note.")
        if base:
            actions = [
                {"action": "http", "label": label, "method": "POST", "clear": True,
                 "url": quick_links.med_url(conn, user_id, base, date,
                                            dose["med_id"], dose["slot"], status)}
                for status, label in (("taken", "Pris"), ("skipped", "Sauter"))]
            sent_log.send(conn, user_id, "med", text, title=f"Smart Coach - {dose['name']}",
                          topic=topic, actions=actions,
                          click=f"{base.rstrip('/')}/medicaments")
        else:
            # A plain push carries its title in an HTTP header, which
            # only takes Latin-1: the name stays in the text.
            sent_log.send(conn, user_id, "med", text + " (Note-le dans Medicaments.)",
                          topic=topic)
        meds.record_sent(conn, user_id, dose["med_id"], date, dose["slot"], now)
        sent += 1
    return sent


def main() -> None:
    conn = db.connect()
    db.init_db(conn)
    for user in db.all_users(conn):
        try:
            count = remind(conn, dict(user))
        except Exception as error:  # noqa: BLE001 -- one account never blocks another
            print(f"{user['username']}: meds FAILED -- {error}")
            continue
        if count:
            print(f"{user['username']}: {count} medication reminder(s) sent")


if __name__ == "__main__" and sys.argv[1:] == ["--selfcheck"]:
    import tempfile
    from pathlib import Path

    import notify

    conn = db.connect(Path(tempfile.mkdtemp()) / "run_meds.db")
    db.init_db(conn)
    uid = db.create_user(conn, "r", "pw-123456")
    user = dict(db.get_user(conn, uid))
    pushed = []
    notify.notify = lambda text, title="Smart Coach", topic=None: pushed.append((text, None))
    notify.notify_with_actions = (
        lambda text, actions, title="Smart Coach", topic=None, click=None:
        pushed.append((text, actions)))
    mg = meds.add(conn, uid, "Magnésium", "12:00", dose="1 gélule")
    noon = dt.datetime(2026, 11, 9, 12, 3)
    # No dashboard address: the reminder still goes, without buttons.
    assert remind(conn, user, noon) == 1 and pushed[-1][1] is None
    assert "Magnésium (1 gélule) : prise de 12:00" in pushed[-1][0], pushed
    db.set_setting(conn, uid, "public_url", "https://coach.example")
    assert remind(conn, user, noon + dt.timedelta(minutes=5)) == 0  # not yet
    assert remind(conn, user, noon + dt.timedelta(minutes=15)) == 1
    text, actions = pushed[-1]
    assert "Rappel 2" in text and [a["label"] for a in actions] == ["Pris", "Sauter"]
    assert "/quick/med/" in actions[0]["url"] and actions[0]["url"].count(".taken/") == 1
    # Pressed "Pris": no more reminders for that dose.
    meds.mark(conn, uid, mg, "2026-11-09", "12:00", "taken")
    assert remind(conn, user, noon + dt.timedelta(minutes=45)) == 0
    assert conn.execute("SELECT COUNT(*) FROM sent_messages WHERE kind = 'med'"
                        ).fetchone()[0] == 2
    print("run_meds.py: all checks passed (no live push sent)")
elif __name__ == "__main__":
    main()
