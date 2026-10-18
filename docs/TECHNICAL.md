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
  when the activity has a track), sleep (sessions + stages, plus
  Garmin's own sleep score and overnight HRV/SpO2/respiration), the
  whole-day rollup (`get_stats`: steps, resting/min/max HR, calories,
  intensity minutes, active-vs-sedentary seconds), and daily HRV /
  training readiness / body battery / stress / respiration / SpO2 /
  VO2max. Garmin's Health Connect writer mislabels activity types and
  never syncs workout HR series, GPS tracks, or any of the recovery
  signals — the API has all of it, so these domains bypass Health
  Connect entirely.
- **`health_connect_export.db`** — Android's raw internal Health
  Connect SQLite backup — for every other record type (steps, heart
  rate, weight, body fat, nutrition, hydration, ...), from whichever
  apps write to it (Garmin Connect, MyFitnessPal, ...).

Where the two overlap, `metrics.daily_wellness` picks per field.
Health Connect wins on steps/resting HR/floors/distance/calories: it
merges every app writing to the phone, not just the watch. But it
only syncs overnight, so it never has *today* — Garmin's rollup fills
those in rather than leaving the current day blank on the dashboard.
Intensity minutes invert that: the rollup's field names are the ones
the Garmin client library models explicitly, so they beat the
undocumented per-metric endpoint's guessed ones. Sleep score works
the same way — Garmin's own score replaces `metrics.score_sleep`'s
stage-derived approximation whenever Garmin has scored the night, and
`sleep_score_source` records which one you're looking at.

HRV status and training readiness feed real votes in the daily
green/yellow/red status (`training.compute_status`), alongside a
resting-HR baseline computed from your own ingested history, a
sleep-score approximation from sleep stages, and an activity-load
signal from recent exercise volume. Body battery and stress have no
vote — body battery is a running energy gauge, not a morning score,
and a separate stress vote would double-count what
training-readiness's own aggregate already factors in — both are
dashboard/LLM context only. VO2max, respiration, SpO2 and intensity
minutes are ingested as context too; `get_max_metrics` still has no
stable typed schema in the Garmin client, so VO2max is read
best-effort out of its "generic" block and simply stays NULL when the
shape doesn't match.

Two capabilities were explicitly checked and are **not available**:
Garmin Explore's route-suggestion/popularity-routing feature has no
endpoint in the `garminconnect` client (or any known reverse-engineered
one) — it's bound to Garmin Connect's own map UI and Explore-badged
devices. Conversely, **the week's sessions are on the watch as
Garmin workouts** (`ingest/garmin_api.py`, called from
`run_coach.py`): `plan_week_ahead` pushes each planned day of the next
six once, at its level that morning, and `sync_planned_workout`
replaces today's only when the morning decision differs from what is
already there (rest removes it, a recovery walk or a new level
replaces it; an identical plan makes no Garmin call). Workouts are
built from `training.session_values()` (a single timed step for
treadmill, a repeat-group of rep/time steps for the circuits, with
timed rest steps for the kettlebell intervals); days older than
yesterday are deleted.
Each rep step carries a `category`/`exerciseName` pair from Garmin's
own exercise catalog (`garminconnect.exercises`), which is what drives
the exercise name and muscle diagram on the watch — the free-text
`description` field it used to rely on is not rendered on-device at
all. The segment is typed `strength_training` for circuits and
`cardio_training` for the treadmill; `walking` (sportTypeId 17) looked
right but Garmin renders it as a pool swim, verified against a real
account.

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
(goals, weekly plan editor, ingestion health check). The look is
flat cards on a zinc (dark) or warm paper (light) background, Inter,
icon navigation (a bottom tab bar on phones) and one accent colour,
teal or blue, picked under Settings > Apparence (`accent` setting,
`<html data-accent>`, tokens in `web/static/theme.css`). Today is laid
out in three columns on a wide screen: level, quick actions and 7/30-day
activity (`metrics.period_stats`) on the left, the session and the
coach's message in the middle, goals and signals on the right. How a
session felt (too easy / right / too hard) is asked on Today for
yesterday's session first, and for today's only once a workout is
recorded or its slot is over (`training.pending_feedback`); any
session of the last two weeks can be rated or re-rated from Sessions
(`training.rateable_sessions`).

Hard days. A declared or sensor-read illness rests the day; the day
after a rest is a gentle flat walk (20 min). A red day trains at a
lowered level, even several in a row (the deload guardrail handles
streaks); only a red day with the level already at its floor becomes
an active uphill walk (35 min, 5 km/h, 5 %), at most once per rolling
week (`training.tier_after_guardrail`, `coach_log.recovery_kind`). The
calendar event is retitled on those days. Protein and fat targets use
the target weight when it is set below the current one
(`progress.macro_weight_kg`).

Kettlebell days use one of two 20/10 circuits
(`training.KETTLEBELL_CIRCUITS`). A is the standing video workout with
a deadlift, a one-arm row, the overhead triceps extension and a squeeze
press added: everything B works plus the back and the hinge, so it is
the only circuit by default and can replace the upper-body day. B
(floor press, Russian twist, overhead extension, pull-over, hammer
curl, sit-up press) stays for "Circuits kettlebell" in Réglages
(`kettlebell_circuits`: A, B or AB, read through
`exercise_library.equipment_for`, so the watch, the Programme page and
the two-way sync all follow it) and for the bonus workouts. The tour
bands are for six moves; a longer circuit does fewer tours for the same
number of efforts (3 x 9 at level 0, like 5 x 6). The
session cap shortens the work time before it takes a tour away, so the
top levels are not lighter than the ones below. Today also
lists optional bonus workouts (`bonus.py`: abs & arms, chest &
triceps, an active walk), sized from the person's levels, with tired
muscles flagged; none on a rest day, only the walk on a recovery day,
and they never move a level.

Exercise names sent to the watch are checked against Garmin's own
catalog (official FIT SDK, `scripts/garmin_catalog.mjs` +
`scripts/build_garmin_names.py`); the checked list lives in
`library/garmin_exercises_used.json` and the `ingest/garmin_api.py`
self-check refuses any name not in it. `watch_test.py` previews, sends
and removes "Smart Coach TEST" workouts (both kettlebell circuits, the
bonus workouts) without touching the planned week.

Programme (`/programme`, `custom_moves.py`): the person edits their
circuits on the website -- takes library moves out, adds moves of their
own (reps or seconds, in every tour or once at the end, with an optional
Garmin exercise from a checked list), per kettlebell circuit or both.
Stored in `custom_moves` / `removed_moves` and applied by
`training.user_session_values`, which every real session goes through
(morning run, dashboard, level edit, week ahead on the watch), so an edit
is never undone by the next morning. Own moves keep a fixed count; a
change that would empty a session is ignored. "Envoyer sur la montre" (Programme page,
and next to tonight's moves on Today) runs `garmin_api.push_programme`:
today's workout rebuilt from this morning's decision, and every coming
day already on the watch rebuilt at its type's current level with the
person's own moves -- only the changed ones re-uploaded, no new decision,
no level change. The web container mounts `data/garmin-tokens` for it.

Two-way watch sync (`watch_sync.py`): each workout put on the watch is
stored with its shape (steps, the slot each came from, counts). Before
the morning run builds today's session and before "Envoyer sur la
montre", the planned workouts are read back from Garmin
(`get_workout_by_id`) and what the person changed in Garmin Connect is
taken into the programme instead of overwritten: a move added (own
move, in every tour or at the end), removed (taken out), a count
changed (fixed: `slot_overrides`, or their own move's amount), the
number of tours (fixed), the workout deleted (that day stays empty).
Kettlebell changes apply to that day's circuit. Every change is logged
in `watch_edits` and shown on the Programme page, with "Rétablir" for
fixed counts. Treadmill and recovery walks are not compared.

Sessions done other than planned (`done_sessions.py`): what was done
on a day is what the person declared on the Sessions page ("Ce n'est
pas ce que j'ai fait", or a day not listed), else what the recorded
activity's name or corrected category says ("Smart Coach - Kettlebell
...", "Calisthenie"...), else the plan. Muscle fatigue, ratings (and so
the level of the type actually done), the progression gate, "session
skipped yesterday" and the dashboard all read it; the morning message
gets `yesterday_session` when yesterday's session was not the plan.

Daily status (`training.compute_status`): red on a strong red signal
(resting-HR spike, low HRV, two "too hard" in a row) or any two reds;
green when nothing is red, a core signal is green, still-tired muscles
do not block it, and the yellows weigh at most 40 % of the signals
(battery and stress count half). The vote bands are gentler than the
"good night" marks (sleep 70+, readiness 55+ is green). Even on a
yellow day a level goes up after `STEADY_SESSIONS` (2) sessions done at
that level without "too hard", or after one rated "too easy"
(`training.steady_progress`); the morning message gets `progress_hint`
with what will make the session go up. Green used to need every one of
up to nine signals green, which held levels still for weeks.

One-tap rating (`quick_links.py`): the 21:00 check-in, when a session
was planned and is not rated yet, sends an ntfy notification with three
buttons ("Trop facile", "Bien", "Trop dur"). Each button POSTs to
`/quick/rate/<user>/<date>/<rating>/<signature>`, a public route: the
signature is an HMAC of exactly that action under a random per-user
secret (`link_secret` setting), so a link can rate that one session and
nothing else, and stops working after 3 days. The buttons need the
site's public address: `public_url` in Réglages, or `PUBLIC_URL`.

Heavier kettlebell (`training.heavier_bell_hint`): once a loaded type
(kettlebell, circuits) is at the top of its levels with the current
bell, Today, the Programme page and the morning message suggest the
next one (+2 kg up to 16 kg: 6 -> 8 -> 10 ..., then 20, 24, 32).
Saving a heavier bell in Réglages lowers those levels so the new weight
starts gently: kettlebell -5 and circuits -3 for a third more weight
(6 -> 8, 12 -> 16), proportionally less for a smaller step.

Meals (`/repas`, `meals.py`): a photo of the plate gives an estimate
per food and in total (calories, protein, carbs, fat); a photo of a
menu or canteen board gives the best choice for what is left of today's
targets. The image is cleaned (EXIF stripped), sent once to the model
(`body.vision_json`, structured output, `MEAL_ANALYSIS_MODEL`) and never
stored; only the estimate is (`meal_estimates`). "Ajouter à ma journée"
writes the total into the day's nutrition under uuid
`smartcoach-meal-<id>`; the Health Connect reconciliation leaves
`smartcoach-` rows alone, so the import does not delete it.
MyFitnessPal has no API to write into and reads no food from Health
Connect, so each estimate shows a "Copier pour MyFitnessPal" line for
its quick add. When the meal comes back from the diary through Health
Connect (`meals.match_logged`, after each ingest, on the Repas page and
on "Ajouter"), an added estimate whose meal type (from the photo's
local time) and calories (within 25 % or 120 kcal, one entry or the
sum of that meal's entries) match loses its own row: the meal counts
once. Each diary meal matches one estimate (`diary_uuids`); "Pas le
même repas" undoes a wrong match and keeps it from matching again.

History per session (`session_history.py`, Progrès page): the morning
run keeps what each session asked for (`coach_log.values_json`: tours,
moves with their counts, own moves and Garmin edits included). Per
session type, the page lists every session (level, tours, moves, reps
and seconds of effort, planned and recorded minutes, rating, done or
not) and compares the latest with the one about four weeks before --
the same kettlebell circuit when there is one, moves matched by their
slot so a harder variant shows as a step up, not as fewer reps. Days
logged before the column existed, and sessions done instead of the
plan, are rebuilt from the library at their level (marked).

## Architecture

```text
worker container cron (times are the defaults; all four are
configurable per deployment -- see "What runs on its own
afterwards" below)
  07:15  run_ingest.py    Garmin API fetch (activities + sleep,
                           trailing GARMIN_LOOKBACK_DAYS window),
                           then rclone-sync the Drive export,
                           extract, upsert the rest into
                           data/db/smart_coach.db (idempotent --
                           full snapshot each time)
  07:45  run_coach.py     metrics.py + progress.py compute today's
                           wellness + weekly trends; training.py
                           applies the deload guardrail (3 reds in a
                           row, OR a single critically negative TSB
                           reading -> forced lighter week) on top of
                           the daily status/level (training.plan_day:
                           an illness -- declared with sick_until or
                           read from RHR/HRV/readiness plus battery,
                           stress and breathing -- turns the day into
                           REST; a red day the level system cannot
                           lighten further becomes a flat RECOVERY
                           walk, never the same level-0 treadmill);
                           circuits come from exercise_library.py
                           (kettlebell_kg in Settings swaps squat,
                           lunge, bridge, dips and superman for loaded
                           goblet squat/swing/press/row ladders; the
                           "kettlebell" session type is a 20 s on /
                           10 s off interval workout of six standing
                           moves, timed on the watch)
                           ladders (double progression: reps climb a
                           range, then a harder variant; the week's
                           alternative rotates); muscles.py turns
                           every logged activity into per-muscle
                           fatigue (36 h half-life) and holds a level
                           when tonight's main muscles are still
                           tired; each level change is stored with its
                           reason (coach_log.level_reason);
                           metrics.movement_summary judges yesterday's
                           steps/floors/intensity minutes/sitting time/
                           stress/battery against the goals, and
                           desk.py picks silent exercises for a shared
                           office from those flags; gcal.py checks the
                           user's real calendar for conflicts and
                           moves tonight's slot if needed before
                           updating the event; garmin_api.py pushes
                           tonight's session to the watch as a
                           scheduled workout; weather.py adds today's
                           forecast as context if a city is set;
                           coach_brief.py decides the day's angle and
                           the ONE point about yesterday worth making
                           (all data-quality caveats applied there);
                           llm.py (claude -p) writes that brief up as
                           a short, warm message -- no sections --
                           (try it: preview_message.py --live);
                           COACH_MODEL picks the model, LLM_FALLBACKS
                           the providers tried when it fails (API,
                           OpenAI-compatible/Ollama), and a plain
                           message built from the brief alone
                           (plain_message.py) always goes out last; achievements.py checks/
                           announces unlocks; notify.py pushes it;
                           logged to coach_log
  16:00  run_checkin.py   afternoon: refreshes today's Garmin
                           rollups, then nudges if hydration/steps are
                           meaningfully behind pace or you have sat
                           7 h+, with one silent desk exercise --
                           silent if on track (no running commentary)
  21:00  run_checkin.py   evening: nudges to wind down early if the
                           last 3 nights are meaningfully short on
                           sleep -- silent otherwise

Note: exercise/sleep/wellness are Garmin-API-fresh (same day), and
Garmin's whole-day rollup covers today's steps/resting HR too. The
Health-Connect-only fields it does not carry (weight, nutrition,
body fat, ...) still lag a day -- that export syncs once overnight,
so those numbers reflect yesterday until the next ingest.

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

Two things that bite here. **The value needs a colon** --
`gdrive:HealthConnectExports`, or bare `gdrive:` for the Drive root,
but never `gdrive`. rclone reads a colonless value as a *local* path
and fails with `directory not found`, which points nowhere near the
mistake.

**Finding the export:** it is named from the phone's locale ("Santé
Connect.zip" on a French device), and `rclone lsd` lists directories
only, so it never appears there. Use
`rclone lsf gdrive: -R --include "*.zip"`. Several Android builds
don't let you choose the destination at all and always write to the
Drive root. That's fine — **point `RCLONE_REMOTE` straight at the
file**, quoting included:

```bash
RCLONE_REMOTE=gdrive:Santé Connect.zip
```

That fetches exactly that one file and touches nothing else, which is
what you want when the export shares a folder with unrelated
archives. Pointing at a folder (`gdrive:` for the root) also works:
only the zips directly inside it are fetched — never a recursive
sweep of the account — rclone transfers them incrementally, and the
export is recognised by its contents, so neighbours are ignored
rather than mistaken for it when they happen to be newer.

**And if rclone's prompt asked for a `client_id` and you pasted the
OAuth client from step 3** -- the natural thing to do, since you've
just made one -- that Cloud project only has the *Calendar* API
enabled. Drive calls then fail with `Error 403: Google Drive API has
not been used in project N before or it is disabled`. Enable the Drive
API for that same project
(`console.developers.google.com/apis/api/drive.googleapis.com/overview?project=<N>`,
the error quotes the number) and give it a couple of minutes. Leaving
`client_id` blank avoids this entirely by using rclone's own shared
credentials, at the cost of sharing their rate limit.

`RCLONE_REMOTE` in `.env` only names *that rclone remote itself* --
`run_ingest.py` actually reads a separate **per-user** `rclone_remote`
setting to decide whose export to sync, and it defaults to empty for
every account, admin included. Set it once you've signed up (step 6):
Settings page -> "Remote rclone" -> the same value as `RCLONE_REMOTE`
(e.g. `gdrive:HealthConnectExports`). Skipping this leaves
steps/nutrition/hydration/weight silently empty forever -- `run_ingest.py`
prints `no rclone_remote configured, skipping HC export` for that
account, but nothing surfaces it on the dashboard.

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
>
> **Working through a jump host, or can't forward ports at all?** Add
> `--manual` to the command:
>
> ```bash
> docker compose run --rm -it -p 8765:8765 \
>     smart_coach-worker python setup_calendar.py <your-account> --manual
> ```
>
> It skips the local callback server. Open the printed URL, approve
> access — the page it redirects to afterwards will fail to load, which
> is fine; copy the full URL from the browser's address bar (it starts
> with `http://localhost:8765/?...`) and paste it back at the prompt.

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
| `INGEST_TIME` (07:15) | ingestion: Garmin + the phone's export |
| `COACH_TIME` (07:45) | readiness, tonight's session, calendar, coaching message |
| `CHECKIN_AFTERNOON_TIME` (16:00) | afternoon check-in — silent unless you are falling behind |
| `CHECKIN_EVENING_TIME` (21:00) | evening check-in, same rule |

All four are `HH:MM` in `TZ`, set in `.env`; the defaults above apply
when they're unset. **`INGEST_TIME` has to fall after you actually get
up.** Garmin only publishes a night's sleep score, HRV and training
readiness once the sleep session has *ended* and the watch has synced,
so an ingest that runs while you're still asleep collects none of them
and the coaching message goes out with no recovery signals behind its
verdict — body battery and stress are all it will have. Allow ~15-30
minutes after waking for the watch to sync. An unparseable value falls
back to the default rather than writing a crontab cron would reject.

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
  `setup_calendar.py` for that account. It tests the existing token
  first: one Google refuses is kept as `.bak` and replaced; a working
  one is left alone unless you pass `--replace`.
- `setup_calendar.py` prints a URL and then seems to hang: that is it
  waiting for the approval. If the browser says the page cannot be
  reached after you approve, the redirect never got back — open the SSH
  session with `-L 8765:localhost:8765` and try again, or rerun with
  `--manual` if nothing can reach `localhost:8765` on the server at all
  (e.g. you only reach it through a jump host).
- Google refuses the approval with "app is blocked" or "not verified":
  the consent screen is in Testing mode and your address is not in
  **Test users**.
- `ModuleNotFoundError` or "can't open file" in the worker's logs means
  the image is older than the code: `docker compose up -d --build`.
- `rclone` fails with `Error 403: Google Drive API has not been used in
  project N` (or `SERVICE_DISABLED`): the OAuth client rclone is using
  belongs to a Cloud project without the Drive API turned on — usually
  because the step 3 Calendar client got reused here. Enable it at
  `console.developers.google.com/apis/api/drive.googleapis.com/overview?project=N`
  and wait a couple of minutes. `doctor.py` reports this one by name.
- `rclone` fails with `directory not found` and a notice that the remote
  "refers to a local folder": the remote is missing its colon. It must
  be `gdrive:HealthConnectExports`, not `gdrive` — in `.env`
  (`RCLONE_REMOTE`) *and* in Settings > "Remote rclone", which are read
  independently.
- `setup_garmin.py` fails with `429` from `mobile+cffi`/`mobile+requests`
  then a `widget+cffi` error: the first two are Garmin's IP rate limit
  (wait it out, don't keep retrying — that extends the block) and the
  third is Garmin's now-mandatory email MFA, which `garminconnect`'s
  bundled login can't complete. `get_client` (`ingest/garmin_api.py`)
  hands the interactive login to `garmin-auth` instead, which prompts
  for the emailed code correctly — this needs `docker compose build`
  to pick up if the image predates it.
- `nutrition_today` / Progress page empty: nutrition logging is new
  and sparse by design — Progress degrades to "pas assez de donnees"
  rather than a misleading chart until enough history accumulates.
- Settings page's ingestion status table shows the last row count
  per table — a stale timestamp there means rclone/cron is the thing
  to check, not the dashboard.
