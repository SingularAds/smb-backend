"""Global Recepte Knowledge Base.

Loads a Firestore document at ``system/recepte_kb`` that contains product,
sales, pricing, FAQ, and brand-voice knowledge common to ALL businesses.

The KB is small enough to inject directly into prompts — no embeddings or
retrieval pipelines are needed.

Update the KB via Firestore console or the seed_global_kb() helper — changes
take effect on the next request (cached for 5 minutes in memory).

Layer order for LLM prompt injection:
  1. Base system prompt
  2. Current mode instructions
  3. Current sales phase instructions
  4. Global Recepte KB   ← this module
  5. Per-business context
  6. Conversation message / user message
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── In-process cache ──────────────────────────────────────────────────────────
_kb_cache: Optional[str] = None
_kb_cache_ts: float = 0.0
_KB_TTL_S: int = 300  # 5-minute TTL — Firestore is not polled on every request

def _approx_token_count(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


# ── Default KB content ────────────────────────────────────────────────────────
# This is used as the seed value when the Firestore doc does not exist yet.
# Update the Firestore doc to change live behaviour without redeploying.

DEFAULT_KB_TEXT = """\
=== RECEPTE GLOBAL KNOWLEDGE BASE ===

WHAT IS RECEPTE?
Recepte is an AI receptionist platform built specifically for small businesses.
It automates customer conversations, appointment bookings, reminders, and
follow-ups on WhatsApp — so business owners never miss a booking or a lead.
The AI receptionist handles customer messages 24/7, speaks the customer's language
automatically, and integrates with Google Calendar for real-time availability.

PRODUCT OVERVIEW
• AI receptionist that replies instantly on WhatsApp (and voice calls)
• Automated booking, rescheduling, and cancellation
• Real-time Google Calendar sync — no double bookings
• Multilingual (auto-detects customer language and replies in it)
• Booking reminders sent automatically to customers
• Win-back and referral automation (Pro plan)
• Works for: salons, barbershops, clinics, spas, gyms, restaurants, stores, and more

HOW IT WORKS
1. Business owner onboards on WhatsApp with Sofia (2–3 minutes)
2. Owner links their WhatsApp business number via a pairing code
3. AI receptionist goes live — customers start chatting directly with it
4. Owner manages their business via simple WhatsApp commands (today, tomorrow, settings, etc.)
5. Google Calendar is optionally linked for full availability sync

SUBSCRIPTION PLANS

Starter Plan
• AI receptionist on WhatsApp + voice calls
• Booking, rescheduling, cancellation
• Google Calendar integration
• Automated email + WhatsApp confirmations
• Booking reminder notifications
• Up to 500 customer conversations per month
• Email + WhatsApp support

Pro Plan — Everything in Starter, plus:
• Win-back automation (re-engages inactive customers)
• Smart reminders (vaccine, treatment follow-up, birthday messages)
• Google Review automation
• Referral system (give discounts for referrals, grow word-of-mouth)
• Instagram collabs & tag-a-friend autopost
• Loyalty stamp card
• WhatsApp Status content generator
• Customer lifetime value insights
• Priority support
• Unlimited conversations

PRICING
Prices depend on country/region and are shown per business at checkout.
Typical monthly pricing:
  • Starter & Pro plans range from $7 to $149/month depending on region
Annual billing is available at 2 months free (10 months charged for 12).

FREE TRIAL
Every new business gets a 7-day FREE trial with FULL Pro features.
No credit card required to start.
The trial starts automatically once the business WhatsApp number is connected.

PAYMENT & BILLING
• Payment is processed securely via Stripe
• Monthly or annual billing available
• Activation is automatic after payment — no manual step needed
• Owner receives a WhatsApp confirmation once the plan is active
• Payment link is a secure Stripe-hosted checkout page

ONBOARDING PROCESS
Step 1: Sofia (AI) chats with the owner on WhatsApp, extracts business details
        (can auto-fill from website, Google Maps, or Instagram link)
Step 2: Owner confirms the business details card
Step 3: Owner links their WhatsApp business number via pairing code
Step 4: Optionally connect Google Calendar for live availability
Step 5: AI receptionist is live — 7-day free trial begins immediately

ACTIVATION GUIDANCE
• To start: message the Recepte WhatsApp number and Sofia will guide you
• Setup takes 2–4 minutes on average
• No technical knowledge required
• You can update your business details, services, and hours anytime via commands

DEMO
When a business owner wants to see how the AI works:
• Offer a live booking demo: invite them to pretend they are a customer
• Show how the AI handles a booking request in their business type
• Demonstrate multilingual support if relevant
• Keep the demo to 2–3 exchanges — quality over quantity

FAQ

Q: Do my customers need to install anything?
A: No. Customers just message the business on WhatsApp as they always do.
   The AI replies automatically — completely invisible to the customer.

Q: Can the AI handle all languages?
A: Yes. Recepte auto-detects the customer's language and replies in it.
   It supports English, Portuguese, Spanish, French, German, Hindi, Arabic, and more.

Q: What happens when the AI doesn't know something?
A: The AI answers from the business profile. If truly unsure, it apologises politely
   and suggests the customer call or message back during business hours.

Q: Can I still reply manually?
A: Yes. The owner can turn auto-reply off at any time with a simple command.
   Turning it back on is equally instant.

Q: What if I have multiple staff members?
A: You can add staff and assign bookings. Customers can also choose their preferred
   stylist or practitioner.

Q: Is there a contract?
A: No long-term contract. Monthly plans can be cancelled anytime.
   Annual plans offer 2 months free.

Q: How do I cancel?
A: Reply CANCEL on WhatsApp or manage your subscription via the billing portal.
   Cancellation takes effect at the end of the current billing period.

OBJECTION HANDLING

"It's too expensive"
Recepte typically pays for itself by recovering 2–3 missed bookings per month
that would otherwise be lost. The free trial lets you see the value before paying.

"I already have a receptionist"
Recepte doesn't replace your team — it handles the repetitive stuff (booking, reminders,
FAQs) so your team can focus on higher-value work. It also works 24/7, answering
customers at 2am when no human is available.

"I'm not sure WhatsApp will work for my customers"
WhatsApp is used by over 2 billion people. For most small businesses, it is already
the #1 way customers prefer to communicate. Recepte meets them where they already are.

"I need to think about it"
Totally fine! The 7-day free trial means there's no risk — you get to see real results
with your actual customers before making any decision.

"I'm worried it will sound robotic"
The AI is trained on natural conversation patterns and matches your business's tone.
You can set it to be casual, professional, or anything in between. Most customers
don't even realise they're talking to an AI.

BRAND VOICE GUIDELINES
• Warm, direct, and confident — never robotic or corporate
• Use "I" (Sofia) not "we" or "Recepte system"
• Concise messages — this is WhatsApp, not email
• Emojis are welcome but used sparingly
• Celebrate the owner's decision to automate — it's a smart move
• Never say "I can't help with that" — find a way to help or escalate warmly
• Language must always match the user's language exactly

=== END OF KNOWLEDGE BASE ===
"""


def get_global_kb(force_refresh: bool = False) -> str:
    """Return the Global Recepte KB text.

    Loads from Firestore ``system/recepte_kb`` with a 5-minute in-memory cache.
    Falls back to DEFAULT_KB_TEXT when Firestore is unavailable.

    Args:
        force_refresh: Bypass the cache and reload from Firestore.
    """
    global _kb_cache, _kb_cache_ts

    now = time.monotonic()
    if not force_refresh and _kb_cache is not None and (now - _kb_cache_ts) < _KB_TTL_S:
        logger.info(
            "[GLOBAL_KB] Cache hit (chars=%d, approx_tokens=%d)",
            len(_kb_cache), _approx_token_count(_kb_cache),
        )
        return _kb_cache

    try:
        import app.firestore as db_module
        kb_data = db_module.get_global_kb()
        if kb_data and kb_data.get("content"):
            _kb_cache = str(kb_data["content"])
            _kb_cache_ts = now
            logger.info(
                "[GLOBAL_KB] Loaded from Firestore system/recepte_kb (chars=%d, approx_tokens=%d)",
                len(_kb_cache), _approx_token_count(_kb_cache),
            )
            return _kb_cache
    except Exception as exc:
        logger.warning("[GLOBAL_KB] Could not load from Firestore: %s — using default", exc)

    # Fallback to embedded default
    _kb_cache = DEFAULT_KB_TEXT
    _kb_cache_ts = now
    logger.warning(
        "[GLOBAL_KB] Using default KB (chars=%d, approx_tokens=%d)",
        len(_kb_cache), _approx_token_count(_kb_cache),
    )
    return _kb_cache


def seed_global_kb() -> bool:
    """Write the default KB to Firestore if the document does not already exist.

    Safe to call at startup — will NOT overwrite an existing KB document.
    Returns True when the document was created, False when it already existed.
    """
    try:
        import app.firestore as db_module
        existing = db_module.get_global_kb()
        if existing and existing.get("content"):
            logger.info("[GLOBAL_KB] Firestore KB already exists — skipping seed")
            return False
        db_module.set_global_kb(DEFAULT_KB_TEXT)
        logger.info("[GLOBAL_KB] Default KB seeded to Firestore system/recepte_kb")
        return True
    except Exception as exc:
        logger.warning("[GLOBAL_KB] Seed failed: %s", exc)
        return False


def build_kb_prompt_section() -> str:
    """Return the Global KB formatted for injection into a system prompt."""
    kb = get_global_kb()
    return (
        "--- GLOBAL RECEPTE PRODUCT KNOWLEDGE ---\n"
        "Use this section ONLY for product/sales/pricing/onboarding/demo questions.\n"
        "Do NOT use it for booking, calendar, capacity, or business-specific queries.\n\n"
        f"{kb}\n"
        "--- END GLOBAL PRODUCT KNOWLEDGE ---"
    )
