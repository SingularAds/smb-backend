"""Language detection + translation for owner command replies.

Strategy (zero-hardcode):
  1. Detect the language of the owner's raw message using langdetect.
  2. If it differs from English (the language command replies are
     authored in), call Claude to translate into the detected language.
  3. Return the translated (or original) reply.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Supported languages — iso 639-1 codes.
# Command reply strings in services.py are authored in English; we skip
# translation when the owner is already writing in English.
_TEMPLATE_LANG = "en"


def _detect_language(text: str) -> str:
    """Return an ISO 639-1 language code for *text*, fallback 'en'.

    Uses detect_langs() to get confidence scores.
    Only returns a language if its probability >= 0.80 AND it is in our
    supported whitelist — otherwise falls back to 'en'.
    Short texts (< 3 words) always return 'en' since langdetect is unreliable.
    """
    # Supported language whitelist — anything outside this is ignored
    _SUPPORTED = {"en", "pt", "es", "fr", "de", "it", "nl", "pl",
                  "he", "hi", "ar", "zh-cn", "zh-tw", "ja", "ko", "ru"}
    _MIN_CONFIDENCE = 0.80

    # Too short to classify reliably
    words = text.strip().split()
    if len(words) < 3:
        return "en"

    try:
        from langdetect import detect_langs, LangDetectException  # type: ignore
        results = detect_langs(text)  # returns list of Language(lang, prob)
        if results:
            top = results[0]
            lang_code = top.lang.lower()
            # Normalise zh-cn / zh-tw → zh
            lang_code_norm = lang_code.replace("zh-cn", "zh").replace("zh-tw", "zh")
            if top.prob >= _MIN_CONFIDENCE and lang_code in _SUPPORTED:
                return lang_code
        return "en"
    except Exception:
        return "en"


async def translate_reply(raw_message: str, reply: str, lang: str | None = None) -> str:
    """Translate *reply* into the same language as *raw_message*.

    If the detected language is English (the language command replies are
    authored in) or detection fails, the reply is returned unchanged.

    Pass *lang* directly when the caller has already resolved the session
    language (e.g. from onboarding_service) to avoid re-detecting from a
    short or ambiguous command body like "resume ai" which langdetect may
    misclassify.
    """
    if lang is None:
        lang = _detect_language(raw_message)
    if not lang or lang == _TEMPLATE_LANG:
        return reply

    lang_names = {
        "en": "English",
        "pt": "Portuguese",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "it": "Italian",
        "nl": "Dutch",
        "pl": "Polish",
        "he": "Hebrew",
        "hi": "Hindi",
        "ar": "Arabic",
        "zh": "Chinese (Simplified)",
        "ja": "Japanese",
        "ko": "Korean",
        "ru": "Russian",
    }
    lang_name = lang_names.get(lang, lang.upper())

    try:
        from app.integrations.openai_adapter import AsyncOpenAIAnthropicWrapper
        from app.config import settings

        client = AsyncOpenAIAnthropicWrapper(api_key=settings.OPENAI_API_KEY)

        # Source-language-agnostic prompt: command replies are authored in
        # English but some flows (e.g. handle_owner_message acknowledgements)
        # already produce localized text. Telling Claude the source explicitly
        # caused mistranslations (English source → "translate from Portuguese"
        # → Portuguese output). Have it auto-detect and no-op when already
        # in the target language.
        system = (
            f"You are a professional translator. "
            f"Translate the following WhatsApp message into {lang_name}. "
            f"If the message is already in {lang_name}, return it unchanged. "
            "Preserve all WhatsApp formatting (bold *text*, italics _text_, "
            "bullet characters, emojis, phone numbers, and identifiers like "
            "+919905252720). Return ONLY the translated message text — no "
            "explanation, no quotes."
        )

        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": reply}],
        )
        translated = message.content[0].text.strip()
        logger.debug("Translated reply → %s", lang)
        return translated
    except Exception as exc:
        logger.warning("Translation failed (%s), returning original reply: %s", lang, exc)
        return reply
