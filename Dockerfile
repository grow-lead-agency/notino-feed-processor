# ── Build stage (compile deps) ────────────────────────────────────
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage (no gcc, no dev headers) ────────────────────────
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates libpq5 util-linux \
    && rm -rf /var/lib/apt/lists/*

# Supercronic — production-grade cron for containers
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

# Copy compiled Python packages from builder
COPY --from=builder /install /usr/local

# Non-root user
RUN useradd -m -s /bin/bash appuser

WORKDIR /app

# App code
COPY . .

# Runtime directories (volumes will override these)
RUN mkdir -p /app/cache /app/logs /app/feeds && chown -R appuser:appuser /app

USER appuser

CMD ["/usr/local/bin/supercronic", "/app/crontab"]
