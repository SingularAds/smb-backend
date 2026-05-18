"""Cartesia Text-to-Speech client.

Calls Cartesia's /tts/bytes endpoint and returns MP3 audio bytes.
Cartesia does NOT support OGG output; MP3 is the recommended format for
WhatsApp audio messages (sent as audio/mpeg, appears as a playable audio clip).
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"
_CARTESIA_VERSION = "2025-04-16"

# Default voice: multilingual, warm female — works for EN / PT / ES / FR etc.
# Override per-business via business.verticalSettings.cartesiaVoiceId
DEFAULT_VOICE_ID = "a0e99841-438c-4a64-b679-ae501e7d6091"

# MIME type produced by this client (used by WhatsApp bridge)
OUTPUT_MIME_TYPE = "audio/mpeg"


async def synthesize(
    text: str,
    voice_id: str | None = None,
    language: str = "en",
) -> bytes:
    """Convert text to MP3 audio using Cartesia sonic-multilingual.

    Args:
        text:     The text to speak.
        voice_id: Cartesia voice UUID. Falls back to DEFAULT_VOICE_ID.
        language: ISO 639-1 language code (en, pt, es, fr, …).

    Returns:
        Raw MP3 bytes (audio/mpeg).  Use OUTPUT_MIME_TYPE when sending.

    Raises:
        ValueError: If CARTESIA_API_KEY is not configured.
        httpx.HTTPStatusError: If Cartesia returns a non-2xx response.
    """
    if not settings.CARTESIA_API_KEY:
        raise ValueError("CARTESIA_API_KEY is not configured")

    vid = voice_id or DEFAULT_VOICE_ID

    headers = {
        "X-API-Key": settings.CARTESIA_API_KEY,
        "Cartesia-Version": _CARTESIA_VERSION,
        "Content-Type": "application/json",
    }
    body = {
        "transcript": text,
        "model_id": "sonic-multilingual",
        "voice": {
            "mode": "id",
            "id": vid,
        },
        "output_format": {
            "container": "mp3",
            "encoding": "mp3",
            "sample_rate": 44100,
            "bit_rate": 128000,
        },
        "language": language,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_CARTESIA_URL, json=body, headers=headers)
        if not resp.is_success:
            logger.error(
                "Cartesia TTS error %s: %s", resp.status_code, resp.text[:300]
            )
        resp.raise_for_status()

    audio_bytes = resp.content
    logger.debug(
        "Cartesia TTS: %d bytes MP3, voice=%s, lang=%s", len(audio_bytes), vid, language
    )
    return audio_bytes
