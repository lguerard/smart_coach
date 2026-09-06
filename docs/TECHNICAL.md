# Technical guide

Setup, architecture and operations for Smart Coach. For the short
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
- A Google account with a target calendar (any name, e.g. "Sport" —
  set it per user in Settings > "Calendrier Google"; created
  automatically on first push if it doesn't exist yet, so a
  dedicated calendar needs no manual setup). The one-time Google
  Cloud setup is step 3 below — nothing to prepare beforehand.

## Input and expected output

**Input**, two sources:

- **Garmin API** (`ingest/garmin_api.py`): exercise sessions
  (correct activity types, per-session HR series, GPS route points
  when the activity has a track), sleep (sessions + stages), and
  daily HRV / training readiness / body battery / stress. Garmin's
  Health Connect writer mislabels activity types and never syncs
  workout HR series, GPS tracks, or any of the recovery signals — the
  API has all of it, so these domains bypass Health Connect entirely.
- **`health_connect_export.db`** — Android's raw internal Health
  Connect SQLite backup — for every other record type (steps, heart
  rate, weight, body fat, nutrition, hydration, ...), from whichever
  apps write to it (Garmin Connect, MyFitnessPal, ...).

HRV status and training readiness feed real votes in the daily
green/yellow/red status (`training.compute_status`), alongside a
resting-HR baseline computed from your own ingested history, a
sleep-score approximation from sleep stages, and an activity-load
signal from recent exercise volume. Body battery and stress have no
vote — body battery is a running energy gauge, not a morning score,
and a separate stress vote would double-count what
training-readiness's own aggregate already factors in — both are
dashboard/LLM context only. VO2max is not ingested (`get_max_metrics`
has no stable typed schema in the Garmin client used here).

Two capabilities were explicitly checked and are **not available**:
Garmin Explore's route-suggestion/popularity-routing feature has no
endpoint in the `garminconnect` client (or any known reverse-engineered
one) — it's bound to Garmin Connect's own map UI and Explore-badged
devices. Conversely, **pushing tonight's session to the watch as a
Garmin workout is implemented**
(`ingest/garmin_api.py:push_workout_for_session`, called from
`run_coach.py`): it builds a typed workout from
`training.session_values()` (a single timed step for treadmill, a
repeat-group of rep/time steps for the bodyweight circuits) and
uploads + schedules it, deleting yesterday's pushed template first.
Ceiling: per-exercise step labels ride an unofficial `description`
field with no confirmed on-watch display — verify against a real
account; the mechanism (upload/schedule/cleanup) is solid regardless.

Three more life-integration pieces:

- **Calendar-aware scheduling** (`gcal.py:find_available_start`):
  before pushing tonight's event, a freebusy query checks a real
  calendar (Settings → "calendrier vérifié pour les conflits",
  `busy_calendar_name`, "primary" if unset) for the planned slot. A
  conflict moves the session to the next free slot before 22:00; no
  free slot keeps the original time rather than picking something
  unreasonable.
- **Weather context** (`weather.py`, Open-Meteo — free, no API key):
  if a city is set, today's forecast rides in `weather_today` and
  the prompt may mention it (e.g. suggesting an outdoor alternative
  on a nice day) — it never changes the prescribed session, since
  none of this project's session types have an outdoor equivalent.
- **Menstrual cycle** (`ingest/garmin_api.py:upsert_menstrual_cycle`,
  Settings → "suivre le cycle menstruel", opt-in and off by
  default): like Garmin badges, `get_menstrual_data_for_date` has no
  typed wrapper or test fixture upstream, so the phase is a
  best-effort field guess — never fetched unless opted in, surfaced
  as LLM context only, never a hard training/nutrition rule.

**Output**: every morning, one concrete plan — tonight's session
(level-adapted numbers, also pushed to the watch) plus the day's
calorie/macro/hydration budget — delivered as an ntfy push and
Google Calendar event update, phrased by Claude from your full
history (last 7 days of real sessions with HR/RPE/calories,
planned-vs-done adherence, daily status streak, CTL/ATL/TSB training
load, weight/nutrition trends). Plus two optional daily nudges
(`run_checkin.py`, 16:00 and 21:00) if hydration/steps fall behind
pace or recent sleep is running short — silent when you're on track.
Plus a dashboard at
`http://<host>:8080` with Today (targets card with live progress
bars; tonight's level is editable inline and the edit is pushed
straight to the calendar event), Progress, Trends (including a
30-day HRV/training-readiness chart), Sessions (a Leaflet map of
every GPS-tracked route across all activity types -- click one for
that session's details -- plus per-workout avg/max HR, an HR-zone
breakdown bar, and a small route-shape preview per session),
Achievements (homegrown unlocks plus earned Garmin Connect badges,
surfaced as achievements rather than a separate section; a full
history view lists every one with its unlock date), and Settings
(goals, weekly plan editor, ingestion health check).

## Architecture

```text
worker container cron
  05:30  run_ingest.py    Garmin API fetch (activities + sleep,
                           trailing GARMIN_LOOKBACK_DAYS window),
                           then rclone-sync the Drive export,
                           extract, upsert the rest into
                           data/db/smart_coach.db (idempotent --
                           full snapshot each time)
  06:00  run_coach.py     metrics.py + progress.py compute today's
                           wellness + weekly trends; training.py
                           applies the deload guardrail (3 reds in a
                           row, OR a single critically negative TSB
                           reading -> forced lighter week) on top of
                           the daily status/level; gcal.py checks the
                           user's real calendar for conflicts and
                           moves tonight's slot if needed before
                           updating the event; garmin_api.py pushes
                           tonight's session to the watch as a
                           scheduled workout; weather.py adds today's
                           forecast as context if a city is set;
                           llm.py (claude -p) phrases the message
                           (folding in cycle phase/weather if
                           relevant); achievements.py checks/
                           announces unlocks; notify.py pushes it;
                           logged to coach_log
  16:00  run_checkin.py   afternoon: nudges if hydration/steps are
                           meaningfully behind pace -- silent if on
                           track (no running commentary)
  21:00  run_checkin.py   evening: nudges to wind down early if the
                           last 3 nights are meaningfully short on
                           sleep -- silent otherwise

Note: exercise/sleep/wellness are Garmin-API-fresh (same day), but
Health-Connect-only fields (steps, weight, nutrition, ...) still lag
a day -- that export syncs once overnight, so those numbers reflect
yesterday until the next 05:30 ingest.

web container (always on)
  web/app.py               FastAPI reads the same db read-mostly;
                            Home's "Regenerate" button re-runs only
                            the LLM phrasing step, live
```

## Setup

Everything runs through `docker compose` — there is no virtualenv to
create and no file to copy by hand. Count about twenty minutes, most of
it waiting on Google and Garmin.

### 1. Settings

```bash
cp .env.example .env
nano .env
```

Four values matter now; the fifth comes back in step 4.

| Variable | What it is |
|---|---|
| `TZ` | your timezone, e.g. `Europe/Paris` — the whole schedule keys off it |
| `NTFY_TOPIC` | a secret topic name of your choosing. Install the **ntfy** app and subscribe to the same name: that is where the coaching message lands |
| `RCLONE_REMOTE` | where your phone drops its Health Connect export, e.g. `gdrive:HealthConnectExports`. The remote itself is created in step 2 |
| `SESSION_SECRET` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `CLAUDE_CODE_OAUTH_TOKEN` | filled in at step 4 |

### 2. Access to your phone's export

Your Android phone backs Health Connect up to Google Drive on its own
(Settings > Health Connect > backup/export). Point rclone at that
folder:

```bash
docker compose run --rm -it smart_coach-worker rclone config
```

Pick `drive`, follow the prompts, and name the remote exactly as in
`RCLONE_REMOTE`. The configuration is kept in `data/rclone/`.

### 3. Google Calendar

**a. Create the credentials, once, in the Google Cloud Console.**

1. [console.cloud.google.com](https://console.cloud.google.com) → create
   or pick a project
2. **APIs & Services → Library** → enable **Google Calendar API**
3. **APIs & Services → Credentials → Create credentials → OAuth client
   ID**
4. *Which API are you using?* → **Google Calendar API**;
   *what data will you be accessing?* → **User data**.
   Not "Application data": that creates a service account, which has
   its own empty calendar and cannot see yours
5. *Application type* → **Desktop app**. This is the one that matters —
   a "Web application" client rejects the local redirect used below
6. **Download JSON**, then on the server:

```bash
mkdir -p data/gcal-config
cp ~/Downloads/client_secret_*.json data/gcal-config/calendar_client_secret.json
```

If the consent screen stays in **Testing** mode, add your own address
under **Test users** — otherwise Google refuses the approval in the next
step.

**b. Approve the access, once per account.**

```bash
docker compose run --rm -it -p 8765:8765 \
    smart_coach-worker python setup_calendar.py <your-account>
```

It prints a URL: open it in a browser, approve, done. The token is
written straight where the containers read it — nothing to copy.

> **Working over SSH.** Google redirects back to
> `http://localhost:8765`, which has to reach the server. Open the
> session with that port forwarded and the browser on your own machine
> will do:
>
> ```bash
> ssh -L 8765:localhost:8765 you@your-server
> ```

`<your-account>` is the Smart Coach account name — you create it in step
6 at `/signup`, or now with `manage_users.py` (see *Adding a user*). Its
Garmin and Calendar credentials are keyed by that name, so two people on
one deployment never share either.

### 4. Claude

```bash
docker compose run --rm -it smart_coach-worker claude setup-token
```

Copy the printed token into `.env` as `CLAUDE_CODE_OAUTH_TOKEN`. This
uses a Claude subscription; to use the Anthropic API instead, set
`LLM_PROVIDER=anthropic_api` and `ANTHROPIC_API_KEY`.

### 5. Garmin

```bash
docker compose run --rm -it smart_coach-worker python setup_garmin.py <your-account>
```

E-mail, password, and the MFA code if your account has one. The tokens
land in `data/garmin-tokens/<account>` and last about a year.

### 6. Check it end to end, then start

```bash
docker compose run --rm -it smart_coach-worker python run_ingest.py
docker compose run --rm -it smart_coach-worker python run_coach.py
docker compose up -d --build
```

`run_ingest.py` pulls the data, `run_coach.py` produces the day's message
— run both once before trusting the schedule, so a failure shows up in
front of you rather than at 6 a.m.

Any time something feels off afterwards, one command answers most of it:

```bash
docker compose run --rm smart_coach-worker python doctor.py
```

It checks disk space, the rclone remote, the Claude token, ntfy, that
every module the cron jobs need is actually importable, the age of each
ingestion source, the Garmin tokens and the calendar consent — and exits
non-zero if anything is broken, so it also works from cron.

To backfill more than the default 30 days, once:

```bash
docker compose run --rm -e GARMIN_LOOKBACK_DAYS=365 \
    smart_coach-worker python run_ingest.py
```

Dashboard: `http://<host>:8080`. Create your account at `/signup` — the
first account ever created is the admin.

### What runs on its own afterwards

| Time | What happens |
|---|---|
| 05:30 | ingestion: Garmin + the phone's export |
| 06:00 | readiness, tonight's session, calendar, coaching message |
| 16:00 | afternoon check-in — silent unless you are falling behind |
| 21:00 | evening check-in, same rule |

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

The CLI alternative still works (creates pre-approved accounts), and the
per-account credentials are exactly the same three commands as the first
setup:

```bash
# 1. Create the account
docker compose run --rm -it smart_coach-worker python manage_users.py alice

# 2. Their own Health Connect export
docker compose run --rm -it smart_coach-worker rclone config   # new remote
# then set rclone_remote for that user (Settings page)

# 3. Their calendar
docker compose run --rm -it -p 8765:8765 \
    smart_coach-worker python setup_calendar.py alice

# 4. Their Garmin account
docker compose run --rm -it smart_coach-worker python setup_garmin.py alice
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
docker compose run --rm smart_coach-worker python tests/run_all.py
```

Every module's plain-assert self-check, no framework.

## Troubleshooting

Start here:

```bash
docker compose run --rm smart_coach-worker python doctor.py
```

Every failure this project has actually suffered was silent — a module
missing from the image, a full disk, an ingestion stopped for days,
expired tokens. Nothing crashed, nothing restarted, the dashboard kept
serving yesterday's numbers. `doctor.py` asks the questions nobody
thinks to ask until something is already wrong, and names the command
that fixes each one.

- A "Coach failed: ..." ntfy notification means the ingest/coach
  pipeline itself failed and sent you the error; a silent morning
  means cron/rclone trouble, not a swallowed exception.
- A "(Calendrier non mis a jour: ...)" note appended to an otherwise
  normal message means only the Calendar step failed — rerun
  `setup_calendar.py` for that account (delete the existing
  `data/gcal-config/calendar_token_<account>.json` first, the script
  refuses to overwrite one).
- `setup_calendar.py` prints a URL and then seems to hang: that is it
  waiting for the approval. If the browser says the page cannot be
  reached after you approve, the redirect never got back — open the SSH
  session with `-L 8765:localhost:8765` and try again.
- Google refuses the approval with "app is blocked" or "not verified":
  the consent screen is in Testing mode and your address is not in
  **Test users**.
- `ModuleNotFoundError` or "can't open file" in the worker's logs means
  the image is older than the code: `docker compose up -d --build`.
- `nutrition_today` / Progress page empty: nutrition logging is new
  and sparse by design — Progress degrades to "pas assez de donnees"
  rather than a misleading chart until enough history accumulates.
- Settings page's ingestion status table shows the last row count
  per table — a stale timestamp there means rclone/cron is the thing
  to check, not the dashboard.
