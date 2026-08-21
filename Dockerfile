# Biexce Social — application image (app + one-shot alembic + one-shot
# data-copy). Build context is the repo root; .dockerignore keeps secrets,
# databases and media out of the image (see .dockerignore).
#
#   docker build -t biexce-social:local .
#
# Runtime configuration (all via environment, see compose.yaml):
#   TING_DATABASE_URL   postgresql+psycopg://user:pass@db:5432/biexce_social
#   TING_JWT_SECRET     openssl rand -hex 32
#   TING_UPLOADS_DIR    /app/uploads (compose mounts a named volume here)
#
# The entrypoint runs a SINGLE uvicorn worker on purpose: rate limiting,
# /metrics counters and upload quotas are in-process state.

ARG PYTHON_BASE=python:3.13-slim
FROM ${PYTHON_BASE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root runtime user.
RUN useradd --create-home --uid 10001 app

WORKDIR /app

# Dependencies first (layer cache): pinned prod lock, then the application
# itself without re-resolving dependencies. The editable install keeps
# ``ting_ting/__file__`` under /app so ``ting_ting.database`` resolves
# alembic.ini from the repo root; the egg-info it leaves in the source tree
# is build metadata — remove it so no *.egg-info leak can reach the image
# (CI image hygiene asserts this). The PEP 660 editable finder in
# site-packages does not depend on the egg-info directory at import time.
COPY requirements.lock pyproject.toml ./
COPY ting_ting ./ting_ting
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps -e . \
    && rm -rf /app/ting_ting.egg-info

# Alembic lives next to the package: `ting_ting.database` locates alembic.ini
# relative to the repo root, so the migration service and the app's embedded
# schema validation both work from /app.
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

RUN mkdir -p /app/uploads && chown app:app /app/uploads

USER app

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=4s --start-period=15s --retries=12 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"

# One worker (in-process rate limit / metrics / upload quota).
CMD ["python", "-m", "uvicorn", "ting_ting.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]