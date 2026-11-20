# One image for gateway, outbox publisher and workers (different commands).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# FFmpeg/ffprobe for the EXTRACTING_FRAMES stage (ADR-16): decodes real frames from the downloaded
# source video (Debian package, no recommended extras). yt-dlp itself is a Python requirement. No
# JavaScript runtime is installed: yt-dlp currently downloads YouTube streams without one (verified
# 2026-11-20; see ADR-16).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --root-user-action=ignore -r requirements.txt

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 app

COPY notesagent ./notesagent
COPY migrations ./migrations

# ---------------------------------------------------------------- runtime
FROM base AS runtime
USER app
CMD ["python", "-m", "notesagent.worker"]

# ---------------------------------------------------------------- test
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --root-user-action=ignore -r requirements-dev.txt
COPY tests ./tests
COPY pytest.ini .
USER app
CMD ["pytest"]
