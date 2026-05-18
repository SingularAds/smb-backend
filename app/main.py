"""BoomReception FastAPI Main Application"""

import socket as _socket
import warnings
import urllib3
import os as _os


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


_environment = (_os.environ.get("ENVIRONMENT") or "development").strip().lower()
_allow_insecure_transport = _truthy(_os.environ.get("ALLOW_INSECURE_TRANSPORT")) or _environment != "production"
_enable_sslip_dns_override = _truthy(_os.environ.get("ENABLE_SSLIP_DNS_OVERRIDE")) or _environment != "production"

# ── DNS patch: Windows DNS refuses *.sslip.io queries ────────────────────────
# nslookup resolves fine via the router (192.168.1.1), but the Windows DNS
# client (used by Python's socket module) returns REFUSED.  We bypass it by
# hard-coding the known IP so the original HTTPS URL and its TLS cert work.
_SSLIP_OVERRIDES: dict[str, str] = {
    "91-99-169-109.sslip.io": "91.99.169.109",
}
_orig_getaddrinfo = _socket.getaddrinfo


def _patched_getaddrinfo(host, port, *args, **kwargs):
    host = _SSLIP_OVERRIDES.get(host, host)
    # Retry up to 3 times for transient Windows DNS failures (errno 11001).
    # httpx runs getaddrinfo in a thread pool so short blocking sleeps are safe.
    import time as _time
    last_exc: Exception | None = None
    for _attempt in range(3):
        try:
            return _orig_getaddrinfo(host, port, *args, **kwargs)
        except OSError as _exc:
            if _exc.errno != 11001:   # only retry "getaddrinfo failed"
                raise
            last_exc = _exc
            if _attempt < 2:
                _time.sleep(0.5)
    raise last_exc  # type: ignore[misc]


if _enable_sslip_dns_override and _environment != "production":
    _socket.getaddrinfo = _patched_getaddrinfo
# ─────────────────────────────────────────────────────────────────────────────

if _allow_insecure_transport and _environment != "production":
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager

from app.config import settings
from app.firebase import init_firebase
from app.services.automation.scheduler import start_scheduler, stop_scheduler

# Import routers
from app.api.v1 import businesses, billing, bookings, calendar, customers, health, recepte, reminders, vapi, voice, webhooks, whatsapp


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    init_firebase()
    start_scheduler()
    yield
    stop_scheduler()
    

from fastapi.staticfiles import StaticFiles 
# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI Voice Receptionist — answers calls, books appointments, speaks every language",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

if settings.ENVIRONMENT.lower() == "production":
    if settings.ALLOW_INSECURE_TRANSPORT:
        warnings.warn("ALLOW_INSECURE_TRANSPORT is enabled in production; disable it for secure TLS validation")
    if settings.ENABLE_SSLIP_DNS_OVERRIDE:
        warnings.warn("ENABLE_SSLIP_DNS_OVERRIDE is enabled in production; disable host overrides in production")
_media_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "media")
_os.makedirs(_media_dir, exist_ok=True)
app.mount("/media", StaticFiles(directory=_media_dir), name="media")
# ── Middleware ──

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=settings.ALLOWED_METHODS,
    allow_headers=settings.ALLOWED_HEADERS,
)

# GZip compression
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ── Routes ──

# Health check
app.include_router(health.router, tags=["Health"])

# VAPI webhook (primary call-handling entry point)
app.include_router(vapi.router, prefix="/vapi", tags=["VAPI"])

# Voice pipeline (Twilio TwiML — kept for direct Twilio flows)
app.include_router(voice.router, prefix="/twilio", tags=["Voice"])

# Bookings API
app.include_router(bookings.router, prefix="/api/v1/bookings", tags=["Bookings"])

# Businesses API (onboarding, management)
app.include_router(businesses.router, prefix="/api/v1/businesses", tags=["Businesses"])

# Customers API
app.include_router(customers.router, prefix="/api/v1/customers", tags=["Customers"])

# Webhooks (Stripe + other integrations)
app.include_router(webhooks.router, prefix="/webhooks", tags=["Webhooks"])

# Billing API (checkout, plan status, billing portal)
app.include_router(billing.router, prefix="/api/v1/billing", tags=["Billing"])

# WhatsApp / whatsmeow bridge webhook (top-level — no prefix)
app.include_router(whatsapp.router, tags=["WhatsApp"])

# Google Calendar OAuth connection — mounted on all registered redirect URIs
# /api/v1/calendar/callback   – internal / direct access
# /oauth/callback             – old ngrok-era redirect URI (kept for backwards compat)
# /auth/google/callback       – new OAuth client registered redirect URI
app.include_router(calendar.router, prefix="/api/v1/calendar", tags=["Calendar"])
app.include_router(calendar.router, prefix="/oauth", tags=["Calendar-OAuth"])
app.include_router(calendar.router, prefix="/auth/google", tags=["Calendar-OAuth-Google"])

# Recepte.co lead ingestion
app.include_router(recepte.router, prefix="/api/v1/recepte", tags=["Recepte"])
# Booking reminders cron endpoint
app.include_router(reminders.router, prefix="/api/v1/reminders", tags=["Reminders"])


# ── Root Endpoint ──

@app.get("/")
async def root():
    """API root endpoint"""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "environment": settings.ENVIRONMENT,
        "docs": "/docs" if settings.DEBUG else "disabled",
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
