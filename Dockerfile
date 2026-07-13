# ── Build stage: compile the analytics dashboard (React/Vite) ──
FROM node:22-alpine AS dashboard-builder

WORKDIR /build

COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci

COPY dashboard/ .
RUN npm run build
# ── Runtime stage: Python backend ──
FROM python:3.11-slim

WORKDIR /app

# Install system deps: curl (healthcheck) + Node.js (dashboard build)
RUN apt-get update && apt-get install -y curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Dashboard (React/Vite) — build inside Docker so no manual npm run build ──
COPY dashboard/package*.json ./dashboard/
RUN cd dashboard && npm ci --silent
COPY dashboard/ ./dashboard/
RUN cd dashboard && npm run build

# ── Application source ────────────────────────────────────────────────────────
COPY . .

# Built dashboard assets — served at /dashboard by app/main.py (StaticFiles)
COPY --from=dashboard-builder /build/dist ./dashboard/dist

RUN mkdir -p /app/media /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=240s --start-period=10s --retries=3 \
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
