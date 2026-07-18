#!/bin/sh
set -e

# One-off commands (`docker compose run --rm -it smart_sport-worker
# rclone config`, `claude setup-token`, an interactive first run of
# run_coach.py for the Calendar OAuth consent, etc.) just get exec'd
# directly, skipping the dispatch below.
case "$1" in
    web)
        exec uvicorn web.app:app --host 0.0.0.0 --port 8080
        ;;
    cron-foreground)
        # cron doesn't inherit the container's environment, so persist
        # it to a file the crontab lines can source first.
        printenv | sed 's/^\(.*\)$/export \1/' > /app/container.env

        {
            echo "30 5 * * * . /app/container.env; cd /app && python run_ingest.py >> /proc/1/fd/1 2>&1"
            echo "0 6 * * * . /app/container.env; cd /app && python run_coach.py >> /proc/1/fd/1 2>&1"
            echo "0 16 * * * . /app/container.env; cd /app && python run_checkin.py afternoon >> /proc/1/fd/1 2>&1"
            echo "0 21 * * * . /app/container.env; cd /app && python run_checkin.py evening >> /proc/1/fd/1 2>&1"
        } | crontab -

        exec cron -f
        ;;
    *)
        exec "$@"
        ;;
esac
