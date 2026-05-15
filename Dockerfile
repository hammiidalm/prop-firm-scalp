# =============================================================================
# prop-firm-scalp - Production Docker image
# Multi-stage build: slim Python 3.11 on Debian bookworm (glibc compat with
# Oracle Linux / RHEL derivatives).
# =============================================================================

# --- Stage 1: build dependencies ---
FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

# System deps for compiled wheels (e.g. asyncpg, orjson)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# --- Stage 2: runtime ---
FROM python:3.11-slim-bookworm AS runtime

LABEL maintainer="prop-firm-scalp contributors"
LABEL description="Async scalping bot for prop-firm forex trading via TradeLocker"

# Non-root user for security
RUN groupadd -r scalp && useradd -r -g scalp -d /app -s /sbin/nologin scalp

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY pyproject.toml .

# Create data directory for SQLite journal
RUN mkdir -p /app/data && chown -R scalp:scalp /app

USER scalp

# Expose dashboard API port
EXPOSE 8080

# Healthcheck against the /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Default: run the live trading engine
ENTRYPOINT ["python", "-m", "scripts.run_live"]
