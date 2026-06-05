# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Multi-stage build for the QnA service.
#
#   * builder stage  - installs the full build toolchain and compiles all
#                       Python dependencies into an isolated virtualenv.
#   * runtime stage  - copies only the populated virtualenv and the app code,
#                       so the published image carries no compilers/headers.
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

# Build toolchain + ODBC dev headers. These live ONLY in the builder so the
# runtime image stays free of compilers. unixodbc-dev is kept here because the
# legacy build provisioned it; the runtime stage needs only the shared library.
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

# Runtime-only system packages.
#   * unixodbc + libgssapi-krb5-2     - shared libraries for the MS ODBC driver.
#   * curl/gnupg/apt-transport-https  - needed transiently to add the Microsoft
#     apt repo for msodbcsql18; purged in the same layer to keep the image lean.
# The ODBC driver is preserved from the legacy image to keep runtime behavior
# identical; only the build toolchain has been moved out to the builder stage.
# pipefail ensures the curl|gpg pipeline fails the build if curl fails.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        gnupg2 \
        apt-transport-https \
        unixodbc \
        libgssapi-krb5-2 && \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /usr/share/keyrings/microsoft.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/10/prod buster main" > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 && \
    apt-get purge -y --auto-remove curl gnupg2 apt-transport-https && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy the pre-built virtualenv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Copy application code. The .dockerignore keeps tests, caches and the venv out
# of the build context, so `COPY . .` stays lean. WORKDIR=/app preserves the
# `app:app` import resolution (uvicorn runs with cwd = service dir).
COPY . .

# Run as a dedicated non-root user.
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5500

# Probe the in-process /health route via the stdlib (no curl needed at the app
# layer). urlopen raises on a non-2xx response (e.g. the 503 unhealthy branch),
# exiting non-zero and marking the container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5500/health', timeout=4).status == 200 else 1)"]

# UVICORN_WORKERS can be overridden via environment variable.
ENTRYPOINT ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port 5500 --workers ${UVICORN_WORKERS}"]
