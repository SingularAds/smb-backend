# Deployment — CI/CD, the dashboard build, and two outages

This document covers how `smb-backend` gets from a `git push` to a running
Cloud Run service, and the root cause of two production outages (2026-07-07
and 2026-07-14) so the same mistakes don't ship a third time.

## How deploys work

- **Source of truth**: GitHub repo `SingularAds/smb-backend`, branch `main`.
- **Trigger**: a single Cloud Build trigger (`ff10ce34-cf0e-4704-a266-ea49ec8c8bd1`)
  fires on every push to `main`. It builds the image from the root
  `Dockerfile`, pushes it to Artifact Registry, then runs
  `gcloud run services update` against **`boomreception-api` in
  `southamerica-east1`** — the only region this service runs in.
- **No staging environment.** A push to `main` is a push to production.
  There is no build-time check that the container actually *runs* — Cloud
  Build's `docker build` step only proves the image compiles, not that it
  starts. That gap is what let both incidents below reach production.

A companion trigger used to also deploy `boomreception-api` to
`europe-west1`, and the `whatsapp-meow` repo used to also deploy
`whatsapp-bridge` to `us-central1`. Both of those regions were replaced by
`southamerica-east1` equivalents; the old services were deleted but their
Cloud Build triggers were not, so every push kept re-deploying to a service
that no longer needed to exist. Both orphaned triggers (and the leftover
`boomreception-api`/`europe-west1` service, which had never successfully
served traffic) were deleted on 2026-07-14. Each repo now has exactly one
trigger, pointed at the region actually in use.

## Incident 1 — dashboard never built (2026-07-07)

**Symptom:** every Cloud Build run failed at the `docker build` step with
`tsc` errors like:

```
src/components/ActivityChart.tsx(13,36): error TS2307: Cannot find module '../lib/format' or its corresponding type declarations.
```

...repeated across 12 files, all importing from the same missing module.

**Root cause:** `dashboard/src/lib/format.ts` was imported by 12 components
(`fmtDate`, `fmtNumber`, `fmtPhone`, `fmtPercent`, `fmtRelative`, `fmtToken`,
`fmtDateTime`, `fmtDuration`) but the file itself was never added when the
dashboard was committed. The dashboard had never been buildable since it was
added — it just hadn't been wired into the Docker build yet, so nothing had
tried to compile it in CI until a Dockerfile change added the frontend build
stage.

A separate, unrelated `.gitignore` bug compounded this: a bare `lib/`
pattern (meant for Python's `venv/lib/`) matches at *any* depth, so it was
silently excluding `dashboard/src/lib/` from git entirely. `.dockerignore`
was also itself listed in `.gitignore`, so it could never be committed
either. Both were fixed (`lib/` → `/lib/`, `.dockerignore` un-ignored).

**Fix (commit `01d93e7`):**
- Added `dashboard/src/lib/format.ts` with the actual formatters the
  components call, matching real usage and the nullable fields documented
  in `dashboard/src/types.ts`.
- Restored the Dockerfile's multi-stage build (Node stage compiles
  `dashboard/dist`, Python stage copies it in — see below).
- Added `.dockerignore` (the local `env/` virtualenv alone is ~466MB and
  was otherwise part of every build context).
- Fixed the two `.gitignore` bugs above.

## Incident 2 — container had no entrypoint (2026-07-14)

**Symptom:** Cloud Build succeeded through the build and push steps, then
failed at deploy:

```
ERROR: (gcloud.run.services.update) The user-provided container failed to
start and listen on the port defined provided by the PORT=8080 environment
variable within the allocated timeout.
```

Cloud Run revision logs showed the container starting and immediately
exiting, with **no application output at all** — no uvicorn startup line,
nothing:

```
Container called exit(0).
```

**Root cause:** a manual Dockerfile edit (commit `dbfbe00`) deleted the
`HEALTHCHECK`'s `curl ... || exit 1` probe line but left the instruction's
trailing `\` line-continuation in place:

```dockerfile
HEALTHCHECK --interval=30s --timeout=240s --start-period=10s --retries=3 \
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

This is syntactically **valid** Dockerfile — `HEALTHCHECK [OPTIONS] CMD
<command>` is correct syntax — so `docker build` never complained. But it
means the `uvicorn` command became `HEALTHCHECK`'s own probe command, not a
top-level `CMD` instruction. With no `CMD` left in the file, Docker fell
back to the base image's default (`python:3.11-slim` → bare `python3`),
which hits EOF on stdin immediately and exits cleanly. The app never ran,
so nothing ever bound to `$PORT`, so Cloud Run's readiness check timed out
on every single deploy — this is why `docker build` succeeding gave no
confidence the image actually worked.

The same commit also added a second, redundant dashboard build directly in
the Python runtime stage (installing Node.js via NodeSource, running
`npm ci && npm run build` a second time) alongside the existing
`dashboard-builder` stage. This didn't cause the outage, but it doubled
build time and bloated the runtime image with a Node.js toolchain it never
needed — the builder stage's output was being copied in and overwriting it
anyway.

**Fix (commit `8147525`):**
- Restored `HEALTHCHECK` and `CMD` as two separate instructions.
- Removed the redundant Node.js install + duplicate dashboard build from
  the runtime stage.
- Added a comment directly above `HEALTHCHECK` in the Dockerfile explaining
  this exact failure mode, so a future hand-edit is less likely to
  reintroduce it silently.

### Current Dockerfile shape

```dockerfile
FROM node:22-alpine AS dashboard-builder   # builds dashboard/dist only — never shipped
...
FROM python:3.11-slim                      # runtime image
...
COPY --from=dashboard-builder /build/dist ./dashboard/dist
...
HEALTHCHECK ... CMD curl -f http://localhost:${PORT:-8080}/health || exit 1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

## Preventing a repeat: `scripts/verify_docker_build.sh`

Neither incident was catchable by `docker build` alone — incident 1 would
have been (it's a real compile error), but incident 2 is exactly the class
of bug that a successful build cannot detect, because the broken Dockerfile
is syntactically valid. The only way to catch it is to actually run the
container and hit its endpoints.

**Run this before pushing any change to `Dockerfile`, `docker-compose.yml`,
or the dashboard build config:**

```bash
scripts/verify_docker_build.sh
```

It builds the image, starts it via `docker compose`, polls `/health`, then
checks that `/dashboard/` returns the built `index.html` and that its JS
bundle actually loads. It tears the container down on exit either way and
fails loudly (with the last 80 lines of container logs) if anything doesn't
come up clean. If this script passes, the class of bug behind both
incidents above cannot be present.

## A note on the deleted europe-west1 service

While investigating incident 2, `boomreception-api`/`europe-west1` was
found to have been failing since its very first revision, for a third,
unrelated reason: it never had the Secret Manager volume mounts for
`serviceAccount.json`/`credentials.json` that `southamerica-east1` has, so
Firebase init raised `FileNotFoundError` on every startup. That service and
its trigger (along with the already-decommissioned
`whatsapp-bridge`/`us-central1` trigger in the `whatsapp-meow` repo) were
permanently deleted on 2026-07-14 rather than fixed, since both regions had
already been replaced by their `southamerica-east1` counterparts.
