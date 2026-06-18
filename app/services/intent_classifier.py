"""Intent Classifier — fast, single-pass business/personal triage.

Replaces the brittle [SILENT_IGNORE] prompt injection pattern with a
structured, auditable classification step that runs BEFORE the main
conversational AI.

Returns one of four intents:
  BUSINESS  — message is clearly related to the business (score > 70)
  PERSONAL  — message is clearly personal / social
  GREETING  — a bare greeting with no business context ("Hi", "Hello")
  MIXED     — contains both personal and business content

Plus three independent safety flags (booleans):
  frustrated   — customer is upset, complaining, demanding escalation
  abusive      — message uses insults, slurs, threats, sexual harassment
  out_of_scope — business-related but not booking/reschedule/cancellation/FAQ
                 (e.g. post-service advice, technical product questions)

`out_of_scope` lets us silence things the AI shouldn't answer ("yesterday
I dyed my hair and it got washed, what should I do?") without misclassifying
them as PERSONAL.

The classifier uses gpt-4o-mini with response_format=json_object so the
output is guaranteed to be parseable JSON, never a free-form string.

Usage:
    result = await classify_intent(message, business_name, business_type, history)
    if result.intent == Intent.BUSINESS or result.intent == Intent.MIXED:
        # wake up the AI
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum

from app.config import settings
from app.integrations.langfuse_client import get_async_openai

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
# Minimum confidence score (0-100) for a message to be treated as business.
# Messages below this threshold are silently ignored.
BUSINESS_THRESHOLD = 70

# How many recent conversation turns to include as context for the classifier.
# Enough to detect "now asking about business" after earlier personal messages.
CLASSIFIER_HISTORY_WINDOW = 20


class Intent(str, Enum):
    BUSINESS = "BUSINESS"   # clearly business — wake AI
    PERSONAL = "PERSONAL"   # clearly personal — stay silent
    GREETING = "GREETING"   # bare greeting — stay silent (customer will follow up)
    MIXED    = "MIXED"      # both present — wake AI, ignore personal part


@dataclass
class ClassifierResult:
    intent: Intent
    score: int            # 0-100 confidence that message is business-related
    reason: str           # short human-readable explanation (for logs/debug)
    frustrated: bool = False
    abusive: bool = False
    out_of_scope: bool = False


_SYSTEM_PROMPT = """\
You are an intent classifier for a WhatsApp AI receptionist.

The receptionist ONLY handles four topics:
  1. Booking / appointments / reservations
  2. Rescheduling existing bookings
  3. Cancelling existing bookings
  4. FAQs about the business (services, prices, hours, availability, location)

Anything else — even if business-related — is OUT OF SCOPE and the human
owner must handle it. Examples of out-of-scope business messages:
  - "Yesterday I dyed my hair and it got washed, what should I do?"
  - "The product I bought is faulty, what's the return process?"
  - "Can you give me advice on aftercare?"
  - "Is this safe to use on sensitive skin?"
  - Detailed complaints that need human judgment.

Your job: classify the message AND flag safety signals.

INTENTS:
  BUSINESS  — In-scope booking/reschedule/cancel/FAQ request.
  PERSONAL  — Social, emotional, or off-business chat (jokes, family, etc.).
  GREETING  — A bare greeting with no business content ("Hi", "Hello").
  MIXED     — In-scope request wrapped inside friendly chat.

SOCIAL INVITATIONS vs TRANSACTIONS: If the sender is asking the owner to hang \
out socially (e.g., "let's grab dinner bro", "want to get drinks?"), this is \
PERSONAL. Only classify as BUSINESS if they are acting as a customer asking \
for services/reservations at the establishment.

PERSONAL-CHAT CONTINUITY — VERY IMPORTANT (read carefully):
Many SMB owners use ONE WhatsApp number for both personal and business
chats. The recent conversation context is the strongest signal of which
mode the current chat is in.
- If the recent history is clearly personal/family/social ("let's have
  dinner today", "love you", emojis only, casual chitchat) and the new
  message is a short business-shaped follow-up like "should I book?",
  "can I book the table today?", "make the booking?", "shall we?" —
  classify as MIXED (the personal-chat partner is just continuing the
  banter, NOT acting as a customer). Set score 50–70.
- A message is only BUSINESS when the sender is clearly behaving like a
  customer: gives a name, party size, time, service, booking ID, asks
  about hours / prices, complains about a real visit, etc.
- When the sender is the owner's family/friend continuing a social
  thread, NEVER classify as BUSINESS — prefer MIXED or PERSONAL.
- If in doubt, MIXED beats BUSINESS. The AI will stay silent on MIXED
  and the owner can intervene manually.

FLAGS (independent of intent — set true when applicable):
  frustrated   — RARE. Only set when the customer is clearly *angry* and
                 escalating: shouting in caps, repeating angrily, threatening
                 a refund/complaint, saying "this is unacceptable", "worst
                 service ever", "I'm so done", etc. Setting this flag
                 SILENCES the AI, so be conservative.
                 Do NOT set frustrated for:
                   • Impatience ("what's going on?", "still waiting", "can
                     someone reply please")
                   • Asking the same question again ("I asked for the menu")
                   • A short curt message ("hello??", "anyone there?")
                   • Mild dissatisfaction inside a normal request
                 These are normal customer behaviour; the AI should still
                 try to help.
  abusive      — Message contains insults, profanity directed at the business
                 or staff, slurs, threats, or sexual harassment.
  out_of_scope — Message IS business-related but NOT booking/reschedule/cancel/FAQ.
                 (Post-service advice, technical product questions, detailed
                 complaints needing human judgment. Do not set this for PERSONAL
                 or GREETING — they have their own intents.)

Rules:
- Respond ONLY with valid JSON. No markdown, no explanation outside JSON.
- Schema (every field is required, booleans must be true or false):
  {"intent": "BUSINESS|PERSONAL|GREETING|MIXED", "score": 0-100,
   "reason": "one sentence",
   "frustrated": false, "abusive": false, "out_of_scope": false}
- "score" is your confidence (0-100) that the message is in-scope business.
  BUSINESS → 70-100
  PERSONAL → 0-30
  GREETING → 0-40
  MIXED    → 50-80
- Be strict: if in doubt about a vague message, prefer PERSONAL or GREETING.
- Context (recent chat history) is provided to help judge continuity — a
  short reply like "12pm" after the assistant asked "what time?" is BUSINESS.

EXAMPLES:

User: "Can we have dinner today bro"
Output: {"intent": "PERSONAL", "score": 10, "reason": "Social invitation from a friend",
         "frustrated": false, "abusive": false, "out_of_scope": false}

User: "Can we have dinner today for 2 people"
Output: {"intent": "BUSINESS", "score": 90, "reason": "Customer requesting a reservation",
         "frustrated": false, "abusive": false, "out_of_scope": false}

User: "Hey! Can I get the menu please?"
Output: {"intent": "BUSINESS", "score": 95, "reason": "Customer inquiring about services",
         "frustrated": false, "abusive": false, "out_of_scope": false}

User: "12pm"   (assistant just asked "what time?")
Output: {"intent": "BUSINESS", "score": 90, "reason": "Time reply continuing a booking flow",
         "frustrated": false, "abusive": false, "out_of_scope": false}

User: "Yesterday I dyed my hair and now it got washed out, what should I do?"
Output: {"intent": "BUSINESS", "score": 60, "reason": "Post-service complaint needing human advice",
         "frustrated": false, "abusive": false, "out_of_scope": true}

User: "This is the worst service ever, I want my money back NOW!!"
Output: {"intent": "BUSINESS", "score": 70, "reason": "Angry refund demand",
         "frustrated": true, "abusive": false, "out_of_scope": true}

User: "Whats happening i am asking for menu"
Output: {"intent": "BUSINESS", "score": 70, "reason": "Customer chasing for the menu — impatient but not angry",
         "frustrated": false, "abusive": false, "out_of_scope": true}

User: "hello?? anyone there?"
Output: {"intent": "GREETING", "score": 20, "reason": "Customer checking if anyone is online — not angry",
         "frustrated": false, "abusive": false, "out_of_scope": false}

User: "you f***ing idiots ruined my hair"
Output: {"intent": "BUSINESS", "score": 65, "reason": "Abusive complaint",
         "frustrated": true, "abusive": true, "out_of_scope": true}

Recent context:
  USER: Let's have a dinner today
  ASSISTANT: (silent — owner replied manually)
User: "Should i make the bookings ?"
Output: {"intent": "MIXED", "score": 55,
         "reason": "Personal-chat partner joking about booking — not a real customer request",
         "frustrated": false, "abusive": false, "out_of_scope": false}

Recent context:
  USER: Bro can we have dinner today
  USER: Should we book a table or just walk in
User: "Can i book the table today ?"
Output: {"intent": "MIXED", "score": 60,
         "reason": "Continuation of a personal dinner-plan conversation, not a customer booking request",
         "frustrated": false, "abusive": false, "out_of_scope": false}

Recent context:
  USER: Hi, what time are you open today?
  ASSISTANT: We're open 9am to 9pm.
User: "Can i book the table today ?"
Output: {"intent": "BUSINESS", "score": 95,
         "reason": "Genuine booking request continuing a customer enquiry",
         "frustrated": false, "abusive": false, "out_of_scope": false}
"""


async def classify_intent(
    message: str,
    business_name: str,
    business_type: str,
    recent_history: list[dict] | None = None,
    *,
    trace_metadata: dict | None = None,
) -> ClassifierResult:
    """Classify whether *message* is business-related for the given business.

    Args:
        message: The raw incoming WhatsApp text to classify.
        business_name: Name of the business (e.g. "Glamour Salon").
        business_type: Type of business (e.g. "hair salon", "restaurant").
        recent_history: Last N conversation turns [{"role": "user"|"assistant", "content": "..."}].

    Returns:
        ClassifierResult with intent, score, and reason.
        Defaults to PERSONAL on any error to ensure silent fail-safe.
    """
    client = get_async_openai(api_key=settings.OPENAI_API_KEY)
    _is_traced = client.__class__.__module__.startswith("langfuse")

    # Build context snippet from recent history (last CLASSIFIER_HISTORY_WINDOW turns)
    history_snippet = ""
    if recent_history:
        window = recent_history[-CLASSIFIER_HISTORY_WINDOW:]
        lines = []
        for turn in window:
            role = turn.get("role", "user")
            content = str(turn.get("content", ""))[:200]
            lines.append(f"{role.upper()}: {content}")
        history_snippet = "\n".join(lines)

    user_content = (
        f"Business name: {business_name}\n"
        f"Business type: {business_type}\n"
    )
    if history_snippet:
        user_content += f"\nRecent conversation context:\n{history_snippet}\n"

    user_content += f"\nNew message to classify:\n{message}"

    try:
        start = time.monotonic()
        _call_kwargs = dict(
            model="gpt-4o-mini",
            max_tokens=100,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_content},
            ],
        )
        if _is_traced and trace_metadata:
            _call_kwargs["name"] = "intent_classifier"
            _call_kwargs["metadata"] = {
                k: v for k, v in trace_metadata.items()
                if k not in {"session_id", "user_id", "tags"}
            }
            if trace_metadata.get("session_id"):
                _call_kwargs["session_id"] = trace_metadata["session_id"]
            if trace_metadata.get("user_id"):
                _call_kwargs["user_id"] = trace_metadata["user_id"]
            if trace_metadata.get("tags"):
                _call_kwargs["tags"] = trace_metadata["tags"]
        resp = await client.chat.completions.create(**_call_kwargs)
        elapsed = time.monotonic() - start
        raw = resp.choices[0].message.content or "{}"
        logger.info(
            "[CLASSIFIER] %.2fs | msg=%r | raw=%s",
            elapsed, message[:60], raw[:120],
        )

        parsed = json.loads(raw)
        intent_str = str(parsed.get("intent", "PERSONAL")).upper()
        score = int(parsed.get("score", 0))
        reason = str(parsed.get("reason", ""))

        # Validate intent enum; fall back to PERSONAL on unknown value
        try:
            intent = Intent(intent_str)
        except ValueError:
            logger.warning("[CLASSIFIER] Unknown intent %r — defaulting to PERSONAL", intent_str)
            intent = Intent.PERSONAL

        return ClassifierResult(
            intent=intent,
            score=score,
            reason=reason,
            frustrated=bool(parsed.get("frustrated", False)),
            abusive=bool(parsed.get("abusive", False)),
            out_of_scope=bool(parsed.get("out_of_scope", False)),
        )

    except Exception as exc:
        # Any failure (network, parse, etc.) → default to PERSONAL so the AI
        # remains silent rather than accidentally replying to a personal message.
        # Safety flags default to False so a single classifier outage does not
        # mass-pause every conversation.
        logger.error("[CLASSIFIER] Failed to classify message: %s — defaulting to PERSONAL", exc)
        return ClassifierResult(
            intent=Intent.PERSONAL,
            score=0,
            reason=f"classification error: {exc}",
        )
