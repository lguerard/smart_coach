#!/bin/sh
set -e

# One-off commands (`docker compose run --rm -it smart_coach-worker
# rclone config`, `claude setup-token`, an interactive first run of
# run_coach.py for the Calendar OAuth consent, etc.) just get exec'd
# directly, skipping the dispatch below.

# "07:15" -> "15 7", i.e. the minute+hour fields of a crontab line.
# Anything that isn't HH:MM falls back to the default rather than
# writing a malformed crontab, which cron would reject wholesale --
# taking every other job down with it.
cron_fields() {
    value="$1"
    fallback="$2"
    label="$3"
    if ! echo "$value" | grep -Eq '^([01][0-9]|2[0-3]|[0-9]):[0-5][0-9]$'; then
        echo "entrypoint: $label='$value' is not HH:MM, using $fallback" >&2
        value="$fallback"
    fi
    hour=$(echo "${value%%:*}" | sed 's/^0*\([0-9]\)/\1/')
    minute=$(echo "${value##*:}" | sed 's/^0*\([0-9]\)/\1/')
    echo "$minute $hour"
}

case "$1" in
    web)
        exec uvicorn web.app:app --host 0.0.0.0 --port 8080
        ;;
    cron-foreground)
        # cron doesn't inherit the container's environment, so persist
        # it to a file the crontab lines can source first.
        printenv | sed 's/^\(.*\)$/export \1/' > /app/container.env

        # Wall-clock times in TZ. INGEST_TIME must land after you are
        # actually awake: Garmin only has last night's sleep score,
        # HRV and training readiness once the sleep session has ended
        # and the watch has synced, so an ingest that runs while you
        # are still asleep produces a coaching message with none of
        # the recovery signals it is supposed to weigh.
        ingest_at=$(cron_fields "${INGEST_TIME:-07:15}" "07:15" INGEST_TIME)
        coach_at=$(cron_fields "${COACH_TIME:-07:45}" "07:45" COACH_TIME)
        afternoon_at=$(cron_fields \
            "${CHECKIN_AFTERNOON_TIME:-16:00}" "16:00" CHECKIN_AFTERNOON_TIME)
        evening_at=$(cron_fields \
            "${CHECKIN_EVENING_TIME:-21:00}" "21:00" CHECKIN_EVENING_TIME)

        {
            echo "$ingest_at * * * . /app/container.env; cd /app && python run_ingest.py >> /proc/1/fd/1 2>&1"
            echo "$coach_at * * * . /app/container.env; cd /app && python run_coach.py >> /proc/1/fd/1 2>&1"
            echo "$afternoon_at * * * . /app/container.env; cd /app && python run_checkin.py afternoon >> /proc/1/fd/1 2>&1"
            echo "$evening_at * * * . /app/container.env; cd /app && python run_checkin.py evening >> /proc/1/fd/1 2>&1"
        } | crontab -

        echo "entrypoint: ingest at ${INGEST_TIME:-07:15}, coach at ${COACH_TIME:-07:45}, check-ins at ${CHECKIN_AFTERNOON_TIME:-16:00} and ${CHECKIN_EVENING_TIME:-21:00} (${TZ:-UTC})"

        exec cron -f
        ;;
    *)
        exec "$@"
        ;;
esac
