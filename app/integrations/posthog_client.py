"""PostHog product analytics — backend events.

Mirrors the bridge's PostHog tracker (whatsapp-meow/analytics/posthog.go)
so the full funnel — message received → intent classified → AI replied →
booking created → CSAT scored — lives in a single PostHog project.

Distinct ID strategy:
    distinct_id = "biz:{business_id}:cust:{customer_phone_hash}"

Customer phones are one-way hashed (FNV-style — same algorithm as the
Go bridge) so PII never lands in PostHog dashboards while still allowing
per-customer funnels.

All capture calls are fire-and-forget — analytics failures must never
break message processing.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_posthog_client: Any = None
_initialised: bool = False


def _enabled() -> bool:
    return bool(settings.POSTHOG_ENABLED and settings.POSTHOG_API_KEY)


def _client() -> Any:
    """Return the singleton PostHog client, or None when disabled."""
    global _posthog_client, _initialised
    if _initialised:
        return _posthog_client
    _initialised = True
    if not _enabled():
        logger.info("[POSTHOG] disabled (POSTHOG_API_KEY not set)")
        return None
    try:
        from posthog import Posthog  # type: ignore
        _posthog_client = Posthog(
            project_api_key=settings.POSTHOG_API_KEY,
            host=settings.POSTHOG_HOST,
        )
        logger.info("[POSTHOG] enabled host=%s", settings.POSTHOG_HOST)
    except Exception as exc:
        logger.warning("[POSTHOG] failed to initialise — analytics disabled: %s", exc)
        _posthog_client = None
    return _posthog_client


def _hash_phone(phone: str) -> str:
    """One-way hash of a phone number for PostHog distinct IDs."""
    if not phone:
        return "unknown"
    digest = hashlib.sha256(phone.encode("utf-8")).hexdigest()
    return digest[:16]


def distinct_id(business_id: str, customer_phone: str) -> str:
    return f"biz:{business_id}:cust:{_hash_phone(customer_phone)}"


# Cache of distinct_ids that have already been identified this process lifetime.
# Avoids sending an identify() on every single message.
_identified: set[str] = set()


def identify(
    business_id: str,
    customer_phone: str,
    person_properties: dict | None = None,
) -> None:
    """Link distinct_id to a PostHog Person so events appear in dashboards.

    PostHog's Activity tab shows raw events regardless, but Insights /
    Persons / Funnels only work when the distinct_id has been identified.
    Call this once per customer×business pair (cached in-process).
    Never raises.
    """
    client = _client()
    if client is None:
        return
    did = distinct_id(business_id, customer_phone)
    if did in _identified:
        return
    try:
        props = dict(person_properties or {})
        props.setdefault("business_id", business_id)
        # Send $identify event with $set to populate Person properties.
        # This is the only way PostHog Python SDK supports setting Person properties.
        client.capture(
            distinct_id=did,
            event="$identify",
            properties={"$set": props},
        )
        _identified.add(did)
    except Exception as exc:
        logger.warning("[POSTHOG] identify failed: %s", exc)


def capture(
    business_id: str,
    customer_phone: str,
    event: str,
    properties: dict | None = None,
    person_properties: dict | None = None,
) -> None:
    """Fire a PostHog event. Never raises.

    Automatically calls identify() on the first event for each
    customer×business pair so events appear in PostHog dashboards.
    Pass person_properties to enrich the Person record (business name,
    plan, language, etc.).
    """
    client = _client()
    if client is None:
        return
    # Ensure the person exists in PostHog before the event
    identify(business_id, customer_phone, person_properties)
    try:
        props = dict(properties or {})
        props.setdefault("business_id", business_id)
        client.capture(
            distinct_id=distinct_id(business_id, customer_phone),
            event=event,
            properties=props,
        )
    except Exception as exc:
        logger.warning("[POSTHOG] capture failed event=%s: %s", event, exc)


def shutdown() -> None:
    """Flush queued events before process exit (called from FastAPI lifespan)."""
    if _posthog_client is None:
        return
    try:
        _posthog_client.shutdown()
    except Exception:
        pass
