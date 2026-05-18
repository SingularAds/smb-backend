"""Apify API client.

Provides lightweight wrappers around two Apify actors used during onboarding:
  - ``apify~instagram-scraper``  — scrape Instagram business profiles
  - ``compass~crawler-google-places`` — fallback for Google Maps share links that
    the regular Google Places API flow cannot resolve

All HTTP calls use ``httpx`` (already a project dependency), so no new
packages are needed.  Every public method is guarded: if ``APIFY_API_KEY`` is
empty the method returns ``None`` immediately, letting the caller fall back
gracefully.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Base Apify REST API URL
_APIFY_BASE = "https://api.apify.com/v2"

# How long to wait between status polls (seconds)
_POLL_INTERVAL = 4

# Terminal run statuses
_TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


class ApifyClient:
    """Thin async wrapper around the Apify REST API."""

    # ── internal helpers ──────────────────────────────────────────────────────

    async def _start_run(self, actor_id: str, run_input: dict[str, Any]) -> str:
        """Start an Apify actor run and return the run ID."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_APIFY_BASE}/acts/{actor_id}/runs",
                params={"token": settings.APIFY_API_KEY},
                json=run_input,
            )
            if resp.status_code >= 400:
                body = (resp.text or "").strip()
                if len(body) > 800:
                    body = body[:800] + "..."
                raise RuntimeError(
                    f"Apify start run failed status={resp.status_code} actor={actor_id} body={body or '<empty>'}"
                )
            data = resp.json()
            return data["data"]["id"]

    @staticmethod
    def _canonical_instagram_url(url: str) -> str:
        """Normalize Instagram profile URLs by stripping query/fragments."""
        try:
            parsed = urlparse(url.strip())
            host = (parsed.netloc or "").lower()
            if not host:
                return url
            if not host.endswith("instagram.com"):
                return url

            path = parsed.path or ""
            parts = [p for p in path.split("/") if p]
            if not parts:
                return "https://www.instagram.com/"
            # Keep only the username segment for profile scrape.
            username = parts[0]
            return f"https://www.instagram.com/{username}/"
        except Exception:
            return url

    async def _poll_until_done(self, run_id: str, timeout: float) -> str:
        """Poll run status until it reaches a terminal state or timeout."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Apify run {run_id} timed out after {timeout}s")
            await asyncio.sleep(_POLL_INTERVAL)
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_APIFY_BASE}/actor-runs/{run_id}",
                    params={"token": settings.APIFY_API_KEY},
                )
                resp.raise_for_status()
                status: str = resp.json()["data"]["status"]
            logger.debug("[Apify] run=%s status=%s", run_id, status)
            if status in _TERMINAL:
                return status

    async def _fetch_items(self, run_id: str) -> list[dict]:
        """Fetch the dataset items produced by a completed run."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_APIFY_BASE}/actor-runs/{run_id}/dataset/items",
                params={"token": settings.APIFY_API_KEY},
            )
            resp.raise_for_status()
            return resp.json()  # list of dicts

    async def _resolve_redirect_url(self, url: str) -> str:
        """Resolve redirects and return the final URL (or original on failure)."""
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                return str(resp.url)
        except Exception as exc:
            logger.debug("[Apify] Could not resolve redirects for %s: %s", url, exc)
            return url

    def _extract_search_query_from_url(self, url: str) -> str:
        """Extract a useful Google search query from URL path/query when possible."""
        try:
            parsed = urlparse(url)
            query = parse_qs(parsed.query)

            # Common pattern for share links that end on google.com/search?...&q=...
            for key in ("q", "query"):
                val = (query.get(key) or [""])[0].strip()
                if val:
                    return unquote_plus(val)

            # Fallback: canonical Maps URL path contains /maps/place/<name>
            parts = parsed.path.split("/maps/place/", 1)
            if len(parts) == 2:
                place_slug = parts[1].split("/", 1)[0].split("@", 1)[0].strip()
                if place_slug:
                    return unquote_plus(place_slug)
        except Exception:
            pass
        return ""

    async def run_actor(
        self,
        actor_id: str,
        run_input: dict[str, Any],
        timeout: float = 120,
    ) -> list[dict]:
        """Start an actor run, wait for it to finish, and return dataset items.

        Args:
            actor_id: Apify actor ID in ``owner~name`` format.
            run_input: JSON-serialisable input for the actor.
            timeout: Maximum seconds to wait for the run to complete.

        Returns:
            List of result dicts from the actor's dataset.

        Raises:
            RuntimeError: If the run ends in a non-success status.
            TimeoutError: If the run does not finish within ``timeout`` seconds.
        """
        run_id = await self._start_run(actor_id, run_input)
        logger.info("[Apify] started run=%s actor=%s", run_id, actor_id)

        final_status = await self._poll_until_done(run_id, timeout)
        if final_status != "SUCCEEDED":
            raise RuntimeError(
                f"Apify actor {actor_id!r} run {run_id} ended with status {final_status!r}"
            )

        items = await self._fetch_items(run_id)
        logger.info("[Apify] run=%s returned %d item(s)", run_id, len(items))
        return items

    # ── public scrapers ───────────────────────────────────────────────────────

    async def scrape_instagram_profile(self, url: str) -> dict | None:
        """Scrape an Instagram business profile and return normalised data.

        Returns ``None`` if:
        - ``APIFY_API_KEY`` is not set
        - The actor returns no results
        - Any network / API error occurs (logged as warning)

        Returned dict keys::

            name            full display name (or username fallback)
            username        Instagram username (without @)
            bio             profile biography text
            website         external link from profile
            followersCount  integer
            postsCount      integer
            verified        bool
            profilePicUrl   profile picture URL (may be empty)
        """
        if not settings.APIFY_API_KEY:
            logger.debug("[Apify] APIFY_API_KEY not set — skipping Instagram scrape")
            return None

        canonical_url = self._canonical_instagram_url(url)
        try:
            items = await self.run_actor(
                settings.APIFY_INSTAGRAM_ACTOR_ID,
                {
                    "directUrls": [canonical_url],
                    "resultsType": "details",
                    "resultsLimit": 1,
                },
                timeout=120,
            )
        except Exception as exc:
            logger.warning("[Apify] Instagram scrape failed for %s (canonical=%s): %s", url, canonical_url, exc)
            return None

        if not items:
            logger.info("[Apify] Instagram scrape returned no items for %s", url)
            return None

        raw = items[0]
        return {
            "name": raw.get("fullName") or raw.get("username") or "",
            "username": raw.get("username") or "",
            "bio": raw.get("biography") or "",
            "website": raw.get("externalUrl") or "",
            "followersCount": raw.get("followersCount") or 0,
            "postsCount": raw.get("postsCount") or 0,
            "verified": bool(raw.get("verified")),
            "profilePicUrl": raw.get("profilePicUrl") or "",
        }

    async def scrape_google_places_candidates(self, url: str, max_results: int = 5) -> list[dict]:
        """Scrape Google Places via Apify and return up to max_results candidates."""
        if max_results < 1:
            max_results = 1

        if not settings.APIFY_API_KEY:
            logger.debug("[Apify] APIFY_API_KEY not set — skipping Places scrape")
            return []

        resolved_url = await self._resolve_redirect_url(url)
        search_query = self._extract_search_query_from_url(resolved_url) or self._extract_search_query_from_url(url)

        run_inputs: list[dict[str, Any]] = [
            {
                "startUrls": [{"url": resolved_url}],
                "maxCrawledPlaces": max_results,
                "language": "en",
                "includeHistogram": False,
                "includeOpeningHours": True,
            }
        ]

        # share.google links often resolve to google.com/search with ?q=...
        # The actor accepts query-based input in that case.
        if search_query:
            run_inputs.append(
                {
                    "searchStringsArray": [search_query],
                    "maxCrawledPlaces": max_results,
                    "language": "en",
                    "includeOpeningHours": True,
                }
            )

        items: list[dict] = []
        last_exc: Exception | None = None
        for run_input in run_inputs:
            try:
                items = await self.run_actor(
                    settings.APIFY_GOOGLE_PLACES_ACTOR_ID,
                    run_input,
                    timeout=120,
                )
                if items:
                    break
            except Exception as exc:
                last_exc = exc
                logger.info("[Apify] Places run failed for input keys %s: %s", list(run_input.keys()), exc)

        if not items:
            if last_exc:
                logger.warning("[Apify] Google Places scrape failed for %s: %s", url, last_exc)
            else:
                logger.warning("[Apify] Google Places scrape returned no items for %s", url)
            return []

        results: list[dict] = []
        for raw in items[:max_results]:
            # Normalise opening hours: list of {day, hours} dicts → single string + day list
            raw_hours = raw.get("openingHours") or []
            hours_parts: list[str] = []
            opening_days: list[str] = []
            for entry in raw_hours:
                if isinstance(entry, dict):
                    day = entry.get("day") or ""
                    hrs = entry.get("hours") or ""
                    if day:
                        opening_days.append(day)
                    if day and hrs:
                        hours_parts.append(f"{day}: {hrs}")

            results.append(
                {
                    "name": raw.get("title") or raw.get("name") or "",
                    "businessType": (raw.get("categoryName") or raw.get("category") or "other")
                    .lower()
                    .replace("_", " "),
                    "description": raw.get("description") or "",
                    "address": raw.get("address") or raw.get("street") or "",
                    "phone": raw.get("phone") or raw.get("phoneUnformatted") or "",
                    "hours": ", ".join(hours_parts),
                    "openingDays": opening_days,
                    "website": raw.get("website") or "",
                    "placeId": raw.get("placeId") or "",
                    "mapsUrl": resolved_url,
                }
            )

        return results

    async def scrape_google_places_url(self, url: str) -> dict | None:
        """Scrape a Google Maps / Places URL via Apify as a fallback.

        Used when the standard Places API redirect flow cannot resolve the URL
        (e.g. newer ``share.google/…`` links or non-canonical redirect chains).

        Returns ``None`` if:
        - ``APIFY_API_KEY`` is not set
        - The actor returns no results
        - Any network / API error occurs

        Returned dict keys match the structure expected by ``_handle_website_url``::

            name            business name
            businessType    category string (e.g. "beauty salon")
            description     short description
            address         full address
            phone           phone number
            hours           opening hours as a single string
            openingDays     list of day names
            website         official business website
            placeId         Google place ID (if available)
            mapsUrl         the original URL passed in
        """
        candidates = await self.scrape_google_places_candidates(url, max_results=1)
        return candidates[0] if candidates else None
