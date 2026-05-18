"""Referral Service — manages the full referral program lifecycle.

Flow (aligned with spec):
  Stage 5 — Code detection in webhook → handle_referral_message():
    • Look up referrer by code
    • Guard: self-referral → silent block (return True)
    • Guard: already a customer with completed visits → friendly message (return False, AI continues)
    • Guard: already-referred → acknowledge (return True)
    • New referee → store discount fields + create referral doc → welcome msg (return False, AI continues)

  Stage 6/7 — Referee's first completed visit → on_booking_completed():
    • Clear referee's 10 % discount (pendingDiscountConsumedAt set)
    • Grant referrer 25 % via _reward_referrer()
    • Update referral doc → refereeVisited

  Stage 8 — Referrer's discounted visit → on_booking_completed():
    • Clear referrer's 25 % discount (pendingDiscountConsumedAt set)
    • Update referral doc → referrerRewarded

  Invite sweep (referral_automation.py) sends the wa.me link after 2nd visit
  with 90-minute delay and 90-day cooldown, gated by referralFeatureEnabled.

Abuse guardrails:
  - No self-referral
  - No discount for already-visited friend
  - Idempotent referrer reward (won't downgrade an existing higher discount)
  - referralOptOut respected in automation sweep
  - referralFeatureEnabled gating in automation sweep
"""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta

from app import firestore as db
from app.services.automation.whatsapp_notifier import send_to_customer

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

_CODE_LENGTH = 6
# Omit visually ambiguous chars (O/0, I/1/L)
_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Matches common referral code patterns:
# "Code: ABC123"  |  "referral code ABC123"  |  "referral code is ABC123"
_REFERRAL_RE = re.compile(
    r"(?:referral\s*code|code)[\s:]+(?:is\s+)?([A-Z0-9]{" + str(_CODE_LENGTH) + r"})\b",
    re.IGNORECASE,
)

# Owner replies for referral visit confirmation:
#   "YES 1234"  or  "YES1234"
#   "NO"
_OWNER_YES_CONFIRM_RE = re.compile(r"^\s*yes\s*(\d{4})\b", re.IGNORECASE)
_OWNER_NO_CONFIRM_RE = re.compile(r"^\s*no\b", re.IGNORECASE)

# Discount expiry — 6 months ≈ 183 days
_DISCOUNT_EXPIRY_DAYS = 183


# ── code helpers ──────────────────────────────────────────────────────────────

def _generate_code() -> str:
    return "".join(random.choices(_CODE_CHARS, k=_CODE_LENGTH))


def get_or_create_referral_code(business_id: str, phone: str) -> str:
    """Return existing referral code for the customer, creating one if absent.

    Guarantees uniqueness within the business by retrying on collision.
    """
    customer = db.get_customer_by_phone(business_id, phone)
    if customer and customer.get("referralCode"):
        return customer["referralCode"]

    for _attempt in range(10):
        code = _generate_code()
        if not db.get_customer_by_referral_code(business_id, code):
            db.upsert_customer(business_id, phone, {"referralCode": code})
            logger.info("[Referral] Generated code %s for %s (biz=%s)", code, phone, business_id)
            return code

    raise RuntimeError(
        f"Could not generate a unique referral code for {phone} after 10 attempts"
    )


def detect_referral_code(message: str) -> str | None:
    """Return the referral code embedded in *message*, or None.

    Recognises patterns like:
      - "Hi, I was referred by Amit. Code: ABC123"
      - "referral code: XY23AB"
      - "Code ABC123"
    """
    m = _REFERRAL_RE.search(message)
    if m:
        return m.group(1).upper()
    return None


# ── referral handling ─────────────────────────────────────────────────────────

async def handle_referral_message(
    business_id: str,
    sender_phone: str,
    referral_code: str,
    business: dict,
) -> bool:
    """Process a WhatsApp referral message.

    Returns True  → message fully handled; caller STOPS routing (self-referral,
                     already-referred duplicate acknowledgement).
    Returns False → caller CONTINUES to normal AI routing:
                     - code not found (no referral context)
                     - already-a-customer (sent clarification, AI handles booking)
                     - new referee (welcome sent, AI handles booking follow-up)
    """
    # 1. Look up referrer by code
    referrer = db.get_customer_by_referral_code(business_id, referral_code)
    if not referrer:
        logger.info("[Referral] Code %s not found in biz %s", referral_code, business_id)
        return False  # Unknown code — fall through to AI

    referrer_phone = referrer.get("phone") or referrer.get("id", "")

    # 2. Self-referral — silent block, no reply
    if db._clean_phone(referrer_phone) == db._clean_phone(sender_phone):
        logger.info("[Referral] Self-referral attempt by %s — silently ignored", sender_phone)
        return True

    biz_name = business.get("name", "us")
    referrer_name = referrer.get("name") or referrer.get("customerName") or "your friend"

    # Read per-business discount percentages (fall back to spec defaults)
    referee_pct = int(business.get("refereeDiscountPercent") or 10)
    referrer_pct = int(business.get("referrerDiscountPercent") or 25)

    # 3. Already a customer with completed visits — not eligible as referee
    existing = db.get_customer_by_phone(business_id, sender_phone)
    total_visits = int((existing or {}).get("totalVisits") or 0)
    if total_visits > 0:
        logger.info(
            "[Referral] Sender %s already has %d visits — referral not applicable",
            sender_phone, total_visits,
        )
        await send_to_customer(
            business,
            sender_phone,
            f"👋 Welcome back! You're already one of our valued customers at *{biz_name}*. "
            f"We look forward to seeing you again! 😊",
        )
        return False  # Continue to AI so they can book normally

    # 4. Already referred — just re-acknowledge, don't create a second record
    if existing and existing.get("referredBy"):
        await send_to_customer(
            business,
            sender_phone,
            f"👋 Hi! You were referred by *{referrer_name}*. "
            f"We already have your *{referee_pct}% discount* saved for your first visit. "
            f"See you soon! 😊",
        )
        return True  # No need to route to AI again

    # 5. Valid new referee — register and welcome
    now_iso = datetime.utcnow().isoformat()
    expiry_iso = (datetime.utcnow() + timedelta(days=_DISCOUNT_EXPIRY_DAYS)).isoformat()
    sender_phone_clean = db._clean_phone(sender_phone)

    db.upsert_customer(business_id, sender_phone_clean, {
        "referredBy": db._clean_phone(referrer_phone),
        "referralCodeUsed": referral_code,
        "pendingDiscount": referee_pct,
        "pendingDiscountReason": "referral_first_visit",
        "pendingDiscountExpiresAt": expiry_iso,
        "pendingDiscountUsed": False,
    })

    # Create referral document for lifecycle tracking.
    # A random 4-digit verify code is stored on the doc; the owner uses it at
    # checkout to confirm the friend's visit (owner replies "YES {code}").
    verify_code = str(random.randint(1000, 9999))
    try:
        db.create_referral_doc(business_id, {
            "referrerPhone": db._clean_phone(referrer_phone),
            "refereePhone": sender_phone_clean,
            "codeUsed": referral_code,
            "status": "pending",
            "refereeDiscountApplied": referee_pct,
            "referrerDiscountApplied": referrer_pct,
            "verifyCode": verify_code,
        })
    except Exception as exc:
        logger.warning("[Referral] Failed to create referral doc (non-fatal): %s", exc)

    logger.info(
        "[Referral] Registered: referee=%s referrer=%s code=%s biz=%s",
        sender_phone_clean, db._clean_phone(referrer_phone), referral_code, business_id,
    )

    # 6. Welcome message — SHORT so the AI can naturally continue the booking conversation
    await send_to_customer(
        business,
        sender_phone_clean,
        f"👋 *Welcome to {biz_name}!*\n\n"
        f"You were referred by *{referrer_name}* — thank you for coming! 🎉\n\n"
        f"Your *{referee_pct}% discount* is saved for your first visit. "
        f"The team will apply it at checkout.",
    )
    return False  # Let AI continue — it will ask "When would you like to come in?"


# ── post-visit logic ──────────────────────────────────────────────────────────

async def on_booking_completed(
    business_id: str,
    customer_phone: str,
    booking: dict,
    business: dict,
) -> None:
    """Triggered when a booking status transitions to 'completed'.

    Responsibilities:
      - Increment totalVisits / mark as non-new
      - If referred friend completing first visit: clear 10%, reward referrer,
        update referral doc → refereeVisited
      - If referrer completing their discounted visit: clear 25%,
        update referral doc → referrerRewarded
      - For any other pending discount: clear it
      - If customer now has ≥ 2 visits: schedule referral invite sweep
    """
    customer = db.get_customer_by_phone(business_id, customer_phone)
    if not customer:
        logger.warning(
            "[Referral] on_booking_completed: customer %s not found in biz %s",
            customer_phone, business_id,
        )
        return

    prev_visits = int(customer.get("totalVisits") or 0)
    new_visits = prev_visits + 1

    customer_updates: dict = {
        "totalVisits": new_visits,
        "isNewCustomer": False,
        "lastVisit": datetime.utcnow().isoformat(),
    }

    pending_raw = customer.get("pendingDiscount")
    try:
        pending_amount = float(pending_raw) if pending_raw is not None else 0.0
    except (ValueError, TypeError):
        pending_amount = 0.0

    pending_reason: str = customer.get("pendingDiscountReason") or ""
    referred_by: str | None = customer.get("referredBy") or None

    # ── Case 1: referred friend completing FIRST visit ────────────────────────
    if prev_visits == 0 and pending_amount > 0 and pending_reason == "referral_first_visit" and referred_by:
        now_iso = datetime.utcnow().isoformat()
        customer_updates["pendingDiscount"] = None
        customer_updates["pendingDiscountReason"] = None
        customer_updates["pendingDiscountExpiresAt"] = None
        customer_updates["pendingDiscountConsumedAt"] = now_iso
        customer_updates["pendingDiscountUsed"] = True
        db.upsert_customer(business_id, customer_phone, customer_updates)
        logger.info(
            "[Referral] Referee %s completed first visit — clearing %.0f%% and rewarding %s",
            customer_phone, pending_amount, referred_by,
        )

        # Update referral doc to refereeVisited
        try:
            referral = db.get_referral_by_referee(business_id, customer_phone)
            if referral:
                db.update_referral_doc(business_id, referral["id"], {
                    "status": "refereeVisited",
                    "refereeVisitedAt": now_iso,
                })
        except Exception as exc:
            logger.warning("[Referral] Failed to update referral doc to refereeVisited: %s", exc)

        # Reward is now triggered by the owner's YES reply (handle_owner_referral_reply),
        # not automatically on booking completion.
        # First-timers don't get a referral invite (only 2+ visit customers do)
        return

    # ── Case 2: any customer clearing a pending discount ─────────────────────
    if pending_amount > 0:
        now_iso = datetime.utcnow().isoformat()
        customer_updates["pendingDiscount"] = None
        customer_updates["pendingDiscountReason"] = None
        customer_updates["pendingDiscountExpiresAt"] = None
        customer_updates["pendingDiscountConsumedAt"] = now_iso
        customer_updates["pendingDiscountUsed"] = True
        logger.info(
            "[Referral] Cleared %.0f%% discount (%s) for %s after completed visit",
            pending_amount, pending_reason or "unknown", customer_phone,
        )

        # If this was a referral reward redemption, close the referral loop
        if pending_reason == "referral_reward":
            try:
                referral = db.get_referral_by_referrer(business_id, customer_phone)
                if referral:
                    db.update_referral_doc(business_id, referral["id"], {
                        "status": "referrerRewarded",
                        "referrerRewardedAt": now_iso,
                    })
            except Exception as exc:
                logger.warning("[Referral] Failed to update referral doc to referrerRewarded: %s", exc)

    db.upsert_customer(business_id, customer_phone, customer_updates)

    # ── Schedule referral invite if customer now has ≥ 2 completed visits ────
    booking_id: str = booking.get("id") or booking.get("bookingId", "")
    if new_visits >= 2 and booking_id and not booking.get("referralInviteScheduled"):
        db.update_booking(
            booking_id,
            {
                "referralInviteScheduled": True,
                "referralInviteSent": False,
            },
            business_id,
        )
        logger.info(
            "[Referral] Invite scheduled for %s after %d visits (booking=%s)",
            customer_phone, new_visits, booking_id,
        )


async def _reward_referrer(
    business_id: str,
    referrer_phone: str,
    friend_customer: dict,
    business: dict,
) -> None:
    """Set pendingDiscount = 25% (or business config %) for the referrer and notify them."""
    referrer = db.get_customer_by_phone(business_id, referrer_phone)
    if not referrer:
        logger.warning(
            "[Referral] Referrer %s not found in biz %s — cannot reward",
            referrer_phone, business_id,
        )
        return

    referrer_pct = int(business.get("referrerDiscountPercent") or 25)

    # Idempotent: don't replace if they already have a reward pending
    existing_discount = float(referrer.get("pendingDiscount") or 0)
    if existing_discount >= referrer_pct:
        logger.info(
            "[Referral] Referrer %s already has %.0f%% discount — not overwriting",
            referrer_phone, existing_discount,
        )
        return

    expiry_iso = (datetime.utcnow() + timedelta(days=_DISCOUNT_EXPIRY_DAYS)).isoformat()

    db.upsert_customer(business_id, referrer_phone, {
        "pendingDiscount": referrer_pct,
        "pendingDiscountReason": "referral_reward",
        "pendingDiscountExpiresAt": expiry_iso,
        "pendingDiscountUsed": False,
    })

    friend_name = (
        friend_customer.get("name")
        or friend_customer.get("customerName")
        or "Your referred friend"
    )
    biz_name = business.get("name", "us")

    await send_to_customer(
        business,
        referrer_phone,
        f"🎉 *Great news!*\n\n"
        f"*{friend_name}* just completed their first visit at *{biz_name}*!\n\n"
        f"As a thank-you, you've earned a *{referrer_pct}% discount* on your next visit. "
        f"The team will apply it at checkout. 😊",
    )
    logger.info(
        "[Referral] Rewarded %s with %d%% discount (friend=%s)",
        referrer_phone, referrer_pct,
        friend_customer.get("phone") or friend_customer.get("id", "?"),
    )


# ── owner referral confirmation reply ─────────────────────────────────────────

async def handle_owner_referral_reply(
    business: dict,
    owner_phone: str,
    body: str,
    device_id: str,
) -> bool:
    """Intercept a salon owner's YES {code} or NO reply for referral visit confirmation.

    Must be called before normal owner command routing.

    Returns True  → message was a referral confirmation reply; caller stops routing.
    Returns False → not a referral reply; caller continues to normal owner commands.
    """
    text = body.strip()
    yes_match = _OWNER_YES_CONFIRM_RE.match(text)
    no_match = _OWNER_NO_CONFIRM_RE.match(text) if not yes_match else None

    if not yes_match and not no_match:
        return False

    # Import here (inside the function) to avoid circular imports.
    # Only instantiate the client when we actually need to send a reply.
    from app.services.whatsmeow_client import WhatsmeowClient
    _wa = WhatsmeowClient()

    business_id = business["id"]
    now_iso = datetime.utcnow().isoformat()

    # ── YES {code} ────────────────────────────────────────────────────────────
    if yes_match:
        verify_code = yes_match.group(1)
        confirmation = db.get_referral_owner_confirmation_by_code(business_id, verify_code)

        if not confirmation:
            reply = (
                f"❌ Code *{verify_code}* was not recognised or has already expired. "
                f"Please check the code and try again."
            )
            await _wa.send_message(owner_phone, reply, device_id=device_id)
            return True

        # Mark confirmation as confirmed
        db.update_referral_owner_confirmation(business_id, confirmation["id"], {
            "status": "confirmed",
            "confirmedAt": now_iso,
        })

        # Update referral doc to refereeVisited
        try:
            db.update_referral_doc(business_id, confirmation["referralId"], {
                "status": "refereeVisited",
                "refereeVisitedAt": now_iso,
            })
        except Exception as exc:
            logger.warning("[Referral] Failed to update referral doc on owner YES: %s", exc)

        # Reward the referrer
        referrer_phone = confirmation.get("referrerPhone", "")
        referee_phone = confirmation.get("refereePhone", "")
        referee_customer = db.get_customer_by_phone(business_id, referee_phone) or {}
        await _reward_referrer(business_id, referrer_phone, referee_customer, business)

        reply = (
            "✅ *Done!* The referral reward has been sent to the customer who made the referral. "
            "They'll receive a discount on their next visit. 🎉"
        )
        await _wa.send_message(owner_phone, reply, device_id=device_id)
        logger.info(
            "[Referral] Owner %s confirmed visit — code=%s referrer=%s rewarded",
            owner_phone, verify_code, referrer_phone,
        )
        return True

    # ── NO ────────────────────────────────────────────────────────────────────
    owner_phone_clean = db._clean_phone(owner_phone)
    confirmation = db.get_latest_pending_referral_owner_confirmation(business_id, owner_phone_clean)

    if not confirmation:
        return False  # No pending confirmation found — ignore silently

    # Mark confirmation as no-show
    db.update_referral_owner_confirmation(business_id, confirmation["id"], {
        "status": "noshow",
        "noshowAt": now_iso,
    })

    # Cancel the referred friend's pending discount
    referee_phone = confirmation.get("refereePhone", "")
    if referee_phone:
        db.upsert_customer(business_id, referee_phone, {
            "pendingDiscount": None,
            "pendingDiscountReason": None,
            "pendingDiscountExpiresAt": None,
            "pendingDiscountUsed": False,
        })

    # Update referral doc to noshow
    try:
        db.update_referral_doc(business_id, confirmation["referralId"], {
            "status": "noshow",
            "noshowAt": now_iso,
        })
    except Exception as exc:
        logger.warning("[Referral] Failed to update referral doc on owner NO: %s", exc)

    reply = (
        "✅ *Noted.* No referral reward will be sent. "
        "The customer's pending discount has been cancelled."
    )
    await _wa.send_message(owner_phone, reply, device_id=device_id)
    logger.info(
        "[Referral] Owner %s reported no-show — confirmation=%s referee=%s",
        owner_phone, confirmation["id"], referee_phone,
    )
    return True
