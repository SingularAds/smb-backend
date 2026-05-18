"""Google Calendar OAuth2 Connection

Handles the OAuth2 flow for connecting a business owner's Google Calendar.
After successful authorization, stores the connection status in Firestore
so the onboarding flow can verify it from the database.

Routes:
  GET /api/v1/calendar/connect?business_id=xxx  → starts OAuth flow
  GET /api/v1/calendar/callback                  → handles OAuth callback
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app import firestore as db

logger = logging.getLogger(__name__)

router = APIRouter()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar "
    "https://www.googleapis.com/auth/userinfo.email"
)


# ── State signing (CSRF protection) ──────────────────────────────────────────

def _sign_state(business_id: str) -> str:
    """Create a signed state parameter to prevent CSRF / tampering."""
    data = json.dumps({"bid": business_id})
    sig = hmac.new(
        settings.SECRET_KEY.encode(), data.encode(), hashlib.sha256
    ).hexdigest()[:16]
    return base64.urlsafe_b64encode(f"{data}|{sig}".encode()).decode()


def _verify_state(state: str) -> dict | None:
    """Verify and decode the OAuth state parameter."""
    try:
        decoded = base64.urlsafe_b64decode(state).decode()
        data_str, sig = decoded.rsplit("|", 1)
        expected = hmac.new(
            settings.SECRET_KEY.encode(), data_str.encode(), hashlib.sha256
        ).hexdigest()[:16]
        if hmac.compare_digest(sig, expected):
            return json.loads(data_str)
    except Exception:
        pass
    return None


def _get_redirect_uri() -> str:
    return settings.GOOGLE_REDIRECT_URI or f"{settings.BASE_URL}/api/v1/calendar/callback"


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/connect")
async def calendar_connect(business_id: str = Query(..., min_length=1)):
    """Initiate Google Calendar OAuth2 flow for a business."""
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(500, "Google OAuth not configured")

    biz = db.get_business_by_id(business_id)
    if not biz:
        raise HTTPException(404, "Business not found")

    state = _sign_state(business_id)
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": _get_redirect_uri(),
        "response_type": "code",
        "scope": CALENDAR_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url)


@router.get("/callback")
async def calendar_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
):
    logger.debug("Received calendar callback with code=%s, state=%s, error=%s", code, state, error)
    """Handle Google OAuth2 callback — exchange code for tokens and store."""
    if error:
        logger.warning("Calendar OAuth error: %s", error)
        return HTMLResponse(
            _result_page("Authorization Failed", f"Google returned: {error}"),
            status_code=400,
        )

    if not code or not state:
        raise HTTPException(400, "Missing code or state")

    state_data = _verify_state(state)
    if not state_data:
        raise HTTPException(400, "Invalid or expired state parameter")
    logger.debug("Decoded state data: %s", state_data)
    business_id = state_data.get("bid")
    if not business_id:
        raise HTTPException(400, "Invalid state data")

    biz = db.get_business_by_id(business_id)
    if not biz:
        raise HTTPException(404, "Business not found")

    # Exchange authorization code for tokens
    redirect_uri = _get_redirect_uri()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )

    if resp.status_code != 200:
        logger.error("Token exchange failed: %s %s", resp.status_code, resp.text)
        return HTMLResponse(
            _result_page("Authorization Failed", "Could not exchange authorization code."),
            status_code=400,
        )

    tokens = resp.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    logger.info(
        "[calendar/callback] business=%s | access_token=%s... | refresh_token=%s",
        business_id,
        (access_token or "")[:12],
        "PRESENT" if refresh_token else "MISSING (re-auth without consent?)",
    )
    logger.debug("[calendar/callback] access_token preview: %s...", (access_token or '')[:12])
    logger.debug("[calendar/callback] refresh_token: %s", 'PRESENT' if refresh_token else 'MISSING')

    if not access_token:
        logger.error("[calendar/callback] No access_token in Google response: %s", tokens)
        return HTMLResponse(
            _result_page("Authorization Failed", "No access token received from Google."),
            status_code=400,
        )

    # Fetch owner's email (used as calendar ID)
    calendar_email = ""
    async with httpx.AsyncClient(timeout=10.0) as client:
        info_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if info_resp.status_code == 200:
        calendar_email = info_resp.json().get("email", "")
        logger.debug("[calendar/callback] owner email resolved: %s", calendar_email)
    else:
        logger.warning("[calendar/callback] userinfo failed: %s", info_resp.text)

    # Never overwrite an existing refresh_token with an empty value.
    # Google only sends refresh_token on the first consent; subsequent re-auths
    # return only access_token.  Keep the existing token if none was returned.
    existing_refresh = (biz or {}).get("calendarRefreshToken", "")
    final_refresh_token = refresh_token or existing_refresh
    if not final_refresh_token:
        logger.error(
            "[calendar/callback] No refresh_token available for business %s. "
            "Force re-consent by visiting /connect again.",
            business_id,
        )
        return HTMLResponse(
            _result_page(
                "Authorization Incomplete",
                "No refresh token received. Please click the link again and ensure "
                'you click <b>"Allow"</b> on the Google consent screen.',
            ),
            status_code=400,
        )

    # Store calendar connection in business document
    update_data = {
        "calendarConnected": True,
        "calendarConnectedAt": datetime.utcnow().isoformat(),
        "ownerCalendarId": calendar_email or "primary",
        "calendarRefreshToken": final_refresh_token,
        "calendarAccessToken": access_token,
    }
    db.update_business_doc(business_id, update_data)

    logger.info(
        "[calendar/callback] Connected business=%s email=%s refresh_token=%s",
        business_id,
        calendar_email,
        "STORED" if final_refresh_token else "MISSING",
    )

    return HTMLResponse(_result_page(
        "✅ Calendar Connected!",
        "Your Google Calendar is now linked to your AI receptionist.<br>"
        "You can close this window and go back to WhatsApp.<br><br>"
        "Reply <b>DONE</b> in the chat to continue.",
    ))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _result_page(title: str, body: str) -> str:
    return (
        "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
        "<body style='text-align:center;padding:50px;font-family:sans-serif;'>"
        f"<h2>{title}</h2><p>{body}</p>"
        "</body></html>"
    )
