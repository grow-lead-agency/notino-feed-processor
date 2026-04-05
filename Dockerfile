FROM python:3.12-slim

# System deps — curl + cron + supercronic (lightweight cron for containers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Supercronic — production-grade cron for containers (no syslog noise)
ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64
ENV SUPERCRONIC_SHA1SUM=3f9e950c08a7d1dbfb980b07dfc1e5432e6e71c2
RUN curl -fsSL "$SUPERCRONIC_URL" -o /usr/local/bin/supercronic \
    && echo "$SUPERCRONIC_SHA1SUM  /usr/local/bin/supercronic" | sha1sum -c - \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Directories
RUN mkdir -p /app/cache /app/logs /app/feeds

# Crontab is mounted/copied at runtime
COPY crontab /app/crontab

CMD ["/usr/local/bin/supercronic", "/app/crontab"]
