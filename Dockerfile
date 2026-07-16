FROM python:3.11-slim-bookworm

# cron: worker service scheduler. tzdata: correct wall-clock times.
# curl/ca-certificates/unzip: native installers below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        cron tzdata curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

# Native installers -> standalone binaries, no Node/npm needed.
RUN curl -fsSL https://claude.ai/install.sh | bash
RUN curl -fsSL https://rclone.org/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py metrics.py training.py training_load.py progress.py \
     achievements.py llm.py notify.py gcal.py run_ingest.py run_coach.py \
     manage_users.py ./
COPY ingest/ ./ingest/
COPY web/ ./web/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["cron-foreground"]
