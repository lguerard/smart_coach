#!/usr/bin/env python3
"""The other direction: edits made in Garmin Connect come back.

Every workout smart_coach puts on the watch is stored with its shape
(``garmin_planned_workouts.shape``): each step, the slot it came from,
its count. Before the morning run and before "Envoyer sur la montre"
re-sends anything, the planned workouts are read back from Garmin and
compared with that shape. What the person changed there is not
overwritten but taken into their programme (``custom_moves``):

- a move added            -> one of their own moves (in every tour, or
                             once at the end when added after the tours);
- a move removed          -> taken out of the circuit (or their own move
                             deleted);
- a count changed         -> fixed at that count (their own move's
                             amount, or a library move's override);
- the number of tours     -> fixed at that number;
- the workout deleted     -> that day is left without a workout: the
                             coach does not put it back.

Kettlebell changes apply to that day's circuit (A or B). Every change is
logged in ``watch_edits`` and shown on the Programme page, where it can
be undone like any other edit. Treadmill and recovery walks (one step,
targets in the text) are not compared.
"""

import datetime as dt
import json
import sqlite3
from typing import Optional

import custom_moves
import exercise_library

USER_DELETED = "deleted-in-garmin"  # signature of a day the person emptied
_NON_STEP = {"rounds", "duration_min", "variants", "rest_sec", "tours",
             "finisher"}


def _unit(step: dict) -> Optional[str]:
    key = (step.get("endCondition") or {}).get("conditionTypeKey")
    return {"time": "sec", "reps": "reps"}.get(key)


def _is_repeat(step: dict) -> bool:
    return bool(step.get("workoutSteps")) and "numberOfIterations" in step


def _move(step: dict) -> dict:
    return {
        "named": bool((step.get("description") or "").strip()),
        "name": (step.get("description") or "").strip()
        or (step.get("exerciseName") or step.get("category") or "").replace("_", " ").lower(),
        "garmin": [step.get("category") or "", step.get("exerciseName") or ""],
        "unit": _unit(step),
        "value": int(round(float(step.get("endConditionValue") or 0))),
    }


def shape_of(workout: dict) -> dict:
    """A workout's steps, as Garmin shows them: ``rounds`` (repeat count,
    None without a repeat), ``moves`` (in every tour), ``after`` (once,
    after the tours). Rest steps are not moves.
    """
    rounds, moves, after = None, [], []
    for segment in workout.get("workoutSegments") or []:
        for step in segment.get("workoutSteps") or []:
            if _is_repeat(step):
                if rounds is None:
                    rounds = int(step.get("numberOfIterations") or 1)
                    moves = [
                        _move(s) for s in step["workoutSteps"]
                        if (s.get("stepType") or {}).get("stepTypeKey") != "rest"
                        and not _is_repeat(s)
                    ]
                continue
            if (step.get("stepType") or {}).get("stepTypeKey") == "rest":
                continue
            (after if rounds is not None else moves).append(_move(step))
    return {"rounds": rounds, "moves": moves, "after": after}


def sent_shape(values: dict, workout: dict) -> dict:
    """``shape_of`` the workout just built, each step tagged with the
    values key (slot) it came from, in the order they were built."""
    shape = shape_of(workout)
    slots = [k for k in values if k not in _NON_STEP]
    for move, slot in zip(shape["moves"], slots):
        move["slot"] = slot
    for move, item in zip(shape["after"], values.get("finisher") or []):
        move["slot"] = item["slot"]
    return shape


def _match(sent: list[dict], remote: list[dict]):
    """Pair sent and remote moves by name, then by Garmin exercise;
    returns (pairs, removed sent moves, added remote moves)."""
    remote_left = list(remote)
    pairs, removed = [], []
    for index, move in enumerate(sent):
        found = next((r for r in remote_left if r["name"] == move["name"]), None)
        if found is None:
            found = next(
                (r for r in remote_left if r["garmin"] == move["garmin"]
                 and any(move["garmin"])), None,
            )
        if found is None and index < len(remote) and remote[index] in remote_left \
                and not remote[index].get("named") \
                and remote[index]["unit"] == move["unit"]:
            # Same place, same kind of step, its name gone (Garmin may
            # not hand descriptions back): the same move, not an edit.
            found = remote[index]
        if found is None:
            removed.append(move)
        else:
            remote_left.remove(found)
            pairs.append((move, found))
    return pairs, removed, remote_left


def diff(sent: dict, remote: dict) -> list[dict]:
    """What changed between what was sent and what Garmin has now.

    Returns:
        list[dict]: ``kind`` (``removed`` / ``added`` / ``amount`` /
        ``rounds``) with ``slot``, ``name``, ``unit``, ``value``,
        ``placement`` (``round`` / ``end``) and ``garmin`` as relevant.
    """
    changes = []
    if sent.get("rounds") and remote.get("rounds") and sent["rounds"] != remote["rounds"]:
        changes.append({"kind": "rounds", "value": remote["rounds"]})
    for placement, key in (("round", "moves"), ("end", "after")):
        pairs, removed, added = _match(sent.get(key) or [], remote.get(key) or [])
        for move in removed:
            changes.append({"kind": "removed", "placement": placement, **move})
        for move in added:
            if move["unit"] and move["value"] > 0:
                changes.append({"kind": "added", "placement": placement, **move})
        for move, now in pairs:
            if now["unit"] == move["unit"] and now["value"] != move["value"] and now["value"] > 0:
                changes.append({"kind": "amount", "placement": placement,
                                **move, "value": now["value"]})
    return changes


def _garmin_key(pair: list) -> str:
    for key, (_, known) in custom_moves.GARMIN_PICKER.items():
        if known and list(known) == list(pair):
            return key
    return ""


def _own_id(slot: str) -> Optional[int]:
    if not slot.startswith("custom_"):
        return None
    try:
        return int(slot.split("_")[1])
    except (IndexError, ValueError):
        return None


def apply_changes(
    conn: sqlite3.Connection, user_id: int, session_type: str, circuit: str,
    changes: list[dict],
) -> list[str]:
    """Take ``diff`` changes into the programme; French lines on what
    was done, one per change that could be applied."""
    unit_fr = {"sec": " s", "reps": ""}
    done = []
    for change in changes:
        kind, slot = change["kind"], change.get("slot", "")
        own = _own_id(slot)
        try:
            if kind == "rounds":
                custom_moves.set_override(
                    conn, user_id, session_type, circuit, "rounds", change["value"],
                )
                done.append(f"{change['value']} tours au lieu de l'habituel")
            elif kind == "removed" and own is not None:
                custom_moves.delete_move(conn, user_id, own)
                done.append(f"« {change['name']} » retiré")
            elif kind == "removed" and slot:
                custom_moves.set_removed(conn, user_id, session_type, circuit, slot, True)
                done.append(f"« {change['name']} » retiré du programme")
            elif kind == "added":
                custom_moves.add_move(
                    conn, user_id, session_type, circuit, change["name"][:60]
                    or "Mouvement ajouté sur Garmin", "", change["unit"],
                    change["value"], change["placement"], _garmin_key(change["garmin"]),
                )
                where = "à chaque tour" if change["placement"] == "round" else "à la fin"
                done.append(f"« {change['name']} » ajouté ({change['value']}"
                            f"{unit_fr[change['unit']]}, {where})")
            elif kind == "amount" and own is not None:
                custom_moves.set_move_amount(conn, user_id, own, change["value"])
                done.append(f"« {change['name']} » passé à {change['value']}"
                            f"{unit_fr.get(change['unit'], '')}")
            elif kind == "amount" and slot:
                custom_moves.set_override(
                    conn, user_id, session_type, circuit, slot, change["value"],
                )
                done.append(f"« {change['name']} » fixé à {change['value']}"
                            f"{unit_fr.get(change['unit'], '')}")
        except ValueError:
            continue  # out of range or unknown: left as the coach had it
    return done


def _log(conn, user_id: int, date: str, session_type: Optional[str], line: str) -> None:
    conn.execute(
        "INSERT INTO watch_edits (user_id, local_date, session_type, change, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, date, session_type, line,
         dt.datetime.now(dt.timezone.utc).isoformat()),
    )


def _missing(error: Exception) -> bool:
    text = str(error).lower()
    return "404" in text or "not found" in text


def import_watch_edits(
    conn: sqlite3.Connection, user_id: int, client, today: str,
) -> list[dict]:
    """Read back every planned workout from today on and take in what
    the person changed in Garmin Connect.

    Best-effort per day: a day Garmin cannot be read for is skipped, not
    treated as edited. A workout gone from Garmin is a day emptied on
    purpose (``USER_DELETED``).

    Returns:
        list[dict]: ``{date, session_type, changes}`` per day where
        something was taken in (``changes`` in French).
    """
    import training

    events = []
    rows = conn.execute(
        "SELECT local_date, workout_id, kind, shape FROM garmin_planned_workouts "
        "WHERE user_id = ? AND local_date >= ? AND shape IS NOT NULL "
        "AND signature != ? ORDER BY local_date",
        (user_id, today, USER_DELETED),
    ).fetchall()
    for row in rows:
        date, kind = row["local_date"], row["kind"]
        try:
            remote_json = client.get_workout_by_id(row["workout_id"])
        except Exception as error:  # noqa: BLE001 -- Garmin side, per day
            if not _missing(error):
                continue
            conn.execute(
                "UPDATE garmin_planned_workouts SET signature = ? WHERE "
                "user_id = ? AND local_date = ?", (USER_DELETED, user_id, date),
            )
            line = "séance supprimée sur Garmin : le coach ne la remet pas"
            _log(conn, user_id, date, kind, line)
            conn.commit()
            events.append({"date": date, "session_type": kind, "changes": [line]})
            continue
        if kind not in custom_moves.EDITABLE_TYPES:
            continue
        sent = json.loads(row["shape"])
        remote = shape_of(remote_json or {})
        changes = diff(sent, remote)
        if not changes:
            continue
        circuit = (training.kettlebell_circuit(
            date, exercise_library.equipment_for(conn, user_id))
            if kind == "kettlebell" else "")
        lines = apply_changes(conn, user_id, kind, circuit, changes)
        # What is on Garmin now is what was "sent": the same edit is not
        # taken in twice, and the next sync only re-sends real changes.
        for move, now in zip(sent.get("moves") or [], remote["moves"]):
            if move["name"] == now["name"]:
                now["slot"] = move.get("slot")
        conn.execute(
            "UPDATE garmin_planned_workouts SET shape = ? WHERE user_id = ? "
            "AND local_date = ?", (json.dumps(remote), user_id, date),
        )
        for line in lines:
            _log(conn, user_id, date, kind, line)
        conn.commit()
        if lines:
            events.append({"date": date, "session_type": kind, "changes": lines})
    return events


def recent_edits(conn: sqlite3.Connection, user_id: int, limit: int = 10) -> list:
    """The last changes taken in from Garmin, newest first."""
    return conn.execute(
        "SELECT * FROM watch_edits WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


if __name__ == "__main__":
    import copy
    import tempfile
    from pathlib import Path

    import db
    import training
    from ingest import garmin_api

    conn = db.connect(Path(tempfile.mkdtemp()) / "watch.db")
    db.init_db(conn)
    uid = db.create_user(conn, "w", "pw-123456")
    db.set_setting(conn, uid, "kettlebell_kg", "12")
    day = "2026-10-17"
    circuit = training.kettlebell_circuit(day)

    class _Garmin:
        """Garmin Connect as the person left it: the uploaded workouts,
        editable, deletable."""

        def __init__(self):
            self.store, self.next_id = {}, 0

        def upload_workout(self, data):
            self.next_id += 1
            self.store[str(self.next_id)] = copy.deepcopy(data)
            return {"workoutId": self.next_id}

        def schedule_workout(self, workout_id, date):
            return {}

        def delete_workout(self, workout_id):
            self.store.pop(str(workout_id), None)

        def get_workout_by_id(self, workout_id):
            if str(workout_id) not in self.store:
                raise RuntimeError("404 Client Error: Not Found")
            return copy.deepcopy(self.store[str(workout_id)])

    watch = _Garmin()
    values = training.user_session_values(conn, uid, "kettlebell", 2, day)
    assert garmin_api.sync_planned_workout(
        conn, uid, watch, day, "kettlebell", 2, values) == "created"
    # Nothing edited: nothing to take in -- even when Garmin hands the
    # workout back without the step descriptions.
    assert import_watch_edits(conn, uid, watch, day) == []
    stored_id = conn.execute(
        "SELECT workout_id FROM garmin_planned_workouts").fetchone()[0]
    bare = copy.deepcopy(watch.store[stored_id])
    for step in bare["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"]:
        step.pop("description", None)
    assert diff(json.loads(conn.execute(
        "SELECT shape FROM garmin_planned_workouts").fetchone()[0]),
        shape_of(bare)) == []

    # In Garmin Connect: 6 tours instead of 5, the first move dropped,
    # the curl set to 40 s, a triceps stretch added after the tours.
    workout_id = conn.execute(
        "SELECT workout_id FROM garmin_planned_workouts").fetchone()[0]
    edited = watch.store[workout_id]
    group = edited["workoutSegments"][0]["workoutSteps"][0]
    group["numberOfIterations"] = 6
    first = group["workoutSteps"][0]["description"]
    del group["workoutSteps"][0:2]  # the move and its rest
    curl = next(s for s in group["workoutSteps"]
                if "curl" in (s.get("description") or ""))
    curl["endConditionValue"] = 40.0
    edited["workoutSegments"][0]["workoutSteps"].append({
        "type": "ExecutableStepDTO", "stepOrder": 2,
        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
        "endConditionValue": 30.0, "category": "WARM_UP",
        "exerciseName": "TRICEPS_STRETCH", "description": "Etirement triceps",
    })
    events = import_watch_edits(conn, uid, watch, day)
    assert len(events) == 1, events
    said = " | ".join(events[0]["changes"])
    assert "6 tours" in said and first in said and "40 s" in said, said
    assert "Etirement triceps" in said and "à la fin" in said, said

    # The programme now builds what the person made in Garmin...
    rebuilt = training.user_session_values(conn, uid, "kettlebell", 2, day)
    first_slot = training.KETTLEBELL_CIRCUITS[circuit][0]
    assert first_slot not in rebuilt and rebuilt["tours"] == 6, rebuilt
    assert rebuilt["finisher"][0]["garmin"] == ["WARM_UP", "TRICEPS_STRETCH"]
    curl_slot = next(k for k in rebuilt if "curl" in k)
    assert rebuilt[curl_slot] == 40, rebuilt
    # ...the same edit is not taken in twice...
    assert import_watch_edits(conn, uid, watch, day) == []
    # ...and it is logged for the Programme page.
    assert len(recent_edits(conn, uid)) == 4, [dict(r) for r in recent_edits(conn, uid)]

    # Deleted in Garmin: that day stays empty, the coach does not put it
    # back, even with a different workout.
    watch.store.clear()
    gone = import_watch_edits(conn, uid, watch, day)
    assert "supprimée" in gone[0]["changes"][0], gone
    assert garmin_api.sync_planned_workout(
        conn, uid, watch, day, "kettlebell", 3, rebuilt) == "user_deleted"
    assert watch.store == {}
    print("watch_sync.py: all checks passed")
