FROM python:3.12-slim AS base

# Thin server image: feed polling, RSS generation, /api/jobs/* queue, file
# serving. No ffmpeg, no Whisper — all heavy processing runs on the Mac
# worker and uploads results via the queue API.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer)
COPY pyproject.toml README.md ./
RUN uv sync --no-dev --no-install-project

# Copy application code
COPY src/ src/
COPY prompts/ prompts/
RUN uv sync --no-dev

VOLUME /data

EXPOSE 8080

CMD ["uv", "run", "podwash", "--host", "0.0.0.0", "--port", "8080"]
