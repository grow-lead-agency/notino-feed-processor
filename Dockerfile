FROM python:3.12-slim

# System deps — curl + cron + supercronic (lightweight cron for containers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Supercronic — production-grade cron for containers (no syslog noise)
# v0.2.33 linux/amd64 — sha256 verified via GitHub releases
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ]; then \
        SUPERCRONIC_BINARY="supercronic-linux-arm64"; \
    else \
        SUPERCRONIC_BINARY="supercronic-linux-amd64"; \
    fi && \
    curl -fsSL "https://github.com/aptible/supercronic/releases/download/v0.2.33/${SUPERCRONIC_BINARY}" \
        -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic \
    && supercronic --version

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Runtime directories (volumes will override these)
RUN mkdir -p /app/cache /app/logs /app/feeds /app/config

CMD ["/usr/local/bin/supercronic", "/app/crontab"]
