"""Deepgram Speech-to-Text client — prerecorded audio transcription.

Uses Deepgram's Nova-3 model via the /v1/listen REST endpoint.
Accepts raw audio bytes in any format WhatsApp voices produce
(ogg/opus, mp4, webm, mp3) and returns the transcript as a string.

Retries up to 2 times on network/timeout errors.
If Deepgram returns 400 "corrupt or unsupported data", retries once with
``application/octet-stream`` so Deepgram auto-detects the format.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
_MAX_RETRIES = 2
_RETRY_DELAY = 1.5  # seconds between retries

# Known bad first-bytes that indicate an error page instead of audio
_BAD_PREFIXES = (b"<", b"{", b"[", b"HTTP")


def _looks_like_audio(data: bytes) -> bool:
    """Return False if the bytes are clearly not audio (HTML/JSON error page)."""
    if len(data) < 8:
        return False
    for prefix in _BAD_PREFIXES:
        if data[:len(prefix)] == prefix:
            return False
    return True


async def transcribe_audio(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
) -> str:
    """Transcribe audio bytes to text using Deepgram Nova-3.

    Args:
        audio_bytes: Raw audio content (ogg/opus, mp4, webm, mp3, etc.)
        mime_type:   MIME type matching the audio format.

    Returns:
        Transcript string (may be empty if speech was not detected).

    Raises:
        ValueError: If bytes do not appear to be audio data.
        httpx.HTTPStatusError: If Deepgram returns a non-2xx response after all retries.
        RuntimeError: If all retries are exhausted on connection/timeout errors.
    """
    if not settings.DEEPGRAM_API_KEY:
        raise ValueError("DEEPGRAM_API_KEY is not configured")

    # Guard: reject obvious non-audio (HTML error pages, JSON errors, etc.)
    if not _looks_like_audio(audio_bytes):
        preview = audio_bytes[:120]
        raise ValueError(
            f"Downloaded bytes do not appear to be audio (first bytes: {preview!r}). "
            "The media URL may have returned an error page."
        )

    # Normalise mime_type — Deepgram accepts audio/ogg, audio/ogg;codecs=opus, etc.
    ct = mime_type.split(";")[0].strip() or "audio/ogg"

    # Keep the declared type conservative; Deepgram handles most formats fine.
    if ct not in (
        "audio/ogg", "audio/mpeg", "audio/mp4", "audio/webm",
        "audio/wav", "audio/wave", "audio/x-wav", "audio/flac",
        "video/mp4", "video/webm",
    ):
        ct = "audio/ogg"

    params = {
        "model": "nova-3",
        "smart_format": "true",
        "detect_language": "true",
        "punctuate": "true",
    }

    async def _post_to_deepgram(content_type: str) -> dict:
        """POST audio bytes to Deepgram with the given Content-Type."""
        headers = {
            "Authorization": f"Token {settings.DEEPGRAM_API_KEY}",
            "Content-Type": content_type,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                _DEEPGRAM_URL,
                params=params,
                headers=headers,
                content=audio_bytes,
            )
            resp.raise_for_status()
            return resp.json()

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            data = await _post_to_deepgram(ct)

            transcript: str = (
                data.get("results", {})
                .get("channels", [{}])[0]
                .get("alternatives", [{}])[0]
                .get("transcript", "")
                .strip()
            )

            logger.debug(
                "Deepgram transcript attempt=%d (%d bytes, %s): %r",
                attempt + 1, len(audio_bytes), ct, transcript[:100],
            )
            return transcript

        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "Deepgram attempt %d/%d failed (%s: %s) — retrying in %.1fs",
                    attempt + 1, _MAX_RETRIES + 1, type(exc).__name__, exc, _RETRY_DELAY,
                )
                await asyncio.sleep(_RETRY_DELAY)
                continue
            raise RuntimeError(
                f"Deepgram transcription failed after {_MAX_RETRIES + 1} attempts: {exc}"
            ) from exc

        except httpx.HTTPStatusError as exc:
            body = exc.response.text
            logger.error(
                "Deepgram HTTP error %s: %s", exc.response.status_code, body[:200]
            )
            # 400 "corrupt or unsupported data" → retry once with auto-detect
            if (
                exc.response.status_code == 400
                and "corrupt" in body.lower()
                and ct != "application/octet-stream"
            ):
                logger.warning(
                    "Deepgram rejected format %r as corrupt — retrying with auto-detect "
                    "(application/octet-stream, %d bytes)",
                    ct, len(audio_bytes),
                )
                ct = "application/octet-stream"
                await asyncio.sleep(_RETRY_DELAY)
                continue
            raise

    # Should not reach here
    raise RuntimeError(f"Deepgram transcription failed: {last_exc}")

