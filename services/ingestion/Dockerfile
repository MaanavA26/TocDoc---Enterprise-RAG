# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Multi-stage build for the ingestion service.
#
#   * builder stage  - installs the build toolchain and compiles all Python
#                       dependencies into an isolated virtualenv.
#   * runtime stage  - copies only the populated virtualenv and the app code.
#                       This service has no ODBC/runtime-native dependency, so
#                       the runtime stage ships no apt packages at all.
#
# Both stages share the same base (python:3.12-slim) so the copied venv is
# ABI- and path-compatible.
# ---------------------------------------------------------------------------

# ---------- Builder ----------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Build toolchain (compilers + ODBC dev headers) lives ONLY in the builder so
# the runtime image carries no compilers.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        unixodbc-dev && \
    rm -rf /var/lib/apt/lists/*

# Create the virtualenv that we hand off to the runtime stage.
RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /app

# Install dependencies first so this layer caches independently of app code.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---------- Runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    UVICORN_WORKERS=2

# Copy the pre-built virtualenv from the builder stage. All third-party deps
# ship manylinux wheels, so no runtime apt packages are required.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Copy application code. The .dockerignore keeps tests, caches and the venv out
# of the build context, so `COPY . .` stays lean. WORKDIR=/app preserves the
# `app:app` import resolution (uvicorn runs with cwd = service dir).
COPY . .

# Run as a dedicated non-root user.
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5501

# Probe the in-process /health route via the stdlib (no curl needed at the app
# layer). urlopen raises HTTPError on a 4xx/5xx response; any other non-200
# status fails the explicit `.status == 200` check. Either path exits non-zero
# and marks the container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5501/health', timeout=4).status == 200 else 1)"]

# UVICORN_WORKERS can be overridden via environment variable.
ENTRYPOINT ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port 5501 --workers ${UVICORN_WORKERS}"]
