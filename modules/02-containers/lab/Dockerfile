# syntax=docker/dockerfile:1.7
#
# Multi-stage build for flask-todo.
#
#   Stage 1 (builder): install Python deps into a virtualenv.
#   Stage 2 (runtime): copy the venv on top of a slim base. No compilers, no caches.
#
# The default build context is this directory (lab/). The application source
# lives in sample-app/, which the build pulls in via a named build context
# called "src". docker-compose.yaml in this directory wires both automatically:
#
#   docker compose up --build
#
# Equivalent CLI command (run from this directory):
#
#   docker buildx build \
#     --build-context src=../../../sample-app \
#     -t flask-todo:dev \
#     .

# --- Stage 1: builder ----------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Build the virtualenv at /opt/venv. Keeping it out of /app means a bind-mounted
# source dir during local dev cannot shadow it.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Install deps in their own layer. The cache mount keeps re-builds fast without
# bloating the final image. requirements.txt comes from the named "src" context.
# hadolint ignore=DL3022
COPY --from=src requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# --- Stage 2: runtime ----------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends procps=2:4.0.2-3 \
    && rm -rf /var/lib/apt/lists/*

# OCI metadata: traceability for scanners, registries, and CI.
LABEL org.opencontainers.image.title="flask-todo" \
      org.opencontainers.image.description="Flask + Postgres todo service" \
      org.opencontainers.image.source="https://gitlab.arbisoft.com/arbisoft/devops-learning-path"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PORT=8000

# Non-root user. Numeric UID so a K8s securityContext.runAsUser can match later.
RUN groupadd --system --gid 1001 app && \
    useradd  --system --uid 1001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# Bring the venv from the builder. Nothing else from that stage ships.
COPY --from=builder /opt/venv /opt/venv

# Healthcheck helper from the default context (lab/); app source from the
# named "src" context. Source goes last because it changes most often, so
# previous layers stay cached.
# hadolint ignore=DL3022
COPY            --chown=app:app healthcheck.py ./
# hadolint ignore=DL3022
COPY --from=src --chown=app:app app.py         ./

USER 1001

EXPOSE 8000

# Liveness probe. Pure stdlib, so the image stays curl-free.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "/app/healthcheck.py"]

# Exec form via sh -c so ${PORT} expands at runtime. The `exec` replaces the
# shell with gunicorn, so gunicorn becomes PID 1 and gets SIGTERM directly.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --access-logfile - --error-logfile - app:app"]
