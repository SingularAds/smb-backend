"""WhatsApp Bridge Client — HTTP wrapper for the whatsmeow-bridge API.

All outgoing WhatsApp communication flows through this client.
The bridge runs as a Go binary on the server (port 3020 / reverse-proxied).
"""

import asyncio
import base64
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class PairingStateConflict(Exception):
    """Bridge rejected pairing because the session already has linked-device auth."""

    def __init__(self, session_id: str, status: str, phone: str = "", action: str = "reconnect"):
        self.session_id = session_id
        self.status = status
        self.phone = phone
        self.action = action
        super().__init__(
            f"session {session_id!r} is already paired"
            + (f" to {phone}" if phone else "")
            + f"; action={action} status={status}"
        )

# ── Outbound-echo suppression ─────────────────────────────────────────────────
# The whatsmeow bridge echoes every message we send back as a webhook event.
# We register the message_id returned by the bridge immediately after each send.
# The webhook handler calls is_our_outbound_echo() to silently drop those echoes
# before they re-enter the processing pipeline and create a feedback loop.
_SENT_TTL_S: int = 120      # seconds to remember a sent message_id
_sent_ids: dict[str, float] = {}   # message_id -> expiry monotonic time


def _register_sent_id(message_id: str) -> None:
    """Record a message_id that we just sent (bridge will echo it back)."""
    if not message_id:
        return
    now = time.monotonic()
    expired = [k for k, exp in list(_sent_ids.items()) if now > exp]
    for k in expired:
        del _sent_ids[k]
    _sent_ids[message_id] = now + _SENT_TTL_S


def is_our_outbound_echo(message_id: str) -> bool:
    """Return True if this message_id belongs to a message we sent (bridge echo)."""
    return bool(message_id) and message_id in _sent_ids


def _phone_to_jid(phone: str) -> str:
    """Convert a phone number to WhatsApp JID format.

    If the input already contains an ``@`` domain suffix it is returned
    as-is so that privacy-protected contacts (``@lid``) and group chats
    (``@g.us``) are addressed correctly by the bridge.  Only strip the
    leading ``+`` / spaces / dashes from raw digit-only inputs and append
    the default ``@s.whatsapp.net`` server.
    """
    if "@" in phone:
        # Already a full JID — preserve the server domain (@lid, @s.whatsapp.net…)
        return phone
    clean = phone.lstrip("+").replace(" ", "").replace("-", "")
    return f"{clean}@s.whatsapp.net"


class WhatsmeowClient:
    """Thin async HTTP client for whatsmeow-bridge endpoints."""

    def __init__(self):
        self.base_url = settings.WHATSMEOW_API_BASE_URL.rstrip("/")
        self.auth = (settings.WHATSMEOW_API_USERNAME, settings.WHATSMEOW_API_PASSWORD)
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            # Keep existing local-dev behavior while preventing accidental insecure
        # transport in production.
        self._verify_tls = not (
            settings.ALLOW_INSECURE_TRANSPORT and settings.ENVIRONMENT.lower() != "production"
        )
        self.default_device_id = (
            settings.WHATSMEOW_ONBOARDING_DEVICE_ID
            or settings.WHATSMEOW_DEFAULT_DEVICE_ID
        )

    # ── core helpers ──────────────────────────────────────────────────────

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            auth=self.auth,
            timeout=timeout,
            verify=self._verify_tls,
        )

    # ── public API ────────────────────────────────────────────────────────

    async def send_message(
        self,
        phone: str,
        message: str,
        device_id: str | None = None,
    ) -> dict:
        """Send a single text message via the bridge."""
        device = device_id or self.default_device_id
        jid = _phone_to_jid(phone)

        # Log outgoing message for easier debugging / visibility
        try:
            logger.debug("→ WhatsApp OUT to %s (device=%s): %s", jid, device, message)
        except Exception:
            # Fallback: ensure we never crash logging
            logger.exception("→ WhatsApp OUT (logging failed)")

        async with self._client() as client:
            try:
                resp = await client.post(
                    "/send/message",
                    json={"phone": jid, "message": message},
                    headers={"X-Device-Id": device},
                )
                print("message is this: ", message)
                logger.debug("WhatsApp bridge response status: %s", resp.status_code)
                logger.debug("WhatsApp bridge response text: %s", (resp.text or '')[:200])
                resp.raise_for_status()
                resp_data = resp.json()
                # Register the sent message_id so echoes from the bridge are ignored
                _register_sent_id(resp_data.get("message_id", ""))
                logger.debug("Message sent to %s via device %s", phone, device)
                return resp_data
            except httpx.ConnectError as exc:
                logger.error(
                    "WhatsApp bridge unreachable for %s (device=%s): %s",
                    phone, device, exc,
                )
                raise
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "WhatsApp bridge returned %s for %s (device=%s): %s",
                    exc.response.status_code, phone, device, exc.response.text[:200],
                )
                raise

    async def send_messages(
        self,
        phone: str,
        messages: list[str],
        device_id: str | None = None,
        delay: float = 2.0,
    ) -> None:
        """Send multiple messages with a delay between each (anti-spam)."""
        for i, msg in enumerate(messages):
            if i > 0:
                await asyncio.sleep(delay)
            await self.send_message(phone, msg, device_id)

    async def download_media(self, url: str) -> tuple[bytes, str]:
        """Download media from a URL.

        Returns ``(raw_bytes, mime_type)`` where *mime_type* is taken from the
        response ``Content-Type`` header (more reliable than the webhook
        payload value).

        Handles both bridge-hosted URLs (uses bridge auth) and external CDN
        URLs (no auth required).  TLS verification is disabled globally.
        Retries up to 3 times on network / DNS failures (errno 11001 on
        Windows is transient).
        """
        bridge_host = self.base_url.split("//")[-1].split("/")[0]
        url_host = url.split("//")[-1].split("/")[0] if "://" in url else ""
        use_auth = (url_host == bridge_host)

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    auth=self.auth if use_auth else None,
                    verify=self._verify_tls,
                    timeout=60.0,
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    content = resp.content
                    if not content:
                        raise ValueError("Empty response body downloading media")
                    # Prefer Content-Type from response headers
                    ct_header = resp.headers.get("content-type", "")
                    mime = ct_header.split(";")[0].strip() or "audio/ogg"
                    logger.debug(
                        "[Media] Downloaded %d bytes (Content-Type: %r) attempt=%d url=%s",
                        len(content), ct_header[:80], attempt + 1, url[:80],
                    )
                    return content, mime
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < 2:
                    logger.warning(
                        "[Media] Download attempt %d/3 failed (%s) — retrying in 2s: %s",
                        attempt + 1, type(exc).__name__, exc,
                    )
                    await asyncio.sleep(2.0)
                else:
                    raise
        raise last_exc  # type: ignore[misc]  — unreachable

    async def send_audio(
        self,
        phone: str,
        audio_bytes: bytes,
        device_id: str | None = None,
        mime_type: str = "audio/mpeg",
        ptt: bool = True,
    ) -> dict:
        """Send an audio file as a WhatsApp audio message via the bridge.

        The bridge endpoint ``POST /send/audio`` expects a JSON body with
        Base64-encoded audio.  Setting ``ptt=True`` makes it appear as a
        voice note bubble in WhatsApp (works with both OGG/opus and MP3).
        """
        device = device_id or self.default_device_id
        jid = _phone_to_jid(phone)
        audio_b64 = base64.b64encode(audio_bytes).decode()

        try:
            logger.debug(
                "→ WhatsApp AUDIO to %s (device=%s, %d bytes, mime=%s)",
                jid,
                device,
                len(audio_bytes),
                mime_type,
            )
        except Exception:
            logger.exception("→ WhatsApp AUDIO (logging failed)")

        async with self._client(timeout=60.0) as client:
            try:
                resp = await client.post(
                "/send/message",
                json={
                    "phone": jid,
                    "type": "audio",
                    "audio": {
                        "data": audio_b64,
                        "mimetype": "audio/ogg; codecs=opus"
                    },
                    "ptt": True
                },
                headers={"X-Device-Id": device},
            )
                logger.debug("Audio payload size: %d (base64 len: %d)", len(audio_bytes), len(audio_b64))
                logger.debug("Audio send response status: %s", resp.status_code)
                logger.debug("Audio send response text: %s", (resp.text or '')[:200])
                resp.raise_for_status()
                # Print delivery outcome for quick visibility (success)
                try:
                    result = resp.json()
                except Exception:
                    result = {"status_code": resp.status_code, "text": (resp.text or "")[:200]}
                print(f"[AUDIO] Sent audio to {phone} (device={device}) status={resp.status_code} success=True")
                return result
            except httpx.ConnectError as exc:
                logger.error(
                    "WhatsApp bridge unreachable for audio to %s (device=%s): %s",
                    phone, device, exc,
                )
                # Print failure for quick visibility
                print(f"[AUDIO] Failed to send audio to {phone} (device={device}): ConnectError: {exc}")
                raise
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "WhatsApp bridge returned %s for audio to %s (device=%s): %s",
                    exc.response.status_code, phone, device, exc.response.text[:200],
                )
                # Print failure for quick visibility
                print(f"[AUDIO] Failed to send audio to {phone} (device={device}): HTTP {exc.response.status_code} {exc.response.text[:200]}")
                raise

    async def generate_pair_code(
        self,
        session_id: str,
        phone_number: str,
    ) -> dict:
        """Request a pairing code from the bridge for linking a new device.

        Returns ``{"code": "XXXX-XXXX", "sessionId": "..."}``
        """
        async with self._client() as client:
            resp = await client.post(
                "/api/pair-code",
                json={
                    "sessionId": session_id,
                    "phoneNumber": phone_number,
                },
            )
            if resp.status_code == 409:
                data = resp.json()
                raise PairingStateConflict(
                    session_id=data.get("sessionId", session_id),
                    status=data.get("status", "disconnected"),
                    phone=data.get("phone", ""),
                    action=data.get("action", "reconnect"),
                )
            resp.raise_for_status()
            data = resp.json()
            logger.info("Pair code generated for session %s", session_id)
            return data

    async def get_session_status(self, session_id: str) -> dict:
        """Check connection status of a specific session."""
        async with self._client(timeout=15.0) as client:
            resp = await client.get(f"/api/sessions/{session_id}")
            resp.raise_for_status()
            return resp.json()

    async def reconnect_session(self, session_id: str) -> dict:
        """Trigger a reconnect attempt for an already-paired bridge session."""
        async with self._client(timeout=15.0) as client:
            resp = await client.post(f"/api/sessions/{session_id}/reconnect")
            resp.raise_for_status()
            data = resp.json()
            logger.info("Reconnect requested for session %s", session_id)
            return data

    async def logout_session(self, session_id: str) -> dict:
        """Fully unlink a bridge session (clears stored credentials).

        After this call the session status becomes ``needs_pairing`` and a full
        pair-code flow is required to reconnect.  Calling this on a session that
        does not exist or is already unpaired is safe — the bridge returns 200.
        """
        async with self._client(timeout=15.0) as client:
            resp = await client.post(f"/api/sessions/{session_id}/logout")
            resp.raise_for_status()
            data = resp.json()
            logger.info("Logout requested for session %s (status=%s)", session_id, data.get("status"))
            return data

    async def health_check(self) -> dict:
        """Ping the bridge health endpoint (no auth required)."""
        async with self._client(timeout=10.0) as client:
            resp = await client.get("/api/health")
            resp.raise_for_status()
            return resp.json()

    # ── QR-code pairing flow ──────────────────────────────────────────────

    async def get_qr_payload(
        self,
        session_id: str,
        timeout_seconds: int = 15,
    ) -> dict:
        """Start (or reuse) a QR session and block until the first QR payload
        is available.

        The returned ``qr_payload`` is the raw string that must be converted
        into a QR image (PNG) and sent to the user for scanning.

        Returns ``{"qr_payload": "...", "sessionId": "..."}`` on success.
        Raises ``PairingStateConflict`` when the session is already paired.
        """
        # Add extra headroom beyond the bridge-side timeout so the HTTP
        # client does not time out before the bridge responds.
        http_timeout = float(timeout_seconds) + 10.0
        async with self._client(timeout=http_timeout) as client:
            resp = await client.post(
                "/api/qr-payload",
                json={"sessionId": session_id, "timeoutSeconds": timeout_seconds},
            )
            if resp.status_code == 409:
                data = resp.json()
                raise PairingStateConflict(
                    session_id=data.get("sessionId", session_id),
                    status=data.get("status", "disconnected"),
                    phone=data.get("phone", ""),
                    action=data.get("action", "reconnect"),
                )
            resp.raise_for_status()
            data = resp.json()
            logger.info("QR payload received for session %s", session_id)
            return data

    async def get_qr_current(self, session_id: str) -> dict | None:
        """Fetch the latest QR payload for an active QR session without blocking.

        Used to refresh a QR code after the previous one expires (~20 s).
        Returns the response dict when a payload is available, or ``None``
        when the bridge returns 404 (no active QR flow for this session).
        """
        async with self._client(timeout=10.0) as client:
            resp = await client.get(f"/api/qr-current/{session_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    # ── Image sending ─────────────────────────────────────────────────────

    async def send_image(
        self,
        phone: str,
        image_bytes: bytes,
        caption: str = "",
        mime_type: str = "image/png",
        device_id: str | None = None,
    ) -> dict:
        """Send an image (PNG or JPEG) as a WhatsApp image message via the bridge.

        The bridge endpoint ``POST /send/message`` with ``type=image`` accepts
        a JSON body with Base64-encoded image data.

        Args:
            phone: Recipient phone number or full WhatsApp JID.
            image_bytes: Raw image bytes (PNG or JPEG).
            caption: Optional caption text displayed below the image.
            mime_type: MIME type of the image; defaults to ``"image/png"``.
            device_id: Bridge session ID; falls back to the default device.
        """
        device = device_id or self.default_device_id
        jid = _phone_to_jid(phone)
        image_b64 = base64.b64encode(image_bytes).decode()

        try:
            logger.debug(
                "→ WhatsApp IMAGE to %s (device=%s, %d bytes, caption=%r)",
                jid, device, len(image_bytes), caption[:40] if caption else "",
            )
        except Exception:
            logger.exception("→ WhatsApp IMAGE (logging failed)")

        async with self._client(timeout=60.0) as client:
            try:
                resp = await client.post(
                    "/send/message",
                    json={
                        "phone": jid,
                        "type": "image",
                        "image": {
                            "data": image_b64,
                            "mimetype": mime_type,
                            "caption": caption,
                        },
                    },
                    headers={"X-Device-Id": device},
                )
                resp.raise_for_status()
                result = resp.json()
                _register_sent_id(result.get("message_id", ""))
                logger.debug("Image sent to %s via device %s", phone, device)
                return result
            except httpx.ConnectError as exc:
                logger.error(
                    "WhatsApp bridge unreachable for image to %s (device=%s): %s",
                    phone, device, exc,
                )
                raise
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "WhatsApp bridge returned %s for image to %s (device=%s): %s",
                    exc.response.status_code, phone, device, exc.response.text[:200],
                )
                raise
