# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 – Dependency Builder
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install system dependencies required for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip tools
RUN pip install --upgrade pip wheel

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies into isolated install directory
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 – Runtime Image
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Runtime OS dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq5 \
    ca-certificates \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Copy installed dependencies from builder stage
COPY --from=builder /install /usr/local

# Create non-root user
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

# Set working directory
WORKDIR /app

# Copy application source code
# Make sure .dockerignore excludes secrets and unnecessary files
COPY --chown=appuser:appgroup . .

# Create required writable directories
RUN mkdir -p /app/media && chown -R appuser:appgroup /app/media

# Switch to non-root user
USER appuser

# Environment variables
ENV PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

# Expose application port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Proper signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]

# Start FastAPI application
# IMPORTANT:
# Keep WORKERS=1 if APScheduler/background jobs are running
# LOG_LEVEL is lowercased via `tr` because uvicorn only accepts lowercase values
# but .env commonly sets LOG_LEVEL=INFO (uppercase).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS:-1} --timeout-keep-alive 120 --log-level $(echo ${LOG_LEVEL:-info} | tr '[:upper:]' '[:lower:]')"]