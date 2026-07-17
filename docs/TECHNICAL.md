# Technical guide

Setup, architecture and operations for Smart Sport. For the short
overview, see the [README](../README.md).

## Requirements

- Docker + Docker Compose (recommended), or Python 3.11+ directly
- A Garmin account (watch syncing to Garmin Connect) — exercise
  sessions and sleep are fetched from the Garmin API directly
  (unofficial `garminconnect` client, interactive login once, tokens
  cached ~1 year per user)
- Health Connect on Android, exporting automatically to Google Drive
  (Settings > Health Connect > backup/export) — this project reads
  the raw exported `health_connect_export.db` for everything else
  (steps, weight, nutrition, ...)
- [rclone](https://rclone.org) configured with read access to that
  Drive folder
- Claude Code installed and logged in (`claude -p`, subscription
  auth, no API key) — or set `LLM_PROVIDER=anthropic_api` +
  `ANTHROPIC_API_KEY` to use the Anthropic API instead
- The ntfy app on your phone, subscribed to a secret topic
- A Google account with a target calendar (any name — set it per
  user in Settings > "Calendrier Google") and a Google Cloud OAuth
  client (see garmin-coach's README for the one-time Calendar API
  setup — same steps, new config path below)

## Input and expected output

**Input**, two sources:

- **Garmin API** (`ingest/garmin_api.py`): exercise sessions
  (correct activity types, per-session HR series) and sleep
  (sessions + stages). Garmin's Health Connect writer mislabels
  activity types and never syncs workout HR series — the API has
  both, so these two domains bypass Health Connect entirely.
- **`health_connect_export.db`** — Android's raw internal Health
  Connect SQLite backup — for every other record type (steps, heart
  rate, weight, body fat, nutrition, hydration, ...), from whichever
  apps write to it (Garmin Connect, MyFitnessPal, ...).

Note: HRV, VO2max, training readiness and body battery are dropped,
not approximated, in favor of a resting-HR baseline computed from
your own ingested history, a sleep-score approximation from sleep
stages, and an activity-load signal from recent exercise volume.

**Output**: every morning, one concrete plan — tonight's session
(level-adapted numbers) plus the day's calorie/macro/hydration
budget — delivered as an ntfy push + Google Calendar event update,
phrased by Claude from your full history (last 7 days of real
sessions with HR/RPE/calories, planned-vs-done adherence, daily
status streak, CTL/ATL/TSB training load, weight/nutrition trends).
Plus a dashboard at `http://<host>:8080` with Today (targets card
with live progress bars; tonight's level is editable inline and the
edit is pushed straight to the calendar event), Progress, Trends,
Sessions (with per-workout avg/max HR and kcal), Achievements, and
Settings (goals, weekly plan editor, ingestion health check).

## Architecture

```text
worker container cron
  05:30  run_ingest.py    Garmin API fetch (activities + sleep,
                           trailing GARMIN_LOOKBACK_DAYS window),
                           then rclone-sync the Drive export,
                           extract, upsert the rest into
                           data/db/smart_sport.db (idempotent --
                           full snapshot each time)
  06:00  run_coach.py     metrics.py + progress.py compute today's
                           wellness + weekly trends; training.py
                           applies the deload guardrail (3 reds in a
                           row -> forced lighter week) on top of the
                           daily status/level; gcal.py updates
                           tonight's event; llm.py (claude -p) phrases
                           the message; achievements.py checks/announces
                           unlocks; notify.py pushes it; logged to
                           coach_log

Note: no same-day/afternoon check-in -- the Health Connect export
syncs once a day (overnight), so "today's" data doesn't exist until
tomorrow's 05:30 ingest. All coaching is necessarily a day in arrears
(today's plan, informed by yesterday's fully-logged data), not
real-time.

web container (always on)
  web/app.py               FastAPI reads the same db read-mostly;
                            Home's "Regenerate" button re-runs only
                            the LLM phrasing step, live
```

## Setup (Docker)

```bash
cp .env.example .env
nano .env   # NTFY_TOPIC, TZ, RCLONE_REMOTE (CLAUDE_CODE_OAUTH_TOKEN filled in step 2)

# 0. Google Calendar OAuth client (see garmin-coach README for how to
#    create one) -- drop it in before the first interactive run:
mkdir -p data/gcal-config
cp /path/to/client_secret.json data/gcal-config/calendar_client_secret.json

# 1. rclone remote pointing at the Drive folder the phone exports into
docker compose run --rm -it smart_sport-worker rclone config

# 2. Claude subscription token
docker compose run --rm -it smart_sport-worker claude setup-token
#    -> copy the printed token into .env (CLAUDE_CODE_OAUTH_TOKEN)

# 3. Calendar consent -- do this on the host, not in Docker (the OAuth
#    flow opens a local browser port):
.venv/bin/python -c "import gcal; gcal.get_calendar_service()"
cp ~/.config/smart_sport/calendar_token.json data/gcal-config/

# 3b. Garmin login (email/password + possible MFA prompt; tokens land
#     in data/garmin-tokens/<username>, valid ~1 year). Use your
#     smart_sport account name:
docker compose run --rm -it smart_sport-worker python -c \
  "from ingest import garmin_api; garmin_api.get_client('<username>')"
#     First run only: backfill history further than the default
#     14-day window with GARMIN_LOOKBACK_DAYS=365 python run_ingest.py

# 4. End-to-end test before trusting the cron
docker compose run --rm -it smart_sport-worker python run_ingest.py
docker compose run --rm -it smart_sport-worker python run_coach.py

# 5. Start both services
docker compose up -d --build
```

Dashboard: `http://<host>:8080`.

## Adding a user

Every account gets its own data, settings, weekly plan, calendar
and notifications — the deployment is shared, nothing else is.

The **first account ever created is the admin** (whether via the web
form or the CLI). After that, new people sign up themselves at
`/signup`; their account stays pending — no login, no pipeline —
until the admin approves it from Settings > "Comptes en attente".

Once logged in, anyone can add **passkeys** (fingerprint/face/security
key, Settings > Passkeys) and sign in without a password from the
login page. Passkeys need HTTPS (or localhost) — the public profile
below provides exactly that.

The CLI alternative still works (creates pre-approved accounts):

```bash
# 1. Create the account
docker compose run --rm -it smart_sport-worker python manage_users.py alice

# 2. Point ingestion at their own Health Connect export
docker compose run --rm -it smart_sport-worker rclone config   # new remote
# then set rclone_remote for that user (Settings page, or sqlite3)

# 3. Calendar consent for that account (host, not Docker):
.venv/bin/python -c "import gcal; gcal.get_calendar_service('alice')"
cp ~/.config/smart_sport/calendar_token_alice.json data/gcal-config/

# 4. Garmin login for that account:
docker compose run --rm -it smart_sport-worker python -c \
  "from ingest import garmin_api; garmin_api.get_client('alice')"
```

Then the user logs in and fills in Settings: goals and macro ratios,
Google Calendar name, ntfy topic, and the weekly plan (per-weekday
session type, title, start time, duration — "libre" days sit outside
the leveling system but still drive the calendar event).

## Exposing it on the internet

The dashboard stays self-hosted; the login (per-user accounts,
PBKDF2 passwords, rate-limited form) is the only gate — so the
transport has to be HTTPS. The compose file ships an optional Caddy
front that handles certificates automatically:

```bash
# .env: set these four
#   DOMAIN=sport.example.com     # DNS A/AAAA record -> your host
#   SESSION_SECRET=<python3 -c "import secrets; print(secrets.token_hex(32))">
#   COOKIE_SECURE=1              # session cookie never sent over HTTP
#   WEB_BIND=127.0.0.1:8080      # app port no longer reachable directly

docker compose --profile public up -d --build
```

Forward ports 80 + 443 to the host (and nothing else). Caddy
obtains/renews the Let's Encrypt certificate and proxies to the app;
failed logins are throttled per client IP (5 tries / 15 min).

## Testing

```bash
.venv/bin/python tests/run_all.py   # every module's plain-assert self-check
```

## Migrating from garmin-coach

Run smart_sport alongside garmin-coach for 1-2 weeks and compare the
daily status calls (the vote set is smaller -- 3 signals instead of
4 -- so behavior will genuinely differ) before retiring garmin-coach:

```bash
systemctl --user disable --now garmin-coach.timer   # or: docker compose down (old deployment)
```

## Troubleshooting

- A "Coach failed: ..." ntfy notification means the ingest/coach
  pipeline itself failed and sent you the error; a silent morning
  means cron/rclone trouble, not a swallowed exception.
- A "(Calendrier non mis a jour: ...)" note appended to an otherwise
  normal message means only the Calendar step failed — rerun the
  Calendar consent step above.
- `nutrition_today` / Progress page empty: nutrition logging is new
  and sparse by design — Progress degrades to "pas assez de donnees"
  rather than a misleading chart until enough history accumulates.
- Settings page's ingestion status table shows the last row count
  per table — a stale timestamp there means rclone/cron is the thing
  to check, not the dashboard.
