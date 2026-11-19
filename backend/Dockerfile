# One image for gateway, outbox publisher and workers (different commands).
# Phase 4 will add ffmpeg/yt-dlp to the worker; nothing media-related is installed yet.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

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
