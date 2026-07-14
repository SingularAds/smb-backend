#!/usr/bin/env bash
# Local smoke test for the Dockerfile — run this before pushing ANY change to
# Dockerfile, docker-compose.yml, or dashboard/ build config.
#
# Why this exists: `docker build` succeeding is not enough. On 2026-07-14 a
# hand-edit merged the HEALTHCHECK and CMD instructions into one; the image
# built cleanly (HEALTHCHECK's CMD sub-clause is valid syntax) but the
# container had no entrypoint, exited immediately on every Cloud Run
# revision, and the break wasn't visible until the CI/CD deploy step failed
# in production. `docker build` cannot catch that class of bug — only
# actually running the container and hitting its endpoints can. That's all
# this script does.
#
# Usage: scripts/verify_docker_build.sh   (run from the smb-backend root)

set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose"
FAIL=0

cleanup() {
  echo "── tearing down test container ──"
  $COMPOSE down >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "── building image ──"
$COMPOSE build

echo "── starting container ──"
$COMPOSE up -d

PORT="$(grep -oE '^PORT=.*' .env 2>/dev/null | cut -d= -f2)"
PORT="${PORT:-8000}"
BASE="http://localhost:${PORT}"

echo "── waiting for the app to come up on ${BASE} ──"
ready=0
for _ in $(seq 1 30); do
  if curl -sf "${BASE}/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done

if [[ "$ready" -ne 1 ]]; then
  echo "FAIL: /health never responded — container likely has no working CMD or crashed on startup."
  echo "── last container logs ──"
  $COMPOSE logs --tail=80
  exit 1
fi
echo "PASS: /health responded"

echo "── checking /dashboard/ ──"
dash_status="$(curl -s -o /tmp/dashboard_body.html -w '%{http_code}' "${BASE}/dashboard/")"
if [[ "$dash_status" != "200" ]]; then
  echo "FAIL: /dashboard/ returned HTTP ${dash_status} (expected 200) — was dashboard/dist copied into the image?"
  FAIL=1
elif ! grep -q 'assets/index-.*\.js' /tmp/dashboard_body.html; then
  echo "FAIL: /dashboard/ did not reference a built JS bundle — dist looks stale or empty."
  FAIL=1
else
  asset_path="$(grep -oE 'src="[^"]*\.js"' /tmp/dashboard_body.html | head -1 | sed -E 's/src="(.*)"/\1/')"
  asset_status="$(curl -s -o /dev/null -w '%{http_code}' "${BASE}${asset_path}")"
  if [[ "$asset_status" != "200" ]]; then
    echo "FAIL: dashboard JS bundle ${asset_path} returned HTTP ${asset_status}."
    FAIL=1
  else
    echo "PASS: /dashboard/ serves index.html and its JS bundle"
  fi
fi
rm -f /tmp/dashboard_body.html

if [[ "$FAIL" -ne 0 ]]; then
  echo "── last container logs ──"
  $COMPOSE logs --tail=80
  echo ""
  echo "RESULT: FAILED — do not push."
  exit 1
fi

echo ""
echo "RESULT: all checks passed — safe to push."
