#!/usr/bin/env python3
"""After a loaded session felt too easy: heavier, or longer?

A "too easy" used to have one answer: the next session of that type
goes up a level -- a few more seconds of effort or another tour. With a
kettlebell there is a second way up, a heavier bell, which builds more
muscle where the light one only lengthens the session. So once a
session with the kettlebell is rated too easy, the person is offered
both, for the whole session (one bell for everything):

- longer: nothing to do, the next session goes up a level as before;
- heavier: the next bell up (training.next_bell_kg) becomes the
  kettlebell, the loaded sessions' levels go down
  (training.lower_levels_for_heavier_bell) and the "too easy" given at
  the old weight no longer counts, so the first session at the new
  weight is not also lengthened.

The offer stands until the next session of that type is planned; with
no answer by then, the level goes up (longer).
"""

import datetime as dt
import json
import sqlite3
from typing import Optional

import db
import exercise_library
import session_history
import training

ANSWERED_KEY = "progress_choice"
CHOICES = ("longer", "heavier")
OFFER_DAYS = 3


def _answered(conn: sqlite3.Connection, user_id: int) -> dict:
    try:
        return json.loads(db.get_setting(conn, user_id, ANSWERED_KEY) or "{}")
    except ValueError:
        return {}


def _easy_rating(conn, user_id: int, today: str) -> Optional[dict]:
    """The latest loaded session rated too easy that still waits for an
    answer: ``{date, type}`` or None."""
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=OFFER_DAYS)).isoformat()
    answered = _answered(conn, user_id)
    for row in conn.execute(
        "SELECT local_date, session_type FROM session_feedback WHERE "
        "user_id = ? AND rating = 'easy' AND local_date BETWEEN ? AND ? "
        "ORDER BY local_date DESC", (user_id, start, today),
    ):
        kind, date = row["session_type"], row["local_date"]
        if kind not in training.LOADED_TYPES or answered.get(kind) == date:
            continue
        # Still the latest rating of that type, given at this weight.
        if training.recent_feedback(conn, user_id, kind, limit=1) != ["easy"]:
            continue
        last = conn.execute(
            "SELECT MAX(local_date) AS d FROM session_feedback WHERE "
            "user_id = ? AND session_type = ?", (user_id, kind),
        ).fetchone()["d"]
        if last != date:
            continue
        # The next session of that type is already planned: the level
        # went up this morning, the moment has passed.
        later = conn.execute(
            "SELECT 1 FROM coach_log WHERE user_id = ? AND session_type = ? "
            "AND local_date > ? LIMIT 1", (user_id, kind, date),
        ).fetchone()
        if later:
            continue
        return {"date": date, "type": kind}
    return None


def _change_text(before: dict, after: dict) -> str:
    """What one level more changes, in a few words."""
    from day_review import _plain

    a, b = session_history.snapshot(before), session_history.snapshot(after)
    for snap in (a, b):
        for move in snap.get("moves") or []:
            move["name"] = _plain(move["name"])  # the weight is not the change
    parts = []
    if a.get("rounds") != b.get("rounds"):
        parts.append(f"{a['rounds']} → {b['rounds']} tours")
    old = {m["slot"]: m for m in a.get("moves") or []}
    moves = [m for m in b.get("moves") or [] if m["slot"] in old]
    timed = {(old[m["slot"]]["amount"], m["amount"]) for m in moves}
    # A circuit where every move has the same work time (the kettlebell
    # intervals) says it once; otherwise move by move.
    if (moves and all(m["unit"] == "s" for m in moves) and len(timed) == 1
            and next(iter(timed))[0] != next(iter(timed))[1]):
        before_sec, after_sec = next(iter(timed))
        parts.append(f"{before_sec} → {after_sec} s d'effort")
    else:
        for move in b.get("moves") or []:
            before_move = old.get(move["slot"])
            if not before_move or len(parts) >= 3:
                continue
            if before_move["name"] != move["name"]:
                parts.append(f"{before_move['name']} → {move['name']}")
            elif before_move["amount"] != move["amount"]:
                unit = " s" if move["unit"] == "s" else ""
                parts.append(f"{move['name']} {before_move['amount']} → "
                             f"{move['amount']}{unit}")
    if parts and b.get("duration_min") and a.get("duration_min") != b.get("duration_min"):
        parts.append(f"environ {b['duration_min']} min")
    return ", ".join(parts)


def offer(conn: sqlite3.Connection, user_id: int, today: str) -> Optional[dict]:
    """The pending choice, or None.

    Returns:
        dict | None: ``date`` and ``type`` of the session rated too
        easy, ``label`` (French), ``longer`` (``{level, text}``, None at
        the top level) and ``heavier`` (``{from_kg, to_kg, levels}``
        with ``levels`` as French "type a -> b" lines).
    """
    kg = exercise_library.equipment_for(conn, user_id).get("kettlebell_kg")
    if not kg:
        return None
    rated = _easy_rating(conn, user_id, today)
    if rated is None:
        return None
    kind = rated["type"]
    level = training.get_level(conn, user_id, kind)
    # Longer is not on offer at the top, or when the session cap leaves
    # the next level the same as this one.
    longer = None
    if level < training.LEVEL_MAX:
        text = _change_text(
            training.user_session_values(conn, user_id, kind, level, today),
            training.user_session_values(conn, user_id, kind, level + 1, today),
        )
        if text:
            longer = {"level": level + 1, "text": text}
    to_kg = training.next_bell_kg(kg)
    levels = training.heavier_bell_levels(conn, user_id, kg, to_kg)
    return {
        **rated, "label": training.SESSION_LABEL_FR[kind],
        "longer": longer,
        "heavier": {"from_kg": kg, "to_kg": to_kg, "levels": [
            f"{training.SESSION_LABEL_FR[t].lower()} {a} → {b}"
            for t, (a, b) in levels.items()]},
    }


def choose(
    conn: sqlite3.Connection, user_id: int, today: str, date: str,
    kind: str, choice: str,
) -> Optional[str]:
    """Answer the offer for the session of ``date``; what to tell the
    person, or None when there is no such offer (any more)."""
    pending = offer(conn, user_id, today)
    if (choice not in CHOICES or pending is None or pending["date"] != date
            or pending["type"] != kind):
        return None
    answered = _answered(conn, user_id)
    answered[kind] = date
    db.set_setting(conn, user_id, ANSWERED_KEY, json.dumps(answered))
    conn.commit()
    if choice == "longer":
        if not pending["longer"]:
            return None
        return (f"La prochaine séance {pending['label'].lower()} sera plus "
                f"longue : {pending['longer']['text']}.")
    bell = pending["heavier"]
    db.set_setting(conn, user_id, "kettlebell_kg", f"{bell['to_kg']:g}")
    conn.commit()
    lowered = training.lower_levels_for_heavier_bell(
        conn, user_id, bell["from_kg"], bell["to_kg"])
    return (f"Kettlebell passée à {bell['to_kg']:g} kg pour toutes les séances"
            + (" ; niveaux ajustés (" + ", ".join(
                f"{training.SESSION_LABEL_FR[t].lower()} {a} → {b}"
                for t, (a, b) in lowered.items()) + ")" if lowered else "")
            + ". La durée ne monte pas cette fois.")


def send_offer(
    conn: sqlite3.Connection, user_id: int, today: str, base_url: str,
    topic: Optional[str] = None,
) -> bool:
    """Push the choice with one-tap buttons (signed links), right after
    the session was rated too easy from the notification. Without a
    known dashboard address the choice stays on the Today page.

    Returns:
        bool: Whether a notification went out.
    """
    import quick_links
    import sent_log

    pending = offer(conn, user_id, today)
    if pending is None or not base_url:
        return False
    bell = pending["heavier"]
    lines = [f"Séance {pending['label'].lower()} trop facile : pour la prochaine,"]
    actions = []
    if pending["longer"]:
        lines.append(f"- plus longue : {pending['longer']['text']} ;")
        actions.append(("longer", "Plus longue"))
    lines.append(f"- ou plus lourde : {bell['to_kg']:g} kg pour toutes les "
                 "séances, niveaux redescendus (si tu as cette kettlebell).")
    actions.append(("heavier", f"{bell['to_kg']:g} kg"))
    if pending["longer"]:
        lines.append("Sans réponse, elle sera plus longue.")
    sent_log.send(
        conn, user_id, "progress", "\n".join(lines), topic=topic,
        actions=[{"action": "http", "label": label, "method": "POST",
                  "clear": True, "url": quick_links.progress_url(
                      conn, user_id, base_url, pending["date"], choice)}
                 for choice, label in actions],
        click=f"{base_url.rstrip('/')}/#progression",
    )
    return True


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    conn = db.connect(Path(tempfile.mkdtemp()) / "choice.db")
    db.init_db(conn)
    uid = db.create_user(conn, "c", "pw-123456")
    today = "2026-10-21"

    def rate(date, kind, rating, at=None):
        conn.execute(
            "INSERT INTO session_feedback (user_id, local_date, session_type, "
            "rating, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, "
            "local_date) DO UPDATE SET rating = excluded.rating, "
            "created_at = excluded.created_at",
            (uid, date, kind, rating,
             at or dt.datetime.now(dt.timezone.utc).isoformat()))
        conn.commit()

    def plan(date, kind, level):
        conn.execute(
            "INSERT INTO coach_log (user_id, created_at, local_date, status, "
            "session_type, level, message, tier) VALUES (?, 'x', ?, 'green', "
            "?, ?, 'm', 'train')", (uid, date, kind, level))
        conn.commit()

    # No kettlebell: nothing to choose.
    plan("2026-10-20", "kettlebell", 6)
    training.set_level(conn, uid, "kettlebell", 6)
    training.set_level(conn, uid, "lower_body", 4)
    rate("2026-10-20", "kettlebell", "easy")
    assert offer(conn, uid, today) is None
    db.set_setting(conn, uid, "kettlebell_kg", "6")

    # Rated too easy at 6 kg: both ways up are offered.
    first = offer(conn, uid, today)
    assert first["type"] == "kettlebell" and first["date"] == "2026-10-20", first
    longer = first["longer"]["text"]
    assert first["longer"]["level"] == 7 and (
        "s d'effort" in longer or "tours" in longer), first
    assert first["heavier"]["to_kg"] == 8, first
    assert any(line.startswith("kettlebell a la maison 6 → ") for line in first["heavier"]["levels"]), first
    # A wrong answer (other day, unknown choice) does nothing.
    assert choose(conn, uid, today, "2026-10-19", "kettlebell", "heavier") is None
    assert choose(conn, uid, today, "2026-10-20", "kettlebell", "lighter") is None

    # Heavier: 8 kg, levels down, and the "too easy" at 6 kg no longer
    # counts, so it does not also lengthen the next session.
    note = choose(conn, uid, today, "2026-10-20", "kettlebell", "heavier")
    assert "8 kg" in note and "kettlebell a la maison 6 →" in note, note
    assert db.get_setting(conn, uid, "kettlebell_kg") == "8"
    assert training.get_level(conn, uid, "kettlebell") < 6
    assert training.recent_feedback(conn, uid, "kettlebell") == []
    assert offer(conn, uid, today) is None  # answered
    assert training.steady_progress(conn, uid, "kettlebell", today,
                                    training.get_level(conn, uid, "kettlebell")) is None

    # At 8 kg, rated too easy again: offered again, toward 10 kg.
    plan("2026-10-22", "kettlebell", training.get_level(conn, uid, "kettlebell"))
    rate("2026-10-22", "kettlebell", "easy")
    again = offer(conn, uid, "2026-10-22")
    assert again and again["heavier"]["to_kg"] == 10, again
    # Longer: the level is left to go up as before.
    note = choose(conn, uid, "2026-10-22", "2026-10-22", "kettlebell", "longer")
    assert note.startswith("La prochaine séance kettlebell a la maison"), note
    assert db.get_setting(conn, uid, "kettlebell_kg") == "8"
    assert training.recent_feedback(conn, uid, "kettlebell", limit=1) == ["easy"]
    assert offer(conn, uid, "2026-10-22") is None

    # From the notification: two signed buttons, kept on Messages.
    import notify

    pushed = []
    notify.notify_with_actions = (
        lambda text, actions, title="Smart Coach", topic=None, click=None:
        pushed.append((text, actions, click)))
    plan("2026-10-24", "kettlebell", training.get_level(conn, uid, "kettlebell"))
    rate("2026-10-24", "kettlebell", "easy")
    assert not send_offer(conn, uid, "2026-10-24", "")  # no address
    assert send_offer(conn, uid, "2026-10-24", "https://coach.example")
    text, actions, click = pushed[-1]
    assert "10 kg" in text and click.endswith("/#progression"), pushed
    assert [a["label"] for a in actions][-1] == "10 kg", actions
    assert all("/quick/progress/" in a["url"] for a in actions), actions
    assert conn.execute("SELECT kind FROM sent_messages").fetchone()["kind"] == "progress"
    assert choose(conn, uid, "2026-10-24", "2026-10-24", "kettlebell", "longer")

    # Unanswered until the next session of that type is planned: the
    # level went up that morning, the offer is gone.
    plan("2026-10-23", "lower_body", 4)
    rate("2026-10-23", "lower_body", "easy")
    assert offer(conn, uid, "2026-10-23")["type"] == "lower_body"
    plan("2026-10-25", "lower_body", 5)
    assert offer(conn, uid, "2026-10-25") is None
    # Not a loaded session (the treadmill): never offered.
    plan("2026-10-26", "treadmill", 3)
    rate("2026-10-26", "treadmill", "easy")
    assert offer(conn, uid, "2026-10-26") is None
    print("progression_choice.py: all checks passed")
