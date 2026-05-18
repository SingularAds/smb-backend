"""Notification Service

Sends SMS notifications via Twilio to:
- Business owner  → booking/complaint/spam alerts
- Customer        → booking confirmation


Temporary: can also send via port 3002 endpoint for testing.
"""

import logging
import asyncio
import re
import urllib.parse
import httpx
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

from app.config import settings

logger = logging.getLogger(__name__)


class NotificationService:
    """Sends SMS notifications using the Twilio REST API"""

    def __init__(self):
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        return self._client

    # ── Internal helper ─────────────────────────────────────────────────────

    @staticmethod
    def _wa_footer(business_phone: str) -> str:
        """Build a WhatsApp deep-link footer to append to customer SMS."""
        if not business_phone:
            return ""
        digits = re.sub(r"[^\d]", "", str(business_phone))
        if not digits:
            return ""
        encoded_text = urllib.parse.quote("I need help regarding my booking")
        link = f"https://wa.me/{digits}?text={encoded_text}"
        return f"\n\n💬 Need help? Connect with us on WhatsApp:\n{link}"

    @staticmethod
    def _ensure_e164(phone: str) -> str:
        """Ensure phone has a leading '+'. E.g. '919434800080' → '+919434800080'."""
        if not phone:
            return phone
        phone = phone.strip()
        if not phone.startswith("+"):
            phone = "+" + phone
        return phone

    async def _send_via_port_3002(self, to: str, body: str) -> bool:
        """Temporary: Send SMS via port 3002 endpoint (testing only)."""
        to = self._ensure_e164(to)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "http://localhost:3002/send/sms",
                    json={"phone": to, "message": body},
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                print(f"[Notifications] SMS sent via port 3002 → {to}")
                return True
        except Exception as e:
            print(f"[Notifications] Port 3002 SMS failed for {to}: {e}")
            return False

    def _send(self, to: str, body: str, from_: str | None = None) -> bool:
        """Low-level send. Returns True on success.
        
        If SMS_GATEWAY_PORT_3002 is enabled, sends via port 3002 (testing).
        Otherwise, sends via Twilio.
        """

        to = self._ensure_e164(to)
        
        # Check if we should use port 3002 for testing
        if settings.SMS_GATEWAY_PORT_3002:
            try:
                # Run async function in sync context
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(self._send_via_port_3002(to, body))
                loop.close()
                return result
            except Exception as e:
                print(f"[Notifications] Port 3002 fallback error: {e}")
                # Fall through to Twilio as backup

        sender = from_ or settings.TWILIO_FROM_NUMBER

        if not sender:
            logger.warning("[Notifications] TWILIO_FROM_NUMBER not configured — skipping SMS")
            return False

        # Basic validation (helps avoid Twilio errors)
        if not to.startswith("+"):
            print(f"[Notifications] Invalid phone number format: {to}")
            return False

        try:
            msg = self.client.messages.create(to=to, from_=sender, body=body)
            logger.info("[Notifications] SMS sent → %s (SID: %s)", to, msg.sid)
            return True

        except TwilioRestException as e:
            logger.error("[Notifications] SMS failed to %s: %s", to, e)
            return False

        except Exception as e:
            print(f"[Notifications] Unexpected error sending SMS to {to}: {e}")
            return False
    # ── Owner notifications ──────────────────────────────────────────────────

    def notify_owner_new_booking(
        self,
        owner_phone: str,
        customer_name: str,
        customer_phone: str,
        service_name: str,
        booking_datetime: str,
        is_new_customer: bool = False,
    ) -> bool:
        tag = "🆕 New" if is_new_customer else "🔄 Returning"
        body = (
            f"📅 New booking!\n"
            f"{tag} customer: {customer_name}\n"
            f"{service_name} on {booking_datetime}\n"
            f"📞 {customer_phone}"
        )
        return self._send(owner_phone, body)

    def notify_owner_complaint(
        self,
        owner_phone: str,
        customer_name: str,
        customer_phone: str,
        complaint_text: str,
        category: str = "general",
    ) -> bool:
        body = (
            f"⚠️ Feedback from {customer_name}\n"
            f"📞 {customer_phone}\n\n"
            f"{complaint_text[:150]}"
            + ("..." if len(complaint_text) > 150 else "")
        )
        return self._send(owner_phone, body)

    def notify_owner_spam(
        self,
        owner_phone: str,
        caller_phone: str,
        reason: str = "",
    ) -> bool:
        body = (
            f"🚫 Spam call detected\n"
            f"Number: {caller_phone}\n"
            + (f"Reason: {reason}" if reason else "")
        )
        return self._send(owner_phone, body)

    # ── Customer notifications ───────────────────────────────────────────────

    def confirm_booking_to_customer(
        self,
        customer_phone: str,
        customer_name: str,
        business_name: str,
        service_name: str,
        booking_datetime: str,
        language: str = "en",
        business_phone: str = "",
    ) -> bool:
        messages = {
            "pt": (
                f"✅ Sua marcação foi confirmada!\n"
                f"📍 {business_name}\n"
                f"📅 {service_name} on {booking_datetime}\n"
                f"Obrigado!"
            ),
            "es": (
                f"✅ ¡Tu cita está confirmada!\n"
                f"📍 {business_name}\n"
                f"📅 {service_name} on {booking_datetime}\n"
                f"¡Gracias!"
            ),
            "fr": (
                f"✅ Votre rendez-vous est confirmé!\n"
                f"📍 {business_name}\n"
                f"📅 {service_name} on {booking_datetime}\n"
                f"Merci!"
            ),
            "en": (
                f"✅ Your booking is confirmed!\n"
                f"📍 {business_name}\n"
                f"📅 {service_name} on {booking_datetime}"
            ),
        }
        body = messages.get(language, messages["en"]) + self._wa_footer(business_phone)
        return self._send(customer_phone, body)

    def acknowledge_complaint_to_customer(
        self,
        customer_phone: str,
        customer_name: str,
        business_name: str,
        language: str = "en",
    ) -> bool:
        messages = {
            "pt": (
                f"Olá {customer_name},\n"
                f"Recebemos o seu feedback e vamos analisá-lo.\n"
                f"Obrigado por nos contactar — {business_name}"
            ),
            "en": (
                f"Hi {customer_name},\n"
                f"We've received your feedback and will look into it.\n"
                f"Thank you — {business_name}"
            ),
        }
        body = messages.get(language, messages["en"])
        return self._send(customer_phone, body)

    def notify_owner_reschedule(
        self,
        owner_phone: str,
        customer_name: str,
        customer_phone: str,
        service_name: str,
        old_datetime: str,
        new_datetime: str,
    ) -> bool:
        body = (
            f"🔁 Booking rescheduled\n"
            f"{customer_name} moved their {service_name}\n"
            f"From: {old_datetime}\n"
            f"To: {new_datetime}\n"
            f"📞 {customer_phone}"
        )
        return self._send(owner_phone, body)

    def notify_customer_reschedule(
        self,
        customer_phone: str,
        customer_name: str,
        business_name: str,
        service_name: str,
        new_datetime: str,
        language: str = "en",
        business_phone: str = "",
    ) -> bool:
        msgs = {
            "pt": (
                f"🔁 Sua marcação foi remarcada!\n"
                f"📍 {business_name}\n"
                f"📅 {service_name} on {new_datetime}\n"
                f"Obrigado!"
            ),
            "es": (
                f"🔁 ¡Tu cita fue reprogramada!\n"
                f"📍 {business_name}\n"
                f"📅 {service_name} on {new_datetime}\n"
                f"¡Gracias!"
            ),
            "en": (
                f"🔁 Your appointment was rescheduled!\n"
                f"📍 {business_name}\n"
                f"📅 {service_name} on {new_datetime}"
            ),
        }
        body = msgs.get(language, msgs["en"]) + self._wa_footer(business_phone)
        return self._send(customer_phone, body)

    def notify_owner_cancellation(
        self,
        owner_phone: str,
        customer_name: str,
        customer_phone: str,
        service_name: str,
        booking_datetime: str,
    ) -> bool:
        body = (
            f"❌ Booking cancelled\n"
            f"{customer_name} cancelled their {service_name}\n"
            f"Was scheduled: {booking_datetime}\n"
            f"📞 {customer_phone}"
        )
        return self._send(owner_phone, body)

    def notify_customer_cancellation(
        self,
        customer_phone: str,
        customer_name: str,
        business_name: str,
        service_name: str,
        booking_datetime: str,
        language: str = "en",
        business_phone: str = "",
    ) -> bool:
        msgs = {
            "pt": (
                f"❌ Sua marcação foi cancelada\n"
                f"📍 {business_name}\n"
                f"📅 {service_name} on {booking_datetime}\n"
                f"Se mudar de ideias, nos contacte!"
            ),
            "es": (
                f"❌ Tu cita fue cancelada\n"
                f"📍 {business_name}\n"
                f"📅 {service_name} on {booking_datetime}\n"
                f"¡Si cambias de opinión, contáctanos!"
            ),
            "en": (
                f"❌ Your appointment was cancelled\n"
                f"📍 {business_name}\n"
                f"📅 {service_name} on {booking_datetime}\n"
                f"Feel free to rebook anytime!"
            ),
        }
        body = msgs.get(language, msgs["en"]) + self._wa_footer(business_phone)
        return self._send(customer_phone, body)

    # ── Reminder notifications ───────────────────────────────────────────────

    # Maps business type keywords → service line emoji
    _SERVICE_EMOJI: dict[str, str] = {
        "restaurant": "🍽️",
        "cafe": "☕",
        "coffee": "☕",
        "pizza": "🍕",
        "bakery": "🥐",
        "bar": "🍻",
        "salon": "💅",
        "barbershop": "💈",
        "barber": "💈",
        "spa": "💆",
        "massage": "💆",
        "beauty": "💄",
        "nail": "💅",
        "clinic": "🏥",
        "doctor": "🩺",
        "dental": "🦷",
        "dentist": "🦷",
        "vet": "🐾",
        "gym": "🏋️",
        "fitness": "🏋️",
        "yoga": "🧘",
        "pilates": "🧘",
        "hotel": "🏨",
        "store": "🛍️",
        "shop": "🛍️",
        "auto": "🚗",
        "car": "🚗",
        "mechanic": "🔧",
        "tutor": "📚",
        "class": "📚",
        "studio": "🎨",
        "photo": "📸",
    }

    def _service_emoji(self, business_type: str, business_name: str) -> str:
        """Pick an emoji for the service line based on business type/name."""
        haystack = (business_type + " " + business_name).lower()
        for keyword, icon in self._SERVICE_EMOJI.items():
            if keyword in haystack:
                return icon
        return "📋"  # generic fallback

    def send_booking_reminder(
        self,
        customer_phone: str,
        customer_name: str,
        business_name: str,
        service_name: str,
        booking_datetime: str,
        days_until: int,
        language: str = "en",
        business_type: str = "",
        business_phone: str = "",
    ) -> bool:
        """Send a friendly booking reminder to a customer.

        days_until: 0 = today, 1 = tomorrow, 2 = in 2 days, 3 = in 3 days
        """
        svc_emoji = self._service_emoji(business_type, business_name)
        if days_until == 0:
            timing = {
                "en": "is TODAY",
                "pt": "é HOJE",
                "es": "es HOY",
                "fr": "est AUJOURD'HUI",
            }
            emoji = "🌟"
        elif days_until == 1:
            timing = {
                "en": "is TOMORROW",
                "pt": "é AMANHÃ",
                "es": "es MAÑANA",
                "fr": "est DEMAIN",
            }
            emoji = "⏰"
        else:
            timing = {
                "en": f"is in {days_until} days",
                "pt": f"é em {days_until} dias",
                "es": f"es en {days_until} días",
                "fr": f"est dans {days_until} jours",
            }
            emoji = "📅"

        lang = language if language in timing else "en"
        msgs = {
            "pt": (
                f"{emoji} Olá {customer_name}! Lembrete amigável:\n"
                f"A sua marcação {timing[lang]}!\n"
                f"📍 {business_name}\n"
                f"{svc_emoji} {service_name}\n"
                f"🕐 {booking_datetime}\n"
                f"Esperamos vê-lo(a) em breve! 😊"
            ),
            "es": (
                f"{emoji} ¡Hola {customer_name}! Recordatorio amistoso:\n"
                f"¡Tu cita {timing[lang]}!\n"
                f"📍 {business_name}\n"
                f"{svc_emoji} {service_name}\n"
                f"🕐 {booking_datetime}\n"
                f"¡Nos vemos pronto! 😊"
            ),
            "fr": (
                f"{emoji} Bonjour {customer_name}! Petit rappel :\n"
                f"Votre rendez-vous {timing[lang]} !\n"
                f"📍 {business_name}\n"
                f"{svc_emoji} {service_name}\n"
                f"🕐 {booking_datetime}\n"
                f"À très bientôt ! 😊"
            ),
            "en": (
                f"{emoji} Hey {customer_name}! Friendly reminder:\n"
                f"Your appointment {timing[lang]}!\n"
                f"📍 {business_name}\n"
                f"{svc_emoji} {service_name}\n"
                f"🕐 {booking_datetime}\n"
                f"See you soon! 😊"
            ),
        }
        body = msgs.get(lang, msgs["en"]) + self._wa_footer(business_phone)
        return self._send(customer_phone, body)


notifications = NotificationService()
