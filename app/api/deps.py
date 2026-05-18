"""API Dependencies (Auth via API Secret Key)"""

import secrets
from fastapi import Depends, HTTPException, Request, status

from app.config import settings
from app import firestore as fs


def require_api_key(request: Request) -> None:
    """
    Verify API secret — mirrors the Node.js requireApiKey middleware.
    Accepts key from:
      - x-api-key header
      - Authorization: Bearer <key> header
      - ?key= query param
    """
    key = (
        request.headers.get("x-api-key")
        or request.headers.get("authorization", "").replace("Bearer ", "").strip() or None
        or request.query_params.get("key")
    )
    api_secret = settings.API_SECRET
    if not api_secret:
        # No secret configured — allow through (dev mode)
        return
    if not key or not secrets.compare_digest(key, api_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


async def get_current_business(
    request: Request,
    _: None = Depends(require_api_key),
) -> dict:
    """Return the Firestore business document identified by ?biz_id= or x-biz-id header."""
    biz_id = request.query_params.get("biz_id") or request.headers.get("x-biz-id")
    if not biz_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="biz_id is required (query param or x-biz-id header).",
        )
    business = fs.get_business_by_owner(biz_id)
    if not business:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No business found for this account.",
        )
    return business
