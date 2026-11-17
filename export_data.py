#!/usr/bin/env python3
"""Everything one account has, as one file: the Données page's export.

Every table holding a ``user_id`` column is exported, this account's
rows only, as JSON (one file) or as CSV (one per table, in a zip). Left
out on purpose:

* ``passkeys``: credentials, of no use outside this server;
* ``body_photos``: the private photos stay on the server, encrypted
  (they are deleted from the Corps page);
* settings that open something to whoever reads the file: the ntfy
  topic (anyone knowing it reads the notifications), the rclone remote,
  and any key naming a token, secret, password or key.
"""

import csv
import datetime as dt
import io
import json
import sqlite3
import zipfile

LEFT_OUT_TABLES = {"passkeys", "body_photos"}
LEFT_OUT_SETTINGS = {"ntfy_topic", "rclone_remote"}
SECRET_WORDS = ("token", "secret", "password", "key")


def tables(conn: sqlite3.Connection) -> list[str]:
    """The tables with a ``user_id`` column, less the ones left out."""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT "
        "LIKE 'sqlite_%' ORDER BY name")]
    return [n for n in names if n not in LEFT_OUT_TABLES and any(
        col[1] == "user_id" for col in conn.execute(f'PRAGMA table_info("{n}")'))]


def _rows(conn, user_id: int, table: str) -> tuple[list[str], list[tuple]]:
    cursor = conn.execute(f'SELECT * FROM "{table}" WHERE user_id = ?', (user_id,))
    columns = [d[0] for d in cursor.description]
    rows = [tuple(r) for r in cursor.fetchall()]
    if table == "settings" and "key" in columns:
        k = columns.index("key")
        rows = [r for r in rows if r[k] not in LEFT_OUT_SETTINGS
                and not any(w in str(r[k]).lower() for w in SECRET_WORDS)]
    return columns, rows


def as_json(conn: sqlite3.Connection, user_id: int) -> bytes:
    """``{exported_at, left_out, tables: {name: [row, ...]}}``, UTF-8."""
    out = {"exported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "left_out": sorted(LEFT_OUT_TABLES) + ["secret settings"], "tables": {}}
    for table in tables(conn):
        columns, rows = _rows(conn, user_id, table)
        if rows:
            out["tables"][table] = [dict(zip(columns, r)) for r in rows]
    return json.dumps(out, ensure_ascii=False, indent=1, default=str).encode("utf-8")


def as_zip(conn: sqlite3.Connection, user_id: int) -> bytes:
    """One CSV per non-empty table (UTF-8 with BOM, so a spreadsheet
    opens the accents right), zipped."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for table in tables(conn):
            columns, rows = _rows(conn, user_id, table)
            if not rows:
                continue
            text = io.StringIO()
            writer = csv.writer(text)
            writer.writerow([c for c in columns if c != "user_id"])
            keep = [i for i, c in enumerate(columns) if c != "user_id"]
            for row in rows:
                writer.writerow([row[i] for i in keep])
            archive.writestr(f"{table}.csv", "﻿" + text.getvalue())
    return buffer.getvalue()


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db

    conn = db.connect(Path(tempfile.mkdtemp()) / "export.db")
    db.init_db(conn)
    uid = db.create_user(conn, "e", "pw-123456")
    other = db.create_user(conn, "o", "pw-123456")
    conn.execute("INSERT INTO weight VALUES ('w1', ?, '2026-11-01T07:00:00+00:00', "
                 "'2026-11-01', 81.5)", (uid,))
    conn.execute("INSERT INTO weight VALUES ('w2', ?, '2026-11-01T07:00:00+00:00', "
                 "'2026-11-01', 64.0)", (other,))
    db.set_setting(conn, uid, "step_goal", "10000")
    db.set_setting(conn, uid, "ntfy_topic", "secret-topic-123")
    db.set_setting(conn, uid, "garmin_token", "abc")
    conn.commit()

    assert "passkeys" not in tables(conn) and "body_photos" not in tables(conn)
    data = json.loads(as_json(conn, uid))
    assert [w["kg"] if "kg" in w else list(w.values())[-1] for w in data["tables"]["weight"]] == [81.5]
    keys = {s["key"] for s in data["tables"]["settings"]}
    assert "step_goal" in keys and "ntfy_topic" not in keys and "garmin_token" not in keys
    raw = as_json(conn, uid).decode()
    assert "secret-topic-123" not in raw and "64.0" not in raw

    archive = zipfile.ZipFile(io.BytesIO(as_zip(conn, uid)))
    assert "weight.csv" in archive.namelist()
    weight_csv = archive.read("weight.csv").decode("utf-8-sig")
    assert "81.5" in weight_csv and "64.0" not in weight_csv and "user_id" not in weight_csv
    assert "secret-topic-123" not in archive.read("settings.csv").decode("utf-8-sig")
    print("export_data.py: all checks passed")
