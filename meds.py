#!/usr/bin/env python3
"""Medications and supplements: what to take when, and the reminders.

Each item has its times of day (and the weekdays it is taken). At each
time a notification goes out with "Pris" and "Sauter" buttons, and comes
back every ``repeat_min`` minutes until one of them is pressed -- or the
item is marked on the Médicaments page or from the chat -- up to
``max_reminders`` times. ``run_meds.py`` (every 5 minutes from cron)
sends them; this module only says what is due and keeps the record.

The coach never says what to take or how much: the list is the
person's own, as their doctor or pharmacist set it.
"""

import datetime as dt
import re
import sqlite3
from typing import Optional

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
REPEAT_RANGE = (5, 240)
MAX_REMINDERS = 8
STATUSES = ("taken", "skipped")
ALL_DAYS = "0123456"


def _times(raw) -> list[str]:
    """``"8h, 20:30"`` or a list -> ``["08:00", "20:30"]``, sorted."""
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,; ]+", str(raw or ""))
    out = []
    for item in items:
        text = str(item).strip().lower().replace("h", ":")
        if not text:
            continue
        if text.endswith(":"):
            text += "00"
        if re.fullmatch(r"\d:\d\d", text):
            text = "0" + text
        if not TIME_RE.match(text):
            raise ValueError(f"Heure invalide : {item} (format HH:MM).")
        out.append(text)
    if not out or len(out) > 8:
        raise ValueError("Entre 1 et 8 heures de prise par jour.")
    return sorted(set(out))


def _days(raw) -> str:
    text = "".join(sorted(set(str(raw if raw not in (None, "") else ALL_DAYS))))
    if not text or any(c not in ALL_DAYS for c in text):
        raise ValueError("Jours : chiffres de 0 (lundi) à 6 (dimanche).")
    return text


def _row(row) -> dict:
    out = dict(row)
    out["times"] = out["times"].split(",") if out["times"] else []
    return out


def add(
    conn: sqlite3.Connection, user_id: int, name: str, times, dose: str = "",
    weekdays=ALL_DAYS, repeat_min=15, notes: str = "",
) -> int:
    """Add an item; its id. Raises ValueError on a bad field."""
    name = str(name or "").strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("Le nom fait de 1 à 60 caractères.")
    repeat = int(repeat_min)
    if not REPEAT_RANGE[0] <= repeat <= REPEAT_RANGE[1]:
        raise ValueError(f"Rappel toutes les {REPEAT_RANGE[0]} à {REPEAT_RANGE[1]} min.")
    cursor = conn.execute(
        "INSERT INTO meds (user_id, name, dose, times, weekdays, repeat_min, "
        "active, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (user_id, name, str(dose or "").strip()[:60], ",".join(_times(times)),
         _days(weekdays), repeat, str(notes or "").strip()[:200],
         dt.datetime.now(dt.timezone.utc).isoformat()))
    conn.commit()
    return cursor.lastrowid


def get(conn: sqlite3.Connection, user_id: int, med_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM meds WHERE id = ? AND user_id = ?",
                       (med_id, user_id)).fetchone()
    return _row(row) if row else None


def update(conn: sqlite3.Connection, user_id: int, med_id: int, **fields) -> dict:
    """Change some fields; the item as it was before. Raises ValueError
    for an unknown item or a bad field."""
    before = get(conn, user_id, med_id)
    if before is None:
        raise ValueError("Médicament inconnu.")
    values = {}
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not 1 <= len(name) <= 60:
            raise ValueError("Le nom fait de 1 à 60 caractères.")
        values["name"] = name
    if "dose" in fields:
        values["dose"] = str(fields["dose"] or "").strip()[:60]
    if "times" in fields:
        values["times"] = ",".join(_times(fields["times"]))
    if "weekdays" in fields:
        values["weekdays"] = _days(fields["weekdays"])
    if "repeat_min" in fields:
        repeat = int(fields["repeat_min"])
        if not REPEAT_RANGE[0] <= repeat <= REPEAT_RANGE[1]:
            raise ValueError(f"Rappel toutes les {REPEAT_RANGE[0]} à {REPEAT_RANGE[1]} min.")
        values["repeat_min"] = repeat
    if "active" in fields:
        values["active"] = 1 if fields["active"] else 0
    if "notes" in fields:
        values["notes"] = str(fields["notes"] or "").strip()[:200]
    if values:
        conn.execute(
            f"UPDATE meds SET {', '.join(f'{k} = ?' for k in values)} "
            "WHERE id = ? AND user_id = ?", (*values.values(), med_id, user_id))
        conn.commit()
    return before


def delete(conn: sqlite3.Connection, user_id: int, med_id: int) -> bool:
    deleted = conn.execute("DELETE FROM meds WHERE id = ? AND user_id = ?",
                           (med_id, user_id)).rowcount
    conn.execute("DELETE FROM med_log WHERE med_id = ? AND user_id = ?",
                 (med_id, user_id))
    conn.execute("DELETE FROM med_reminders WHERE med_id = ? AND user_id = ?",
                 (med_id, user_id))
    conn.commit()
    return bool(deleted)


def list_meds(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return [_row(r) for r in conn.execute(
        "SELECT * FROM meds WHERE user_id = ? ORDER BY active DESC, times, name",
        (user_id,))]


def mark(conn: sqlite3.Connection, user_id: int, med_id: int, date: str,
         slot: str, status: str) -> bool:
    """Record a dose taken or skipped (it stops its reminders)."""
    med = get(conn, user_id, med_id)
    if med is None or status not in STATUSES or slot not in med["times"]:
        return False
    conn.execute(
        "INSERT INTO med_log (user_id, med_id, local_date, slot, status, at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, med_id, local_date, "
        "slot) DO UPDATE SET status = excluded.status, at = excluded.at",
        (user_id, med_id, date, slot, status,
         dt.datetime.now(dt.timezone.utc).isoformat()))
    conn.commit()
    return True


def unmark(conn: sqlite3.Connection, user_id: int, med_id: int, date: str,
           slot: str) -> None:
    conn.execute("DELETE FROM med_log WHERE user_id = ? AND med_id = ? AND "
                 "local_date = ? AND slot = ?", (user_id, med_id, date, slot))
    conn.commit()


def _status(conn, user_id: int, med_id: int, date: str, slot: str):
    row = conn.execute(
        "SELECT status FROM med_log WHERE user_id = ? AND med_id = ? AND "
        "local_date = ? AND slot = ?", (user_id, med_id, date, slot)).fetchone()
    return row["status"] if row else None


def day(conn: sqlite3.Connection, user_id: int, now: dt.datetime) -> list[dict]:
    """Today's doses in order: ``{med_id, name, dose, slot, status}``,
    status ``taken`` / ``skipped`` / ``due`` (time passed) / ``later``."""
    date = now.date().isoformat()
    clock = now.strftime("%H:%M")
    out = []
    for med in list_meds(conn, user_id):
        if not med["active"] or str(now.weekday()) not in med["weekdays"]:
            continue
        for slot in med["times"]:
            status = _status(conn, user_id, med["id"], date, slot) or (
                "due" if slot <= clock else "later")
            out.append({"med_id": med["id"], "name": med["name"],
                        "dose": med["dose"], "slot": slot, "status": status})
    return sorted(out, key=lambda d: (d["slot"], d["name"]))


def due(conn: sqlite3.Connection, user_id: int, now: dt.datetime) -> list[dict]:
    """The reminders to send now: doses whose time has come, not marked,
    whose last reminder is ``repeat_min`` old (or none yet), at most
    ``MAX_REMINDERS`` each. Each carries ``count`` (reminders so far)."""
    date = now.date().isoformat()
    out = []
    for dose in day(conn, user_id, now):
        if dose["status"] != "due":
            continue
        med = get(conn, user_id, dose["med_id"])
        sent = conn.execute(
            "SELECT sent_count, last_sent FROM med_reminders WHERE user_id = ? "
            "AND med_id = ? AND local_date = ? AND slot = ?",
            (user_id, med["id"], date, dose["slot"])).fetchone()
        count = sent["sent_count"] if sent else 0
        if count >= MAX_REMINDERS:
            continue
        if sent:
            last = dt.datetime.fromisoformat(sent["last_sent"])
            # A minute of slack: cron fires every 5 minutes, not exactly.
            if (now - last).total_seconds() < (med["repeat_min"] - 1) * 60:
                continue
        out.append({**dose, "count": count, "repeat_min": med["repeat_min"]})
    return out


def record_sent(conn: sqlite3.Connection, user_id: int, med_id: int,
                date: str, slot: str, now: dt.datetime) -> None:
    conn.execute(
        "INSERT INTO med_reminders (user_id, med_id, local_date, slot, "
        "sent_count, last_sent) VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT "
        "(user_id, med_id, local_date, slot) DO UPDATE SET sent_count = "
        "sent_count + 1, last_sent = excluded.last_sent",
        (user_id, med_id, date, slot, now.isoformat()))
    conn.commit()


def history(conn: sqlite3.Connection, user_id: int, today: str,
            days: int = 7) -> list[dict]:
    """Per item, the last ``days`` days before today: doses planned,
    taken, skipped."""
    out = []
    end = dt.date.fromisoformat(today)
    for med in list_meds(conn, user_id):
        planned = taken = skipped = 0
        for k in range(1, days + 1):
            date = end - dt.timedelta(days=k)
            if str(date.weekday()) not in med["weekdays"]:
                continue
            for slot in med["times"]:
                planned += 1
                status = _status(conn, user_id, med["id"], date.isoformat(), slot)
                taken += status == "taken"
                skipped += status == "skipped"
        out.append({"med_id": med["id"], "name": med["name"], "planned": planned,
                    "taken": taken, "skipped": skipped})
    return out


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db

    conn = db.connect(Path(tempfile.mkdtemp()) / "meds.db")
    db.init_db(conn)
    uid = db.create_user(conn, "m", "pw-123456")
    other = db.create_user(conn, "o", "pw-123456")

    try:
        add(conn, uid, "Magnesium", "25:00")
        raise AssertionError("a bad time must be refused")
    except ValueError:
        pass
    mg = add(conn, uid, "Magnésium", "12h, 20:00", dose="1 gélule", repeat_min=15)
    vit = add(conn, uid, "Vitamine C", "8h", weekdays="01234")  # weekdays only
    assert get(conn, uid, mg)["times"] == ["12:00", "20:00"]
    assert get(conn, other, mg) is None  # another account's item is not seen

    monday_noon = dt.datetime(2026, 11, 9, 12, 2)  # a Monday
    today = [(d["name"], d["slot"], d["status"]) for d in day(conn, uid, monday_noon)]
    assert today == [("Vitamine C", "08:00", "due"), ("Magnésium", "12:00", "due"),
                     ("Magnésium", "20:00", "later")], today
    # Both due doses are reminded, then not again before 15 minutes.
    first = due(conn, uid, monday_noon)
    assert [(d["name"], d["count"]) for d in first] == [("Vitamine C", 0), ("Magnésium", 0)]
    for d in first:
        record_sent(conn, uid, d["med_id"], "2026-11-09", d["slot"], monday_noon)
    assert due(conn, uid, monday_noon + dt.timedelta(minutes=5)) == []
    again = due(conn, uid, monday_noon + dt.timedelta(minutes=15))
    assert [d["count"] for d in again] == [1, 1], again
    # "Pris": that dose stops; the other keeps coming.
    assert mark(conn, uid, mg, "2026-11-09", "12:00", "taken")
    assert [d["name"] for d in due(conn, uid, monday_noon + dt.timedelta(minutes=30))] \
        == ["Vitamine C"]
    assert not mark(conn, uid, mg, "2026-11-09", "13:00", "taken")  # not a slot
    # At most MAX_REMINDERS for one dose.
    later = monday_noon
    for _ in range(MAX_REMINDERS + 2):
        later += dt.timedelta(minutes=16)
        for d in due(conn, uid, later):
            record_sent(conn, uid, d["med_id"], "2026-11-09", d["slot"], later)
    assert conn.execute("SELECT sent_count FROM med_reminders WHERE med_id = ?",
                        (vit,)).fetchone()[0] == MAX_REMINDERS
    # Weekdays: no vitamin C on Saturday.
    saturday = dt.datetime(2026, 11, 14, 9, 0)
    assert [d["name"] for d in day(conn, uid, saturday)] == ["Magnésium", "Magnésium"]
    # Paused, edited, history, deleted.
    before = update(conn, uid, vit, active=False)
    assert before["active"] == 1 and day(conn, uid, monday_noon)[0]["name"] == "Magnésium"
    mark(conn, uid, mg, "2026-11-09", "20:00", "skipped")
    week = {h["name"]: h for h in history(conn, uid, "2026-11-10", 1)}
    assert week["Magnésium"] == {"med_id": mg, "name": "Magnésium", "planned": 2,
                                 "taken": 1, "skipped": 1}, week
    assert delete(conn, uid, vit) and get(conn, uid, vit) is None
    assert not delete(conn, other, mg) and get(conn, uid, mg)
    print("meds.py: all checks passed")
