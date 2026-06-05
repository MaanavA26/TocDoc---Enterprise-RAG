FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gnupg2 curl apt-transport-https unixodbc unixodbc-dev gcc g++ \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies before copying code (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run as non-root user for container security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5501

# UVICORN_WORKERS can be overridden via environment variable
ENV UVICORN_WORKERS=2

ENTRYPOINT ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port 5501 --workers ${UVICORN_WORKERS}"]
