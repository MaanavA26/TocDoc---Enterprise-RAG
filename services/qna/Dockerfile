FROM python:3.12-slim

# Install system dependencies and Microsoft ODBC driver
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gnupg2 \
        curl \
        apt-transport-https \
        unixodbc \
        unixodbc-dev \
        gcc \
        g++ \
        libgssapi-krb5-2 && \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /usr/share/keyrings/microsoft.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/10/prod buster main" > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies before copying code (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip show sqlmodel

# Copy application code
COPY . .

# Run as non-root user for container security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5500

# UVICORN_WORKERS can be overridden via environment variable
ENV UVICORN_WORKERS=2

ENTRYPOINT ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port 5500 --workers ${UVICORN_WORKERS}"]