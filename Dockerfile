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

# Every module at the root, not a hand-kept list. The explicit list silently
# dropped weather.py and run_checkin.py when they were added: the image built
# fine, and the failure only showed up hours later in cron output --
# "ModuleNotFoundError: No module named 'weather'" at 06:00 and
# "can't open file '/app/run_checkin.py'" at 16:00 and 21:00.
COPY *.py ./
COPY ingest/ ./ingest/
COPY web/ ./web/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["cron-foreground"]
