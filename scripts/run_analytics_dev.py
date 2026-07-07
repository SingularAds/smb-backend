"""Local dev server for the analytics dashboard ONLY.

Unlike `app.main:app`, this does NOT start the automation scheduler (no real
WhatsApp messages get sent) — safe to leave running while building/testing
the dashboard frontend.

Usage:
    .venv/Scripts/python scripts/run_analytics_dev.py
    # serves http://127.0.0.1:8000/api/v1/analytics/...
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

from app.firebase import init_firebase  # noqa: E402

init_firebase()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from app.api.v1.analytics import router  # noqa: E402

app = FastAPI(title="analytics-dev")
# Parity with app.main: detail responses are hundreds of KB uncompressed.
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api/v1/analytics")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
