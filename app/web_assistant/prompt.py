"""Website-assistant system prompt.

Hard rules baked into the prompt:
  * Answer ONLY from the Global KB + approved learnings.
  * If the answer is not in the knowledge → return the literal token
    ``[ESCALATE]`` (and nothing else). The orchestrator detects this token
    and triggers the human handoff path. The token is internal — visitors
    never see it because we replace it with a friendly escalation message
    before sending.
  * Always respond in the visitor's locked-in language.
  * Friendly, concise, web-chat tone (markdown OK, NO WhatsApp framing).

The KB and the learnings are passed in via ``build_system_prompt``. The
caller is responsible for picking the language code; this module only
inlines it.
"""

from __future__ import annotations
from urllib.parse import quote as _url_quote

from app.config import settings
from app.services import global_kb as global_kb_service
from app.web_assistant import learnings as web_learnings


ESCALATE_TOKEN = "[ESCALATE]"


_SYSTEM_PROMPT_TEMPLATE = """\
You are the Recepte website assistant. You help potential customers \
visiting recepte.co understand the product before they sign up.

# YOUR ONLY KNOWLEDGE SOURCE
You answer **exclusively** from the knowledge in the blocks below \
(MAIN KNOWLEDGE BASE + LEARNED CONTENT + ONBOARDING CONTACT). If the \
answer is not there, do not guess, do not invent, do not improvise.

# WHEN YOU CANNOT ANSWER
Return the single literal token: {escalate_token}
Nothing else. No apology, no explanation, no greeting. Just: {escalate_token}
Our backend will replace this with a friendly human-handoff message.

# WHEN YOU CAN ANSWER
- Be warm, concise, helpful — this is a chat widget, not an email.
- Markdown formatting is welcome (bold, lists, links) — visitors are on a website.
- Two short paragraphs maximum unless the visitor asks for detail.
- Never repeat the question back verbatim.
- Never include the {escalate_token} token in a real answer.

# LANGUAGE RULE
The visitor's locked-in language is: **{language_label}**.
Always reply in that language, regardless of which language they switch to mid-conversation. \
(If the visitor explicitly asks you to switch languages, reply briefly in the new \
language and ask them to refresh the chat to confirm the change.)

# SCOPE
You ONLY discuss:
- What Recepte is and how it works
- Pricing, plans, free trial, payment, billing
- Onboarding process and time-to-value
- Features (AI receptionist on WhatsApp + voice, bookings, calendar, multilingual, etc.)
- Supported business types
- FAQs and common objections
- Pointing visitors to start their free trial when they're ready

You do NOT:
- Book appointments (that's a different product — for a business's customers)
- Look up specific businesses, accounts, or bookings
- Give legal, tax, or financial advice
- Speculate about features, integrations, or release dates not in the knowledge below
- Promise discounts, custom pricing, or anything not in the knowledge below
For any of the above → return {escalate_token}.

{onboarding_link_block}

# {main_kb_header}
{main_kb_body}

{learnings_block}
""".strip()


_LANGUAGE_LABELS: dict[str, str] = {
    "en": "English",
    "pt": "European Portuguese",
    "pt-br": "Brazilian Portuguese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "ro": "Romanian",
    "hi": "Hindi",
    "ar": "Arabic",
}


def language_label(code: str | None) -> str:
    """Human-readable language name for prompt insertion."""
    if not code:
        return "English"
    norm = code.strip().lower()
    if norm in _LANGUAGE_LABELS:
        return _LANGUAGE_LABELS[norm]
    # Try the 2-letter prefix for codes like "pt-PT", "en-GB".
    short = norm.split("-")[0]
    if short in _LANGUAGE_LABELS:
        return _LANGUAGE_LABELS[short]
    return code.strip() or "English"


def _onboarding_link_section() -> str:
    """Return the onboarding hard-rule block injected into the system prompt.

    Placed BEFORE the KB body so the model treats it as a binding constraint
    rather than information it may ignore after reading a long knowledge block.
    Empty string when ``WHATSMEOW_GLOBAL_NUMBER`` is unset.
    """
    raw = (settings.WHATSMEOW_GLOBAL_NUMBER or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    prefill = _url_quote("Hey! I want to start with onboarding.", safe="")
    wa_link = f"https://wa.me/{digits}?text={prefill}"
    return (
        "# ONBOARDING LINK — MANDATORY RULE\n"
        "Whenever a visitor asks about onboarding, signing up, starting the free trial, "
        "getting started, activating the AI receptionist, or asks for the WhatsApp "
        "number / contact details, you MUST include this exact clickable link in your reply:\n\n"
        f"[👉 Start your free trial on WhatsApp]({wa_link})\n\n"
        "Rules:\n"
        "- ALWAYS embed the link as a markdown hyperlink (never paste the raw URL alone).\n"
        "- Clicking the link opens WhatsApp with a pre-filled message — the visitor "
        "just taps Send and Sofia starts the onboarding.\n"
        "- NEVER say 'message the Recepte WhatsApp number' without immediately "
        "following it with the clickable link above.\n"
        "- NEVER say you cannot share or don't have the number — you have it and "
        "MUST share it via the link above.\n"
        "- Adapt the surrounding phrasing to the visitor's language; the link itself "
        "stays unchanged."
    )


def build_system_prompt(language: str | None) -> str:
    """Assemble the full system prompt with KB + learnings inlined."""
    # Main KB ships pre-formatted with section markers from global_kb_service.
    main_kb_text = global_kb_service.build_kb_prompt_section()

    # Learnings — empty string when none exist.
    entries = web_learnings.get_learnings()
    learnings_block = web_learnings.format_for_prompt(entries)

    return _SYSTEM_PROMPT_TEMPLATE.format(
        escalate_token=ESCALATE_TOKEN,
        language_label=language_label(language),
        main_kb_header="MAIN KNOWLEDGE BASE (curated)",
        main_kb_body=main_kb_text,
        onboarding_link_block=_onboarding_link_section(),
        learnings_block=learnings_block or "(no learned content yet)",
    )


# ── User-visible strings -----------------------------------------------------
# Kept here (not scattered through service.py) so copy edits stay in one file.


_GREETING_BY_LANG: dict[str, str] = {
    "en": "Hi! 👋 I'm Recepte's website assistant. Ask me anything about pricing, plans, or how Recepte works.",
    "pt": "Olá! 👋 Sou o assistente da Recepte. Pergunte-me sobre preços, planos ou como funciona a Recepte.",
    "es": "¡Hola! 👋 Soy el asistente de Recepte. Pregúntame sobre precios, planes o cómo funciona Recepte.",
    "fr": "Bonjour ! 👋 Je suis l'assistant Recepte. Posez-moi vos questions sur les prix, les plans ou le fonctionnement de Recepte.",
    "de": "Hallo! 👋 Ich bin der Recepte-Website-Assistent. Frag mich zu Preisen, Plänen oder wie Recepte funktioniert.",
    "it": "Ciao! 👋 Sono l'assistente di Recepte. Chiedimi di prezzi, piani o come funziona Recepte.",
}


def greeting_for(language: str | None) -> str:
    """Locale-aware first-touch greeting."""
    code = (language or "en").strip().lower().split("-")[0]
    return _GREETING_BY_LANG.get(code, _GREETING_BY_LANG["en"])


_ESCALATION_PUBLIC_BY_LANG: dict[str, str] = {
    "en": "Great question — let me bring one of our team in to help you with that. 🙌",
    "pt": "Boa pergunta — vou chamar alguém da nossa equipa para o ajudar. 🙌",
    "es": "¡Buena pregunta! Voy a llamar a alguien de nuestro equipo para ayudarte. 🙌",
    "fr": "Excellente question — je vais demander à quelqu'un de notre équipe de vous aider. 🙌",
    "de": "Gute Frage — ich hole jemanden aus unserem Team dazu, der dir hilft. 🙌",
    "it": "Ottima domanda — coinvolgo qualcuno del nostro team per aiutarti. 🙌",
}


def escalation_public_message(language: str | None) -> str:
    """Friendly message visible to the visitor when we hand off to a human."""
    code = (language or "en").strip().lower().split("-")[0]
    return _ESCALATION_PUBLIC_BY_LANG.get(code, _ESCALATION_PUBLIC_BY_LANG["en"])


def escalation_private_note(question: str, esc_id: str, learn_command: str) -> str:
    """Internal note shown to agents when the AI escalates a conversation."""
    return (
        "🤖 AI could not answer this question:\n\n"
        f"> {question}\n\n"
        "Reply to the visitor, then save this Q&A to the knowledge base by posting:\n\n"
        f"`{learn_command} {esc_id}`"
    )


def learn_success_note(question: str, esc_id: str | None = None) -> str:
    esc_suffix = f" `{esc_id}`" if esc_id else ""
    return (
        f"✅ Saved to knowledge base{esc_suffix}.\n\n"
        f"Future visitors asking *{question[:160]}* will receive your answer automatically."
    )


def learn_failure_note(reason: str) -> str:
    return f"⚠️ Could not save to knowledge base — {reason}."


# ── Pause / resume / help ----------------------------------------------------


def pause_success_note(resume_command: str) -> str:
    return (
        "⏸️ AI paused for this conversation.\n\n"
        f"You're now in full control. Post **{resume_command}** as a private note "
        "when you want the AI to handle the next visitor message."
    )


def pause_already_note() -> str:
    return "⏸️ AI is already paused for this conversation — no change."


def resume_success_note() -> str:
    return (
        "▶️ AI resumed for this conversation.\n\n"
        "I'll respond to the next visitor message from the knowledge base."
    )


def resume_already_note() -> str:
    return "▶️ AI is already active for this conversation — no change."


def takeover_hint_note(learn_command: str, resume_command: str) -> str:
    return (
        "💡 AI paused — you're now handling this conversation.\n\n"
        f"• To save a visitor question + your reply to the knowledge base: "
        f"post **{learn_command} <ID>** "
        f"(each AI escalation note shows the exact ready-to-copy command)\n"
        f"• To hand the conversation back to the AI: post **{resume_command}**"
    )


def help_note(
    learn_command: str,
    pause_command: str,
    resume_command: str,
) -> str:
    return (
        "🤖 Available agent commands (post as private notes):\n\n"
        f"• **{pause_command}** — stop the AI from replying to this conversation\n"
        f"• **{resume_command}** — re-enable the AI for this conversation\n"
        f"• **{learn_command} <ID>** — save a specific escalated Q&A "
        "(the ID is in each AI escalation note — copy-paste ready)\n"
        f"• **{learn_command}** — save the most recent visitor question + your reply"
    )
