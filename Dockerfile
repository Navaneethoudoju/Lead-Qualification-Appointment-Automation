# AI LeadFlow — backend Dockerfile
#
# Multi-stage-lite build: a single slim Python image, non-root user, and a
# healthcheck that hits the app's own /health endpoint (which already
# reports llm_mode/calendar_mode/crm_mode/admin_auth_mode — see
# app/main.py — so `docker inspect` on this container tells you at a
# glance whether it's running in mock mode or wired to real services).
#
# NOTE: this Dockerfile has not been built/run in this project's build
# sandbox (no Docker daemon available there — see PROGRESS.md). It's
# written to the same standard as everything else here: correct by
# inspection, with every deviation from "just works" called out, rather
# than claimed-and-unverified. Build it once with network + Docker
# available (`docker build -t ai-leadflow .`) before trusting it in CI/CD.

FROM python:3.11-slim AS base

# System deps kept minimal on purpose — the web layer needs
# fastapi/uvicorn/pydantic, and the core engine (app/core/) now requires
# psycopg2/redis/chromadb (session 6: Postgres + Redis + real vector store,
# see requirements.txt — no longer an optional/commented-out stack).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (better layer caching — only re-runs when
# requirements.txt actually changes, not on every code edit).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY app ./app
COPY knowledge_base ./knowledge_base
COPY frontend ./frontend

# Non-root user — running as root in a container is an avoidable risk with
# zero benefit here.
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# /app/data now holds only the persistent Chroma vector store
# (AI_LEADFLOW_CHROMA_DIR) — actual app data lives in Postgres/Redis,
# configured via DATABASE_URL/REDIS_URL at run time (see
# docker-compose.yml), not baked in here.
ENV AI_LEADFLOW_CHROMA_DIR=/app/data/chroma
RUN mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
