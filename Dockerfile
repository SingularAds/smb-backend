# ── Build stage: Node.js to build the dashboard ──
FROM node:18-alpine AS dashboard-builder
 
WORKDIR /build
 
COPY dashboard/package*.json ./
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
 
# Copy the built dashboard from the builder stage
COPY --from=dashboard-builder /build/dist ./dashboard/dist
 
RUN mkdir -p /app/media /data
 
EXPOSE 8080
 
HEALTHCHECK --interval=30s --timeout=240s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1
 
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
 