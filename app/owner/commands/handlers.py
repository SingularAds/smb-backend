"""Owner command handler — routes parsed commands to service functions.

Entry point: handle_owner_command(business, message, device_id)
"""
from __future__ import annotations

import logging
from typing import Callable, Awaitable

from app.owner.commands.parser import CommandType, parse_command
from app.owner.commands import services as svc
from app.owner.commands.language import translate_reply
from app.services.whatsmeow_client import WhatsmeowClient

logger = logging.getLogger(__name__)
_wa = WhatsmeowClient()


# ── router ────────────────────────────────────────────────────────────────────

async def _dispatch(command: dict, business: dict) -> str:
    """Map a parsed command to the appropriate service coroutine and return reply."""
    cmd_type: CommandType = command["type"]
    args: dict = command["args"]

    match cmd_type:
        case CommandType.TODAY:
            return await svc.get_today_bookings(business)
        case CommandType.TOMORROW:
            return await svc.get_tomorrow_bookings(business)
        case CommandType.SUMMARY:
            return await svc.get_summary(business)
        case CommandType.VIP:
            return await svc.get_vip_clients(business)
        case CommandType.SETTINGS:
            return await svc.view_settings(business)
        case CommandType.CANCEL:
            return await svc.cancel_booking_flow(business, args.get("ref"))
        case CommandType.BLOCK:
            return await svc.block_slot_flow(business, args.get("slot"))
        case CommandType.ADD_SERVICE:
            return await svc.add_service_flow(business, args)
        case CommandType.REMOVE_SERVICE:
            return await svc.remove_service_flow(business, args)
        case CommandType.SHOW_SERVICES:
            return await svc.show_services(business)
        case CommandType.CHANGE_HOURS:
            return await svc.change_hours_flow(business, args)
        case CommandType.CLOSE_DAY:
            return await svc.close_day_flow(business, args)
        case CommandType.ADD_FAQ:
            return await svc.add_faq_flow(business, args)
        case CommandType.ADD_STYLIST:
            return await svc.add_stylist_flow(business, args)
        case CommandType.CHANGE_VIBE:
            return await svc.change_vibe_flow(business, args)
        case CommandType.SCAN_WEBSITE:
            return await svc.scan_website_flow(business, args)
        case CommandType.INACTIVE_CLIENTS:
            return await svc.inactive_clients_flow(business, args)
        case CommandType.SEND_OUTREACH:
            return await svc.send_outreach_flow(business, args)
        case CommandType.AUTO_REPLY_OFF:
            return await svc.auto_reply_flow(business, {"enabled": False})
        case CommandType.AUTO_REPLY_ON:
            return await svc.auto_reply_flow(business, {"enabled": True})
        case CommandType.HELP | CommandType.UNKNOWN:
            return await svc.help_command(business)
        case _:
            return await svc.help_command(business)


def _is_owner(business: dict, phone: str) -> bool:
    """Return True if phone belongs to the business owner or an admin.

    Handles:
      - Leading + / country code variations
      - @s.whatsapp.net JID suffixes
      - Trailing last-10-digits fallback (handles country code mismatches)
    """
    owner = business.get("ownerPhone") or business.get("owner_phone") or ""
    admins: list = business.get("adminPhones") or []

    def _norm(p: str) -> str:
        """Strip all non-digits from a phone / JID string."""
        # Remove JID suffix e.g. 919905252720@s.whatsapp.net
        p = str(p).split("@")[0]
        return "".join(ch for ch in p if ch.isdigit())

    def _matches(a: str, b: str) -> bool:
        na, nb = _norm(a), _norm(b)
        if not na or not nb:
            return False
        if na == nb:
            return True
        # Country-code-flexible: compare last 10 digits
        return na[-10:] == nb[-10:]

    logger.debug(
        "[OWNER_CHECK] phone=%r norm=%r ownerPhone=%r norm_owner=%r",
        phone,
        _norm(phone),
        owner,
        _norm(owner),
    )
    if _matches(phone, owner):
        return True
    return any(_matches(phone, a) for a in admins)


async def handle_owner_command(
    business: dict,
    message: str,
    device_id: str,
    owner_phone: str,
    reply_jid: str | None = None,
) -> None:
    """Parse message, dispatch to service, and send reply via WhatsApp.
    
    Called only after confirming that owner_phone belongs to the business owner/admin.
    ``reply_jid`` is the full sender JID (e.g. ``917696794756@s.whatsapp.net`` or
    ``134544296509456@lid``) used as the reply-to address.
    """
    logger.info("Owner command from %s on device %s: %r", owner_phone, device_id, message[:80])

    # ── Device-link guard ─────────────────────────────────────────────────────
    # Owner commands are only meaningful when the business WhatsApp device is
    # linked and serving customers.  If the device is disconnected or unpaired,
    # replies would be misleading (e.g. "today you have 3 bookings" while the AI
    # is unreachable).  Block commands and prompt the owner to re-link first.
    target_jid = reply_jid or owner_phone
    wa_session_id = business.get("waSessionId")
    if wa_session_id:
        try:
            status = await _wa.get_session_status(wa_session_id)
            is_connected = bool(status.get("paired")) and status.get("status") == "connected"
        except Exception as _chk_exc:
            # Bridge unreachable — fail open so commands still work during bridge restarts.
            logger.warning(
                "Could not verify device status for session %s (%s) — allowing command",
                wa_session_id, _chk_exc,
            )
            is_connected = True

        if not is_connected:
            unlinked_msg = (
                "⚠️ *Your WhatsApp is not connected to Recepte.*\n\n"
                "Owner commands are paused because your business number is offline "
                "— customers cannot receive replies right now.\n\n"
                "To reconnect, send:\n"
                "*reconnect my whatsapp*"
            )
            try:
                # Always reply via the global device since the business device is offline.
                await _wa.send_message(target_jid, unlinked_msg, device_id=_wa.default_device_id)
            except Exception as exc:
                logger.error("Could not send unlinked reminder to %s: %s", owner_phone, exc)
            return

    command = parse_command(message)
    logger.debug("Parsed command: %s", command)

    reply = await _dispatch(command, business)
    reply = await translate_reply(message, reply)

    try:
        await _wa.send_message(target_jid, reply, device_id=device_id)
    except Exception as exc:
        logger.warning(
            "Failed to send owner reply via device %s (%s) — retrying via global device",
            device_id, exc,
        )
        # Business device may be temporarily disconnected (session not connected).
        # Fall back to the global device so the owner always gets a reply.
        try:
            await _wa.send_message(target_jid, reply, device_id=_wa.default_device_id)
        except Exception as exc2:
            logger.error("Failed to send owner reply to %s: %s", owner_phone, exc2)


__all__ = ["handle_owner_command", "is_owner_message"]


def is_owner_message(business: dict, phone: str) -> bool:
    """Public helper used by the webhook to check whether to route to owner commands."""
    return _is_owner(business, phone)
