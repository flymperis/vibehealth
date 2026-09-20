# VibeHealth image: React frontend built with Node, served by the FastAPI app on Python.
#
#   docker build -t vibehealth .            (Docker)
#   podman build --format docker -t vibehealth .   (Podman: --format docker keeps the HEALTHCHECK)

# Base images are pinned to a minor version tag; pinning them by digest (and hashing the Python
# requirements) is a known follow-up, see docs/DESIGN.md. Dependabot proposes updates weekly.

# --- 1. frontend ------------------------------------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build
# Dependencies first: this layer is reused until the lock file changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- 2. runtime -------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    VIBEHEALTH_FRONTEND=/app/static

# Fixed uid/gid so that a bind-mounted data folder can be prepared for it (see compose.yaml).
RUN groupadd --gid 10001 vibehealth \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin vibehealth

WORKDIR /app

# Runtime requirements only (pinned); tests and dev tools are not installed.
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# The app is imported as `app.main`; the sandbox child (`python -c ...`) finds the same package
# through /app, and runs with the same interpreter and the same non-root user.
COPY backend/app ./app
COPY --from=frontend /build/dist ./static

# The data folder belongs to the app user. A named volume copies this ownership on first use.
RUN mkdir /data && chown 10001:10001 /data && chmod 700 /data
VOLUME /data

USER 10001:10001
EXPOSE 5001

# Python instead of curl: nothing else has to be installed. 127.0.0.1 is always an allowed Host.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:5001/api/health', timeout=4).read()"]

# ONE worker on purpose: the reading queue, login throttle and upload limits live in memory,
# and the app refuses to start with more (WEB_CONCURRENCY / --workers).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5001"]
