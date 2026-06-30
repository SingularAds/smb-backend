"""Website assistant orchestrator.

Single entry point ``handle_event(inbound)``:
  * Routes by event type and sender type to the right path.
  * For visitor messages: loads history + KB + learnings → LLM → reply or
    escalate via Chatwoot transport.
  * For agent private notes: detects the ``/kb-learn`` command and saves
    the most recent visitor question + agent reply as a learning.
  * For agent public messages: marks the conversation as paused (human
    took over). The AI stops responding until conversation is re-opened.

The service holds no global state; everything persists through ``store``
or ``learnings`` modules. This keeps the function deterministic and
trivially testable.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.config import settings
from app.web_assistant import commands, prompt as prompt_mod, store, transport
from app.web_assistant.types import (
    InboundChatMessage,
    SenderType,
    WebhookEvent,
)

logger = logging.getLogger(__name__)


# ── Public entrypoint ──────────────────────────────────────────────────────


async def handle_event(inbound: InboundChatMessage) -> None:
    """Dispatch a normalised Chatwoot event."""
    # CONVERSATION_CREATED is intentionally ignored — the LLM handles the
    # first visitor message directly, so there is no separate auto-greeting
    # step. This guarantees one message per visitor exchange and removes the
    # ordering race that produced double-greetings in production.
    if inbound.event is WebhookEvent.CONVERSATION_CREATED:
        return

    # MESSAGE_CREATED — branch on sender role.
    if inbound.sender_type is SenderType.VISITOR:
        await _handle_visitor_message(inbound)
        return

    if inbound.sender_type is SenderType.AGENT:
        await _handle_agent_message(inbound)
        return

    # Bot / system — adapter already filters most of these out.
    logger.debug(
        "[WEB-AI] Ignoring event=%s sender=%s conv=%s",
        inbound.event, inbound.sender_type, inbound.conversation_id,
    )


# ── Visitor → AI path ──────────────────────────────────────────────────────


async def _handle_visitor_message(inbound: InboundChatMessage) -> None:
    conv_id = inbound.conversation_id
    body = inbound.body.strip()
    if not body:
        return

    doc = store.load(conv_id)

    # Lock language on the first substantial visitor message.
    if not doc.get("language"):
        doc["language"] = _normalise_language(
            inbound.visitor_browser_language
        ) or _guess_language_from_text(body)
    doc["visitorName"] = inbound.sender_name or doc.get("visitorName")
    doc["inboxId"] = inbound.inbox_id or doc.get("inboxId")

    store.append_message(doc, "user", body)

    # Respect human takeover — once an agent has replied publicly, the AI
    # stays out of the conversation until it is explicitly resumed.
    if doc.get("aiPaused"):
        store.save(conv_id, doc)
        logger.info(
            "[WEB-AI] AI is paused for conv=%s — skipping LLM reply",
            conv_id,
        )
        return

    language = doc.get("language") or "en"

    # ── LLM call ----------------------------------------------------------
    try:
        llm_text = await _ask_llm(
            history=doc.get("messages") or [],
            language=language,
            conversation_id=conv_id,
            visitor_name=doc.get("visitorName"),
        )
    except Exception as exc:
        logger.exception("[WEB-AI] LLM call failed for conv=%s: %s", conv_id, exc)
        # On hard failure → escalate gracefully so the visitor is never stranded.
        await _escalate(conv_id, doc, body, inbound.message_id, language, reason="llm_error")
        store.save(conv_id, doc)
        return

    llm_text = (llm_text or "").strip()

    # ── Branch on the escalation marker ----------------------------------
    if _is_escalation(llm_text):
        await _escalate(conv_id, doc, body, inbound.message_id, language, reason="kb_miss")
        store.save(conv_id, doc)
        return

    # ── Public reply path ------------------------------------------------
    sent_id = await transport.send_message(conv_id, llm_text)
    if sent_id:
        store.register_bot_message(doc, sent_id)
        store.append_message(doc, "assistant", llm_text)
        # Persist BEFORE analytics — Chatwoot fires a message_created webhook
        # for our outgoing reply, and that echo races against this save. If
        # the save loses, the echo handler loads a doc that does not yet
        # know about ``sent_id`` and treats it as a real human takeover,
        # pausing the AI and posting the takeover hint for no reason.
        store.save(conv_id, doc)
        _emit_analytics("web_chat.reply_sent", conv_id, {"language": language})
        return

    logger.warning(
        "[WEB-AI] Failed to deliver public reply for conv=%s — leaving history unchanged",
        conv_id,
    )
    store.save(conv_id, doc)


# ── Agent path (human handoff + /kb-learn) ─────────────────────────────────


async def _handle_agent_message(inbound: InboundChatMessage) -> None:
    conv_id = inbound.conversation_id
    doc = store.load(conv_id)

    if inbound.is_private_note:
        # Suppress echoes of our own private notes (e.g. escalation notes,
        # /kb-learn confirmations) before running the command parser.
        if store.is_bot_message(doc, inbound.message_id):
            logger.debug(
                "[WEB-AI] Suppressing private note echo msg_id=%s conv=%s",
                inbound.message_id, conv_id,
            )
            return
        cmd = commands.parse(inbound.body)
        if cmd is None:
            # Plain private note from agent — nothing to do.
            store.save(conv_id, doc)
            return
        if cmd == commands.Command.LEARN:
            await commands.execute_learn(
                conversation_id=conv_id,
                doc=doc,
                agent_name=inbound.sender_name,
                body=inbound.body,
            )
        elif cmd == commands.Command.PAUSE:
            await commands.execute_pause(conversation_id=conv_id, doc=doc)
        elif cmd == commands.Command.RESUME:
            await commands.execute_resume(conversation_id=conv_id, doc=doc)
        elif cmd == commands.Command.HELP:
            await commands.execute_help(conversation_id=conv_id, doc=doc)
        store.save(conv_id, doc)
        return

    # ── Echo suppression ──────────────────────────────────────────────────
    # When the bot posts a public reply via the Chatwoot Messages API,
    # Chatwoot fires a ``message_created`` webhook for that outgoing message.
    # The payload is indistinguishable from a real human reply (same
    # sender.type="User", same agent name).  We detect it by comparing the
    # inbound message ID against the registry of IDs we stored at send time.
    if store.is_bot_message(doc, inbound.message_id):
        logger.debug(
            "[WEB-AI] Suppressing bot echo msg_id=%s conv=%s",
            inbound.message_id, conv_id,
        )
        return  # Nothing changed — no Firestore write needed.

    # ── Genuine human-agent public reply → pause AI ───────────────────────
    is_first_takeover = not doc.get("aiPaused")
    if is_first_takeover:
        store.mark_ai_paused(doc, True)
        logger.info(
            "[WEB-AI] Human takeover detected — pausing AI for conv=%s agent=%s",
            conv_id, inbound.sender_name,
        )
        _emit_analytics(
            "web_chat.human_takeover",
            conv_id,
            {"agent": inbound.sender_name or "unknown"},
        )

    # Remember the agent's reply in history (gives /kb-learn a clean source).
    store.append_message(doc, "assistant", inbound.body)
    store.save(conv_id, doc)

    # Surface the available commands on the very first human takeover *of this
    # conversation* — never again. The hint stays sticky across /ai-pause and
    # /ai-resume cycles so a single owner is not pinged with the same guide
    # note every time the AI re-escalates.
    if is_first_takeover and not doc.get("takeoverHintShown"):
        hint_id = await transport.send_private_note(
            conv_id,
            prompt_mod.takeover_hint_note(
                settings.CHATWOOT_LEARN_COMMAND,
                settings.CHATWOOT_RESUME_COMMAND,
            ),
        )
        if hint_id:
            store.register_bot_message(doc, hint_id)
            doc["takeoverHintShown"] = True
            store.save(conv_id, doc)


# ── LLM helper ─────────────────────────────────────────────────────────────


async def _ask_llm(
    *,
    history: list[dict[str, Any]],
    language: str,
    conversation_id: str,
    visitor_name: str | None,
) -> str:
    """Call OpenAI through the shared adapter (Langfuse-traced when enabled)."""
    from app.config import settings as _settings
    from app.integrations.openai_adapter import AsyncOpenAIAnthropicWrapper

    system_prompt = prompt_mod.build_system_prompt(language)

    # Convert our compact history to the adapter format (which mirrors
    # Anthropic's). The newest visitor message is already inside history.
    messages: list[dict[str, str]] = []
    for msg in history:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if not content or role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": content})

    if not messages:
        return ""

    client = AsyncOpenAIAnthropicWrapper(api_key=_settings.OPENAI_API_KEY)
    response = await client.messages.create(
        model=_settings.CHATWOOT_LLM_MODEL,
        max_tokens=600,
        system=system_prompt,
        messages=messages,
        trace_metadata={
            "name": "web_assistant_reply",
            "channel": "chatwoot",
            "conversation_id": conversation_id,
            "language": language,
            "visitor_name": visitor_name or "anonymous",
            "session_id": f"chatwoot:{conversation_id}",
            "tags": ["web_assistant", "chatwoot"],
        },
    )

    parts: list[str] = []
    for block in response.content or []:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(block.text)
    return "\n".join(parts).strip()


# ── Escalation helper ──────────────────────────────────────────────────────


async def _escalate(
    conv_id: str,
    doc: dict[str, Any],
    question: str,
    message_id: str | None,
    language: str,
    *,
    reason: str,
) -> None:
    """Run the escalation sequence: public msg → team assignment → private note."""
    esc_id = uuid.uuid4().hex[:8]
    store.add_pending_escalation(doc, esc_id, question, message_id)
    _emit_analytics(
        "web_chat.escalated",
        conv_id,
        {"language": language, "reason": reason},
    )

    public_msg = prompt_mod.escalation_public_message(language)
    sent_id = await transport.send_message(conv_id, public_msg)
    if sent_id:
        store.register_bot_message(doc, sent_id)
        store.append_message(doc, "assistant", public_msg)
        # Save immediately so the webhook echo of this message is suppressed
        store.save(conv_id, doc)

    await transport.assign_team(conv_id, int(settings.CHATWOOT_SUPPORT_TEAM_ID or 0))

    private_msg = prompt_mod.escalation_private_note(
        question, esc_id, settings.CHATWOOT_LEARN_COMMAND
    )
    note_id = await transport.send_private_note(conv_id, private_msg)
    if note_id:
        store.register_bot_message(doc, note_id)
        # Save immediately so the webhook echo of this private note is suppressed
        store.save(conv_id, doc)

    logger.info(
        "[WEB-AI] Escalated conv=%s reason=%s lang=%s",
        conv_id, reason, language,
    )


# ── Small utilities ────────────────────────────────────────────────────────


def _is_escalation(text: str) -> bool:
    if not text:
        return True
    return prompt_mod.ESCALATE_TOKEN in text.strip().upper()


_LANG_ALIASES = {
    "pt-pt": "pt", "pt-br": "pt-br", "en-us": "en", "en-gb": "en",
    "es-es": "es", "es-mx": "es", "fr-fr": "fr", "de-de": "de",
}


def _normalise_language(code: str | None) -> str | None:
    if not code:
        return None
    norm = code.strip().lower()
    return _LANG_ALIASES.get(norm, norm.split("-")[0])


def _guess_language_from_text(text: str) -> str:
    """Cheap heuristic — used only as a fallback when browser_language is missing.

    The LLM itself is far better at language detection; this guess is only
    used to seed the locked-in conversation language for the *first* reply.
    Subsequent replies follow the locked language regardless.
    """
    # Tiny keyword sniff — no external dependency, no extra LLM call.
    lower = (text or "").lower()
    hints = {
        "pt": ("olá", "obrigad", "preço", "como funciona", "quanto custa"),
        "es": ("hola", "gracias", "precio", "cuánto cuesta", "cómo funciona"),
        "fr": ("bonjour", "merci", "combien", "comment ça marche"),
        "de": ("hallo", "danke", "preis", "wie funktioniert"),
        "it": ("ciao", "grazie", "prezzo", "come funziona"),
    }
    for lang, words in hints.items():
        if any(w in lower for w in words):
            return lang
    return "en"


def _emit_analytics(event: str, conv_id: str, props: dict[str, Any]) -> None:
    """Fire a PostHog event without ever raising into the orchestrator.

    The PostHog backend client expects ``business_id`` and ``customer_phone``
    so a single project can serve both WhatsApp and website traffic. We
    pass a synthetic ``business_id="recepte-website"`` and use the Chatwoot
    conversation id (prefixed) as the customer identifier — the hash on
    the PostHog side keeps personalisation cohorts clean.
    """
    try:
        from app.integrations.posthog_client import capture as ph_capture

        ph_capture(
            business_id="recepte-website",
            customer_phone=f"chatwoot:{conv_id}",
            event=event,
            properties={"channel": "chatwoot", **props},
        )
    except Exception:
        # PostHog must never break the chat flow.
        logger.debug("[WEB-AI] posthog capture failed silently", exc_info=True)
