FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install dependencies and Microsoft ODBC driver with updated key handling
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

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip show sqlmodel

# Copy application code
COPY . .

# Set entrypoint and default command
ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5500"]
CMD ["--reload"]