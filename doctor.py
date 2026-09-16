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
import re
import shutil
import sqlite3
import subprocess
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
    if ":" not in remote:
        # rclone reads a colonless value as a local path, so it fails
        # with "directory not found" instead of anything that points
        # at the real mistake.
        check(
            "rclone", FAIL,
            f"RCLONE_REMOTE='{remote}' sans ':' — rclone y voit un "
            "dossier local. Utilisez '<remote>:<dossier>', ex. "
            "'gdrive:HealthConnectExports' ; "
            "'rclone lsd gdrive:' liste les dossiers disponibles",
        )
        return
    name = remote.split(":", 1)[0]
    if f"[{name}]" not in conf.read_text():
        check("rclone", FAIL, f"remote {name!r} absent de rclone.conf")
        return
    # A valid-looking config still fails at 07:15 for reasons no file
    # inspection can see -- the Drive API disabled on the Cloud
    # project, a revoked token, a folder that moved. Actually listing
    # the remote is the only check that catches those, and catching
    # them here beats finding out from a silent morning.
    check("rclone", *_rclone_reachable(remote))


def _rclone_reachable(remote: str) -> tuple[str, str]:
    """Try listing the remote, and translate the usual failures.

    Parameters:
        remote (str): Full ``name:folder`` rclone path.

    Returns:
        tuple[str, str]: ``(level, detail)`` for :func:`check`.
    """
    # --max-depth only means something for a folder; a remote naming
    # the export zip itself is listed as-is.
    args = ["rclone", "lsjson", remote]
    if not remote.lower().endswith(".zip"):
        args[2:2] = ["--max-depth", "1"]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=45,
        )
    except FileNotFoundError:
        return FAIL, "binaire rclone introuvable"
    except subprocess.TimeoutExpired:
        return WARN, f"{remote} — pas de reponse en 45 s"
    if result.returncode == 0:
        return OK, remote
    error = " ".join(result.stderr.split())
    if "SERVICE_DISABLED" in error or "has not been used in project" in error:
        project = ""
        match = re.search(r"project (\d+)", error)
        if match:
            project = (
                " — activez-la sur console.developers.google.com/apis/"
                f"api/drive.googleapis.com/overview?project={match.group(1)}"
            )
        return FAIL, f"API Google Drive desactivee sur le projet OAuth{project}"
    if "directory not found" in error:
        return FAIL, f"{remote} introuvable — verifiez le nom du dossier"
    if "token" in error.lower() or "oauth" in error.lower():
        return FAIL, f"autorisation refusee — rclone config reconnect {name_of(remote)}"
    return FAIL, f"rclone: {error[:160]}"


def name_of(remote: str) -> str:
    """Remote name (the part before the colon) of an rclone path."""
    return remote.split(":", 1)[0]


def check_rclone_remote_setting(conn: sqlite3.Connection) -> None:
    """Per-user rclone_remote -- run_ingest.py silently skips the whole
    Health Connect import (steps/nutrition/hydration/weight) for any
    account where this is unset, with no dashboard-visible sign of it.
    """
    conf = Path.home() / ".config/rclone/rclone.conf"
    known = conf.read_text() if conf.exists() else ""
    for user in conn.execute("SELECT id, username FROM users"):
        name = f"Remote rclone ({user['username']})"
        remote = db.get_setting(conn, user["id"], "rclone_remote")
        if not remote:
            check(
                name, FAIL,
                "non configure — Reglages > Remote rclone, sinon pas/"
                "nutrition/hydratation/poids restent vides",
            )
        elif ":" not in remote:
            # Without a colon rclone treats it as a local path and
            # fails with "directory not found", which points nowhere
            # near the actual mistake.
            check(
                name, FAIL,
                f"'{remote}' sans ':' — rclone y voit un dossier local. "
                "Utilisez '<remote>:<dossier>', ex. "
                "'gdrive:HealthConnectExports'",
            )
        elif f"[{remote.split(':', 1)[0]}]" not in known:
            check(
                name, FAIL,
                f"remote '{remote.split(':', 1)[0]}' absent de "
                "rclone.conf — rclone config",
            )
        else:
            check(name, OK, remote)


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
        check_rclone_remote_setting(conn)

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
