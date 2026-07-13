"""Canonical acquisition-attribution model for onboarding prospects.

ONE place that decides *where an onboarding prospect came from* and normalizes
every entry path (Click-to-WhatsApp ads, the recepte.co website leads, our own
recepte_leads ingestion, UTM-tagged prefilled messages, and plain organic
first-contacts) into a single `attribution` object.

Why this exists: before this module the same "website" prospect was recorded
three different ways — `website_leads.source = "whatsapp_prefilled"`,
`recepte_leads.source = "recepte.co"`, and a flat `registrationSource` on the
session — while the `businesses` collection carried no acquisition provenance at
all. The internal dashboard needs one consistent `channel` to group by, so this
builder is called from every onboarding session-creation path and its output is
persisted on both the onboarding session AND the business doc.

Attribution is captured for ONBOARDING PROSPECTS ONLY (owners messaging the
global onboarding number) — never end customers on business numbers.

The `channel` enum is the single extension point: adding a new network later is
a branch in `_normalize_channel` plus a dashboard label — no schema change.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Canonical channels. Anything acquisition-related groups by one of these.
CHANNEL_WEBSITE = "website"
CHANNEL_META_ADS = "meta_ads"
CHANNEL_GOOGLE_ADS = "google_ads"
CHANNEL_ORGANIC = "organic"
CHANNEL_REFERRAL = "referral"

# Human labels for the dashboard (frontend may override, but these are the
# defaults so a new channel shows something sensible without a UI change).
CHANNEL_LABELS = {
    CHANNEL_WEBSITE: "Website",
    CHANNEL_META_ADS: "Meta Ads",
    CHANNEL_GOOGLE_ADS: "Google Ads",
    CHANNEL_ORGANIC: "Organic",
    CHANNEL_REFERRAL: "Referral",
}

# Existing (inconsistent) website source strings seen in production leads.
_WEBSITE_SOURCE_MARKERS = {"whatsapp_prefilled", "recepte.co", "recepte", "website", "web"}

# Meta / Facebook / Instagram hosts that appear in a CTWA ad's source_url.
_META_HOST_MARKERS = ("facebook.", "fb.me", "fb.com", "instagram.", "fbclid")

# UTM / click-id keys we parse out of a prefilled message body (wa.me?text=...).
_UTM_KEYS = (
    "utm_source", "utm_campaign", "utm_medium", "utm_content", "utm_term",
    "gclid", "fbclid", "clickId", "click_id", "source", "campaign",
)
_UTM_RE = re.compile(
    r"\b(" + "|".join(_UTM_KEYS) + r")\s*[:=]\s*([^\s&]+)",
    re.IGNORECASE,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utm_from_text(body: str | None) -> dict[str, str]:
    """Extract UTM/click params from a prefilled WhatsApp message body.

    Meta/Google ad deep-links can prefill a message like
    ``Hi! utm_source=facebook utm_campaign=spring gclid=abc`` — parse those
    key=val / key:val pairs. Returns only the keys that were present.
    """
    if not body:
        return {}
    params: dict[str, str] = {}
    for key, val in _UTM_RE.findall(body):
        k = key.lower()
        if k == "source":
            k = "utm_source"
        elif k == "campaign":
            k = "utm_campaign"
        elif k in ("clickid", "click_id"):
            k = "clickId"
        params[k] = val
    return params


def _host_of(url: str | None) -> str:
    if not url:
        return ""
    return url.lower()


def _normalize_channel(
    referral: dict[str, Any] | None,
    params: dict[str, str],
    lead: dict[str, Any] | None,
) -> tuple[str, str, str]:
    """Map raw signals to (channel, source, sourceType).

    Priority: an explicit CTWA ad referral wins, then UTM/click ids, then an
    existing website/recepte lead, then organic. This is the ONE place raw
    signals become a canonical channel — the reconciliation point for the
    previously-inconsistent website source strings.
    """
    # 1. Click-to-WhatsApp ad referral (industry-standard, most authoritative).
    if referral:
        src_url = _host_of(referral.get("source_url") or referral.get("sourceUrl"))
        src_type = (referral.get("source_type") or referral.get("sourceType") or "ad").lower()
        # Instagram vs Facebook is only advisory; both are the Meta ads channel.
        if "instagram." in src_url:
            source = "instagram"
        elif any(m in src_url for m in _META_HOST_MARKERS) or src_type == "ad":
            source = "facebook"
        else:
            source = "facebook"
        return CHANNEL_META_ADS, source, "ad"

    # 2. UTM / click ids parsed from the prefilled message text.
    utm_source = (params.get("utm_source") or "").lower()
    if params.get("gclid") or utm_source in ("google", "google_ads", "googleads", "adwords"):
        return CHANNEL_GOOGLE_ADS, "google", "ad"
    if params.get("fbclid") or utm_source in ("facebook", "instagram", "meta", "fb", "ig"):
        return CHANNEL_META_ADS, utm_source or "facebook", "ad"
    if utm_source:
        # A tagged but non-ad source (e.g. a newsletter) — record it verbatim.
        return CHANNEL_REFERRAL, utm_source, "referral"

    # 3. Existing website / recepte lead (reconcile the legacy source strings).
    if lead:
        raw_src = str(lead.get("source") or "").lower().strip()
        return CHANNEL_WEBSITE, raw_src or "recepte.co", "website"

    # 4. Nothing → organic first-contact.
    return CHANNEL_ORGANIC, "organic", "organic"


def build_attribution(
    *,
    referral: dict[str, Any] | None = None,
    body: str | None = None,
    lead: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical attribution object for an onboarding prospect.

    Called from every onboarding session-creation path so the persisted shape
    is identical regardless of how the prospect arrived.

    Args:
        referral: CTWA ad-referral metadata forwarded by the bridge (source_id,
            source_url, ctwa_clid, title, body, media_type, source_type), or None.
        body: the prospect's first message text (used for UTM/click parsing).
        lead: a matched website_leads / recepte_leads document, or None.
    """
    referral = referral or None
    params = parse_utm_from_text(body)
    channel, source, source_type = _normalize_channel(referral, params, lead)

    ref = referral or {}
    click_id = (
        ref.get("ctwa_clid")
        or ref.get("ctwaClid")
        or params.get("fbclid")
        or params.get("gclid")
        or params.get("clickId")
    )

    return {
        "channel": channel,
        "source": source,
        "sourceType": source_type,
        "adId": ref.get("source_id") or ref.get("sourceId"),
        "clickId": click_id,
        "campaign": params.get("utm_campaign"),
        "medium": params.get("utm_medium"),
        "sourceUrl": ref.get("source_url") or ref.get("sourceUrl"),
        "headline": ref.get("title"),
        "adBody": ref.get("body"),
        "leadCollection": (lead or {}).get("_collection") or (lead or {}).get("_source_collection"),
        "capturedAt": _iso_now(),
        # Keep the originals so a mis-mapping can be audited/re-derived later.
        "raw": {
            "referral": referral,
            "utm": params or None,
            "leadSource": (lead or {}).get("source"),
        },
    }


def is_ad_channel(attribution: dict[str, Any] | None) -> bool:
    """True when the prospect came from a paid ad (Meta/Google) — the population
    that gets the pre-onboarding 'reply YES to start' gate."""
    if not attribution:
        return False
    return str(attribution.get("channel") or "").endswith("_ads")
