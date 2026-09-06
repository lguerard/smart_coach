"""Check everything that can break Smart Coach in silence.

    docker compose run --rm smart_coach-worker python doctor.py

Every failure this project has actually suffered was silent: the image
missing a module, the disk full, an ingestion stopped for days, tokens
expired. Nothing crashed, nothing restarted, the dashboard kept serving
yesterday's numbers. This command asks the questions nobody thinks to
ask until something is already wrong.

Exit code 0 when everything is fine, 1 when at least one check failed --
so it can run from cron and only speak up when it matters.
"""

import datetime as dt
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import db

# A Garmin token lasts about a year. Warning three weeks ahead leaves
# time to reconnect before the morning pipeline starts failing.
GARMIN_TOKEN_MAX_DAYS = 365 - 21
# Ingestion runs daily; past 30 h a run was missed.
INGEST_STALE_HOURS = 30
DISK_WARN_GB = 5

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}

results: list[tuple[str, str, str]] = []


def check(name: str, level: str, detail: str = "") -> None:
    results.append((name, level, detail))


def _age_hours(iso: str) -> float:
    when = dt.datetime.fromisoformat(iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 3600


def check_disk() -> None:
    target = db.DB_PATH.parent if db.DB_PATH.parent.exists() else Path(".")
    free_gb = shutil.disk_usage(target).free / 1024**3
    level = FAIL if free_gb < 1 else WARN if free_gb < DISK_WARN_GB else OK
    check("Espace disque", level, f"{free_gb:.1f} Go libres")


def check_database() -> sqlite3.Connection | None:
    if not db.DB_PATH.exists():
        check("Base de donnees", FAIL, f"{db.DB_PATH} absente")
        return None
    conn = db.connect()
    users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    check(
        "Base de donnees", OK if users else WARN,
        f"{users} compte(s)" if users else "aucun compte cree",
    )
    return conn


def check_ingestion(conn: sqlite3.Connection) -> None:
    """Per source: the two feeds fail independently."""
    for user in conn.execute("SELECT id, username FROM users"):
        row = conn.execute(
            "SELECT "
            "  MAX(CASE WHEN table_name LIKE 'garmin:%' THEN ran_at END) g, "
            "  MAX(CASE WHEN table_name NOT LIKE 'garmin:%' THEN ran_at END) p "
            "FROM ingest_runs WHERE user_id = ?", (user["id"],),
        ).fetchone()
        for label, value in (("Garmin", row["g"]), ("Telephone", row["p"])):
            name = f"Ingestion {label} ({user['username']})"
            if not value:
                check(name, WARN, "jamais executee")
                continue
            age = _age_hours(value)
            check(
                name, WARN if age > INGEST_STALE_HOURS else OK,
                f"il y a {age:.0f} h",
            )


def check_modules() -> None:
    """Every module the scheduled jobs import, actually importable.

    This is the check that would have caught the real one: weather.py
    and run_checkin.py existed in the repository but were missing from
    the image, so three of the four cron jobs failed every day while the
    container stayed 'Up' and the dashboard answered normally.
    """
    import importlib

    for module in (
        "run_ingest", "run_coach", "run_checkin",
        "weather", "gcal", "notify", "llm", "training", "metrics",
    ):
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 -- reported, not handled
            check(
                f"Module {module}", FAIL,
                f"{type(exc).__name__}: {exc} — image a reconstruire ?",
            )
        else:
            check(f"Module {module}", OK)


def check_garmin_tokens(conn: sqlite3.Connection) -> None:
    # Read the path rather than importing garmin_api: that import pulls
    # the whole Garmin client in, and a doctor must not fail on the very
    # thing it is meant to diagnose.
    token_root = Path(os.environ.get("GARMIN_TOKEN_DIR", "data/garmin-tokens"))
    for user in conn.execute("SELECT username FROM users"):
        name = f"Jetons Garmin ({user['username']})"
        token_dir = token_root / user["username"]
        if not token_dir.exists():
            check(name, FAIL, f"{token_dir} absent — setup_garmin.py")
            continue
        files = list(token_dir.glob("*"))
        if not files:
            check(name, FAIL, "dossier vide — setup_garmin.py")
            continue
        age_days = (
            dt.datetime.now().timestamp() - max(f.stat().st_mtime for f in files)
        ) / 86400
        check(
            name, WARN if age_days > GARMIN_TOKEN_MAX_DAYS else OK,
            f"{age_days:.0f} j"
            + (" — a renouveler bientot" if age_days > GARMIN_TOKEN_MAX_DAYS else ""),
        )


def check_calendar(conn: sqlite3.Connection) -> None:
    config = Path.home() / ".config/smart_coach"
    if not (config / "calendar_client_secret.json").exists():
        check("Google Calendar", WARN, "client OAuth absent — etape 3 du guide")
        return
    for user in conn.execute("SELECT username FROM users"):
        token = config / f"calendar_token_{user['username']}.json"
        check(
            f"Calendrier ({user['username']})",
            OK if token.exists() else WARN,
            "" if token.exists() else "consentement manquant — setup_calendar.py",
        )


def check_rclone() -> None:
    remote = os.environ.get("RCLONE_REMOTE", "")
    if not remote:
        check("rclone", FAIL, "RCLONE_REMOTE absent du .env")
        return
    conf = Path.home() / ".config/rclone/rclone.conf"
    if not conf.exists():
        check("rclone", FAIL, "aucune configuration — rclone config")
        return
    name = remote.split(":", 1)[0]
    known = f"[{name}]" in conf.read_text()
    check(
        "rclone", OK if known else FAIL,
        f"remote {name!r}" + ("" if known else " absent de rclone.conf"),
    )


def check_llm() -> None:
    provider = os.environ.get("LLM_PROVIDER", "claude_cli")
    if provider == "claude_cli":
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        ok = bool(token) and token != "change-me"
        check("Claude", OK if ok else FAIL,
              "" if ok else "CLAUDE_CODE_OAUTH_TOKEN absent — claude setup-token")
    else:
        ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
        check("Claude (API)", OK if ok else FAIL,
              "" if ok else "ANTHROPIC_API_KEY absent")


def check_notifications() -> None:
    topic = os.environ.get("NTFY_TOPIC", "")
    ok = bool(topic) and "change-me" not in topic
    check("ntfy", OK if ok else WARN,
          "" if ok else "NTFY_TOPIC non renseigne — aucune notification")


def main() -> int:
    check_disk()
    check_rclone()
    check_llm()
    check_notifications()
    check_modules()
    conn = check_database()
    if conn is not None:
        check_ingestion(conn)
        check_garmin_tokens(conn)
        check_calendar(conn)

    width = max(len(name) for name, _, _ in results)
    for name, level, detail in results:
        print(f"[{_MARK[level]}] {name.ljust(width)}  {detail}")

    failed = sum(1 for _, level, _ in results if level == FAIL)
    warned = sum(1 for _, level, _ in results if level == WARN)
    print()
    if failed:
        print(f"{failed} probleme(s) bloquant(s), {warned} avertissement(s).")
        print("Detail des procedures : docs/TECHNICAL.md #Setup")
        return 1
    print(f"Tout est en ordre ({warned} avertissement(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
