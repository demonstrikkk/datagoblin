# DATAGOBLIN - one Dockerfile, two images, two targets.
#
#   target api  -> FastAPI on :8000
#   target web  -> nginx serving the built SPA and proxying /api to it
#
# The API is never published on its own in a way the browser can reach, and the
# SPA calls /api relative to its own origin, so nothing here needs CORS.
#
# Build:
#   docker build --target api .
#   docker build --target web .
#
# Bloat controls (the whole point of this file):
#   * pip runs with --no-cache-dir into a venv at /opt/venv, so wheels are not
#     left behind in a second copy inside the image.
#   * the builder stage is never copied into the runtime image: no apt, no
#     compiler, no build cache in the final layer.
#   * Playwright's Chromium is NOT installed by default (~450 MB of browser
#     plus system libs). Consequence: Crawl4AI cannot render JavaScript pages,
#     so JS-only sources come back thin. Everything static still works.
#     Opt in only if he actually needs it:
#         docker compose build --build-arg INSTALL_BROWSERS=true
#   * the image contains no tests, no docs, no fixtures, no venv, no .env.
#
# Two args, both overridable at build time.
ARG PYTHON_VERSION=3.11
ARG NODE_VERSION=22

# --------------------------------------------------------------------------
# Stage 1 - frontend build. node + the dev toolchain stay in this stage only.
# --------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS frontend
WORKDIR /build

# Dependencies first: this layer is cached until package-lock.json itself
# changes, so editing source does not re-run npm ci.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# vite.config.js only sets the dev-server proxy; the built bundle calls /api
# relative to its own origin, which nginx answers. No VITE_* needed.
RUN npm run build


# --------------------------------------------------------------------------
# Stage 2 - python dependencies. Also discarded before the runtime image.
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS venv
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# Build toolchain is a safety net for source-only sdists (lxml, curl_cffi,
# psycopg have manylinux wheels on 3.11, so this usually stays unused). It
# costs build time only: none of it reaches the runtime image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Off by default; see the header. --with-deps installs the shared libraries
# Chromium needs, so it must run as root - it does, in this stage.
ARG INSTALL_BROWSERS=false
RUN if [ "$INSTALL_BROWSERS" = "true" ]; then \
        /opt/venv/bin/python -m playwright install --with-deps chromium; \
    fi


# --------------------------------------------------------------------------
# Stage 3 - the API runtime image. The only target most people need.
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS api

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

# Non-root. uid 10001 rather than a name, so a bind-mounted host volume has a
# stable owner to match.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin datagoblin

COPY --from=venv /opt/venv /opt/venv

WORKDIR /app
# app/ (runtime) · scripts/ (db_migrate) · migrations/ · selectors/ · tests are
# intentionally absent. backend/selectors is NOT optional: selectors.py
# resolves its domain schemas as parents[2]/"selectors", i.e. this directory.
COPY backend/app /app/backend/app
COPY backend/scripts /app/backend/scripts
COPY backend/migrations /app/backend/migrations
COPY backend/selectors /app/backend/selectors
COPY backend/requirements.txt /app/backend/requirements.txt

# Created here so a fresh named volume mounted on it inherits this ownership
# (Docker seeds a new empty volume from the image directory). Without it the
# non-root user cannot write the JSONL store and every run fails on startup.
RUN mkdir -p /app/.datagoblin-local \
 && chown -R datagoblin:datagoblin /app

USER datagoblin
EXPOSE 8000

# No curl in slim, so the check uses the interpreter that is already here.
# /api/health is intentionally left ungated by the API-key gate, so this
# reports real health and not a 401 that would look like a dead container.
HEALTHCHECK --interval=30s --timeout=6s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health', timeout=5)" || exit 1

# 0.0.0.0 inside the container; the host port is bound to 127.0.0.1 in
# docker-compose.yaml, so the API is not reachable from the local network.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# --------------------------------------------------------------------------
# Stage 4 - the web runtime image: nginx:alpine (~50 MB) + the built bundle.
# --------------------------------------------------------------------------
FROM nginx:stable-alpine AS web

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend /build/dist /usr/share/nginx/html

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1