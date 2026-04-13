FROM python:3.10-slim

WORKDIR /app

# System dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gnupg2 \
        curl \
        apt-transport-https \
        unixodbc \
        unixodbc-dev \
        gcc \
        g++ && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

EXPOSE 5501

ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5501"]
CMD ["--workers", "2"]
