"""Central registry of Recepte's global onboarding numbers.

Historically Recepte onboarded every SMB business through ONE global WhatsApp
number (one whatsmeow bridge session). This module lets the system run the SAME
onboarding flow on SEVERAL global numbers at once, while staying 100 %
backward-compatible:

  * If ``settings.WHATSMEOW_GLOBAL_NUMBERS`` is set, it is the source of truth
    (dynamic — any number of global numbers).
  * If it is EMPTY, we fall back to the single legacy settings
    (``WHATSMEOW_ONBOARDING_DEVICE_ID`` / ``WHATSMEOW_DEFAULT_DEVICE_ID`` +
    ``WHATSMEOW_GLOBAL_NUMBER``). An un-configured deployment therefore behaves
    EXACTLY as before this module existed — nothing changes until the new
    variable is filled in.

A "global number" has two identifiers:
  * ``device_id`` — the whatsmeow bridge session ID. Inbound webhooks are tagged
    with it, and it tells us which number to send a reply FROM.
  * ``number``    — the actual phone digits. Used so a business's own AI never
    mistakes one of our global numbers for a customer.

Config format — comma-separated ``deviceId:number`` pairs::

    WHATSMEOW_GLOBAL_NUMBERS="smba:918968012547, smbb:917696794756"

This module is deliberately dependency-light (only ``settings``) and holds NO
state, so importing it can never affect the existing onboarding flow. Every
lookup re-reads ``settings`` so tests that monkeypatch settings take effect
without a restart (same pattern the webhook router already uses).
"""
from __future__ import annotations

from app.config import settings


def _digits(value: str | None) -> str:
    """Keep only phone digits (drops '+', spaces, '@s.whatsapp.net', ':' suffix)."""
    if not value:
        return ""
    head = str(value).split("@", 1)[0]
    return "".join(ch for ch in head if ch.isdigit())


def registry() -> list[tuple[str, str]]:
    """Return the list of ``(device_id, number)`` global-number pairs.

    Dynamic source (``WHATSMEOW_GLOBAL_NUMBERS``) wins when present; otherwise we
    synthesise a single-entry registry from the legacy settings so the rest of
    the system has one uniform shape to read. ``number`` may be "" if unknown
    (e.g. legacy config set the device but not the phone digits).
    """
    raw = (getattr(settings, "WHATSMEOW_GLOBAL_NUMBERS", "") or "").strip()
    if raw:
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            device_id, _, number = chunk.partition(":")
            device_id = device_id.strip()
            number = _digits(number)
            if not device_id or device_id in seen:
                continue
            seen.add(device_id)
            pairs.append((device_id, number))
        if pairs:
            return pairs

    # ── Legacy fallback: the single global number (current behavior) ──────────
    legacy_device = (
        settings.WHATSMEOW_ONBOARDING_DEVICE_ID
        or settings.WHATSMEOW_DEFAULT_DEVICE_ID
        or ""
    ).strip()
    legacy_number = _digits(settings.WHATSMEOW_GLOBAL_NUMBER)
    if legacy_device:
        return [(legacy_device, legacy_number)]
    return []


def onboarding_device_ids() -> set[str]:
    """Every bridge session ID that runs the global onboarding flow."""
    return {dev for dev, _ in registry() if dev}


def is_onboarding_device(device_id: str | None) -> bool:
    """True iff messages on ``device_id`` should go to the onboarding flow."""
    if not device_id:
        return False
    return device_id in onboarding_device_ids()


def number_for_device(device_id: str | None) -> str:
    """The phone digits behind an onboarding ``device_id`` ("" if unknown)."""
    if not device_id:
        return ""
    for dev, number in registry():
        if dev == device_id:
            return number
    return ""


def all_global_numbers() -> list[str]:
    """Digits of every configured global number (skips blanks)."""
    return [num for _, num in registry() if num]


def primary_device_id() -> str:
    """The default/first global device — used as an outbound fallback when a
    session has no stored onboarding device (e.g. a mid-flight session created
    before multi-number support, or an automation job with no per-owner device).
    Matches the legacy default so single-number deployments are unaffected."""
    reg = registry()
    return reg[0][0] if reg else ""


def primary_number() -> str:
    """Phone digits of the default/first global number ("" if none configured).

    Used where ONE representative global number is needed (e.g. the standalone
    demo's "message us here to connect for real" link) — picks the first entry,
    which in legacy single-number config is simply THE number, so behavior is
    unchanged until a second number is added.
    """
    reg = registry()
    return reg[0][1] if reg else ""
