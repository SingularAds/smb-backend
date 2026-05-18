"""WhatsApp notification helpers for the automation layer.

All sends go through WhatsmeowClient using the business's linked
waSessionId (device_id) so messages appear from the business number.
Falls back to the global default device if no session is linked.
"""
from __future__ import annotations

import asyncio
import logging

from app.services.whatsmeow_client import WhatsmeowClient

logger = logging.getLogger(__name__)
_wa = WhatsmeowClient()


def _device(business: dict) -> str:
    """Return the device_id to use for a business."""
    return business.get("waSessionId") or _wa.default_device_id


async def send_to_customer(business: dict, customer_phone: str, message: str) -> bool:
    """Send a WhatsApp message to a customer on behalf of a business."""
    device = _device(business)
    try:
        await _wa.send_message(customer_phone, message, device_id=device)
        logger.info("[Automation] Sent to customer %s via device %s | biz=%s", customer_phone, device, business.get("id"))
        return True
    except Exception as exc:
        logger.warning("[Automation] Failed to send to %s: %s | biz=%s", customer_phone, exc, business.get("id"))
        return False


async def send_to_owner(business: dict, message: str) -> bool:
    """Send a WhatsApp message to the business owner."""
    owner_phone = business.get("ownerPhone") or business.get("owner_phone") or ""
    if not owner_phone:
        logger.warning("[Automation] Business %s has no ownerPhone — skipping owner notification", business.get("id"))
        return False
    device = _device(business)
    try:
        await _wa.send_message(owner_phone, message, device_id=device)
        logger.info("[Automation] Sent to owner %s via device %s | biz=%s", owner_phone, device, business.get("id"))
        return True
    except Exception as exc:
        logger.warning("[Automation] Failed to send to owner %s: %s | biz=%s", owner_phone, exc, business.get("id"))
        return False
