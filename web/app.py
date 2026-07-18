#!/usr/bin/env python3
"""smart_sport dashboard: FastAPI + Jinja2 + HTMX + Chart.js (CDN).

Six pages -- Home (today), Progress (weight/calorie/protein +
plateau), Trends (steps/RHR/sleep/level history), Sessions (exercise
log), Achievements (Player Level/XP/Coach Score), Settings (goals +
ingestion health) -- behind a login. No build step: Tailwind and
Chart.js load from CDN, HTMX handles the one bit of interactivity
(the Home page's regenerate button).

Multi-user: a signed session cookie carries ``user_id``; every route
scopes its queries to that person's rows only via the modules'
user_id-aware functions. A lightweight ASGI middleware gates every
path except /login and /static behind having a valid session.
"""

import datetime as dt
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from pathlib import Path

import webauthn
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
    ResidentKeyRequirement, UserVerificationRequirement,
)

import achievements
import db
import gcal
import llm
import metrics
import progress
import training
import training_load
from ingest.parse_health_connect import EXERCISE_TYPE_LABELS

APP_DIR = Path(__file__).parent
app = FastAPI(title="smart_sport")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

_PUBLIC_PREFIXES = ("/login", "/static", "/signup", "/passkey/login")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Gate every path except /login and /static behind a session."""
    if request.url.path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)
    if not request.session.get("user_id"):
        return RedirectResponse("/login")
    return await call_next(request)


# Registered AFTER require_login: Starlette middlewares wrap in LIFO
# order (last-added = outermost = runs first), so SessionMiddleware
# must be added after the custom middleware to actually run before it
# -- otherwise require_login sees a scope with no "session" key yet.
# ponytail: if SESSION_SECRET isn't set, sessions won't survive a
# container restart (everyone gets logged out) -- fine for dev, but
# set it explicitly in .env for a real deployment so logins persist.
# COOKIE_SECURE=1 (set it whenever the app is served over HTTPS, e.g.
# behind the compose file's caddy profile) marks the session cookie
# Secure so it is never sent over plain HTTP.
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
app.add_middleware(
    SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax",
    https_only=os.environ.get("COOKIE_SECURE") == "1",
)


def get_conn() -> sqlite3.Connection:
    """Open a request-scoped db connection with the schema ensured.

    Returns:
        sqlite3.Connection: Ready-to-query connection.
    """
    conn = db.connect()
    db.init_db(conn)
    return conn


def current_user_id(request: Request) -> int:
    """The logged-in user's id (guaranteed present -- require_login
    already redirected anyone without one).
    """
    return request.session["user_id"]


def today_str(conn: sqlite3.Connection, user_id: int) -> str:
    """Today's date in the user's configured local timezone."""
    return dt.datetime.now(metrics.local_tz(conn, user_id)).date().isoformat()


def latest_coach_entry(
    conn: sqlite3.Connection, user_id: int, date: str,
) -> sqlite3.Row | None:
    """Today's coach_log row, if the daily cron has already run."""
    return conn.execute(
        "SELECT * FROM coach_log WHERE user_id = ? AND local_date = ? "
        "ORDER BY id DESC LIMIT 1", (user_id, date),
    ).fetchone()


def last_ingest_status(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Most recent ingestion row count per table, for the Settings page."""
    rows = conn.execute(
        "SELECT table_name, row_count, MAX(ran_at) AS ran_at "
        "FROM ingest_runs WHERE user_id = ? GROUP BY table_name "
        "ORDER BY table_name", (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# --- Auth ---

# Brute-force throttle for the public login form: after
# LOGIN_MAX_FAILURES failed attempts from the same client IP within
# LOGIN_WINDOW_SEC, further attempts are rejected until the window
# slides past. In-memory on purpose.
# ponytail: per-process state -- resets on restart and doesn't share
# across workers. The web service runs a single uvicorn process; move
# this to a db table if that ever changes.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SEC = 900
_login_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """Best-effort client IP, proxy-aware.

    Behind the caddy profile the first X-Forwarded-For entry is the
    real client; without a proxy the header is absent and the socket
    peer is authoritative. (A direct attacker could forge the header,
    but then they control the source IP anyway -- per-key throttling
    still holds.)
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _throttled(key: str, now: float) -> bool:
    """True if this key has exhausted its failure budget."""
    attempts = [
        t for t in _login_failures.get(key, ())
        if now - t < LOGIN_WINDOW_SEC
    ]
    _login_failures[key] = attempts
    return len(attempts) >= LOGIN_MAX_FAILURES


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    """Login form. Also redirects home if already logged in."""
    if request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request, "login.html", {"error": request.query_params.get("error")},
    )


@app.post("/login")
async def login_submit(request: Request):
    """Verify credentials and start a session (rate-limited)."""
    key = _client_ip(request)
    now = time.monotonic()
    if _throttled(key, now):
        return RedirectResponse(
            "/login?error=Trop+de+tentatives+--+reessayez+dans+15+min",
            status_code=303,
        )
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    conn = get_conn()
    user = db.verify_login(conn, username, password)
    if user is None:
        _login_failures.setdefault(key, []).append(now)
        return RedirectResponse(
            "/login?error=Identifiants+incorrects", status_code=303,
        )
    if not user["approved"]:
        # Valid credentials, pending account: different message, and
        # no failure recorded (they typed the right password).
        return RedirectResponse(
            "/login?error=Compte+en+attente+d'approbation+par+l'admin",
            status_code=303,
        )
    _login_failures.pop(key, None)
    request.session["user_id"] = user["id"]
    request.session["username"] = username
    return RedirectResponse("/", status_code=303)


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request) -> HTMLResponse:
    """Self-signup form: account is created pending admin approval."""
    if request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request, "signup.html", {
            "error": request.query_params.get("error"),
            "created": request.query_params.get("created") is not None,
        },
    )


@app.post("/signup")
async def signup_submit(request: Request):
    """Create a pending account (rate-limited like the login form)."""
    key = "signup:" + _client_ip(request)
    now = time.monotonic()
    if _throttled(key, now):
        return RedirectResponse(
            "/signup?error=Trop+de+tentatives+--+reessayez+plus+tard",
            status_code=303,
        )
    _login_failures.setdefault(key, []).append(now)
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    if not (1 <= len(username) <= 40):
        return RedirectResponse(
            "/signup?error=Nom+d'utilisateur+invalide", status_code=303,
        )
    if len(password) < 8:
        return RedirectResponse(
            "/signup?error=Mot+de+passe+trop+court+(8+caracteres+min)",
            status_code=303,
        )
    conn = get_conn()
    try:
        db.create_user(conn, username, password, approved=False)
    except ValueError:
        return RedirectResponse(
            "/signup?error=Nom+d'utilisateur+deja+pris", status_code=303,
        )
    return RedirectResponse("/signup?created=1", status_code=303)


@app.post("/logout")
def logout(request: Request):
    """End the session."""
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- Passkeys (WebAuthn) ---
# Requires a secure context in the browser (HTTPS, or localhost in
# dev) -- exactly what the compose 'public' profile provides.

def _relying_party(request: Request) -> tuple[str, str]:
    """(rp_id, expected_origin) derived from the request.

    Behind the caddy profile X-Forwarded-Proto is https; direct
    LAN/dev access falls back to the request's own scheme.
    """
    host_header = request.headers.get("host", "localhost")
    rp_id = host_header.split(":")[0]
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return rp_id, f"{proto}://{host_header}"


@app.get("/passkey/register/options")
def passkey_register_options(request: Request) -> Response:
    """WebAuthn creation options for the logged-in user."""
    conn = get_conn()
    user_id = current_user_id(request)
    rp_id, _ = _relying_party(request)
    options = webauthn.generate_registration_options(
        rp_id=rp_id, rp_name="Smart Sport",
        user_id=str(user_id).encode(),
        user_name=request.session.get("username", str(user_id)),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(row["credential_id"]),
            )
            for row in db.passkeys_for_user(conn, user_id)
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    request.session["pk_challenge"] = bytes_to_base64url(options.challenge)
    return Response(
        webauthn.options_to_json(options), media_type="application/json",
    )


@app.post("/passkey/register")
async def passkey_register(request: Request):
    """Verify the authenticator's response and store the credential."""
    conn = get_conn()
    user_id = current_user_id(request)
    challenge = request.session.pop("pk_challenge", None)
    if not challenge:
        return JSONResponse({"error": "challenge expiree"}, status_code=400)
    body = await request.json()
    rp_id, origin = _relying_party(request)
    try:
        verification = webauthn.verify_registration_response(
            credential=body["credential"],
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id, expected_origin=origin,
        )
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    db.add_passkey(
        conn, user_id, bytes_to_base64url(verification.credential_id),
        verification.credential_public_key, verification.sign_count,
        str(body.get("label", ""))[:60],
    )
    return {"ok": True}


@app.post("/passkey/delete")
async def passkey_delete(request: Request):
    """Remove one of the logged-in user's passkeys."""
    conn = get_conn()
    form = await request.form()
    db.delete_passkey(
        conn, current_user_id(request),
        str(form.get("credential_id", "")),
    )
    return RedirectResponse("/settings", status_code=303)


@app.get("/passkey/login/options")
def passkey_login_options(request: Request) -> Response:
    """WebAuthn request options (discoverable credentials: the
    browser picks the passkey, no username typed)."""
    rp_id, _ = _relying_party(request)
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    request.session["pk_challenge"] = bytes_to_base64url(options.challenge)
    return Response(
        webauthn.options_to_json(options), media_type="application/json",
    )


@app.post("/passkey/login")
async def passkey_login(request: Request):
    """Verify a passkey assertion and start a session."""
    key = "pk:" + _client_ip(request)
    now = time.monotonic()
    if _throttled(key, now):
        return JSONResponse(
            {"error": "Trop de tentatives -- reessayez dans 15 min"},
            status_code=429,
        )
    challenge = request.session.pop("pk_challenge", None)
    if not challenge:
        return JSONResponse({"error": "challenge expiree"}, status_code=400)
    body = await request.json()
    credential = body.get("credential", {})
    conn = get_conn()
    row = db.passkey_by_credential(conn, str(credential.get("id", "")))
    if row is None:
        _login_failures.setdefault(key, []).append(now)
        return JSONResponse({"error": "passkey inconnue"}, status_code=400)
    rp_id, origin = _relying_party(request)
    try:
        verification = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id, expected_origin=origin,
            credential_public_key=row["public_key"],
            credential_current_sign_count=row["sign_count"],
        )
    except Exception as error:
        _login_failures.setdefault(key, []).append(now)
        return JSONResponse({"error": str(error)}, status_code=400)
    user = db.get_user(conn, row["user_id"])
    db.set_passkey_sign_count(
        conn, row["credential_id"], verification.new_sign_count,
    )
    _login_failures.pop(key, None)
    request.session["user_id"] = row["user_id"]
    request.session["username"] = user["username"]
    return {"ok": True}


# --- Admin: signup approvals ---

@app.post("/admin/users/{user_id}/approve")
def admin_approve(user_id: int, request: Request):
    """Approve a pending signup (admin only)."""
    conn = get_conn()
    if not db.is_admin(conn, current_user_id(request)):
        return RedirectResponse("/", status_code=303)
    db.approve_user(conn, user_id)
    return RedirectResponse("/settings", status_code=303)


@app.post("/admin/users/{user_id}/reject")
def admin_reject(user_id: int, request: Request):
    """Reject (delete) a pending signup (admin only; approved
    accounts are not deletable through this)."""
    conn = get_conn()
    if not db.is_admin(conn, current_user_id(request)):
        return RedirectResponse("/", status_code=303)
    db.reject_user(conn, user_id)
    return RedirectResponse("/settings", status_code=303)


# --- Home ---

@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    """Today: readiness chip, tonight's session, the coaching message."""
    conn = get_conn()
    user_id = current_user_id(request)
    date = today_str(conn, user_id)
    entry = latest_coach_entry(conn, user_id, date)

    session_values = None
    description = None
    if entry and entry["session_type"]:
        session_values = training.session_values(
            entry["session_type"], entry["level"],
            training.session_cap_min(conn, user_id),
        )
        description = training.format_description_fr(
            entry["session_type"], entry["level"], session_values,
            entry["status"],
        )
    last_sync = conn.execute(
        "SELECT MAX(ran_at) AS ran_at FROM ingest_runs WHERE user_id = ?",
        (user_id,),
    ).fetchone()["ran_at"]
    wellness = metrics.daily_wellness(conn, user_id, date)
    nutrition = metrics.nutrition_for_date(conn, user_id, date)
    targets = progress.macro_targets(conn, user_id, date)
    # Today's budget vs what's logged so far -- rows with no target
    # are omitted rather than shown empty.
    target_rows = [
        {"label": label, "actual": actual or 0, "target": target,
         "unit": unit}
        for label, actual, target, unit in (
            ("Calories", nutrition.get("calories_kcal"),
             targets.get("calorie_target_kcal"), "kcal"),
            ("Proteines", nutrition.get("protein_g"),
             targets.get("protein_target_g"), "g"),
            ("Lipides", nutrition.get("fat_g"),
             targets.get("fat_target_g"), "g"),
            ("Glucides", nutrition.get("carbs_g"),
             targets.get("carb_target_g"), "g"),
            ("Eau", wellness.get("hydration_ml_today"),
             targets.get("hydration_target_ml"), "ml"),
            ("Pas", wellness.get("steps_today"),
             wellness.get("step_goal"), ""),
        ) if target
    ]
    coach_score = achievements.score(conn, user_id)
    player_level = achievements.player_level(conn, user_id)
    new_today = [
        {**achievements.ACHIEVEMENTS[row["key"]], "key": row["key"]}
        for row in conn.execute(
            "SELECT key FROM achievements WHERE user_id = ? AND "
            "unlocked_at = ?", (user_id, date),
        ).fetchall()
    ]
    deload_until = (
        training.get_deload_until(conn, user_id, entry["session_type"])
        if entry and entry["session_type"] else None
    )
    in_deload = bool(deload_until and deload_until >= date)
    weekday = dt.date.fromisoformat(date).weekday()
    off_title = training.schedule_for_user(conn, user_id)[weekday]["title"]

    return templates.TemplateResponse(
        request, "home.html", {
            "date": date, "entry": entry, "values": session_values,
            "off_title": off_title,
            "description": description,
            "session_label_fr": training.SESSION_LABEL_FR,
            "status_label_fr": training.STATUS_LABEL_FR,
            "last_sync": last_sync, "wellness": wellness,
            "target_rows": target_rows,
            "coach_score": coach_score, "player_level": player_level,
            "new_achievements_today": new_today, "in_deload": in_deload,
            "cal_note": request.query_params.get("cal_note"),
            "username": request.session.get("username"),
        },
    )


@app.post("/regenerate", response_class=HTMLResponse)
def regenerate(request: Request) -> HTMLResponse:
    """Re-run only the LLM phrasing step against today's persisted
    session/status (does not touch levels, calendar, or ntfy -- those
    are the daily cron's job, not a dashboard click's).
    """
    conn = get_conn()
    user_id = current_user_id(request)
    date = today_str(conn, user_id)
    entry = latest_coach_entry(conn, user_id, date)
    if not entry:
        return templates.TemplateResponse(
            request, "_message.html", {
                "entry": None,
                "error": "Le coach n'a pas encore tourne aujourd'hui.",
            },
        )

    wellness = metrics.daily_wellness(conn, user_id, date)
    nutrition = metrics.nutrition_for_date(conn, user_id, date)
    weekly = progress.weekly_progress(conn, user_id, date)
    deload_until = (
        training.get_deload_until(conn, user_id, entry["session_type"])
        if entry["session_type"] else None
    )
    today_session = {"type": "off_system"} if not entry["session_type"] else {
        "type": entry["session_type"], "status": entry["status"],
        "level": entry["level"],
        "values": training.session_values(
            entry["session_type"], entry["level"],
            training.session_cap_min(conn, user_id),
        ),
        "in_deload": bool(deload_until and deload_until >= date),
        "deload_triggered": False,  # regenerate never re-triggers one
    }
    message = llm.coach({
        "date": date,
        "language": db.get_setting(conn, user_id, "language") or "fr",
        "wellness_today": wellness,
        "nutrition_today": nutrition, "weekly_progress": weekly,
        "today_session": today_session,
        "today_targets": progress.macro_targets(conn, user_id, date),
        **metrics.history_snapshot(conn, user_id, date),
    })
    conn.execute(
        "UPDATE coach_log SET message = ?, created_at = ? WHERE id = ? "
        "AND user_id = ?",
        (message, dt.datetime.now(dt.timezone.utc).isoformat(),
         entry["id"], user_id),
    )
    conn.commit()
    entry = latest_coach_entry(conn, user_id, date)
    return templates.TemplateResponse(
        request, "_message.html", {"entry": entry, "error": None},
    )


@app.post("/today/level")
async def edit_today_level(request: Request):
    """Edit tonight's level from the Home card and sync the calendar.

    The ``levels`` row is updated too, so tomorrow's daily +-1 starts
    from the edit. The DB write is committed before the calendar
    push: a gcal failure (missing token, calendar renamed) surfaces
    as a note on the Home page, never as a lost edit or a 500.
    """
    conn = get_conn()
    user_id = current_user_id(request)
    date = today_str(conn, user_id)
    entry = latest_coach_entry(conn, user_id, date)
    if not entry or not entry["session_type"]:
        return RedirectResponse(url="/", status_code=303)
    form = await request.form()
    try:
        level = int(form.get("level", ""))
    except (TypeError, ValueError):
        return RedirectResponse(url="/", status_code=303)
    level = max(training.LEVEL_MIN, min(training.LEVEL_MAX, level))

    session_type = entry["session_type"]
    training.set_level(conn, user_id, session_type, level)
    conn.execute(
        "UPDATE coach_log SET level = ? WHERE id = ? AND user_id = ?",
        (level, entry["id"], user_id),
    )
    conn.commit()

    values = training.session_values(
        session_type, level, training.session_cap_min(conn, user_id),
    )
    description = training.format_description_fr(
        session_type, level, values, entry["status"],
    )
    note = None
    calendar_name = db.get_setting(conn, user_id, "calendar_name")
    day = dt.date.fromisoformat(date)
    if not calendar_name:
        note = "Calendrier non configure (reglez calendar_name)."
    else:
        try:
            gcal.push_description(
                request.session["username"], calendar_name, day,
                training.schedule_for_user(conn, user_id)[day.weekday()],
                description, duration_min=values.get("duration_min"),
            )
        except Exception as error:
            note = f"Calendrier non mis a jour: {error}"
    url = "/" if not note else "/?cal_note=" + urllib.parse.quote(note)
    return RedirectResponse(url=url, status_code=303)


# --- Progress ---

@app.get("/progress", response_class=HTMLResponse)
def progress_page(request: Request) -> HTMLResponse:
    """Weight/body-comp/calorie/protein trends + plateau callout."""
    conn = get_conn()
    user_id = current_user_id(request)
    date = today_str(conn, user_id)
    bundle = progress.weekly_progress(conn, user_id, date)

    start90 = (dt.date.fromisoformat(date) - dt.timedelta(days=90)).isoformat()
    weight_series = conn.execute(
        "SELECT local_date, AVG(kg) AS kg FROM weight WHERE user_id = ? "
        "AND local_date >= ? GROUP BY local_date ORDER BY local_date",
        (user_id, start90),
    ).fetchall()
    calorie_series = progress.daily_calorie_balance(conn, user_id, date, 30)
    protein_series = conn.execute(
        "SELECT local_date, SUM(protein_g) AS protein_g FROM nutrition "
        "WHERE user_id = ? AND local_date >= ? GROUP BY local_date "
        "ORDER BY local_date",
        (user_id, (dt.date.fromisoformat(date) - dt.timedelta(days=30)).isoformat()),
    ).fetchall()

    return templates.TemplateResponse(
        request, "progress.html", {
            "bundle": bundle,
            "weight_labels": [r["local_date"] for r in weight_series],
            "weight_values": [round(r["kg"], 1) for r in weight_series],
            "calorie_labels": [r["date"] for r in calorie_series],
            "calorie_values": [r["balance_kcal"] for r in calorie_series],
            "protein_labels": [r["local_date"] for r in protein_series],
            "protein_values": [
                round(r["protein_g"], 1) for r in protein_series
            ],
            "target_weight": (
                db.get_setting(conn, user_id, "target_weight_kg") or None
            ),
        },
    )


# --- Trends ---

@app.get("/trends", response_class=HTMLResponse)
def trends(request: Request) -> HTMLResponse:
    """Steps, resting HR vs baseline, sleep score, level progression."""
    conn = get_conn()
    user_id = current_user_id(request)
    date = today_str(conn, user_id)
    days = 30
    dates = [
        (dt.date.fromisoformat(date) - dt.timedelta(days=n)).isoformat()
        for n in range(days - 1, -1, -1)
    ]
    steps_by_date = metrics.steps_for_range(conn, user_id, date, days)
    rhr_rows = conn.execute(
        "SELECT local_date, bpm FROM resting_heart_rate WHERE "
        "user_id = ? AND local_date >= ? ORDER BY local_date",
        (user_id, dates[0]),
    ).fetchall()
    rhr_by_date = {r["local_date"]: r["bpm"] for r in rhr_rows}
    baseline = training.rhr_baseline(conn, user_id, date)

    sleep_scores = []
    for d in dates:
        sleep = metrics.sleep_for_date(conn, user_id, d)
        sleep_scores.append(sleep.get("sleep_score"))

    levels = conn.execute(
        "SELECT local_date, session_type, level FROM coach_log WHERE "
        "user_id = ? AND session_type IS NOT NULL ORDER BY local_date",
        (user_id,),
    ).fetchall()

    load_history = training_load.training_load_history(
        conn, user_id, days=days,
    )
    load_by_date = {r["local_date"]: r for r in load_history}
    latest_load = training_load.latest_training_load(conn, user_id)

    hrv_rows = conn.execute(
        "SELECT local_date, last_night_avg FROM garmin_hrv WHERE "
        "user_id = ? AND local_date >= ? ORDER BY local_date",
        (user_id, dates[0]),
    ).fetchall()
    hrv_by_date = {r["local_date"]: r["last_night_avg"] for r in hrv_rows}
    readiness_rows = conn.execute(
        "SELECT local_date, score FROM garmin_training_readiness WHERE "
        "user_id = ? AND local_date >= ? ORDER BY local_date",
        (user_id, dates[0]),
    ).fetchall()
    readiness_by_date = {r["local_date"]: r["score"] for r in readiness_rows}

    return templates.TemplateResponse(
        request, "trends.html", {
            "dates": dates,
            "steps_values": [steps_by_date.get(d) for d in dates],
            "step_goal": int(
                db.get_setting(conn, user_id, "step_goal") or 0
            ),
            "rhr_values": [rhr_by_date.get(d) for d in dates],
            "rhr_baseline": round(baseline, 1) if baseline else None,
            "sleep_values": sleep_scores,
            "level_labels": [r["local_date"] for r in levels],
            "level_series": {
                session_type: [
                    r["level"] if r["session_type"] == session_type
                    else None for r in levels
                ]
                for session_type in training.SESSION_LABEL_FR
            },
            "session_label_fr": training.SESSION_LABEL_FR,
            "ctl_values": [
                round(load_by_date[d]["ctl"], 1) if d in load_by_date
                else None for d in dates
            ],
            "atl_values": [
                round(load_by_date[d]["atl"], 1) if d in load_by_date
                else None for d in dates
            ],
            "tsb_values": [
                round(load_by_date[d]["tsb"], 1) if d in load_by_date
                else None for d in dates
            ],
            "latest_load": latest_load,
            "hrv_values": [hrv_by_date.get(d) for d in dates],
            "readiness_values": [readiness_by_date.get(d) for d in dates],
        },
    )


# --- Sessions ---

def _route_svg_points(
    conn: sqlite3.Connection, user_id: int, uuid: str,
) -> str | None:
    """Normalized SVG polyline points for one session's GPS route.

    ponytail: a simple lat/long linear rescale, not a real map
    projection -- fine for a tiny route-shape preview (loop vs
    out-and-back), would distort at high latitudes or long routes;
    swap for a proper projection if that ever matters here.

    Parameters:
        conn (sqlite3.Connection): smart_sport db connection.
        user_id (int): Owning user.
        uuid (str): exercise_sessions uuid.

    Returns:
        str | None: ``"x,y x,y ..."`` for a 100x60 viewBox, or
        ``None`` if this session has fewer than 2 route points.
    """
    rows = conn.execute(
        "SELECT latitude, longitude FROM exercise_route_points WHERE "
        "user_id = ? AND exercise_uuid = ? ORDER BY epoch_utc",
        (user_id, uuid),
    ).fetchall()
    if len(rows) < 2:
        return None
    lats = [r["latitude"] for r in rows]
    lons = [r["longitude"] for r in rows]
    lat_span = (max(lats) - min(lats)) or 1e-6
    lon_span = (max(lons) - min(lons)) or 1e-6
    points = [
        f"{(lon - min(lons)) / lon_span * 100:.1f},"
        f"{60 - (lat - min(lats)) / lat_span * 60:.1f}"
        for lat, lon in zip(lats, lons)
    ]
    return " ".join(points)


@app.get("/sessions", response_class=HTMLResponse)
def sessions(request: Request) -> HTMLResponse:
    """Browsable table of recent exercise sessions."""
    conn = get_conn()
    user_id = current_user_id(request)
    max_hr_est = metrics.estimated_max_hr(conn, user_id)
    rows = conn.execute(
        "SELECT * FROM exercise_sessions WHERE user_id = ? ORDER BY "
        "start_utc DESC LIMIT 60", (user_id,),
    ).fetchall()
    hr_by_uuid = {}
    if rows:
        marks = ",".join("?" * len(rows))
        hr_by_uuid = {
            r["exercise_uuid"]: r
            for r in conn.execute(
                f"SELECT exercise_uuid, AVG(bpm) AS avg_hr, "
                f"MAX(bpm) AS max_hr FROM exercise_hr_samples "
                f"WHERE user_id = ? AND exercise_uuid IN ({marks}) "
                f"GROUP BY exercise_uuid",
                (user_id, *[r["uuid"] for r in rows]),
            ).fetchall()
        }
    items = []
    for row in rows:
        start = dt.datetime.fromisoformat(row["start_utc"])
        end = dt.datetime.fromisoformat(row["end_utc"])
        auto_label = EXERCISE_TYPE_LABELS.get(
            row["exercise_type"], "autre"
        ).replace("_", " ")
        hr = hr_by_uuid.get(row["uuid"])
        kcal = conn.execute(
            "SELECT SUM(kcal) AS kcal FROM active_calories WHERE "
            "user_id = ? AND start_utc < ? AND end_utc > ?",
            (user_id, row["end_utc"], row["start_utc"]),
        ).fetchone()["kcal"]
        items.append({
            "uuid": row["uuid"],
            "date": row["local_date"],
            "label": row["label_override"] or auto_label,
            "auto_label": auto_label,
            "is_corrected": bool(row["label_override"]),
            "duration_min": round((end - start).total_seconds() / 60),
            "title": row["title"] or "",
            "notes": row["notes"] or "",
            "rpe": row["rpe"],
            "avg_hr": round(hr["avg_hr"]) if hr else None,
            "max_hr": hr["max_hr"] if hr else None,
            "kcal": round(kcal) if kcal is not None else None,
            "hr_zones": (
                metrics.hr_zone_pct(conn, user_id, row["uuid"], max_hr_est)
                if max_hr_est and hr else []
            ),
            "route_points": _route_svg_points(conn, user_id, row["uuid"]),
        })

    # Join every GPS-tracked session (all history) back to its own
    # details, so clicking a route on the map can show them -- the
    # common case (already in `items`, the 60 most recent) is free;
    # only a route older than that falls back to a small extra
    # lookup, with no HR/kcal (out of scope for that batch above).
    polylines = metrics.all_route_polylines(conn, user_id)
    items_by_uuid = {item["uuid"]: item for item in items}
    missing_uuids = [uuid for uuid in polylines if uuid not in items_by_uuid]
    extra_meta = {}
    if missing_uuids:
        marks = ",".join("?" * len(missing_uuids))
        extra_meta = {
            r["uuid"]: r
            for r in conn.execute(
                f"SELECT uuid, local_date, start_utc, end_utc, "
                f"exercise_type, label_override FROM exercise_sessions "
                f"WHERE user_id = ? AND uuid IN ({marks})",
                (user_id, *missing_uuids),
            ).fetchall()
        }
    route_features = []
    for uuid, points in polylines.items():
        item = items_by_uuid.get(uuid)
        if item:
            route_features.append({
                "uuid": uuid, "points": points, "date": item["date"],
                "label": item["label"], "duration_min": item["duration_min"],
                "avg_hr": item["avg_hr"], "max_hr": item["max_hr"],
                "kcal": item["kcal"],
            })
            continue
        row = extra_meta.get(uuid)
        if not row:
            continue
        start = dt.datetime.fromisoformat(row["start_utc"])
        end = dt.datetime.fromisoformat(row["end_utc"])
        auto_label = EXERCISE_TYPE_LABELS.get(
            row["exercise_type"], "autre"
        ).replace("_", " ")
        route_features.append({
            "uuid": uuid, "points": points, "date": row["local_date"],
            "label": row["label_override"] or auto_label,
            "duration_min": round((end - start).total_seconds() / 60),
            "avg_hr": None, "max_hr": None, "kcal": None,
        })

    return templates.TemplateResponse(
        request, "sessions.html", {
            "sessions": items, "route_features": route_features,
        },
    )


@app.post("/sessions/{uuid}/label")
async def correct_session_label(uuid: str, request: Request):
    """Save a manual correction for Garmin's often-wrong HC category.

    Scoped to ``user_id`` in the WHERE clause -- not just for
    correctness, but so one account can never edit another's session
    by guessing/reusing a uuid seen in their own page source.
    """
    conn = get_conn()
    user_id = current_user_id(request)
    form = await request.form()
    label = str(form.get("label", "")).strip() or None
    conn.execute(
        "UPDATE exercise_sessions SET label_override = ? WHERE uuid = ? "
        "AND user_id = ?", (label, uuid, user_id),
    )
    conn.commit()
    return RedirectResponse(url="/sessions", status_code=303)


# --- Data (unified explorer) ---

def _sum_by_date(
    conn: sqlite3.Connection, user_id: int, table: str, column: str,
    start: str,
) -> dict[str, float]:
    """``{local_date: SUM(column)}`` for one interval/point table."""
    return {
        r["local_date"]: r["total"]
        for r in conn.execute(
            f"SELECT local_date, SUM({column}) AS total FROM {table} "
            "WHERE user_id = ? AND local_date >= ? GROUP BY local_date",
            (user_id, start),
        )
    }


@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request) -> HTMLResponse:
    """Every ingested metric, one row per day -- the single place to
    see Garmin + Health Connect data side by side instead of hopping
    between apps.
    """
    conn = get_conn()
    user_id = current_user_id(request)
    date = today_str(conn, user_id)
    try:
        days = int(request.query_params.get("days", 30))
    except ValueError:
        days = 30
    days = max(7, min(180, days))
    start = (
        dt.date.fromisoformat(date) - dt.timedelta(days=days - 1)
    ).isoformat()

    steps = _sum_by_date(conn, user_id, "steps", "count", start)
    nutrition_kcal = _sum_by_date(conn, user_id, "nutrition", "calories", start)
    protein = _sum_by_date(conn, user_id, "nutrition", "protein_g", start)
    hydration = _sum_by_date(conn, user_id, "hydration", "volume_ml", start)
    active_kcal = _sum_by_date(conn, user_id, "active_calories", "kcal", start)
    total_kcal = _sum_by_date(
        conn, user_id, "total_calories_burned", "kcal", start,
    )
    distance = _sum_by_date(conn, user_id, "distance", "meters", start)
    floors = _sum_by_date(conn, user_id, "floors_climbed", "floors", start)
    avg = lambda table, col: {  # noqa: E731 -- tiny local helper
        r["local_date"]: r["v"]
        for r in conn.execute(
            f"SELECT local_date, AVG({col}) AS v FROM {table} "
            "WHERE user_id = ? AND local_date >= ? GROUP BY local_date",
            (user_id, start),
        )
    }
    rhr = avg("resting_heart_rate", "bpm")
    weight = avg("weight", "kg")
    body_fat = avg("body_fat", "percentage")
    lean = avg("lean_body_mass", "kg")

    # Sleep hours per wake-up local date: one pass over all sessions.
    # ponytail: O(sessions) full scan per page view -- fine for one
    # user's phone data; add a local_date column at ingest if it ever
    # isn't.
    tz = metrics.local_tz(conn, user_id)
    sleep_hours: dict[str, float] = {}
    for row in conn.execute(
        "SELECT start_utc, end_utc FROM sleep_sessions WHERE user_id = ?",
        (user_id,),
    ):
        if row["end_utc"] <= row["start_utc"]:
            continue
        end = dt.datetime.fromisoformat(row["end_utc"])
        day = end.astimezone(tz).date().isoformat()
        if day >= start:
            hours = (
                end - dt.datetime.fromisoformat(row["start_utc"])
            ).total_seconds() / 3600
            sleep_hours[day] = max(sleep_hours.get(day, 0), hours)

    sessions_by_day: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT local_date, start_utc, end_utc FROM exercise_sessions "
        "WHERE user_id = ? AND local_date >= ?", (user_id, start),
    ):
        entry = sessions_by_day.setdefault(
            row["local_date"], {"n": 0, "minutes": 0},
        )
        entry["n"] += 1
        entry["minutes"] += max(0, round((
            dt.datetime.fromisoformat(row["end_utc"])
            - dt.datetime.fromisoformat(row["start_utc"])
        ).total_seconds() / 60))

    rows = []
    current = dt.date.fromisoformat(date)
    for _ in range(days):
        d = current.isoformat()
        sess = sessions_by_day.get(d, {})
        rows.append({
            "date": d,
            "steps": steps.get(d),
            "rhr": round(rhr[d]) if d in rhr else None,
            "sleep_h": round(sleep_hours[d], 1) if d in sleep_hours else None,
            "sessions_n": sess.get("n"),
            "sport_min": sess.get("minutes"),
            "active_kcal": round(active_kcal[d]) if d in active_kcal else None,
            "total_kcal": round(total_kcal[d]) if d in total_kcal else None,
            "kcal_in": round(nutrition_kcal[d]) if d in nutrition_kcal else None,
            "protein_g": round(protein[d]) if d in protein else None,
            "hydration_ml": round(hydration[d]) if d in hydration else None,
            "weight_kg": round(weight[d], 1) if d in weight else None,
            "body_fat_pct": round(body_fat[d], 1) if d in body_fat else None,
            "lean_kg": round(lean[d], 1) if d in lean else None,
            "distance_km": round(distance[d] / 1000, 1) if d in distance else None,
            "floors": round(floors[d]) if d in floors else None,
        })
        current -= dt.timedelta(days=1)

    return templates.TemplateResponse(
        request, "data.html", {"rows": rows, "days": days},
    )


# --- Achievements ---

@app.get("/achievements", response_class=HTMLResponse)
def achievements_page(request: Request) -> HTMLResponse:
    """Xbox-style achievement grid: unlocked first, then locked (with
    live progress bars), plus the full XP ledger for auditability.
    """
    conn = get_conn()
    user_id = current_user_id(request)
    date = today_str(conn, user_id)
    items = achievements.all_achievements_with_status(conn, user_id, date)
    return templates.TemplateResponse(
        request, "achievements.html", {
            "items": items, "score": achievements.score(conn, user_id),
            "player_level": achievements.player_level(conn, user_id),
            "xp_ledger": achievements.xp_ledger_entries(
                conn, user_id, limit=100,
            ),
        },
    )


@app.get("/achievements/history", response_class=HTMLResponse)
def achievements_history(request: Request) -> HTMLResponse:
    """Every achievement (homegrown + earned Garmin badges) with its
    unlock date, unlocked first -- most recent first -- then locked
    ones, for a chronological read the card grid doesn't give.
    """
    conn = get_conn()
    user_id = current_user_id(request)
    date = today_str(conn, user_id)
    items = achievements.all_achievements_with_status(conn, user_id, date)
    return templates.TemplateResponse(
        request, "achievements_history.html", {"items": items},
    )


# --- Settings ---

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Goal parameters, level overrides, ingestion health check."""
    conn = get_conn()
    user_id = current_user_id(request)
    settings = {
        key: db.get_setting(conn, user_id, key) for key in db.DEFAULT_SETTINGS
    }
    levels = {
        session_type: training.get_level(conn, user_id, session_type)
        for session_type in training.SESSION_LABEL_FR
    }
    admin = db.is_admin(conn, user_id)
    return templates.TemplateResponse(
        request, "settings.html", {
            "settings": settings, "levels": levels,
            "passkeys": db.passkeys_for_user(conn, user_id),
            "is_admin": admin,
            "pending_users": db.pending_users(conn) if admin else [],
            "schedule": training.schedule_for_user(conn, user_id),
            "weekday_names": ["Lundi", "Mardi", "Mercredi", "Jeudi",
                              "Vendredi", "Samedi", "Dimanche"],
            "session_label_fr": training.SESSION_LABEL_FR,
            "ingest_status": last_ingest_status(conn, user_id),
            "saved": request.query_params.get("saved") is not None,
            "username": request.session.get("username"),
        },
    )


@app.post("/settings")
async def save_settings(request: Request):
    """Persist edited settings and level overrides, then redirect back."""
    conn = get_conn()
    user_id = current_user_id(request)
    form = await request.form()
    for key in db.DEFAULT_SETTINGS:
        if key in form:
            db.set_setting(conn, user_id, key, str(form[key]).strip())
    for session_type in training.SESSION_LABEL_FR:
        field = f"level_{session_type}"
        if field in form and str(form[field]).strip():
            training.set_level(conn, user_id, session_type, int(form[field]))
    if "schedule_0_title" in form:
        # Rebuild the weekly plan from the form; invalid fields keep
        # the user's previous value rather than corrupting the JSON.
        old = training.schedule_for_user(conn, user_id)
        schedule = {}
        for weekday in range(7):
            entry = dict(old[weekday])
            stype = str(form.get(f"schedule_{weekday}_type", "")).strip()
            entry["session_type"] = (
                stype if stype in training.SESSION_LABEL_FR else None
            )
            title = str(form.get(f"schedule_{weekday}_title", "")).strip()
            if title:
                entry["title"] = title
            start = str(form.get(f"schedule_{weekday}_start", "")).strip()
            if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", start):
                entry["start"] = start
            try:
                duration = int(form.get(f"schedule_{weekday}_duration", ""))
            except (TypeError, ValueError):
                duration = 0
            if 0 < duration <= 24 * 60:
                entry["duration_min"] = duration
            schedule[str(weekday)] = entry
        db.set_setting(conn, user_id, "schedule", json.dumps(schedule))
    return RedirectResponse(url="/settings?saved=1", status_code=303)
