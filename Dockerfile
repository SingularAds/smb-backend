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

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Built dashboard assets — served at /dashboard by app/main.py (StaticFiles).
# Do NOT also build the dashboard here: this COPY is the only source of
# dashboard/dist in the runtime image, and installing Node.js in this stage
# just to rebuild it again is dead weight — the builder stage above already
# does it in an image that never ships.
COPY --from=dashboard-builder /build/dist ./dashboard/dist

RUN mkdir -p /app/media /data

EXPOSE 8080

# NOTE: HEALTHCHECK and CMD must stay as two separate instructions. If the
# `curl ... || exit 1` line is ever deleted while the trailing `\` on
# HEALTHCHECK survives, the CMD below silently becomes HEALTHCHECK's own
# probe command instead of the container's entrypoint — Docker then falls
# back to the base image's default command (bare `python3`), which exits
# immediately with no app ever listening on $PORT. That exact bug shipped
# on 2026-07-14 and failed Cloud Run deploys on every region ("Container
# called exit(0)" in the revision logs, no application log lines at all).
# Run scripts/verify_docker_build.sh before pushing any change to this file.
HEALTHCHECK --interval=30s --timeout=240s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
