# ── Stage 1: install Python deps ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build deps for curl-cffi (needs libcurl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libcurl4-openssl-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime with Playwright + Chromium ───────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# System deps needed by Playwright's Chromium at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium runtime libs
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 libgtk-3-0 \
    # curl needed by curl-cffi
    libcurl4 \
    # cleanup
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Install Playwright browsers (Chromium only — smallest footprint)
RUN playwright install chromium

# Copy application source
COPY src/ ./src/

# Cloud Run passes port via PORT env var (default 8080)
ENV PORT=8080

# Start the MCP server
CMD ["python", "src/mcp_server.py"]
