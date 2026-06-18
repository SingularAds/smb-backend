"""Per-SMB Knowledge Base service.

When a customer asks a question the AI cannot answer (out-of-scope, BUSINESS
intent), we capture a *pending* knowledge entry and ask the owner — in their
WhatsApp self-chat — whether we should remember their answer for future
customers. Confirmed entries are injected into the customer-AI system prompt
so the next person asking the same thing gets an answer instantly.

Hard rules enforced here:

  • PERSONAL / ABUSIVE / GREETING messages are NEVER captured. The intent
    classifier already routes those to the silent branch in customer_ai_service,
    but `should_capture` re-checks defensively so a wrong caller can't bypass
    the guard.
  • Each pending entry gets a 4-character `shortCode` (e.g. ``KB-A3F7``) that
    the owner must echo in their YES/NO reply. This is the dedup key.
  • Dedup: before creating a new entry we look for an existing entry with the
    same normalized question in any of {pending_review, confirmed, rejected}.
    If one exists, we do not re-ask the owner.
  • Pending entries auto-expire after KB_PENDING_TTL_DAYS (default 7).
  • The owner-reply payload may include the answer inline
    (``YES KB-A3F7 We have paneer tikka, served with mint chutney``) or just a
    bare acknowledgement (``YES KB-A3F7``) in which case the entry is held in
    a `awaiting_answer` state for one follow-up message.

Module-level constants are designed to make every threshold configurable via
settings so we can tune behaviour without redeploying logic.
"""
from __future__ import annotations

import logging
import re
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable

from app import firestore as db

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

# Owner has this long to confirm a pending entry before it auto-expires.
KB_PENDING_TTL_DAYS = 7

# After an entry is rejected/expired, the same normalized question won't be
# re-captured for this many days (avoid pestering the owner with repeats).
KB_REJECT_COOLDOWN_DAYS = 30

# Short code: 4 chars, A-Z + digits, ambiguous chars removed.
_SHORTCODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
_SHORTCODE_LEN = 4
_SHORTCODE_PREFIX = "KB-"
_SHORTCODE_MAX_TRIES = 8

# Intents that must NEVER be captured. Defensive check — the classifier already
# routes these to a silent branch upstream; this is a second seatbelt.
_BLOCKED_INTENTS = frozenset({"PERSONAL", "ABUSIVE", "GREETING"})


# ── Status enum ───────────────────────────────────────────────────────────────


class KBStatus(str, Enum):
    PENDING_REVIEW = "pending_review"      # waiting for owner YES/NO
    AWAITING_ANSWER = "awaiting_answer"    # owner said YES but didn't include the answer
    CONFIRMED = "confirmed"                # ready to be served to customers
    REJECTED = "rejected"                  # owner said NO
    EXPIRED = "expired"                    # owner ignored the prompt for KB_PENDING_TTL_DAYS


# ── Normalization / keywords ──────────────────────────────────────────────────

# Very small English stop-word list. Cross-lingual stopwords are out of scope
# for v1; words that aren't stop-words just become extra (low-value) keywords
# which is harmless.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "do", "does", "did", "you", "your", "yours",
    "can", "could", "would", "will", "shall", "should", "may", "might", "we",
    "us", "i", "me", "my", "to", "of", "in", "on", "at", "and", "or", "but",
    "for", "with", "have", "has", "had", "be", "been", "being", "this", "that",
    "these", "those", "what", "when", "where", "how", "why", "who", "whom",
    "any", "some", "it", "its", "as", "by", "from", "if", "so", "than", "then",
    "there", "here", "into", "out", "up", "down", "no", "yes", "not", "ok",
    "okay", "please", "tell", "give", "got", "get",
})

_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def normalize_question(text: str) -> str:
    """Lowercase, collapse whitespace, strip non-alphanumeric. Used as the
    dedup key — two messages produce the same normalized form iff their
    word-content matches case-insensitively.
    """
    if not text:
        return ""
    words = _WORD_RE.findall(text.lower())
    return " ".join(words)


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    """Tokenize and drop stop words. Used by the retrieval-time matcher to
    score confirmed entries against the new customer question.
    """
    if not text:
        return []
    tokens = _WORD_RE.findall(text.lower())
    keywords: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok in _STOPWORDS or len(tok) < 2:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        keywords.append(tok)
        if len(keywords) >= limit:
            break
    return keywords


# ── Short code generation ─────────────────────────────────────────────────────


def generate_short_code(business_id: str | None = None) -> str:
    """Generate a globally unique-enough short code. When ``business_id`` is
    supplied we round-trip through Firestore to check for collisions within the
    business; otherwise we trust randomness (4 chars from 32-char alphabet =
    ~1M space).
    """
    for _ in range(_SHORTCODE_MAX_TRIES):
        suffix = "".join(secrets.choice(_SHORTCODE_ALPHABET) for _ in range(_SHORTCODE_LEN))
        code = f"{_SHORTCODE_PREFIX}{suffix}"
        if business_id is None:
            return code
        existing = db.get_business_kb_entry_by_code(business_id, code)
        if existing is None:
            return code
    # Extremely unlikely fallback: append two more random chars.
    extra = "".join(secrets.choice(_SHORTCODE_ALPHABET) for _ in range(2))
    return f"{_SHORTCODE_PREFIX}{suffix}{extra}"


# Permissive parser for owner replies. Matches:
#   "YES KB-A3F7 <answer>"
#   "NO KB-A3F7"
#   "yes kb-a3f7"
#   "Yes KB A3F7 paneer tikka served with..."  (separator tolerant)
_REPLY_RE = re.compile(
    r"^\s*(?P<verdict>yes|no)\s+kb[-\s]?(?P<code>[A-Za-z0-9]{4,6})\s*(?P<answer>.*)$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class OwnerReply:
    verdict: str        # "yes" | "no"
    short_code: str     # "KB-A3F7"  (normalized — prefix + uppercase suffix)
    answer: str         # trailing text after the code; "" if none

    @property
    def is_yes(self) -> bool:
        return self.verdict == "yes"


def parse_owner_reply(body: str) -> OwnerReply | None:
    """Parse an owner WhatsApp reply. Returns None if the message is not a
    KB confirmation.
    """
    if not body:
        return None
    match = _REPLY_RE.match(body)
    if not match:
        return None
    verdict = match.group("verdict").lower()
    code = f"{_SHORTCODE_PREFIX}{match.group('code').upper()}"
    answer = (match.group("answer") or "").strip()
    return OwnerReply(verdict=verdict, short_code=code, answer=answer)


# ── Capture guard ─────────────────────────────────────────────────────────────


def should_capture(intent: str, out_of_scope: bool, abusive: bool = False) -> bool:
    """Returns True iff the message is a legitimate KB candidate.

    Defensive: even if the caller forgets to check intent, this guard rejects
    PERSONAL/ABUSIVE/GREETING. Only ``out_of_scope=True`` BUSINESS messages are
    eligible.
    """
    if not out_of_scope:
        return False
    if abusive:
        return False
    intent_upper = (intent or "").upper()
    if intent_upper in _BLOCKED_INTENTS:
        return False
    return True


# ── Dedup helper ──────────────────────────────────────────────────────────────


_DEDUP_STATUSES = [
    KBStatus.PENDING_REVIEW.value,
    KBStatus.AWAITING_ANSWER.value,
    KBStatus.CONFIRMED.value,
    KBStatus.REJECTED.value,
]


def find_existing_match(business_id: str, question: str) -> dict | None:
    """Return any existing entry (any non-expired status) that already covers
    this normalized question. Caller uses this to avoid re-asking the owner.
    """
    normalized = normalize_question(question)
    if not normalized:
        return None
    return db.find_business_kb_by_normalized(business_id, normalized, statuses=_DEDUP_STATUSES)


# ── Public lifecycle API ──────────────────────────────────────────────────────


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def create_pending_entry(
    *,
    business_id: str,
    question: str,
    customer_phone: str,
    message_id: str,
    intent: str,
    now: datetime | None = None,
) -> dict | None:
    """Create a pending_review entry. Returns the new entry, or None if the
    capture guard rejected the input or a duplicate already exists.

    Caller is expected to have already verified ``should_capture`` — this
    function re-checks defensively.
    """
    if not should_capture(intent=intent, out_of_scope=True):
        logger.info(
            "[KB] Capture blocked by guard: business=%s intent=%s",
            business_id, intent,
        )
        return None

    if not question or not question.strip():
        return None

    if (now := now) is None:
        now = _now_utc()

    existing = find_existing_match(business_id, question)
    if existing is not None:
        logger.info(
            "[KB] Dedup hit: business=%s normalized=%r existing=%s status=%s",
            business_id, normalize_question(question),
            existing.get("id"), existing.get("status"),
        )
        return None

    normalized = normalize_question(question)
    keywords = extract_keywords(question)
    short_code = generate_short_code(business_id)
    expires_at = (now + timedelta(days=KB_PENDING_TTL_DAYS)).isoformat()

    payload = {
        "shortCode": short_code,
        "question": question.strip()[:500],          # cap to avoid runaway docs
        "questionNormalized": normalized,
        "questionKeywords": keywords,
        "answer": "",
        "status": KBStatus.PENDING_REVIEW.value,
        "source": "owner_reply",
        "intent": (intent or "").upper(),
        "askedByCustomerPhone": customer_phone,
        "askedAtMessageId": message_id,
        "createdAt": now.isoformat(),
        "expiresAt": expires_at,
        "confirmedAt": None,
        "confirmedByOwnerPhone": None,
        "useCount": 0,
        "lastUsedAt": None,
    }
    entry = db.create_business_kb_entry(business_id, payload)
    logger.info(
        "[KB] Pending entry created: business=%s code=%s q=%r",
        business_id, short_code, question[:60],
    )
    return entry


def confirm_entry(
    *,
    business_id: str,
    short_code: str,
    owner_phone: str,
    answer: str,
    now: datetime | None = None,
) -> dict | None:
    """Mark an entry as confirmed with the owner's answer. Returns the updated
    entry, or None if no matching entry exists.

    If ``answer`` is empty we transition to AWAITING_ANSWER instead — the next
    owner message will be captured as the answer (handled by the webhook).
    """
    if (now := now) is None:
        now = _now_utc()
    entry = db.get_business_kb_entry_by_code(business_id, short_code)
    if entry is None:
        return None
    if entry.get("status") not in {KBStatus.PENDING_REVIEW.value, KBStatus.AWAITING_ANSWER.value}:
        logger.info(
            "[KB] confirm_entry: ignoring entry in terminal status: business=%s code=%s status=%s",
            business_id, short_code, entry.get("status"),
        )
        return entry

    answer_clean = (answer or "").strip()
    if not answer_clean:
        return db.update_business_kb_entry(business_id, entry["id"], {
            "status": KBStatus.AWAITING_ANSWER.value,
            "confirmedByOwnerPhone": owner_phone,
            "confirmedAt": now.isoformat(),
        })

    return db.update_business_kb_entry(business_id, entry["id"], {
        "status": KBStatus.CONFIRMED.value,
        "answer": answer_clean[:1000],
        "confirmedByOwnerPhone": owner_phone,
        "confirmedAt": now.isoformat(),
    })


def reject_entry(
    *,
    business_id: str,
    short_code: str,
    owner_phone: str,
    now: datetime | None = None,
) -> dict | None:
    if (now := now) is None:
        now = _now_utc()
    entry = db.get_business_kb_entry_by_code(business_id, short_code)
    if entry is None:
        return None
    if entry.get("status") not in {KBStatus.PENDING_REVIEW.value, KBStatus.AWAITING_ANSWER.value}:
        return entry
    return db.update_business_kb_entry(business_id, entry["id"], {
        "status": KBStatus.REJECTED.value,
        "confirmedByOwnerPhone": owner_phone,
        "confirmedAt": now.isoformat(),
    })


def attach_answer_to_awaiting(
    *,
    business_id: str,
    answer: str,
    owner_phone: str,
    now: datetime | None = None,
) -> dict | None:
    """If exactly one entry is in AWAITING_ANSWER for this business, treat the
    owner's next message as the answer. Returns the updated entry or None.

    We deliberately only auto-attach when there is exactly one awaiting entry —
    ambiguity is safer to ignore than to mis-attribute an answer.
    """
    if not answer or not answer.strip():
        return None
    if (now := now) is None:
        now = _now_utc()
    awaiting = db.list_business_kb_by_status(business_id, KBStatus.AWAITING_ANSWER.value, limit=2)
    if len(awaiting) != 1:
        return None
    entry = awaiting[0]
    return db.update_business_kb_entry(business_id, entry["id"], {
        "status": KBStatus.CONFIRMED.value,
        "answer": answer.strip()[:1000],
        "confirmedByOwnerPhone": owner_phone,
        "confirmedAt": now.isoformat(),
    })


# ── Retrieval (used at customer-AI prompt build time) ─────────────────────────


def _score_match(question_keywords: Iterable[str], entry_keywords: Iterable[str]) -> int:
    """Number of overlapping keywords. Cheap and good enough for v1 — a
    semantic embedder is a future upgrade.
    """
    return len(set(question_keywords) & set(entry_keywords))


def find_confirmed_match(
    business_id: str,
    question: str,
    min_score: int = 2,
) -> dict | None:
    """Return the best-matching confirmed entry whose keyword overlap with the
    customer's question is >= ``min_score``. Returns None when nothing matches.

    Used as the **fast path** (Layer A): if this returns a hit the caller can
    serve the owner's answer verbatim without an LLM call.
    """
    normalized = normalize_question(question)
    if not normalized:
        return None

    # Exact normalized match wins immediately.
    exact = db.find_business_kb_by_normalized(
        business_id, normalized, statuses=[KBStatus.CONFIRMED.value],
    )
    if exact is not None:
        return exact

    q_keywords = extract_keywords(question)
    if not q_keywords:
        return None

    confirmed = db.list_business_kb_by_status(business_id, KBStatus.CONFIRMED.value, limit=200)
    best: tuple[int, dict] | None = None
    for entry in confirmed:
        entry_kw = entry.get("questionKeywords") or []
        score = _score_match(q_keywords, entry_kw)
        if score < min_score:
            continue
        if best is None or score > best[0]:
            best = (score, entry)
    return best[1] if best else None


def get_confirmed_for_prompt(business_id: str, limit: int = 200) -> list[dict]:
    """Return ALL confirmed KB entries for a business, sorted by popularity
    (useCount desc, then confirmedAt desc as tiebreaker).

    Used as the **slow path** (Layer B): the caller injects these into the LLM
    system prompt so the model can semantically match answers that keyword-
    overlap alone would miss.  Sorting by useCount ensures the most battle-
    tested answers appear first when a character budget forces truncation.
    """
    entries = db.list_business_kb_by_status(
        business_id, KBStatus.CONFIRMED.value, limit=limit
    )
    entries.sort(
        key=lambda e: (
            int(e.get("useCount") or 0),
            e.get("confirmedAt") or "",
        ),
        reverse=True,
    )
    return entries


def rank_entries_for_question(
    entries: list[dict],
    question: str,
    top_n: int = 30,
) -> list[dict]:
    """Re-rank *entries* by combined keyword-overlap + popularity score and
    return at most *top_n*.

    This is the **smart injection pre-filter** that runs when a business has
    more confirmed KB entries than we can fit in a prompt.  The combined score
    is (keyword_overlap × 10 + use_count) so relevance dominates but popular
    answers get a tiebreaker boost.

    When the total entry count is <= top_n we return all of them in order
    (cheaper and complete).
    """
    if len(entries) <= top_n:
        return list(entries)

    q_keywords = set(extract_keywords(question))
    scored: list[tuple[int, dict]] = []
    for entry in entries:
        kw_score = len(q_keywords & set(entry.get("questionKeywords") or []))
        use_score = int(entry.get("useCount") or 0)
        scored.append((kw_score * 10 + use_score, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:top_n]]


# ── Owner-reply webhook handler ───────────────────────────────────────────────


async def handle_owner_kb_reply(
    *,
    business: dict,
    owner_phone: str,
    body: str,
    device_id: str,
) -> bool:
    """Intercept the owner's YES/NO reply for a KB confirmation.

    Returns ``True`` if the reply was consumed (caller stops routing).
    Returns ``False`` if the message wasn't a KB confirmation reply.

    Wiring contract: this runs in the webhook **after** referral confirmation
    and **before** the general owner-command parser, so we don't accidentally
    route a KB reply as an unknown command.
    """
    parsed = parse_owner_reply(body)
    if parsed is None:
        return False

    # Lazy import to avoid circulars at module load time.
    from app.services.whatsmeow_client import WhatsmeowClient
    wa = WhatsmeowClient()

    business_id = business["id"]

    # ── YES <code> [<answer>] ─────────────────────────────────────────────────
    if parsed.is_yes:
        # If owner forgot the answer, transition to AWAITING_ANSWER and ask.
        entry = confirm_entry(
            business_id=business_id,
            short_code=parsed.short_code,
            owner_phone=owner_phone,
            answer=parsed.answer,
        )
        if entry is None:
            await wa.send_message(
                owner_phone,
                (
                    f"❌ Code *{parsed.short_code}* was not recognised "
                    f"or has already expired. Check the code and try again."
                ),
                device_id=device_id,
            )
            return True

        status = entry.get("status")
        if status == KBStatus.CONFIRMED.value:
            await wa.send_message(
                owner_phone,
                (
                    f"✅ Saved! I'll use this for future customers asking about "
                    f"the same thing.\n\n_Q: {entry.get('question', '')[:120]}_\n"
                    f"_A: {entry.get('answer', '')[:200]}_"
                ),
                device_id=device_id,
            )
            return True

        if status == KBStatus.AWAITING_ANSWER.value:
            await wa.send_message(
                owner_phone,
                (
                    f"Almost there 🙂  Please resend with your answer:\n"
                    f"*YES {parsed.short_code} <your answer>*"
                ),
                device_id=device_id,
            )
            return True

        # Entry was already in a terminal state (rejected/expired/confirmed prior).
        await wa.send_message(
            owner_phone,
            f"This entry ({parsed.short_code}) was already handled.",
            device_id=device_id,
        )
        return True

    # ── NO <code> ─────────────────────────────────────────────────────────────
    entry = reject_entry(
        business_id=business_id,
        short_code=parsed.short_code,
        owner_phone=owner_phone,
    )
    if entry is None:
        await wa.send_message(
            owner_phone,
            f"❌ Code *{parsed.short_code}* was not recognised or already expired.",
            device_id=device_id,
        )
        return True

    await wa.send_message(
        owner_phone,
        f"👍 Skipped. I won't ask this one again ({parsed.short_code}).",
        device_id=device_id,
    )
    return True


# ── Expiry sweeper ────────────────────────────────────────────────────────────


def expire_stale_pending(business_id: str, now: datetime | None = None) -> int:
    """Move all pending_review entries past their expiresAt → expired.
    Returns the number of entries expired. Called by the daily automation
    sweeper.
    """
    if (now := now) is None:
        now = _now_utc()
    now_iso = now.isoformat()
    pending = db.list_business_kb_by_status(business_id, KBStatus.PENDING_REVIEW.value, limit=500)
    expired = 0
    for entry in pending:
        if entry.get("expiresAt") and entry["expiresAt"] <= now_iso:
            db.update_business_kb_entry(business_id, entry["id"], {
                "status": KBStatus.EXPIRED.value,
            })
            expired += 1
    if expired:
        logger.info("[KB] Expired %d stale pending entries for business=%s", expired, business_id)
    return expired
