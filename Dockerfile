FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (curl for healthcheck)
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create media directory
RUN mkdir -p /app/media /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Use multiple workers in production; override at runtime via WORKERS env var
# Cloud Run injects $PORT (default 8080); fall back to 8080 if unset
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers ${WORKERS:-4}
