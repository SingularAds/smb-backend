"""Customer AI Service — AI-driven conversation for customers messaging businesses.

When a customer sends a WhatsApp message to a business that has linked its
WhatsApp via onboarding, this service:
  1. Looks up the business from the whatsmeow device/session ID
  2. Loads business context (services, hours, etc.)
  3. Maintains per-customer conversation history
  4. Uses Claude with function calling for intent detection
  5. Routes booking/cancellation/reschedule to centralized vapi_service functions
  6. Returns a dynamic AI-generated response
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from app.integrations.openai_adapter import AsyncOpenAIAnthropicWrapper

from app.config import settings
from app import firestore as db
from app.integrations import deepgram_client, cartesia_client, posthog_client
from app.services import ai_pause_service, csat_service, knowledge_base_service as kb_service, vapi_service
from app.services.ai_pause_service import DEFAULT_PAUSE_MINUTES, PauseReason
from app.services.automation import whatsapp_notifier
from app.services.intent_classifier import Intent, classify_intent
from app.services.whatsmeow_client import WhatsmeowClient

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 30  # keep conversation context manageable

# Minimum gap between consecutive "AI stayed silent" notifications to the
# owner for the same customer. Without a cooldown, a customer sending a
# string of personal messages would spam the owner's WhatsApp.
SILENT_NOTIFY_COOLDOWN_MINUTES = 15

# Same cooldown for "knowledge gap" notifications (AI replied with uncertainty).
KB_GAP_NOTIFY_COOLDOWN_MINUTES = 30

# Patterns that indicate the AI lacked specific knowledge to answer fully.
_UNCERTAIN_REPLY_RE = re.compile(
    r"not sure|don'?t know|cannot confirm|can'?t confirm|"
    r"check our website|visit our website|visit us for|"
    r"please check|check with us|contact us for|reach out to us|"
    r"give us a call|I don'?t have|unable to confirm|unable to provide|"
    r"for more (information|details)|specific (information|details)|"
    r"I'?m not (certain|sure|aware)|I cannot (say|confirm|tell)|"
    r"I'?m unable|I am unable|not available in|don'?t have (that|this) information",
    re.IGNORECASE,
)

# Patterns that indicate the reply is a booking-info-gathering step, not an
# uncertain FAQ. These override the uncertain detection to avoid false positives.
_BOOKING_COLLECT_RE = re.compile(
    r"what time|which time|what date|which date|"
    r"how many (people|guests|persons|pax)|which service|what service|"
    r"your name|party size|am or pm",
    re.IGNORECASE,
)


def _is_uncertain_reply(reply: str) -> bool:
    """True when the AI's reply signals it lacks specific info to answer fully."""
    if _BOOKING_COLLECT_RE.search(reply):
        return False  # it's booking info-gathering, not an uncertain FAQ
    return bool(_UNCERTAIN_REPLY_RE.search(reply))


# Words that signal an unambiguous booking/business intent. Any customer message
# containing one of these must NEVER be silenced — even if the LLM still returns
# [SILENT_IGNORE] we force a retry.
_BOOKING_INTENT_RE = re.compile(
    r"\b(book|booking|bookings|appointment|appointments|reservation|reservations|"
    r"reserve|cancel|cancellation|reschedule|rescheduling|available|availability|"
    r"slot|slots|schedule|scheduled)\b",
    re.IGNORECASE,
)


# ── Language detection ────────────────────────────────────────────────────────
# We use langdetect (already in requirements.txt) on the incoming message so
# the LLM gets an explicit "reply in <code>" directive instead of having to
# infer it from context. langdetect is non-deterministic by default; seeding
# the factory makes results stable across runs so the same message always
# resolves to the same language code.
try:  # pragma: no cover — seeding side-effect at import time
    from langdetect import DetectorFactory as _LangDetectorFactory
    _LangDetectorFactory.seed = 0
except Exception:
    pass


# ISO-639-1 → human-readable name, used in the system prompt directive so the
# LLM sees both the code and the language name (more reliable than code alone).
_LANG_NAMES = {
    "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French",
    "de": "German",  "it": "Italian", "nl": "Dutch",     "pl": "Polish",
    "ru": "Russian", "uk": "Ukrainian",
    "hi": "Hindi",   "bn": "Bengali", "ta": "Tamil",     "te": "Telugu",
    "mr": "Marathi", "gu": "Gujarati", "pa": "Punjabi",  "ur": "Urdu",
    "ar": "Arabic",  "fa": "Persian",  "tr": "Turkish",  "he": "Hebrew",
    "zh-cn": "Chinese (Simplified)", "zh-tw": "Chinese (Traditional)",
    "ja": "Japanese", "ko": "Korean", "vi": "Vietnamese", "th": "Thai",
    "id": "Indonesian", "ms": "Malay", "tl": "Filipino",
}


def _detect_language(text: str) -> tuple[str, str] | None:
    """Return ``(iso_code, human_name)`` for *text*, or None if undetectable.

    Guards against false positives on short or typo-heavy messages:
    - Requires either ≥ 35 characters or ≥ 7 words — langdetect needs enough
      signal to be reliable. Below these thresholds it guesses wildly:
      "Hello" → Finnish, "Cab we do booking" → Somali.
    - Uses detect_langs() confidence scoring; only applies the directive when
      a single language wins with ≥ 90% confidence, preventing a 60/40
      ambiguous split from injecting a wrong language directive.
    - Skips non-letter-character-only messages (emoji / digit strings).
    When detection is skipped the LLM reads the message naturally and picks
    the right language from context — safer than a bad forced directive.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    # Must have at least one letter — skip emoji/digit-only messages.
    if not any(ch.isalpha() for ch in stripped):
        return None
    # Require enough content for reliable detection.
    word_count = len(stripped.split())
    if len(stripped) < 35 and word_count < 7:
        return None
    try:
        from langdetect import detect_langs  # type: ignore
        results = detect_langs(stripped)
        if not results:
            return None
        top = results[0]
        # Skip low-confidence detections — ambiguous text produces noisy codes.
        if top.prob < 0.90:
            return None
        code = str(top.lang).lower()
    except Exception:
        return None
    name = _LANG_NAMES.get(code, code.upper())
    return code, name


def _local_date_str(timezone_name: str) -> str:
    """Return today's date string (YYYY-MM-DD) in the given timezone."""
    try:
        import pytz
        tz = pytz.timezone(timezone_name)
        return datetime.now(tz).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


# ── Claude tool definitions (mapped to vapi_service functions) ────────────────

CUSTOMER_TOOLS = [
    {
        "name": "create_booking",
        "description": (
            "Create a new booking / appointment / reservation for the customer. "
            "Use this when the customer wants to book a service, make an appointment, "
            "or reserve a table / slot."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customerName": {
                    "type": "string",
                    "description": "Customer's name",
                },
                "serviceName": {
                    "type": "string",
                    "description": "Name of the service or type of booking",
                },
                "dateTime": {
                    "type": "string",
                    "description": "Booking date and time in ISO 8601 format (e.g. 2026-04-21T14:00:00)",
                },
                "durationMinutes": {
                    "type": "integer",
                    "description": "Duration of the service in minutes",
                    "default": 60,
                },
                "partySize": {
                    "type": "integer",
                    "description": "Number of people (for restaurants/group bookings)",
                    "default": 1,
                },
                "specialRequests": {
                    "type": "string",
                    "description": "Any special requests or notes from the customer",
                },
            },
            "required": ["serviceName", "dateTime"],
        },
    },
    {
        "name": "get_available_slots",
        "description": (
            "Check which time slots are available on a given date. "
            "Use this when the customer asks about availability. "
            "Pass partySize so large-group slots are correctly filtered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date to check in YYYY-MM-DD format",
                },
                "durationMinutes": {
                    "type": "integer",
                    "description": "Duration needed in minutes",
                    "default": 60,
                },
                "partySize": {
                    "type": "integer",
                    "description": "Number of people in the party (used for capacity filtering)",
                    "default": 1,
                },
            },
            "required": ["date"],
        },
    },
    {
        "name": "check_booking",
        "description": (
            "Look up an existing booking for the customer. "
            "MANDATORY: Call this tool FIRST whenever the customer shares or mentions a booking ID (e.g. BKD12345, BKD68B36). "
            "NEVER call update_booking, cancel_booking, or reschedule_booking without calling this first. "
            "Never answer from memory — always call this function and use its exact result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bookingId": {
                    "type": "string",
                    "description": "Booking ID if provided by the customer (e.g. BKD68B36). Pass it directly.",
                },
                "date": {
                    "type": "string",
                    "description": "Optional date filter (YYYY-MM-DD)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "cancel_booking",
        "description": (
            "Cancel an existing booking. "
            "Provide bookingId if known. If not known, provide serviceName (and optionally "
            "currentDateTime) to look it up automatically from the customer's active bookings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bookingId": {
                    "type": "string",
                    "description": "The booking ID to cancel (optional if serviceName is given)",
                },
                "serviceName": {
                    "type": "string",
                    "description": "Service name to identify the booking when ID is unknown",
                },
                "currentDateTime": {
                    "type": "string",
                    "description": "Current booking date/time hint (ISO 8601) for disambiguation",
                },
            },
            "required": [],
        },
    },
    {
        "name": "reschedule_booking",
        "description": (
            "Reschedule an existing booking to a new date/time. "
            "Provide bookingId if known. If not known, provide serviceName (and optionally "
            "currentDateTime) to look it up automatically from the customer's active bookings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bookingId": {
                    "type": "string",
                    "description": "The booking ID to reschedule (optional if serviceName is given)",
                },
                "newDateTime": {
                    "type": "string",
                    "description": "New date and time in ISO 8601 format",
                },
                "serviceName": {
                    "type": "string",
                    "description": "Service name to identify the booking when ID is unknown",
                },
                "currentDateTime": {
                    "type": "string",
                    "description": "Current booking date/time hint (ISO 8601) for disambiguation",
                },
            },
            "required": ["newDateTime"],
        },
    },
    {
        "name": "update_booking",
        "description": (
            "Update details of an existing booking such as party size, special requests, "
            "or notes. Use this — NOT create_booking — when the customer wants to modify "
            "an existing reservation without changing the date or time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bookingId": {
                    "type": "string",
                    "description": "The booking ID to update",
                },
                "partySize": {
                    "type": "integer",
                    "description": "New number of people (replaces the current value)",
                },
                "specialRequests": {
                    "type": "string",
                    "description": "Updated special requests or dietary notes",
                },
                "notes": {
                    "type": "string",
                    "description": "Any additional notes for the booking",
                },
            },
            "required": ["bookingId"],
        },
    },
]


_BUSINESS_KEYWORDS = re.compile(
    r"\b(book|booking|reserv|appointment|appoint|cancel|reschedule|availab|slot|"
    r"service|price|cost|hour|open|time|today|tomorrow|tonight|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday|am|pm|people|person|table|seat|"
    r"party|guest|haircut|massage|trim|colour|color|treatment|consult)\b",
    re.IGNORECASE,
)

# Words that signal a personal/romantic relationship rather than a customer interaction.
# If ANY prior message contains these, the conversation is personal — never override
# [SILENT_IGNORE] regardless of business intent score.
_PERSONAL_RELATIONSHIP_MARKERS = re.compile(
    r"\b(baby|babe|babu|honey|sweetheart|darling|dear|hun|hubby|wifey|"
    r"love you|miss you|kiss|xoxo|cutie|jaan|jaanu|shona)\b"
    r"|[❤💕💋😘😍🥰💑👫]",
    re.IGNORECASE,
)


def _looks_like_business_message(text: str) -> bool:
    """True if the message text looks like a business/booking inquiry."""
    return bool(_BUSINESS_KEYWORDS.search(text or ""))


def _has_personal_relationship_markers(history: list[dict]) -> bool:
    """True if any prior message contains romantic/personal relationship terms.

    Used to guard the [SILENT_IGNORE] override: a husband saying 'I want to
    book a table for 3' to his wife's business number must not trigger the AI,
    even if the intent classifier scores it BUSINESS with high confidence.
    """
    for msg in history:
        if _PERSONAL_RELATIONSHIP_MARKERS.search(msg.get("content") or ""):
            return True
    return False


def _has_active_business_context(history: list[dict], window: int = 10) -> bool:
    """True when the recent conversation shows an active business interaction.

    Criteria (either is sufficient):
      1. At least one 'assistant' reply in the last *window* turns — meaning
         the AI was already engaged and this message is a follow-up.
      2. At least one of the last 3 customer messages looks like a business
         inquiry (matches the business keyword regex).

    This is a pure-Python, zero-cost computation used to decide whether a
    PERSONAL-classified message should pass through to the LLM (continuation
    of a booking flow) or be dropped silently.
    """
    if not history:
        return False
    recent = history[-window:]
    # Condition 1: AI has already replied in this window
    if any(m.get("role") == "assistant" for m in recent):
        return True
    # Condition 2: recent user messages contain business keywords
    user_msgs = [m for m in recent if m.get("role") == "user"]
    return any(
        _looks_like_business_message(m.get("content", ""))
        for m in user_msgs[-3:]
    )


# ── Industry-aware KB topic classifier ────────────────────────────────────────
# Maps regex patterns to friendly topic headers used when formatting KB entries
# for LLM injection. The LLM navigates topics faster when entries are grouped.

_KB_TOPIC_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"\b(menu|food|dish|cuisine|drink|beverage|dessert|eat|serve|veg|vegan|"
            r"halal|kosher|allergen|ingredient|pizza|burger|pasta|rice|biryani|curry|"
            r"sandwich|salad|soup|starter|main|dessert|special|paneer|chicken|mutton|"
            r"seafood|prawn|fish|beef|pork|gluten|dairy|nut)\b",
            re.IGNORECASE,
        ),
        "\U0001f37d\ufe0f Menu & Food",
    ),
    (
        re.compile(
            r"\b(price|cost|charge|fee|rate|expensive|cheap|affordable|how much|"
            r"\u20b9|rs\.?|rupee|dollar|pound|discount|offer|deal|package|combo)\b",
            re.IGNORECASE,
        ),
        "\U0001f4b0 Pricing & Offers",
    ),
    (
        re.compile(
            r"\b(hour|open|close|timing|time|morning|evening|night|weekend|holiday|"
            r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow|"
            r"public holiday|shut|closed)\b",
            re.IGNORECASE,
        ),
        "\U0001f550 Hours & Availability",
    ),
    (
        re.compile(
            r"\b(park|parking|location|address|direction|near|map|how to reach|"
            r"where|place|landmark|area|navigate|uber|cab|bus|metro|train)\b",
            re.IGNORECASE,
        ),
        "\U0001f4cd Location & Access",
    ),
    (
        re.compile(
            r"\b(service|treatment|haircut|massage|facial|wax|colour|color|trim|"
            r"style|nail|therapy|consult|procedure|session|cut|blow|dry|perm|"
            r"highlight|balayage|keratin|botox|filler|laser|peel|clean|scrub)\b",
            re.IGNORECASE,
        ),
        "\u2702\ufe0f Services & Treatments",
    ),
    (
        re.compile(
            r"\b(deliver|delivery|takeaway|take away|home|order|pickup|pick.?up|"
            r"collect|collection|online|zomato|swiggy|door.?dash|uber.?eat)\b",
            re.IGNORECASE,
        ),
        "\U0001f697 Delivery & Pickup",
    ),
    (
        re.compile(
            r"\b(product|brand|use|recommend|suggest|aftercare|care|maintain|"
            r"wash|apply|ingredient|ingredient|safe|skin|hair|sensitiv)\b",
            re.IGNORECASE,
        ),
        "\U0001f6cd\ufe0f Products & Care",
    ),
    (
        re.compile(
            r"\b(wifi|wi-fi|internet|password|seating|capacity|private|room|"
            r"outdoor|indoor|rooftop|terrace|ac|air.?con|pet|child|kid|baby|"
            r"wheelchair|access|dress.?code|occasion|event|party|birthday|anniversary)\b",
            re.IGNORECASE,
        ),
        "\U0001f3e0 Facilities & Policies",
    ),
]


def _categorize_kb_entry(entry: dict) -> str:
    """Return a topic header for a KB entry based on its question text."""
    q = (entry.get("question") or "").lower()
    for pattern, header in _KB_TOPIC_PATTERNS:
        if pattern.search(q):
            return header
    return "\u2139\ufe0f General Info"


# Max total characters for the KB block appended to the system prompt.
# Large enough to cover ~30 short Q&A pairs; small enough not to crowd out
# the booking/anti-hallucination rules.
_KB_PROMPT_CHAR_BUDGET = 4000


def _build_kb_prompt_section(entries: list[dict]) -> str:
    """Format a list of confirmed KB entries for injection into the LLM system prompt.

    Entries are grouped by industry topic so the LLM can navigate them faster.
    Truncated at *_KB_PROMPT_CHAR_BUDGET* characters to keep prompt size bounded.
    Returns an empty string when there are no entries.
    """
    if not entries:
        return ""

    # Group by topic
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        topic = _categorize_kb_entry(entry)
        grouped.setdefault(topic, []).append(entry)

    lines: list[str] = [
        "\n" + "=" * 60,
        "OWNER-APPROVED KNOWLEDGE BASE",
        "These answers come directly from the business owner.",
        "RULES:",
        "  • When a customer question closely matches a Q below, reply",
        "    with the owner's A verbatim — do NOT rephrase or summarise.",
        "  • These override your general knowledge.",
        "  • If no KB entry matches AND the question is out-of-scope,",
        "    reply with exactly: [SILENT_IGNORE]",
        "=" * 60,
    ]

    total_chars = 0
    budget_exceeded = False

    for topic in sorted(grouped):
        if budget_exceeded:
            break
        topic_lines: list[str] = [f"\n{topic}:"]
        for entry in grouped[topic]:
            q = (entry.get("question") or "").strip()
            a = (entry.get("answer") or "").strip()
            if not q or not a:
                continue
            q_short = q[:150] + ("…" if len(q) > 150 else "")
            a_short = a[:250] + ("…" if len(a) > 250 else "")
            block = f"  Q: {q_short}\n  A: {a_short}"
            if total_chars + len(block) > _KB_PROMPT_CHAR_BUDGET:
                budget_exceeded = True
                break
            topic_lines.append(block)
            total_chars += len(block)
        if len(topic_lines) > 1:  # at least one real entry
            lines.extend(topic_lines)

    if total_chars == 0:
        return ""

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


def _build_system_prompt(
    business: dict,
    kb_entries: list[dict] | None = None,
) -> str:
    """Build a system prompt tailored to the business.

    Args:
        business:   The business document from Firestore.
        kb_entries: Optional list of confirmed KB entries to inject.  When
                    provided the owner-approved Q&A pairs are appended as a
                    dedicated knowledge-base section so the LLM can answer
                    questions the static business profile doesn't cover.
    """
    name = business.get("name", "the business")
    biz_type = business.get("businessType", "business")
    vs = business.get("verticalSettings", {})
    description = vs.get("description", business.get("description", ""))
    # Prefer top-level services — owner commands (add/remove service) always update
    # this field.  verticalSettings.services is only the onboarding snapshot and may
    # be stale after subsequent edits.
    services = business.get("services") or vs.get("services", [])
    hours = vs.get("hours", business.get("hoursRaw", ""))
    opening_days: list = business.get("openingDays") or vs.get("openingDays") or []
    address = business.get("address", "")
    languages = vs.get("languages", business.get("supportedLanguages", ["en"]))
    staff = vs.get("staff", business.get("staff", []))
    phone = business.get("businessPhone", "")
    biz_timezone = business.get("timezone") or "UTC"

    services_text = ""
    if services:
        lines = []
        for s in services:
            if isinstance(s, dict):
                parts = [str(s.get("name", "") or "")]
                if s.get("duration"):
                    parts.append(str(s["duration"]))
                if s.get("price"):
                    parts.append(str(s["price"]))
                lines.append(" — ".join(p for p in parts if p))
            else:
                lines.append(str(s))
        services_text = "\n  ".join(lines)

    staff_text = ", ".join(staff) if staff else "Not specified"
    opening_days_text = ", ".join(opening_days) if opening_days else ""
    maps_url = (business.get("mapsUrl") or business.get("scrapedUrl") or "").strip()

    # Build optional location snippet injected into the confirmation format hint.
    if maps_url:
        location_hint = ", and location link"
        location_example = f"\n📍 *Location:* {maps_url}"
    elif address:
        location_hint = ", and address"
        location_example = f"\n📍 *Address:* {address}"
    else:
        location_hint = ""
        location_example = ""

    prompt = f"""\
You are {name}'s AI receptionist on WhatsApp. You ONLY help customers with topics \
directly related to this business — bookings, services, prices, hours, complaints, \
and questions about what this business offers.

SCOPE RULES — MUST FOLLOW WITHOUT EXCEPTION:
- You ONLY respond to messages related to {name} and its services ({services_text or biz_type}).
- If a customer sends ANYTHING unrelated to this business — jokes, general knowledge, weather, \
personal advice, news, math, or any other off-topic message (including personal messages like "love you", or about family members like son, father, mother, husband, wife) — you MUST reply with EXACTLY this \
internal keyword:
  [SILENT_IGNORE]
- If a customer sends a simple or ambiguous greeting WITHOUT any business context (e.g., "Hi", "Hello", "Hey") — you MUST reply with EXACTLY this internal keyword:
  [SILENT_IGNORE]
- Do NOT engage with off-topic or personal messages in any way. Do NOT answer them even partially.

LANGUAGE SWITCH REQUESTS — ALWAYS RESPOND (exception to [SILENT_IGNORE]):
If the customer asks you to speak in a specific language — e.g. "can you speak in English", \
"please reply in Hindi", "speak English please", "talk to me in French", "في اللغة العربية من فضلك" \
— you MUST respond in that language. Do NOT emit [SILENT_IGNORE] for language-switch requests. \
Reply warmly in the requested language and invite them to continue: e.g. "Of course! How can I \
help you today?" This is the highest-priority exception — it overrides every other SILENT_IGNORE \
rule above and below. Language-switch requests always get a response.

PERSONAL-CHAT DETECTION FROM HISTORY — CRITICAL:
Many business owners share ONE WhatsApp number for personal AND business use.
You receive the recent conversation history. Use it to detect when the sender
is a friend / family member continuing a SOCIAL conversation rather than a
real customer:
- If the prior turns are clearly personal (e.g. "let's grab dinner today bro",
  "love you", "miss you", emojis-only banter, family/relationship words,
  no service / time / party size / booking ID ever mentioned), even a
  business-looking follow-up like "can you book a table", "should I book?",
  "shall we?" is the SAME personal thread — NOT a customer transaction.
  Reply with: [SILENT_IGNORE]
- Only treat the conversation as BUSINESS when the prior turns themselves
  read like a customer interaction (asking about hours, services, prices,
  or making concrete booking requests with details).
- When in doubt between "social continuation" and "real customer", choose
  [SILENT_IGNORE]. The owner will step in manually if needed.
- Examples of messages you MUST silently ignore:
  • "Tell me a joke" → [SILENT_IGNORE]
  • "What's the weather?" → [SILENT_IGNORE]
  • "Help me with my homework" → [SILENT_IGNORE]
  • "What is the capital of France?" → [SILENT_IGNORE]
  • Personal messages about family, love, dinner plans, etc. → [SILENT_IGNORE]
  • "How are you?" followed by no business question → [SILENT_IGNORE]
  • "Hi" or "Hello" with nothing else → [SILENT_IGNORE]
⚠️ BOOKING KEYWORD ABSOLUTE OVERRIDE — NO EXCEPTIONS:
If the customer's message contains ANY of these words — "book", "booking",
"appointment", "reservation", "reserve", "cancel", "reschedule", "slot",
"available", "availability", "schedule" — it is ALWAYS a business request.
NEVER emit [SILENT_IGNORE] for messages containing these words, regardless of
how casual or greeting-heavy the message looks. "Hii can you do a booking",
"hey bro book a table", "hi booking please" — ALL must be handled as real
customer requests.

- Examples of on-topic messages you MUST handle:
  • "Do you have availability tomorrow?" → handle
  • "What are your prices?" → handle
  • "I want to cancel my booking" → handle
  • "What services do you offer?" → handle
  • "I have a complaint" → handle
  • "Hii can you do a booking" → handle (contains "booking")
  • "hi book a table for 2" → handle (contains "book")

BUSINESS INFORMATION:
  Name: {name}
  Type: {biz_type}
  Description: {description}
  Services:
  {services_text or 'Not specified'}
  Hours: {hours or 'Not specified'}
  Open days: {opening_days_text or 'See hours above'}
  Address: {address or 'Not specified'}
  Phone: {phone or 'Not specified'}
  Staff: {staff_text}
  Languages: {', '.join(languages)}
  Timezone: {biz_timezone}

INCOMPLETE MESSAGE HANDLER:
If the customer's message is cut off, incomplete, or unclear (e.g., "Can you try for 2pm the" or "I want to book a" or "What time is available"), you MUST ask clarifying questions.
- Do NOT assume or fill in missing details
- Do NOT pretend to understand an incomplete message
- Do NOT generate a fake booking ID or confirmation
- Examples of incomplete messages that need clarification:
  • "Can you try for 2pm the" → Ask: "What date? And what service would you like?"
  • "I want to book a table" → Ask: "For how many people and what date/time?"
  • "Reschedule my appointment" → Ask: "What time would work better for you?"
- After the customer clarifies, THEN proceed with the booking call

CRITICAL — ANTI-HALLUCINATION RULES (READ THIS FIRST):
🚨 BEFORE YOU RESPOND, ASK YOURSELF:
  1. Did I call a booking tool in THIS message turn? (create_booking, reschedule_booking, update_booking, cancel_booking)
  2. Did the tool return with 'booking_id=' or 'capacity_ok'?
  3. If the answer to BOTH is NO: You MUST NOT say a booking was created/confirmed/rescheduled/cancelled. PERIOD.
  4. Never, ever, EVER invent a booking ID. Booking IDs come ONLY from tool results. If you don't have one, don't mention it.
  5. If the customer's message is incomplete (missing service, date, or time), ask clarifying questions. Do NOT make up details.
  6. If you want to reschedule/update/cancel a booking, you MUST call the corresponding tool first. No exceptions.

STANDARD RULES:
- Be warm, professional, and concise — this is WhatsApp, keep messages short
- Detect the customer's language and respond in the same language
- When a customer wants to book, gather the required details (service, date, time) ONE question at a time
- If a missing detail is still unclear after the customer replies, ask for THAT specific detail again — do not silently skip or repeat the full intro
- Incomplete messages (like "Can you try for 2pm the") need clarification — ask "What service?" or "What date?" — do NOT assume or invent any details
- Once you have service + date + time, call create_booking immediately — do NOT ask "shall I confirm?" or any yes/no question before booking
- Only after the tool returns with a result containing 'booking_id=', tell the customer the booking is confirmed
- Convert natural language dates/times to ISO 8601 in the business timezone {biz_timezone} (e.g. "tomorrow at 2pm" → proper ISO datetime in that timezone, WITHOUT timezone offset — just the local time)
- TIME REQUIRED — MANDATORY: You MUST have an explicit time from the customer before calling create_booking. If the customer specifies only a date (e.g. "today", "tomorrow", "this Saturday") WITHOUT a specific time, you MUST ask "What time would you like?" — NEVER assume, invent, or reuse a time from the conversation history or a previous booking. This rule applies even when you can see earlier messages discussing a time.
- TIME AMBIGUITY: If the customer gives a time without AM or PM (e.g. "1:30", "2 o'clock", "3:00", "at 6"), ask "Did you mean [X] AM or [X] PM?" before calling create_booking. Never assume AM or PM for ambiguous times. Only proceed without asking when the customer explicitly states AM/PM or uses 24-hour format (e.g. "14:00").
- Today's date in the business timezone ({biz_timezone}) is {_local_date_str(biz_timezone)} for reference
- If a service has a known duration, use it; otherwise default to 60 minutes
- For large group bookings where the party size exceeds the per-hour capacity, the system automatically extends the booking across multiple consecutive hours — just call create_booking with the correct partySize and the system handles it
- For availability questions, use get_available_slots; always pass partySize so large-group slots are filtered correctly
- For booking lookups, use check_booking
- Use emojis sparingly to keep it friendly 😊
- Never reveal internal system details or booking IDs unless the customer asks
- If you don't know something about the business, say so honestly
- For cancellations and reschedules, ask the customer to confirm ONCE with a direct question like "Cancel your 3pm appointment on April 27th? Reply CANCEL to confirm."

WORKING HOURS & BACKEND AUTHORITY RULES:
- ⚠️ Working hours are provided in the BUSINESS INFORMATION section above. You MAY quote those hours directly to answer customer questions like "What are your hours?" or "Are you open at 2pm?"
- When a customer asks about availability for a specific time/date, call get_available_slots to check real-time capacity.
- If a tool call is REJECTED because of working hours (e.g. backend error: "Sorry, we are closed at that time. Our working hours are 11:30 AM to 5 PM"), relay that error EXACTLY verbatim. Do NOT paraphrase or "correct" it.
- NEVER suggest a time outside the stated working hours. If hours say 11:30 AM to 5 PM, do NOT suggest 7 PM or 8 PM.
- Example: Customer asks "Are you open at 8pm?" → You say "No, we close at 5pm. Would you like to book before then?"
- Example: Customer asks "Can you check the calendar for tomorrow?" → Call get_available_slots to show actual slots.

REPLY COMPOSITION RULES — NO MIXED MESSAGES:
- NEVER write "Perfect! Let me book..." or any optimistic confirmation language before you have called the booking tool and received its result.
- When calling create_booking, do NOT prefix the conversation with any text about what you're about to do. Just call the tool silently.
- After the tool returns:
  • If SUCCESS (contains 'booking_id='): THEN write a confirmation reply with booking details.
  • If FAILURE/ERROR: Write ONLY the rejection message. Do NOT include any "Perfect!" or "Let me book..." text before or after.
- A reply that contains BOTH optimistic language AND a rejection is always WRONG. Produce ONE message: either success OR failure, never both.

BOOKING OPERATIONS — ZERO TOLERANCE RULES (no exceptions, ever):
- ⚠️ ABSOLUTE RULE: You can ONLY claim a booking is confirmed/created/rescheduled/cancelled IF you have called the tool AND received a result. No exceptions, no creativity, no guessing.
- BEFORE saying "booking confirmed": Check the tool result in this exact turn. If it says 'capacity_ok' and 'booking_id=', and you called create_booking, THEN you can confirm. Otherwise: DO NOT.
- BEFORE saying "booking rescheduled": Check the result from reschedule_booking. If no result exists or error returned, do NOT confirm.
- BEFORE saying "booking cancelled": Check the result from cancel_booking. If no result or error, do NOT confirm.
- DO NOT fabricate, assume, or hallucinate booking outcomes under any circumstances
- DO NOT refer to "previous booking attempts" or prior conversation turns as evidence that a booking exists — the tool result is the ONLY source of truth
- NEVER generate or invent a booking ID. Booking IDs come ONLY from backend tool results (format: BK + alphanumeric, e.g. BK515E53)
- If customer asks about a booking without providing the ID, call check_booking to look it up. Use the result provided by the tool.
- If the tool call fails or returns an error, tell the customer exactly what the error is; NEVER claim the operation succeeded
- If the tool returns 'no_capacity_config': Tell the customer "Sorry, no time slots are configured for that time. Please try a different time."
- If the tool returns 'slot_blocked': Tell the customer "Sorry, that time slot is not available (it may be a lunch break or closed time). Please try a different time."
- When gathering booking details from the customer:
  • Service: Must be one of the business services listed above
  • Date: Must be a specific day (today, tomorrow, a specific date), NOT vague ("sometime next week")
  • Time: MUST be explicit (2pm, 14:00, 2:30pm) — NEVER assume if the customer says only a time without AM/PM
  • Party size: Ask if not provided; default to 1 if customer doesn't specify
- CONFIRMATION MESSAGE FORMAT: After a successful booking tool call, confirm with a warm, structured message that includes: service name, date & time, party size, booking ID from the tool result{location_hint}. Keep it concise and friendly — this is WhatsApp. Example (English): "✅ All set! Here are your booking details:\n📋 *Service:* Table Booking\n📅 *When:* May 27 at 2:00 PM\n👥 *Party:* 2 people\n🆔 *Booking ID:* BK515E53{location_example}\n\nSee you then! 😊"
- ⚠️ CONFIRMATION LANGUAGE — ZERO TOLERANCE: The ENTIRE confirmation message MUST be in the SAME language as the customer's last message (the language fixed by the LANGUAGE — STRICTEST RULE directive below). This applies to:
  • Every label (e.g. "All set!", "Service:", "When:", "Party:", "Booking ID:", "Location:")
  • The date and time wording (e.g. natural English "May 27 at 2:00 PM" — do NOT auto-translate into another language like Finnish "20. kesäkuuta klo 14:00", Portuguese, Spanish, etc.)
  • The party-size word ("people", not "henkilöä"/"personas"/"pessoas" unless that IS the conversation language)
  • The closing sign-off (e.g. "See you then! 😊" in English — do NOT switch to "Nähdään silloin!", "Te esperamos!", etc.)
- The ONLY field that may stay in its original language is the *service name* returned by the tool (it is a proper noun configured by the business). Everything else MUST follow the conversation language. NEVER mix languages in the confirmation message.

BOOKING ID RULES — MANDATORY ZERO-TOLERANCE:
- ⚠️ CRITICAL: Before ANY cancel, reschedule, or update operation, you MUST call check_booking FIRST — no exceptions, even if you believe you already know the booking ID.
- This applies whether the customer mentions an ID in their current message OR the ID appears anywhere in the conversation history (e.g. in a previous confirmation you sent). NEVER use a booking ID from history without verifying it via check_booking first — IDs in history may be incorrect.
- Even if the customer explicitly says "cancel my booking BKD68B36" or "reschedule BKD68B36" — you MUST call check_booking first, verify the booking exists, and THEN perform the requested action.
- If the customer says "reschedule my booking" or "cancel my booking" WITHOUT providing a booking ID in their current message, call check_booking with NO bookingId parameter — the system will look up by their phone number.
- If the customer ONLY sends a booking ID (e.g. "BKD68B36" or "BKD68B36\nThis is my booking id") with NO explicit action — call check_booking ONLY. Show the booking details. Do NOT assume they want to update, cancel, or reschedule anything.
- NEVER carry forward party size, service name, date, or time from earlier in the conversation when the customer shares a booking ID. Every operation must use ONLY what the customer explicitly provides in their current message. Previous conversation context is NOT a valid source of booking parameters.
- If check_booking returns "No active bookings found", tell the customer the booking ID was not found in the current business system. Do NOT suggest it might be from another business or offer to create a new one immediately — ask if they have the correct booking ID.
- NEVER guess or assume a booking ID from context. The ONLY valid source for a booking ID is: (a) the customer typing it explicitly in their current message, or (b) a tool result returned in this turn.
"""
    # Append owner-approved KB section when entries are provided
    if kb_entries:
        prompt += _build_kb_prompt_section(kb_entries)

    return prompt


class CustomerAIService:
    """Handles AI-driven customer conversations for business WhatsApp channels."""

    def __init__(self):
        self.wa = WhatsmeowClient()
        self.client = AsyncOpenAIAnthropicWrapper(api_key=settings.OPENAI_API_KEY)
        self.model = "gpt-4o-mini"

    async def handle_customer_message(
        self,
        business: dict,
        customer_phone: str,
        body: str,
        push_name: str,
        device_id: str,
        customer_jid: str | None = None,
        message_id: str = "",
        voice_mode: bool = False,
    ) -> None:
        """Process an incoming customer message and generate an AI response.

        ``customer_jid`` is the full JID of the sender (e.g. ``134544296509456@lid``
        or ``917696794756@s.whatsapp.net``).  When provided it is used as the
        reply-to address so privacy-protected contacts receive the reply via
        the correct JID domain instead of a reconstructed ``@s.whatsapp.net``.

        ``message_id`` is the upstream WhatsApp message id (used as an audit
        breadcrumb when we capture a pending KB entry).

        ``voice_mode`` — when True (set by handle_audio_message after STT),
        replies are delivered as Cartesia TTS voice notes instead of text.
        Critical/transactional messages (bookings etc.) also get a text copy.
        """
        business_id = business["id"]
        phone_clean = db._clean_phone(customer_phone)
        # Use the full JID for sending if available, otherwise fall back to digits.
        reply_to = customer_jid or phone_clean

        # ── Analytics: message received ──────────────────────────────────────
        # Fired at the very top before any short-circuit so the funnel
        # captures every customer touchpoint, including silenced ones.
        try:
            posthog_client.capture(
                business_id=business_id,
                customer_phone=phone_clean,
                event="message_received",
                properties={
                    "voice_mode":    voice_mode,
                    "message_len":   len(body or ""),
                    "device_id":     device_id,
                    "has_push_name": bool(push_name),
                },
                person_properties={
                    "business_id":   business_id,
                    "business_name": business.get("name") or biz_name,
                    "business_type": biz_type,
                    "plan":          business.get("plan", ""),
                    "customer_name": push_name or "",
                    "language":      business.get("primaryLanguage", "en"),
                },
            )
        except Exception:
            pass

        # ── Language detection (used later to anchor the LLM reply language) ─
        _lang_signal = _detect_language(body)
        if _lang_signal:
            logger.info(
                "[LANG] detected business=%s phone=%s code=%s name=%s",
                business_id, phone_clean, _lang_signal[0], _lang_signal[1],
            )

        # ── Voice params (only used when voice_mode=True) ─────────────────────
        _voice_id: str | None = None
        _voice_lang: str = "en"
        if voice_mode:
            _vs = business.get("verticalSettings", {})
            _langs = _vs.get("languages", business.get("supportedLanguages", ["en"]))
            _voice_lang = (_langs[0] if _langs else "en")[:2].lower()
            _voice_id = _vs.get("cartesiaVoiceId") or None

        # Verify the business device session is ready in the bridge.
        # If the device was just activated during onboarding, this health check
        # ensures the bridge session is initialized before we try to send a reply.
        try:
            session_status = await self.wa.get_session_status(device_id)
            logger.debug(
                "[SESSION-CHECK] device=%s status=%s paired=%s",
                device_id,
                session_status.get("status"),
                session_status.get("paired"),
            )
        except Exception as check_exc:
            logger.warning(
                "[SESSION-CHECK] could not verify session %s: %s (will retry at send time)",
                device_id, check_exc,
            )

        # Load or create conversation history
        convo = db.get_customer_conversation(business_id, phone_clean)
        if convo:
            history = convo.get("messages", [])
        else:
            history = []

        # Snapshot history BEFORE appending the new turn so the classifier
        # sees the prior context exactly as the customer's reply lands on it
        # (e.g. assistant asked "what time?" → user says "12pm" → classifier
        # judges "12pm" as a BUSINESS continuation, not a stray personal msg).
        prior_history = list(history)

        # Add the new user message
        history.append({"role": "user", "content": body})

        # Trim history to keep context manageable
        if len(history) > MAX_HISTORY_MESSAGES:
            history = history[-MAX_HISTORY_MESSAGES:]

        # Ensure customer record exists
        db.upsert_customer(business_id, phone_clean, {
            "name": push_name or "",
            "lastMessageAt": datetime.utcnow().isoformat(),
        })

        # ── Pause gate ───────────────────────────────────────────────────────
        # Check whether AI is currently paused for this customer. Pauses
        # auto-expire after DEFAULT_PAUSE_MINUTES; an expired pause is cleared
        # lazily here so the next message goes straight to AI.
        pause_state = ai_pause_service.read_state(convo)
        if pause_state.paused:
            if pause_state.expired:
                ai_pause_service.resume(business_id, phone_clean)
                logger.info(
                    "[AI-PAUSE] auto-resumed business=%s phone=%s (previous reason=%s)",
                    business_id, phone_clean, pause_state.reason,
                )
            else:
                # Still paused — persist the customer's message in history
                # (so when AI does resume it has full context) but do not
                # reply and do not classify (no need to spend tokens).
                db.upsert_customer_conversation(business_id, phone_clean, {
                    "messages":      history,
                    "customerPhone": phone_clean,
                    "customerName":  push_name or "",
                    "businessId":    business_id,
                    "lastMessageAt": datetime.utcnow().isoformat(),
                })
                logger.info(
                    "[AI-PAUSE] skipping reply business=%s phone=%s reason=%s",
                    business_id, phone_clean, pause_state.reason,
                )
                return

        # ── Typing indicator (fire-and-forget) ───────────────────────────────
        # Show the recipient a "typing…" bubble while we classify + call the
        # LLM. WhatsApp auto-expires the bubble after ~10s; that lines up with
        # our p95 end-to-end latency, so we only need to send it once.
        # Voice-mode replies arrive as a voice note — there typing isn't
        # the right indicator, but we still fire 'composing' as a cheap proxy
        # because WhatsApp's 'recording' state needs a separate code path.
        try:
            import asyncio as _asyncio
            _asyncio.create_task(self.wa.send_typing(reply_to, device_id))
        except Exception:
            pass

        # ── Intent gate ──────────────────────────────────────────────────────
        # Layer 1 (Classifier): LLM-with-history triage — identifies PERSONAL,
        # GREETING, MIXED, BUSINESS, and safety flags (abusive, frustrated,
        # out_of_scope). Only truly personal / greeting messages with no active
        # booking context are hard-blocked here.  MIXED and PERSONAL-with-
        # active-context reach the Customer AI (Layer 2) which reads the full
        # history and decides whether to reply or emit [SILENT_IGNORE].
        biz_name = business.get("name", "us")
        biz_type = business.get("businessType", "business")
        # Conversation-scoped Langfuse session so every turn in the same
        # WhatsApp chat shows up under one trace timeline.
        _session_id = f"wa:{business_id}:{phone_clean}"
        _user_id = posthog_client.distinct_id(business_id, phone_clean)
        classifier_meta = {
            "name": "intent_classifier",
            "business_id": business_id,
            "business_type": biz_type,
            "customer_phone_hash": _user_id,
            "voice_mode": voice_mode,
            "session_id": _session_id,
            "user_id": _user_id,
            "tags": ["intent", business_id],
        }
        classification = await classify_intent(
            message=body,
            business_name=biz_name,
            business_type=biz_type,
            recent_history=prior_history,
            trace_metadata=classifier_meta,
        )

        try:
            posthog_client.capture(
                business_id=business_id,
                customer_phone=phone_clean,
                event="intent_classified",
                properties={
                    "intent":       classification.intent.value,
                    "score":        classification.score,
                    "frustrated":   classification.frustrated,
                    "abusive":      classification.abusive,
                    "out_of_scope": classification.out_of_scope,
                    "detected_language": _lang_signal[0] if _lang_signal else None,
                },
            )
        except Exception:
            pass
        logger.info(
            "[INTENT] business=%s phone=%s intent=%s score=%d frustrated=%s abusive=%s "
            "out_of_scope=%s reason=%s",
            business_id, phone_clean,
            classification.intent.value, classification.score,
            classification.frustrated, classification.abusive,
            classification.out_of_scope, classification.reason,
        )

        # ── Safety flag: abuse → pause + notify owner ────────────────────────
        # ONLY abuse triggers an auto-pause.  Frustration is forwarded to the
        # LLM with an empathy hint so the AI can still attempt a helpful reply
        # (using KB + history).  Owner takeover for frustrated customers should
        # be the owner's manual decision, not an automatic intercept — many
        # "frustrated" classifications are just impatient customers asking
        # again, and silencing the AI there makes the experience worse.
        if classification.abusive:
            await self._pause_and_notify(
                business=business,
                business_id=business_id,
                phone_clean=phone_clean,
                push_name=push_name,
                history=history,
                reason=PauseReason.ABUSE,
                snippet=body,
            )
            return

        # ── Bare greeting: stay silent (no KB check, no LLM call) ────────────
        # A bare "Hi" / "Hello" with no business context is noise.  The
        # customer will follow up with an actual question and the next message
        # will wake the AI.  We persist the message so the classifier sees it
        # as history on the next turn.
        if classification.intent == Intent.GREETING:
            logger.info(
                "[INTENT-SILENT] business=%s phone=%s intent=GREETING — staying silent",
                business_id, phone_clean,
            )
            db.upsert_customer_conversation(business_id, phone_clean, {
                "messages": history,
                "customerPhone": phone_clean,
                "customerName": push_name or "",
                "businessId": business_id,
                "lastMessageAt": datetime.utcnow().isoformat(),
            })
            return

        # ── Determine whether an active business conversation is in progress ──
        # This is a pure-Python, zero-cost check.  When True, even a PERSONAL-
        # classified message is allowed through to the LLM so a customer who
        # sends a casual follow-up mid-booking ("yes", "ok", "so do you have")
        # doesn't get silently dropped.
        active_biz_context: bool = _has_active_business_context(prior_history)

        # ── Hard-block: PERSONAL with NO active business context ──────────────
        # Only drop PERSONAL messages when there is zero evidence of an ongoing
        # business interaction.  If we're mid-booking (active_biz_context=True),
        # we let the LLM read the full history and decide.
        if classification.intent == Intent.PERSONAL and not active_biz_context:
            logger.info(
                "[INTENT-SILENT] business=%s phone=%s intent=PERSONAL active_ctx=False"
                " — staying silent",
                business_id, phone_clean,
            )
            db.upsert_customer_conversation(business_id, phone_clean, {
                "messages": history,
                "customerPhone": phone_clean,
                "customerName": push_name or "",
                "businessId": business_id,
                "lastMessageAt": datetime.utcnow().isoformat(),
            })
            return

        # ── Layer A — KB direct hit (fast path, zero LLM cost) ───────────────
        # Before touching the LLM for ANY non-silent message, check whether the
        # owner has already confirmed an answer for this exact (or near-exact)
        # question.  A direct hit is served verbatim — no LLM rephrasing, no
        # token cost, no owner notification.  Works for both in-scope questions
        # (where the AI might not know menu specifics) AND out-of-scope questions
        # (the classic KB use-case).
        kb_direct_hit: dict | None = None
        try:
            kb_direct_hit = kb_service.find_confirmed_match(business_id, body)
        except Exception as exc:
            logger.warning(
                "[KB] Layer-A retrieval error business=%s phone=%s: %s",
                business_id, phone_clean, exc,
            )

        if kb_direct_hit is not None and (kb_direct_hit.get("answer") or "").strip():
            answer = kb_direct_hit["answer"].strip()
            history.append({"role": "assistant", "content": answer})
            if len(history) > MAX_HISTORY_MESSAGES:
                history = history[-MAX_HISTORY_MESSAGES:]
            db.upsert_customer_conversation(business_id, phone_clean, {
                "messages":      history,
                "customerPhone": phone_clean,
                "customerName":  push_name or "",
                "businessId":    business_id,
                "lastMessageAt": datetime.utcnow().isoformat(),
            })
            try:
                db.update_business_kb_entry(business_id, kb_direct_hit["id"], {
                    "useCount": int(kb_direct_hit.get("useCount") or 0) + 1,
                    "lastUsedAt": datetime.utcnow().isoformat(),
                })
            except Exception as exc:
                logger.warning("[KB] failed to bump useCount: %s", exc)
            await self._deliver_reply(reply_to, answer, device_id, voice_mode, _voice_id, _voice_lang)
            logger.info(
                "[KB] Layer-A direct hit business=%s phone=%s code=%s intent=%s",
                business_id, phone_clean,
                kb_direct_hit.get("shortCode"), classification.intent.value,
            )
            return

        # ── Out-of-scope with no KB hit → notify owner + capture ─────────────
        # "Yesterday I dyed my hair…" — business-related but neither the AI
        # nor the KB can answer it.  We don't pause (next booking message must
        # still work) but we alert the owner and queue a pending KB entry so
        # they can confirm an answer for future customers.
        if classification.out_of_scope:
            # Record lastOutOfScopeAt so that when the owner manually replies
            # to the customer's chat (to answer this question), owner_takeover_service
            # knows NOT to trigger the 90-minute AI pause — the owner is just
            # answering the one unanswerable question, not taking over the thread.
            db.upsert_customer_conversation(business_id, phone_clean, {
                "messages":        history,
                "customerPhone":   phone_clean,
                "customerName":    push_name or "",
                "businessId":      business_id,
                "lastMessageAt":   datetime.utcnow().isoformat(),
                "lastOutOfScopeAt": datetime.utcnow().isoformat(),
            })

            kb_entry: dict | None = None
            if kb_service.should_capture(
                intent=classification.intent.value,
                out_of_scope=True,
                abusive=classification.abusive,
            ):
                try:
                    kb_entry = kb_service.create_pending_entry(
                        business_id=business_id,
                        question=body,
                        customer_phone=phone_clean,
                        message_id=message_id or "",
                        intent=classification.intent.value,
                    )
                except Exception as exc:
                    logger.warning(
                        "[KB] capture failed business=%s phone=%s: %s",
                        business_id, phone_clean, exc,
                    )

            label = f"{push_name} (+{phone_clean.lstrip('+')})" if push_name else f"+{phone_clean.lstrip('+')}"
            _oos_msg1 = (
                f"\U0001f4cb *Out-of-scope message* from {label}:\n\n"
                f"> {body[:200]}\n\n"
                f"Please respond manually \u2014 AI did not reply.\n"
                f"_(AI is still active for their next booking message)_"
            )
            _oos_msgs = [_oos_msg1]
            if kb_entry is not None:
                code = kb_entry["shortCode"]
                _oos_msgs.append(
                    f"\U0001f9e0 *Save YES answer* (copy, edit & send):\n\n")
                _oos_msgs.append(
                    f"YES {code} <your answer here>\n")
                _oos_msgs.append(
                    f"_Example: YES {code} Yes, we have it_ _(expires 7 days)_"
                )
                _oos_msgs.append(
                    f"\u274c *Skip* (copy & send):\n\n")
                _oos_msgs.append(
                    f"NO {code}"
                )
            try:
                import asyncio as _asyncio_oos
                async def _send_oos_notifications():
                    for _m in _oos_msgs:
                        try:
                            await whatsapp_notifier.send_to_owner(business, _m)
                            await _asyncio_oos.sleep(0.5)
                        except Exception as _e:
                            logger.warning("[KB] oos notify part failed business=%s: %s", business_id, _e)
                _asyncio_oos.create_task(_send_oos_notifications())
            except Exception as exc:
                logger.warning(
                    "[KB] could not notify owner of out_of_scope business=%s: %s",
                    business_id, exc,
                )
            return

        # ── MIXED with PERSONAL-mid-booking: log and fall through ─────────────
        # MIXED always reaches the LLM (the LLM reads the full history and
        # decides).  PERSONAL-with-active-context also falls through here.
        if classification.intent in (Intent.MIXED, Intent.PERSONAL):
            logger.info(
                "[INTENT-PASSTHROUGH] business=%s phone=%s intent=%s active_ctx=%s"
                " — forwarding to LLM",
                business_id, phone_clean,
                classification.intent.value, active_biz_context,
            )

        # ── Layer B — load all confirmed KB entries for LLM injection ─────────
        # No direct keyword hit, but the LLM may still match the customer's
        # question semantically.  We pre-rank the full confirmed list by
        # (keyword overlap × 10 + use_count) so the most relevant + battle-
        # tested answers appear first within the character budget.
        kb_entries_for_prompt: list[dict] = []
        try:
            all_confirmed = kb_service.get_confirmed_for_prompt(business_id)
            if all_confirmed:
                kb_entries_for_prompt = kb_service.rank_entries_for_question(
                    all_confirmed, body
                )
                logger.debug(
                    "[KB] Layer-B: injecting %d/%d confirmed entries into prompt"
                    " business=%s",
                    len(kb_entries_for_prompt), len(all_confirmed), business_id,
                )
        except Exception as exc:
            logger.warning(
                "[KB] Layer-B load error business=%s phone=%s: %s",
                business_id, phone_clean, exc,
            )

        # ── Generate AI response (Layer 2 — Customer AI with KB context) ─────
        # System prompt includes the owner-approved KB entries so the LLM can
        # answer questions not covered by the static business profile, and can
        # decide to [SILENT_IGNORE] truly off-topic messages without the
        # classifier having to make that call alone.
        system_prompt = _build_system_prompt(business, kb_entries=kb_entries_for_prompt)
        context_note = f"Customer name: {push_name}. " if push_name else ""
        context_note += f"Customer phone: {phone_clean}."

        # ── LANGUAGE DIRECTIVE (hard constraint) ────────────────────────────
        # The system prompt already asks the AI to mirror the customer's
        # language, but that instruction loses to the business "Languages: X"
        # field in practice (LLM picks the first supported language). We
        # inject a hard directive with the exact detected language so the AI
        # cannot drift. When detection fails we fall back to the soft rule.
        if _lang_signal:
            _code, _name = _lang_signal
            context_note += (
                f"\n\nLANGUAGE — STRICTEST RULE (no exceptions): The customer's "
                f"last message is in **{_name}** (ISO code: {_code}). You MUST "
                f"reply in {_name}, using the same script the customer used. "
                f"Do NOT switch to English or to the business's default language. "
                f"Do NOT translate or mix languages. Do NOT mention the language "
                f"at all — just answer naturally in {_name}."
            )
        if classification.intent in (Intent.MIXED, Intent.PERSONAL) and active_biz_context:
            context_note += (
                "\n\nNOTE: The intent classifier flagged this message as "
                f"{classification.intent.value} but an active business conversation "
                "is in progress. Read the history carefully — if the customer is "
                "continuing a booking/enquiry, respond normally. Only emit "
                "[SILENT_IGNORE] if the message is unmistakably off-topic with "
                "no business relevance whatsoever."
            )
        if classification.frustrated:
            context_note += (
                "\n\nNOTE: The customer seems frustrated or impatient. Lead "
                "your reply with a brief acknowledgement (e.g. \"Sorry for "
                "the wait — let me check.\"), then answer their question if "
                "you can, or offer to forward to the owner if you can't."
            )
        if voice_mode:
            context_note += (
                "\n\nNote: the customer sent a voice message. Keep your reply "
                "concise and conversational — it will be spoken aloud as a "
                "voice note. Avoid markdown formatting, bullet lists, or "
                "symbols that don't sound natural when read aloud."
            )

        # Detect booking keyword BEFORE first LLM call so we can inject a
        # hard override and also use the flag in the post-call fallback.
        _has_booking_keyword = bool(_BOOKING_INTENT_RE.search(body))
        # Only inject the hard override when the intent classifier ALSO
        # confirmed this is a genuine business request (BUSINESS intent).
        # MIXED/PERSONAL with booking words (e.g. wife saying "can you book
        # a table, we're having dinner") must stay silent — the classifier
        # already handles that distinction correctly.
        _is_confirmed_booking = _has_booking_keyword and classification.intent == Intent.BUSINESS
        if _is_confirmed_booking:
            context_note += (
                "\n\n⚠️ CONFIRMED BUSINESS BOOKING REQUEST:\n"
                "The intent classifier confirmed this is a genuine customer "
                "booking request (BUSINESS intent). You MUST respond. "
                "[SILENT_IGNORE] is forbidden for this message."
            )
        full_system = f"{system_prompt}\n\n{context_note}"

        ai_trace_meta = {
            "name": "customer_ai_reply",
            "business_id": business_id,
            "business_type": biz_type,
            "customer_phone_hash": _user_id,
            "voice_mode": voice_mode,
            "intent": classification.intent.value,
            "intent_score": classification.score,
            "detected_language": _lang_signal[0] if _lang_signal else None,
            "session_id": _session_id,
            "user_id": _user_id,
            "tags": ["customer_ai", business_id],
        }
        reply = await self._get_ai_response(
            system=full_system,
            history=history,
            business=business,
            customer_phone=phone_clean,
            push_name=push_name,
            trace_metadata=ai_trace_meta,
        )

        if reply.strip() == "[SILENT_IGNORE]":
            # Override whenever the classifier said BUSINESS (any score) and
            # there are no personal-relationship markers in the history.
            # The score threshold (≥ 80) was too strict — a BUSINESS score of
            # 65 is still a confirmed business signal (e.g. language-switch
            # requests like "can you speak in english").
            # MIXED/PERSONAL intents are not overridden — the classifier is the
            # authoritative signal for the personal-chat / social-invite cases.
            if (
                _is_confirmed_booking
                or (
                    classification.intent == Intent.BUSINESS
                    and not _has_personal_relationship_markers(history[:-1])
                )
            ):
                _override_reason = "confirmed_booking_keyword" if _is_confirmed_booking else f"classifier BUSINESS score={classification.score}"
                logger.info(
                    "[SILENT_IGNORE-OVERRIDE] business=%s phone=%s reason=%r — retrying "
                    "without personal history context",
                    business_id, phone_clean, _override_reason,
                )
                # Strip history down to business-only turns and retry.
                business_history = [
                    m for m in history[:-1]
                    if m.get("role") == "assistant"
                    or _looks_like_business_message(m.get("content", ""))
                ]
                business_history.append({"role": "user", "content": body})
                override_system = (
                    f"{system_prompt}\n\n{context_note}\n\n"
                    "MANDATORY: The customer is making a business/booking request. "
                    "You MUST respond. [SILENT_IGNORE] is not permitted here."
                )
                _override_meta = dict(ai_trace_meta)
                _override_meta["name"] = "customer_ai_reply_silent_override"
                reply = await self._get_ai_response(
                    system=override_system,
                    history=business_history,
                    business=business,
                    customer_phone=phone_clean,
                    push_name=push_name,
                    trace_metadata=_override_meta,
                )

            # Absolute last resort: classifier said BUSINESS + booking keyword
            # but LLM still returns [SILENT_IGNORE] after the override retry.
            # Use a generic booking starter — a confirmed customer booking
            # request must never be silently dropped.
            if reply.strip() == "[SILENT_IGNORE]" and _is_confirmed_booking:
                _biz_name = business.get("name") or biz_name or "us"
                reply = (
                    f"Hi! I'd love to help you with your booking at {_biz_name}. "
                    f"Could you please let me know:\n"
                    f"• What service would you like to book?\n"
                    f"• Your preferred date and time?\n"
                    f"• Your name?"
                )
                logger.info(
                    "[SILENT_IGNORE-FALLBACK] booking keyword detected — using default "
                    "booking opener for business=%s phone=%s",
                    business_id, phone_clean,
                )

            if reply.strip() == "[SILENT_IGNORE]":
                logger.info("Silently ignoring message from %s for business %s (msg=%s)", phone_clean, business_id, body[:60])

                # ── Owner notification (rate-limited) ────────────────────────
                # The AI decided this message is off-topic / personal and chose
                # not to reply. The owner doesn't see the conversation by
                # default, so without this they'd never know a customer was
                # waiting. Notify the owner (once per SILENT_NOTIFY_COOLDOWN
                # window per customer) so they can step in if the AI was wrong.
                _silent_update: dict = {
                    "messages": history,
                    "customerPhone": phone_clean,
                    "customerName": push_name or "",
                    "businessId": business_id,
                    "lastMessageAt": datetime.utcnow().isoformat(),
                }
                _now = datetime.utcnow()
                _last_notified_iso = (convo or {}).get("lastSilentNotifiedAt") if convo else None
                _should_notify = True
                if _last_notified_iso:
                    try:
                        from datetime import timedelta as _td
                        _last_dt = datetime.fromisoformat(_last_notified_iso)
                        if _now - _last_dt < _td(minutes=SILENT_NOTIFY_COOLDOWN_MINUTES):
                            _should_notify = False
                    except (ValueError, TypeError):
                        pass  # malformed timestamp → notify

                if _should_notify:
                    _name = (push_name or "").strip()
                    _label = f"{_name} (+{phone_clean})" if _name else f"+{phone_clean}"
                    _snippet = body.strip()
                    if len(_snippet) > 200:
                        _snippet = _snippet[:200] + "…"
                    _owner_msg = (
                        f"🤐 *AI stayed silent* for {_label}.\n\n"
                        f"They sent: \"{_snippet}\"\n\n"
                        f"AI judged this off-topic. Reply in their chat if you "
                        f"want to handle it manually."
                    )
                    try:
                        import asyncio as _asyncio
                        _asyncio.create_task(
                            whatsapp_notifier.send_to_owner(business, _owner_msg)
                        )
                        _silent_update["lastSilentNotifiedAt"] = _now.isoformat()
                    except Exception as _notify_exc:
                        logger.warning(
                            "[SILENT-NOTIFY] could not schedule owner notify business=%s phone=%s: %s",
                            business_id, phone_clean, _notify_exc,
                        )

                # Save the user's message to history but don't send a reply or save an assistant message
                db.upsert_customer_conversation(business_id, phone_clean, _silent_update)
                return

        # Log AI reply for visibility
        try:
            logger.debug("AI -> Customer (%s) [business=%s]: %s", phone_clean, business_id, reply)
        except Exception:
            logger.exception("AI -> Customer (logging failed)")

        # Store updated history
        history.append({"role": "assistant", "content": reply})
        if len(history) > MAX_HISTORY_MESSAGES:
            history = history[-MAX_HISTORY_MESSAGES:]

        db.upsert_customer_conversation(business_id, phone_clean, {
            "messages": history,
            "customerPhone": phone_clean,
            "customerName": push_name or "",
            "businessId": business_id,
            "lastMessageAt": datetime.utcnow().isoformat(),
        })

        # ── Knowledge gap: AI replied with uncertainty → notify owner ────────
        # The AI tried to answer but signalled it lacks the specific info.
        # Owner is notified so they can: (a) reply manually to the customer,
        # and (b) save the confirmed answer to the KB so future customers get
        # an instant, confident reply.
        if (
            classification.intent == Intent.BUSINESS
            and not classification.out_of_scope
            and _is_uncertain_reply(reply)
        ):
            _gap_conv = convo or {}
            _gap_last_iso = _gap_conv.get("lastKbGapNotifiedAt")
            _gap_notify = True
            if _gap_last_iso:
                try:
                    from datetime import timedelta as _td_gap
                    _gap_last_dt = datetime.fromisoformat(_gap_last_iso)
                    if datetime.utcnow() - _gap_last_dt < _td_gap(minutes=KB_GAP_NOTIFY_COOLDOWN_MINUTES):
                        _gap_notify = False
                except (ValueError, TypeError):
                    pass

            if _gap_notify:
                _gap_entry: dict | None = None
                try:
                    _gap_entry = kb_service.create_pending_entry(
                        business_id=business_id,
                        question=body,
                        customer_phone=phone_clean,
                        message_id=message_id or "",
                        intent=classification.intent.value,
                    )
                except Exception as _gap_exc:
                    logger.warning("[KB-GAP] capture failed business=%s: %s", business_id, _gap_exc)

                _gap_name = (push_name or "").strip()
                _gap_label = f"{_gap_name} (+{phone_clean})" if _gap_name else f"+{phone_clean}"
                _gap_snippet = body.strip()[:200]
                _gap_reply_snip = reply.strip()[:120]

                _gap_msgs = [
                    f"🤔 *I couldn't fully answer {_gap_label}:*\n\n"
                    f"> {_gap_snippet}\n\n"
                    f"I replied: _\"{_gap_reply_snip}\"_\n\n"
                    f"Reply in their chat manually, then save the right answer 👇"
                     f"\U0001f9e0 *Save YES answer* (copy, edit & send):\n\n"
                ]
                if _gap_entry is not None:
                    _gap_code = _gap_entry["shortCode"]
                    _gap_msgs.append(
                        f"YES {_gap_code} <your answer here>\n"
                        )
                    _gap_msgs.append(
                       f"_Example: YES {_gap_code} Yes, we have chicken biryani!_ _(expires 7 days)_"
                    )
                    _gap_msgs.append(
                        f"❌ *Skip* (copy & send):\n\n"
                    )
                    _gap_msgs.append(
                        f"NO {_gap_code}"
                    )

                import asyncio as _asyncio_gap
                async def _send_gap_notifications(_msgs=_gap_msgs):
                    for _m in _msgs:
                        try:
                            await whatsapp_notifier.send_to_owner(business, _m)
                            await _asyncio_gap.sleep(0.5)
                        except Exception as _e:
                            logger.warning("[KB-GAP] notify part failed: %s", _e)
                _asyncio_gap.create_task(_send_gap_notifications())

                try:
                    db.upsert_customer_conversation(business_id, phone_clean, {
                        "lastKbGapNotifiedAt": datetime.utcnow().isoformat(),
                    })
                except Exception:
                    pass

                try:
                    posthog_client.capture(
                        business_id=business_id,
                        customer_phone=phone_clean,
                        event="ai_kb_gap",
                        properties={
                            "intent": classification.intent.value,
                            "question_len": len(body or ""),
                            "has_kb_entry": _gap_entry is not None,
                            "short_code": (_gap_entry or {}).get("shortCode"),
                        },
                    )
                except Exception:
                    pass

        # Send reply — text or voice note depending on voice_mode
        await self._deliver_reply(reply_to, reply, device_id, voice_mode, _voice_id, _voice_lang)

        logger.info(
            "Customer AI reply sent to %s for business %s (msg=%s, voice=%s)",
            phone_clean, business_id, body[:60], voice_mode,
        )

        # ── Analytics: AI reply sent ─────────────────────────────────────────
        try:
            posthog_client.capture(
                business_id=business_id,
                customer_phone=phone_clean,
                event="ai_reply_sent",
                properties={
                    "voice_mode":   voice_mode,
                    "intent":       classification.intent.value,
                    "reply_len":    len(reply or ""),
                    "detected_language": _lang_signal[0] if _lang_signal else None,
                },
            )
        except Exception:
            pass

        # ── CSAT: mark conversation eligible for the post-chat rating prompt
        try:
            csat_service.mark_ai_reply(business_id, phone_clean)
        except Exception as exc:
            logger.debug("[CSAT] mark_ai_reply failed (non-fatal): %s", exc)

    async def _deliver_reply(
        self,
        phone: str,
        text: str,
        device_id: str,
        voice_mode: bool = False,
        voice_id: str | None = None,
        lang: str = "en",
    ) -> None:
        """Send a reply as a voice note (voice_mode=True) or plain text."""
        if not voice_mode:
            await self._send(phone, text, device_id)
            return
        try:
            audio = await cartesia_client.synthesize(text, voice_id=voice_id, language=lang)
            await self._send_audio(phone, audio, device_id, mime_type=cartesia_client.OUTPUT_MIME_TYPE)
            # Transactional messages (bookings, IDs, etc.) also need a text copy
            # so customers have a reliable record they can reference later.
            if self._is_critical_message(text):
                await self._send(phone, text, device_id)
        except Exception as exc:
            logger.error("TTS failed for %s — falling back to text: %s", phone, exc)
            await self._send(phone, text, device_id)

    async def _pause_and_notify(
        self,
        *,
        business: dict,
        business_id: str,
        phone_clean: str,
        push_name: str,
        history: list[dict],
        reason: PauseReason,
        snippet: str,
    ) -> None:
        """Pause AI for a customer, persist the trigger message, and notify owner.

        Used for safety triggers (frustration, abuse). For owner takeover this
        same pause is performed by owner_takeover_service so we don't duplicate
        notification logic here.
        """
        ai_pause_service.pause(
            business_id=business_id,
            customer_phone=phone_clean,
            reason=reason,
            snippet=snippet,
            business=business,
        )
        db.upsert_customer_conversation(business_id, phone_clean, {
            "messages":      history,
            "customerPhone": phone_clean,
            "customerName":  push_name or "",
            "businessId":    business_id,
            "lastMessageAt": datetime.utcnow().isoformat(),
        })

        label = (
            f"{push_name} (+{phone_clean.lstrip('+')})"
            if push_name else f"+{phone_clean.lstrip('+')}"
        )
        reason_text = {
            PauseReason.ABUSE:       "🚫 The customer used inappropriate language.",
            PauseReason.FRUSTRATION: "⚠️ The customer seems frustrated.",
        }.get(reason, "AI paused.")

        try:
            await whatsapp_notifier.send_to_owner(
                business,
                (
                    f"{reason_text}\n"
                    f"*AI paused* for {label}.\n\n"
                    f"> {snippet[:200]}\n\n"
                    f"Please respond manually. AI resumes automatically in "
                    f"{DEFAULT_PAUSE_MINUTES} minutes.\n\n"
                    f"To resume early, send this to your business WhatsApp number:\n"
                    f"*resume {phone_clean}*"
                ),
            )
        except Exception as exc:
            logger.warning(
                "[AI-PAUSE] could not notify owner of %s business=%s: %s",
                reason.value, business_id, exc,
            )

    async def _get_ai_response(
        self,
        system: str,
        history: list[dict],
        business: dict,
        customer_phone: str,
        push_name: str,
        trace_metadata: dict | None = None,
    ) -> str:
        """Send conversation to Claude with tools and handle tool calls."""
        try:
            _create_kwargs = dict(
                model=self.model,
                max_tokens=1000,
                system=system,
                messages=history,
                tools=CUSTOMER_TOOLS,
            )
            if trace_metadata:
                _create_kwargs["trace_metadata"] = trace_metadata
            response = await self.client.messages.create(**_create_kwargs)

            # Process the response
            # NOTE: pre_tool_text_parts collects any text Claude emits BEFORE the tool call
            # (e.g. "Perfect! Let me book..."). We intentionally DISCARD this when a tool is
            # called, because the final reply must come entirely from the follow-up response
            # that has the actual tool result — preventing "Perfect! ... Sorry, only 3 seats."
            pre_tool_text_parts: list[str] = []
            final_text_parts: list[str] = []
            tool_results = []
            _booking_created_this_turn = False  # guard: only one create_booking per turn

            for block in response.content:
                if block.type == "text":
                    pre_tool_text_parts.append(block.text)
                elif block.type == "tool_use":
                    # Guard: prevent Claude emitting two create_booking blocks in one turn
                    if block.name == "create_booking" and _booking_created_this_turn:
                        logger.warning(
                            "[DUPLICATE-BOOKING-GUARD] Skipping extra create_booking call "
                            "in the same turn for customer=%s", customer_phone,
                        )
                        tool_results.append({
                            "tool_use_id": block.id,
                            "name": block.name,
                            "result": "Booking already created this turn — do not call create_booking again.",
                        })
                        continue
                    # Execute the tool call
                    result = self._execute_tool(
                        block.name, block.input, business, customer_phone, push_name,
                    )
                    if block.name == "create_booking":
                        _booking_created_this_turn = True
                    tool_results.append({
                        "tool_use_id": block.id,
                        "name": block.name,
                        "result": result,
                    })

            # If there were tool calls, send results back to Claude for final response
            history_with_tools: list[dict] | None = None
            if tool_results:
                # Build tool result messages
                history_with_tools = list(history)
                history_with_tools.append({
                    "role": "assistant",
                    "content": response.content,
                })

                tool_result_content = []
                for tr in tool_results:
                    tool_result_content.append({
                        "type": "tool_result",
                        "tool_use_id": tr["tool_use_id"],
                        "content": tr["result"],
                    })

                history_with_tools.append({
                    "role": "user",
                    "content": tool_result_content,
                })

                # Get final response after tool execution.
                # We do NOT include pre_tool_text_parts here — the complete reply
                # must be derived from the tool result only.
                _follow_kwargs = dict(
                    model=self.model,
                    max_tokens=1000,
                    system=system,
                    messages=history_with_tools,
                    tools=CUSTOMER_TOOLS,
                )
                if trace_metadata:
                    _fm = dict(trace_metadata)
                    _fm["name"] = (trace_metadata.get("name") or "customer_ai_reply") + ":after_tools"
                    _fm["called_tools"] = sorted({tr["name"] for tr in tool_results})
                    _follow_kwargs["trace_metadata"] = _fm
                follow_up = await self.client.messages.create(**_follow_kwargs)

                for block in follow_up.content:
                    if block.type == "text":
                        final_text_parts.append(block.text)
            else:
                # No tool calls — use the pre-tool text directly
                final_text_parts = pre_tool_text_parts

            final_reply = "\n".join(final_text_parts).strip() or "I'm here to help! How can I assist you?"
            
            # ── HALLUCINATION GUARD ──────────────────────────────────────────
            # Only trigger if the AI *claims* a specific action was COMPLETED
            # but the corresponding tool was NOT actually called this turn.
            # Conversational gathering phrases (e.g. "What time?") must NEVER trigger.
            
            _reply_lower = final_reply.lower()

            # Map: (action phrases) → (required tools)
            _action_checks = [
                (
                    # Phrases that ONLY make sense when a NEW booking was just created
                    [
                        "your table has been booked",
                        "reservation successfully created", "booking successfully created",
                        "successfully booked", "i've booked", "i have booked",
                    ],
                    {"create_booking"},
                ),
                (
                    # Phrases that describe booking status — valid after ANY booking
                    # action tool (create, check, reschedule, cancel, update).
                    # "booking id:" appears in reschedule/cancel confirmations too.
                    [
                        "booking confirmed", "reservation confirmed",
                        "your table is confirmed",
                        "booking is confirmed", "reservation is confirmed",
                        "your booking id", "booking id:", "booking id =",
                    ],
                    {"create_booking", "check_booking", "reschedule_booking", "cancel_booking", "update_booking"},
                ),
                (
                    # Booking cancelled
                    [
                        "booking has been cancelled", "reservation cancelled",
                        "cancellation successful", "successfully cancelled",
                        "your booking is cancelled", "booking is canceled",
                        "reservation has been cancelled",
                    ],
                    {"cancel_booking"},
                ),
                (
                    # Booking rescheduled / updated
                    [
                        "booking has been rescheduled", "reservation rescheduled",
                        "appointment has been updated", "booking moved to",
                        "reservation moved to", "successfully rescheduled",
                        "rescheduled to", "updated your booking",
                    ],
                    {"reschedule_booking", "update_booking"},
                ),
                (
                    # Booking lookup / retrieval presented as fact
                    [
                        "here are your booking details", "your reservation is scheduled for",
                        "i found your booking", "found your reservation",
                        "your booking is scheduled",
                    ],
                    {"check_booking", "create_booking", "reschedule_booking", "cancel_booking"},
                ),
            ]

            called_tools = {tr["name"] for tr in tool_results}
            hallucination_detected = False
            for claim_phrases, required_tools in _action_checks:
                if any(phrase in _reply_lower for phrase in claim_phrases):
                    if not called_tools & required_tools:
                        hallucination_detected = True
                        logger.error(
                            "[HALLUCINATION-DETECTED] Customer %s business %s: "
                            "AI claimed completed action without calling required tool(s) %s. "
                            "Called tools: %s. Blocked response: %s",
                            customer_phone, business["id"], required_tools, called_tools,
                            final_reply[:200],
                        )
                        break

            if hallucination_detected:
                # Retry: force the AI to actually call the required tool using full conversation context.
                # This preserves state (service, date, time, party) so the customer does not repeat themselves.
                final_reply = await self._force_tool_retry(
                    system=system,
                    history=history,
                    business=business,
                    customer_phone=customer_phone,
                    push_name=push_name,
                )

            # ── BOOKING ID CORRECTION ────────────────────────────────────────
            # The LLM sometimes garbles the booking ID when composing the
            # confirmation message (e.g. BK4B84D2 → BK484D2).  After any
            # create_booking / reschedule_booking call, extract the authoritative
            # ID from the tool result and patch any BK-pattern in the reply.
            _bk_pattern = re.compile(r"\bBK[A-Z0-9]{4,10}\b")
            _true_ids: list[str] = []
            for _tr in tool_results:
                if _tr["name"] in ("create_booking", "reschedule_booking"):
                    _m = _bk_pattern.search(_tr["result"] or "")
                    if _m:
                        _true_ids.append(_m.group())

            if _true_ids:
                _true_id = _true_ids[0]
                _reply_ids = _bk_pattern.findall(final_reply)
                if _reply_ids and _reply_ids[0] != _true_id:
                    logger.warning(
                        "[BOOKING-ID-CORRECTION] customer=%s: reply had %s, correcting to %s",
                        customer_phone, _reply_ids[0], _true_id,
                    )
                    final_reply = _bk_pattern.sub(_true_id, final_reply)

            try:
                logger.debug("AI (customer) generated reply: %s", final_reply)
            except Exception:
                logger.exception("AI (customer) generated reply (logging failed)")
            return final_reply

        except Exception as exc:
            logger.exception("Customer AI error: %s", exc)
            return (
                "Sorry, I'm having a small technical issue. "
                "Please try again in a moment!"
            )

    async def _force_tool_retry(
        self,
        system: str,
        history: list[dict],
        business: dict,
        customer_phone: str,
        push_name: str,
    ) -> str:
        """Called when hallucination guard fires. Forces Claude to actually call the booking
        tool instead of fabricating a confirmation. Preserves full conversation context so
        the customer does not have to repeat service/date/time/party.
        """
        override_system = (
            system
            + "\n\n"
            "⚠️ SYSTEM OVERRIDE — READ THIS BEFORE RESPONDING:\n"
            "You previously attempted to confirm a booking without calling the create_booking tool. "
            "That response was BLOCKED. You must now call create_booking with the details already "
            "established in this conversation. Do NOT write any confirmation text — call the tool "
            "first, then respond based on its result. If the booking details are unclear, ask ONE "
            "specific clarifying question (do NOT ask for everything again)."
        )
        try:
            retry_response = await self.client.messages.create(
                model=self.model,
                max_tokens=800,
                system=override_system,
                messages=history,
                tools=CUSTOMER_TOOLS,
                tool_choice={"type": "any"},  # Force a tool call
            )

            retry_text_parts: list[str] = []
            retry_tool_results = []
            for block in retry_response.content:
                if block.type == "tool_use":
                    result = self._execute_tool(block.name, block.input, business, customer_phone, push_name)
                    retry_tool_results.append({
                        "tool_use_id": block.id,
                        "name": block.name,
                        "result": result,
                    })

            if retry_tool_results:
                retry_history = list(history)
                retry_history.append({"role": "assistant", "content": retry_response.content})
                retry_history.append({
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tr["tool_use_id"], "content": tr["result"]}
                        for tr in retry_tool_results
                    ],
                })
                final_follow = await self.client.messages.create(
                    model=self.model,
                    max_tokens=500,
                    system=system,
                    messages=retry_history,
                    tools=CUSTOMER_TOOLS,
                )
                for block in final_follow.content:
                    if block.type == "text":
                        retry_text_parts.append(block.text)
            else:
                for block in retry_response.content:
                    if block.type == "text":
                        retry_text_parts.append(block.text)

            result_text = "\n".join(retry_text_parts).strip()
            if result_text:
                logger.info("[HALLUCINATION-RETRY] Recovered context for customer=%s", customer_phone)
                return result_text
        except Exception as retry_exc:
            logger.error("[HALLUCINATION-RETRY] Retry failed for customer=%s: %s", customer_phone, retry_exc)

        # Final fallback: minimal context-preserving message
        return (
            "I need to verify the booking details with the system. "
            "Could you confirm the time you'd like? (with AM/PM please)"
        )

    def _execute_tool(
        self,
        tool_name: str,
        tool_input: dict,
        business: dict,
        customer_phone: str,
        push_name: str,
    ) -> str:
        """Execute a tool call using vapi_service functions (centralized logic)."""
        business_id = business["id"]
        call_info = {"phoneNumberId": "", "customer": {"number": customer_phone}}
        logger.info(
            "[TOOL_CALL] tool=%s biz=%s phone=%s input=%s",
            tool_name, business_id, customer_phone, json.dumps(tool_input)[:300],
        )

        try:
            if tool_name == "create_booking":
                args = {
                    "businessId": business_id,
                    "customerPhone": customer_phone,
                    "customerName": tool_input.get("customerName") or push_name or "Customer",
                    "serviceName": tool_input.get("serviceName", "Appointment"),
                    "dateTime": tool_input.get("dateTime", ""),
                    "durationMinutes": tool_input.get("durationMinutes", 60),
                    "partySize": tool_input.get("partySize", 1),
                    "specialRequests": tool_input.get("specialRequests", ""),
                    "source": "whatsapp",
                }
                result = vapi_service.tool_create_booking(args, call_info)
                if "booking_id=" in result or "capacity_ok" in result:
                    try:
                        posthog_client.capture(
                            business_id=business_id,
                            customer_phone=customer_phone,
                            event="booking_created",
                            properties={
                                "service": args.get("serviceName"),
                                "date_time": args.get("dateTime"),
                                "party_size": args.get("partySize", 1),
                                "source": "whatsapp",
                            },
                        )
                    except Exception:
                        pass
                return result

            elif tool_name == "get_available_slots":
                args = {
                    "businessId": business_id,
                    "date": tool_input.get("date", ""),
                    "durationMinutes": tool_input.get("durationMinutes", 60),
                    "partySize": tool_input.get("partySize", 1),
                }
                payload = vapi_service.get_available_slots_payload(args, call_info)
                if payload.get("error"):
                    return f"Error: {payload['error']}"
                slots = payload.get("slots", [])
                if not slots:
                    return f"No available slots on {tool_input.get('date', 'the requested date')}."
                readable = []
                for s in slots[:8]:
                    try:
                        readable.append(
                            datetime.fromisoformat(s).strftime("%I:%M %p").lstrip("0")
                        )
                    except ValueError:
                        readable.append(s)
                return f"Available slots on {payload.get('date', '')}: {', '.join(readable)}"

            elif tool_name == "check_booking":
                args = {
                    "businessId": business_id,
                    "customerPhone": customer_phone,
                    "date": tool_input.get("date", ""),
                    "bookingId": tool_input.get("bookingId", ""),
                }
                payload = vapi_service.check_booking_payload(args, call_info)
                if payload.get("error"):
                    return f"Error: {payload['error']}"
                all_bookings = payload.get("bookings", [])
                active = [b for b in all_bookings if (b.get("status") or "").lower() != "cancelled"]
                if not active:
                    return "No active bookings found for this customer."

                # Firestore stores booking datetimes in UTC (e.g. "2026-06-19T11:30:00+00:00").
                # We must convert to the business's local timezone before formatting,
                # otherwise the AI tells customers the wrong time (e.g. shows 11:30 AM
                # when the booking is actually at 5:00 PM IST).
                biz_tz = None
                try:
                    import pytz
                    biz_tz = pytz.timezone(business.get("timezone") or "UTC")
                except Exception:
                    biz_tz = None

                result = []
                for b in active:
                    dt_raw = b.get("datetime") or b.get("dateTime") or ""
                    dt_fmt = dt_raw
                    if dt_raw:
                        try:
                            dt_obj = datetime.fromisoformat(str(dt_raw))
                            if biz_tz is not None:
                                if dt_obj.tzinfo is None:
                                    # Naive datetime — assume UTC (matches Firestore convention)
                                    import pytz as _pytz
                                    dt_obj = _pytz.utc.localize(dt_obj)
                                dt_obj = dt_obj.astimezone(biz_tz)
                            dt_fmt = dt_obj.strftime("%Y-%m-%d %I:%M %p")
                        except (ValueError, TypeError):
                            dt_fmt = dt_raw
                    result.append({
                        "bookingId": b.get("id"),
                        "service": b.get("serviceName"),
                        "dateTime": dt_fmt,
                        "status": b.get("status"),
                    })
                logger.info("[TOOL_RESULT] check_booking returned %d active bookings", len(result))
                return json.dumps(result)

            elif tool_name == "cancel_booking":
                booking_id = (tool_input.get("bookingId") or "").strip()
                # Auto-lookup: find the booking by service name when ID is not provided.
                if not booking_id:
                    booking_id = self._resolve_booking_id(
                        business_id=business_id,
                        customer_phone=customer_phone,
                        service_hint=(tool_input.get("serviceName") or "").strip(),
                        time_hint=(tool_input.get("currentDateTime") or "").strip(),
                        call_info=call_info,
                    )
                    if booking_id.startswith("Error:") or booking_id.startswith("Ambiguous:"):
                        logger.warning("[TOOL_RESULT] cancel_booking lookup failed: %s", booking_id)
                        return booking_id
                args = {
                    "businessId": business_id,
                    "bookingId": booking_id,
                    "customerPhone": customer_phone,
                }
                payload = vapi_service.cancel_booking_payload(args, call_info, skip_whatsapp=True)
                if payload.get("error"):
                    error_msg = payload["error"]
                    if "not found" in error_msg.lower() and booking_id:
                        logger.warning(
                            "[TOOL_RESULT] cancel_booking ID %s not found — falling back to phone lookup for customer=%s",
                            booking_id, customer_phone,
                        )
                        fallback_id = self._resolve_booking_id(
                            business_id=business_id,
                            customer_phone=customer_phone,
                            service_hint=(tool_input.get("serviceName") or "").strip(),
                            time_hint=(tool_input.get("currentDateTime") or "").strip(),
                            call_info=call_info,
                        )
                        if not (fallback_id.startswith("Error:") or fallback_id.startswith("Ambiguous:")):
                            logger.info(
                                "[TOOL_RESULT] cancel_booking fallback resolved %s → %s",
                                booking_id, fallback_id,
                            )
                            args["bookingId"] = fallback_id
                            booking_id = fallback_id
                            payload = vapi_service.cancel_booking_payload(args, call_info, skip_whatsapp=True)
                    if payload.get("error"):
                        logger.warning("[TOOL_RESULT] cancel_booking error: %s", payload['error'])
                        return f"Error: {payload['error']}"
                logger.info("[TOOL_RESULT] cancel_booking OK bookingId=%s", booking_id)
                try:
                    posthog_client.capture(
                        business_id=business_id,
                        customer_phone=customer_phone,
                        event="booking_cancelled",
                        properties={"booking_id": booking_id, "source": "whatsapp"},
                    )
                except Exception:
                    pass
                try:
                    import asyncio as _asyncio
                    from app.services.automation.booking_automation import send_cancellation_notice
                    from app.services.automation.whatsapp_notifier import send_to_owner
                    cancelled_booking = payload.get("booking") or {"customerPhone": customer_phone, "id": booking_id}
                    _cust_name = cancelled_booking.get("customerName") or push_name or "Unknown"
                    _party = cancelled_booking.get("partySize") or 1
                    try:
                        _party = int(_party)
                    except (TypeError, ValueError):
                        _party = 1
                    _asyncio.ensure_future(send_cancellation_notice(cancelled_booking, business))
                    _asyncio.ensure_future(send_to_owner(
                        business,
                        f"❌ *Booking cancelled*\n\n"
                        f"👤 {_cust_name}\n"
                        f"📞 +{str(customer_phone).lstrip('+')}\n"
                        f"✂️ {cancelled_booking.get('serviceName', '')}\n"
                        f"👥 {_party} {'person' if _party == 1 else 'people'}\n"
                        f"🆔 {booking_id}",
                    ))
                except Exception as _auto_err:
                    logger.warning("Cancellation notification skipped: %s", _auto_err)
                return "Booking cancelled successfully."

            elif tool_name == "reschedule_booking":
                booking_id = (tool_input.get("bookingId") or "").strip()
                # Auto-lookup: find the booking by service name when ID is not provided.
                if not booking_id:
                    booking_id = self._resolve_booking_id(
                        business_id=business_id,
                        customer_phone=customer_phone,
                        service_hint=(tool_input.get("serviceName") or "").strip(),
                        time_hint=(tool_input.get("currentDateTime") or "").strip(),
                        call_info=call_info,
                    )
                    if booking_id.startswith("Error:") or booking_id.startswith("Ambiguous:"):
                        logger.warning("[TOOL_RESULT] reschedule_booking lookup failed: %s", booking_id)
                        return booking_id
                args = {
                    "businessId": business_id,
                    "bookingId": booking_id,
                    "rescheduleDateTime": tool_input.get("newDateTime", ""),
                    "customerPhone": customer_phone,
                }
                payload = vapi_service.reschedule_booking_payload(args, call_info)
                if payload.get("error"):
                    # If the provided ID was not found, fall back to phone-based lookup.
                    # This handles cases where the AI carries a garbled ID from history.
                    error_msg = payload["error"]
                    if "not found" in error_msg.lower() and booking_id:
                        logger.warning(
                            "[TOOL_RESULT] reschedule_booking ID %s not found — falling back to phone lookup for customer=%s",
                            booking_id, customer_phone,
                        )
                        fallback_id = self._resolve_booking_id(
                            business_id=business_id,
                            customer_phone=customer_phone,
                            service_hint=(tool_input.get("serviceName") or "").strip(),
                            time_hint=(tool_input.get("currentDateTime") or "").strip(),
                            call_info=call_info,
                        )
                        if not (fallback_id.startswith("Error:") or fallback_id.startswith("Ambiguous:")):
                            logger.info(
                                "[TOOL_RESULT] reschedule_booking fallback resolved %s → %s",
                                booking_id, fallback_id,
                            )
                            booking_id = fallback_id  # keep local var in sync for notifications/result
                            args["bookingId"] = booking_id
                            payload = vapi_service.reschedule_booking_payload(args, call_info)
                    if payload.get("error"):
                        logger.warning("[TOOL_RESULT] reschedule_booking error: %s", payload['error'])
                        return f"Error: {payload['error']}"
                logger.info("[TOOL_RESULT] reschedule_booking OK bookingId=%s newDT=%s", booking_id, tool_input.get('newDateTime'))
                try:
                    posthog_client.capture(
                        business_id=business_id,
                        customer_phone=customer_phone,
                        event="booking_rescheduled",
                        properties={
                            "booking_id": booking_id,
                            "new_date_time": tool_input.get("newDateTime"),
                            "source": "whatsapp",
                        },
                    )
                except Exception:
                    pass
                updated_bk = payload.get("booking") or {}
                try:
                    import asyncio as _asyncio
                    from app.services.automation.whatsapp_notifier import send_to_owner
                    new_dt_str = tool_input.get("newDateTime", "")
                    try:
                        new_dt_fmt = datetime.fromisoformat(new_dt_str).strftime("%B %d, %Y at %I:%M %p") if new_dt_str else new_dt_str
                    except ValueError:
                        new_dt_fmt = new_dt_str
                    _rs_name = updated_bk.get("customerName") or push_name or "Unknown"
                    _rs_party = updated_bk.get("partySize") or 1
                    try:
                        _rs_party = int(_rs_party)
                    except (TypeError, ValueError):
                        _rs_party = 1
                    _asyncio.ensure_future(send_to_owner(
                        business,
                        f"🔄 *Booking rescheduled*\n\n"
                        f"👤 {_rs_name}\n"
                        f"📞 +{str(customer_phone).lstrip('+')}\n"
                        f"✂️ {updated_bk.get('serviceName', '')}\n"
                        f"👥 {_rs_party} {'person' if _rs_party == 1 else 'people'}\n"
                        f"🗓 New time: {new_dt_fmt}\n"
                        f"🆔 {booking_id}",
                    ))
                except Exception as _notify_err:
                    logger.warning("Reschedule owner notification skipped: %s", _notify_err)
                return f"Booking rescheduled successfully. booking_id={booking_id}"

            elif tool_name == "update_booking":
                args: dict[str, Any] = {
                    "businessId": business_id,
                    "bookingId": tool_input.get("bookingId", ""),
                    "customerPhone": customer_phone,
                }
                if tool_input.get("partySize") is not None:
                    args["partySize"] = tool_input["partySize"]
                if tool_input.get("specialRequests") is not None:
                    args["specialRequests"] = tool_input["specialRequests"]
                if tool_input.get("notes") is not None:
                    args["notes"] = tool_input["notes"]
                payload = vapi_service.update_booking_payload(args, call_info)
                if payload.get("error"):
                    return f"Error: {payload['error']}"
                return "Booking updated successfully."

            else:
                return f"Unknown tool: {tool_name}"

        except Exception as exc:
            logger.exception("Tool execution error (%s): %s", tool_name, exc)
            return f"Error executing {tool_name}: {str(exc)}"

    def _resolve_booking_id(
        self,
        business_id: str,
        customer_phone: str,
        service_hint: str,
        time_hint: str,
        call_info: dict,
    ) -> str:
        """Find the correct bookingId for a customer's active booking.

        Searches the customer's active (non-cancelled) bookings and returns the
        single matching booking's ID.  Returns an Error/Ambiguous string if the
        lookup fails so the caller can surface it to Claude.
        """
        lookup_payload = vapi_service.check_booking_payload(
            {"businessId": business_id, "customerPhone": customer_phone}, call_info
        )
        all_bkgs = lookup_payload.get("bookings", [])
        active = [b for b in all_bkgs if (b.get("status") or "").lower() != "cancelled"]

        if not active:
            return "Error: No active bookings found for this customer."

        if len(active) == 1:
            return active[0].get("id", "")

        # Multiple active bookings — try to narrow down.
        if service_hint:
            hint_lower = service_hint.lower()
            matched = [b for b in active if hint_lower in (b.get("serviceName") or "").lower()]
            if len(matched) == 1:
                return matched[0].get("id", "")
            if len(matched) > 1 and time_hint:
                # Further filter by time hint (date or hour substring)
                time_filter = time_hint[:16]  # "YYYY-MM-DDTHH:MM"
                for b in matched:
                    if time_filter in (b.get("datetime") or ""):
                        return b.get("id", "")
            if matched:
                # Best effort: return first match
                return matched[0].get("id", "")

        # Could not narrow down — return ambiguous error so Claude asks the customer
        services_list = ", ".join(
            f"{b.get('serviceName')} at {b.get('datetime', '')[:16]}"
            for b in active
        )
        return f"Ambiguous: multiple active bookings found ({services_list}). Please ask the customer which booking they mean."

    def _is_critical_message(self, reply_text: str) -> bool:
        """Return True when a reply looks transactional/critical (bookings,
        reschedules, cancellations, dates/times, payments, booking IDs).
        This heuristic helps decide when to send a text record alongside audio.
        """
        if not reply_text:
            return False
        text = reply_text.lower()
        keywords = [
            "booking confirmed",
            "your booking",
            "booking id",
            "booking #",
            "appointment",
            "rescheduled",
            "reschedule",
            "cancelled",
            "canceled",
            "confirmation",
            "slot",
            "date",
            "time",
            "payment",
            "paid",
            "invoice",
        ]
        for k in keywords:
            if k in text:
                return True
        # Match ISO dates like 2026-05-05
        if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
            return True
        # Match times like 5pm, 5 pm, 17:30
        if re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", text) or re.search(r"\b\d{2}:\d{2}\b", text):
            return True
        return False

    async def handle_audio_message(
        self,
        business: dict,
        customer_phone: str,
        media_url: str,
        mime_type: str,
        push_name: str,
        device_id: str,
        customer_jid: str | None = None,
    ) -> None:
        """Process an incoming WhatsApp voice note and reply with a voice note.

        ``customer_jid`` is the full JID of the sender (preserves ``@lid`` etc.).

        Pipeline:
          1. Download audio bytes from the bridge/CDN URL
          2. Transcribe with Deepgram (STT)
          3. Delegate to handle_customer_message(voice_mode=True) so the full
             business logic runs: intent classification, KB lookup, pause gates,
             booking/cancel/reschedule tools, owner notifications, etc.
          The voice_mode flag causes all replies to be delivered as Cartesia TTS
          voice notes (with text fallback on TTS failure, and a text copy for
          transactional messages like booking confirmations).
        """
        phone_clean = db._clean_phone(customer_phone)
        reply_to = customer_jid or phone_clean

        # ── Step 1: Download ──────────────────────────────────────────────────
        try:
            audio_bytes, detected_mime = await self.wa.download_media(media_url)
            effective_mime = detected_mime if detected_mime not in ("", "application/octet-stream") else mime_type
            logger.info(
                "[AUDIO] Downloaded %d bytes from %s for %s (mime=%r → effective=%r)",
                len(audio_bytes), media_url[:60], phone_clean, detected_mime, effective_mime,
            )
            if len(audio_bytes) < 100:
                raise ValueError(f"Downloaded audio too small ({len(audio_bytes)} bytes) — possible error response")
        except Exception as exc:
            logger.error("Failed to download audio for %s (url=%s): %s", phone_clean, media_url, exc)
            await self._send(
                reply_to,
                "Sorry, I couldn't process your voice message. "
                "Please try again or send a text. 🙏",
                device_id,
            )
            return

        # ── Step 2: Transcribe with Deepgram STT ─────────────────────────────
        try:
            transcript = await deepgram_client.transcribe_audio(audio_bytes, effective_mime)
            logger.info("[AUDIO] Transcript for %s: %r", phone_clean, transcript[:120])
        except Exception as exc:
            logger.error("Deepgram transcription failed for %s: %s", phone_clean, exc)
            await self._send(
                reply_to,
                "Sorry, I couldn't understand your voice message. "
                "Please try again or send a text instead. 🙏",
                device_id,
            )
            return

        if not transcript:
            logger.warning("Empty transcript for %s — audio contained no speech", phone_clean)
            await self._send(
                reply_to,
                "I couldn't make out what you said. "
                "Could you please repeat or send a text message? 😊",
                device_id,
            )
            return

        # ── Step 3: Delegate to the full text pipeline (voice_mode=True) ────
        # Prefix body with [Voice message] so conversation history makes clear
        # this turn came from a voice note, not text.
        voice_body = f"[Voice message]: {transcript}"
        await self.handle_customer_message(
            business=business,
            customer_phone=customer_phone,
            body=voice_body,
            push_name=push_name,
            device_id=device_id,
            customer_jid=customer_jid,
            message_id="",
            voice_mode=True,
        )

    async def _send_audio(
        self,
        phone: str,
        audio_bytes: bytes,
        device_id: str,
        mime_type: str = "audio/mpeg",
    ) -> None:
        """Send an audio message via the WhatsApp bridge."""
        try:
            logger.debug(
                "Sending WA audio (customer AI) to %s (device=%s, %d bytes, mime=%s)",
                phone, device_id, len(audio_bytes), mime_type,
            )
            await self.wa.send_audio(
                phone, audio_bytes, device_id=device_id, mime_type=mime_type, ptt=True
            )
        except Exception as exc:
            logger.error("Failed to send audio reply to %s: %s", phone, exc)
            raise

    async def _send(self, phone: str, message: str, device_id: str) -> None:
        """Send a WhatsApp message via the bridge.
        
        If the primary device (business-specific) fails, fall back to the global
        onboarding device so customer messages are never lost.
        """
        try:
            logger.info("[SEND] → %s (device=%s)", phone, device_id)
            await self.wa.send_message(phone, message, device_id=device_id)
            logger.info("[SEND] ✓ delivered to %s (device=%s)", phone, device_id)
        except Exception as exc:
            # Business device not ready — try fallback to global onboarding device
            logger.warning(
                "[SEND] device %s failed for %s (%s) — attempting fallback to global device",
                device_id, phone, exc,
            )
            try:
                fallback_device = self.wa.default_device_id
                if fallback_device != device_id:
                    logger.info("[SEND-FALLBACK] → %s (device=%s)", phone, fallback_device)
                    await self.wa.send_message(phone, message, device_id=fallback_device)
                    logger.info(
                        "[SEND-FALLBACK] ✓ delivered to %s via fallback device %s",
                        phone, fallback_device,
                    )
                    return
            except Exception as fallback_exc:
                logger.error(
                    "[SEND-FALLBACK] ✗ also failed for %s: %s",
                    phone, fallback_exc,
                )
            # Both primary and fallback failed
            logger.error("[SEND] ✗ message delivery failed for %s (primary: %s, fallback tried)", phone, exc)
