"""Onboarding Service — Entry B State Machine.

Guides business owners through the complete onboarding flow via
WhatsApp conversation.  Messages arrive from the whatsmeow webhook,
are routed to the correct step handler, and replies are sent back
through ``WhatsmeowClient``.

State flow
----------
awaiting_website → (scraped OK) → awaiting_confirm
                 → (no website)  → awaiting_business_name → awaiting_services → awaiting_confirm
awaiting_confirm → (yes)         → awaiting_pairing_done → awaiting_calendar → awaiting_forwarding → complete
                 → (fix/restart) → awaiting_website
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.config import settings
from app import firestore as db
from app.services.whatsmeow_client import WhatsmeowClient
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)


# ── intent detection helpers ──────────────────────────────────────────────────

def _norm(text: str) -> str:
    return text.strip().lower()


def _is_yes(text: str) -> bool:
    return _norm(text) in {
        "yes", "y", "sim", "si", "sí", "oui", "ja",
        "ok", "okay", "confirmo", "confirmar", "confirm",
    }


def _is_fix(text: str) -> bool:
    return _norm(text) in {
        "fix", "edit", "change", "corrigir", "editar",
        "alterar", "mudar", "modificar",
    }


def _is_restart(text: str) -> bool:
    return _norm(text) in {
        "restart", "start over", "recomeçar", "reiniciar",
        "começar de novo", "reset",
    }


def _is_skip(text: str) -> bool:
    return _norm(text) in {
        "skip", "pular", "saltar", "pass", "later",
        "depois", "más tarde",
    }


def _is_done(text: str) -> bool:
    return _norm(text) in {
        "done", "pronto", "feito", "hecho", "fait",
        "ready", "terminei", "concluído", "listo",
    }


def _is_help(text: str) -> bool:
    return _norm(text) in {"help", "ajuda", "ayuda", "aide", "socorro"}


def _is_new_code(text: str) -> bool:
    t = _norm(text)
    return t in {
        "new code", "novo código", "nuevo código",
        "novo codigo", "new", "código novo", "codigo novo",
    }


def _is_no_website(text: str) -> bool:
    t = _norm(text)
    keywords = [
        "no website", "sem site", "sin sitio", "no site",
        "no web", "sem website", "no tengo web", "pas de site",
    ]
    return any(kw in t for kw in keywords)


def _looks_like_url(text: str) -> bool:
    t = text.strip()
    if "." not in t:
        return False
    if " " in t and not t.lower().startswith("http"):
        return False
    parts = t.split(".")
    if len(parts) < 2:
        return False
    tld = parts[-1].lower().split("/")[0].split("?")[0]
    return 2 <= len(tld) <= 10


# ── bilingual message templates ──────────────────────────────────────────────

_M: dict[str, dict[str, str]] = {
    "welcome": {
        "en": (
            "👋 Welcome to Recepte222!\n"
            "I'm your AI receptionist11 — I answer calls and WhatsApp 24/7.\n"
            "Let's set you up in 2 minutes.\n\n"
            "What is your business website URL?\n"
            "Ex: www.mysalon.pt\n\n"
            "(No website? Send *no website*)"
        ),
        "pt": (
            "👋 Bem-vindo ao Recepte!\n"
            "Sou a sua recepcionista IA — respondo chamadas e WhatsApp 24/7.\n"
            "Vamos configurar tudo em 2 minutos.\n\n"
            "Qual é o site do seu negócio?\n"
            "Ex: www.meusalao.pt\n\n"
            "(Sem site? Envie *sem site*)"
        ),
        "es": (
            "👋 ¡Bienvenido a Recepte!\n"
            "Soy tu recepcionista IA — respondo llamadas y WhatsApp 24/7.\n"
            "Te configuro en 2 minutos.\n\n"
            "¿Cuál es la web de tu negocio?\n"
            "Ej: www.misalon.es\n\n"
            "(¿Sin web? Envía *sin web*)"
        ),
    },
    "gdpr": {
        "en": "🇪🇺 You'll see 'logged in from Germany' — that's our secure EU server. Your data stays in Europe.",
        "pt": "🇪🇺 Verá 'logged in from Germany' — é o nosso servidor seguro na UE. Os seus dados ficam na Europa.",
        "es": "🇪🇺 Verás 'logged in from Germany' — es nuestro servidor seguro en la UE. Tus datos se quedan en Europa.",
    },
    "ask_name": {
        "en": "No problem! 😊\nWhat is your business name?",
        "pt": "Sem problema! 😊\nQual é o nome do seu negócio?",
        "es": "¡Sin problema! 😊\n¿Cuál es el nombre de tu negocio?",
    },
    "ask_services": {
        "en": "What services do you offer?\nExample: *Haircut 30min €15, Color 90min €45*",
        "pt": "Que serviços oferece?\nExemplo: *Corte 30min €15, Coloração 90min €45*",
        "es": "¿Qué servicios ofreces?\nEjemplo: *Corte 30min €15, Color 90min €45*",
    },
    "scrape_fail": {
        "en": "I couldn't access your website. Let me set you up manually.\nWhat is your business name?",
        "pt": "Não consegui aceder ao seu site. Vou configurar manualmente.\nQual é o nome do seu negócio?",
        "es": "No pude acceder a tu web. Te configuro manualmente.\n¿Cuál es el nombre de tu negocio?",
    },
    "looking_up": {
        "en": "👋 Looking up your business...",
        "pt": "👋 A procurar o seu negócio...",
        "es": "👋 Buscando tu negocio...",
    },
    "retry_website": {
        "en": "Please send your website URL (e.g. www.mybusiness.com) or type *no website*",
        "pt": "Por favor envie o URL do seu site (ex: www.meunegocio.pt) ou escreva *sem site*",
        "es": "Por favor envía tu URL (ej: www.minegocio.es) o escribe *sin web*",
    },
    "confirm_prompt": {
        "en": "\nReply *yes* to confirm or *fix* to edit",
        "pt": "\nResponda *sim* para confirmar ou *corrigir* para editar",
        "es": "\nResponde *sí* para confirmar o *corregir* para editar",
    },
    "pairing_intro": {
        "en": (
            "🎉 {name} is live!\n\n"
            "Step 1/3 — Connect WhatsApp 💬\n"
            "Get your phone ready:\n"
            "1️⃣ Open WhatsApp\n"
            "2️⃣ Settings\n"
            "3️⃣ Linked Devices\n"
            "4️⃣ Link a Device\n"
            "5️⃣ Link with phone number instead\n"
            "⏳ Code in 3 seconds..."
        ),
        "pt": (
            "🎉 {name} está ativo!\n\n"
            "Passo 1/3 — Conectar WhatsApp 💬\n"
            "Prepare o seu telemóvel:\n"
            "1️⃣ Abra o WhatsApp\n"
            "2️⃣ Definições\n"
            "3️⃣ Dispositivos Ligados\n"
            "4️⃣ Ligar um Dispositivo\n"
            "5️⃣ Ligar com número de telefone\n"
            "⏳ Código em 3 segundos..."
        ),
        "es": (
            "🎉 ¡{name} está activo!\n\n"
            "Paso 1/3 — Conectar WhatsApp 💬\n"
            "Prepara tu teléfono:\n"
            "1️⃣ Abre WhatsApp\n"
            "2️⃣ Ajustes\n"
            "3️⃣ Dispositivos vinculados\n"
            "4️⃣ Vincular un dispositivo\n"
            "5️⃣ Vincular con número de teléfono\n"
            "⏳ Código en 3 segundos..."
        ),
    },
    "pairing_code_sent": {
        "en": (
            "Copy the code above ☝🏼 and paste it on the screen you opened\n"
            "⏱ 60 seconds\n"
            "Reply *done* when linked or *new code*"
        ),
        "pt": (
            "Copie o código acima ☝🏼 e cole no ecrã que abriu\n"
            "⏱ 60 segundos\n"
            "Responda *pronto* quando ligar ou *novo código*"
        ),
        "es": (
            "Copia el código de arriba ☝🏼 y pégalo en la pantalla que abriste\n"
            "⏱ 60 segundos\n"
            "Responde *listo* cuando vincules o *nuevo código*"
        ),
    },
    "pairing_success": {
        "en": "✅ WhatsApp connected!",
        "pt": "✅ WhatsApp conectado!",
        "es": "✅ ¡WhatsApp conectado!",
    },
    "pairing_skip": {
        "en": "👍 No problem — you can connect WhatsApp anytime later.",
        "pt": "👍 Sem problema — pode conectar o WhatsApp mais tarde.",
        "es": "👍 Sin problema — puedes conectar WhatsApp más tarde.",
    },
    "pairing_failed": {
        "en": "We'll skip WhatsApp pairing for now — you can connect it later.",
        "pt": "Vamos pular a ligação WhatsApp por agora — pode conectar mais tarde.",
        "es": "Saltamos la vinculación WhatsApp por ahora — puedes conectar más tarde.",
    },
    "calendar_step": {
        "en": (
            "Step 2/3 — Calendar 📅\n"
            "Tap to connect: {link}\n"
            "Reply *skip* if you don't use Google Calendar"
        ),
        "pt": (
            "Passo 2/3 — Agenda 📅\n"
            "Toque para conectar: {link}\n"
            "Responda *pular* se não usa Google Calendar"
        ),
        "es": (
            "Paso 2/3 — Calendario 📅\n"
            "Toca para conectar: {link}\n"
            "Responde *saltar* si no usas Google Calendar"
        ),
    },
    "calendar_skip": {
        "en": "👍 No problem — we'll continue without calendar. You can connect anytime later.",
        "pt": "👍 Sem problema — continuamos sem agenda. Pode conectar mais tarde.",
        "es": "👍 Sin problema — continuamos sin calendario. Puedes conectar más tarde.",
    },
    "forwarding_step": {
        "en": (
            "Step 3/3 — Missed calls 📞\n"
            "👉 Tap to activate: tel:**61*{phone}#\n"
            "Reply *done* or *help*"
        ),
        "pt": (
            "Passo 3/3 — Chamadas perdidas 📞\n"
            "👉 Toque para ativar: tel:**61*{phone}#\n"
            "Responda *pronto* ou *ajuda*"
        ),
        "es": (
            "Paso 3/3 — Llamadas perdidas 📞\n"
            "👉 Toca para activar: tel:**61*{phone}#\n"
            "Responde *listo* o *ayuda*"
        ),
    },
    "forwarding_help": {
        "en": (
            "📱 iPhone: Settings → Phone → Call Forwarding → When Unanswered\n"
            "📱 Android: Phone app → ⋮ → Settings → Call Forwarding → When Unanswered\n\n"
            "Enter the number: +{phone}\n\n"
            "Reply *done* when ready or *skip*"
        ),
        "pt": (
            "📱 iPhone: Definições → Telefone → Reencaminhar → Quando Não Atender\n"
            "📱 Android: App Telefone → ⋮ → Definições → Reencaminhar → Quando Não Atender\n\n"
            "Insira o número: +{phone}\n\n"
            "Responda *pronto* ou *pular*"
        ),
        "es": (
            "📱 iPhone: Ajustes → Teléfono → Desvío de llamadas → Si no contesta\n"
            "📱 Android: App Teléfono → ⋮ → Ajustes → Desvío → Si no contesta\n\n"
            "Ingresa el número: +{phone}\n\n"
            "Responde *listo* o *saltar*"
        ),
    },
    "complete": {
        "en": "🎉 All set! You won't miss a customer again 💪",
        "pt": "🎉 Tudo pronto! Nunca mais perderá um cliente 💪",
        "es": "🎉 ¡Todo listo! No perderás más clientes 💪",
    },
    "already_onboarded": {
        "en": "Your business *{name}* is already set up! 🎉\nNeed help? Reply *help*",
        "pt": "O seu negócio *{name}* já está configurado! 🎉\nPrecisa de ajuda? Responda *ajuda*",
        "es": "Tu negocio *{name}* ya está configurado! 🎉\n¿Necesitas ayuda? Responde *ayuda*",
    },
}


def _msg(key: str, lang: str, **kwargs: str) -> str:
    """Return a localised message, falling back to English."""
    templates = _M.get(key, {})
    tpl = templates.get(lang, templates.get("en", ""))
    if kwargs:
        tpl = tpl.format(**kwargs)
    return tpl


# ── summary formatter ────────────────────────────────────────────────────────

_SVC_LABELS = {"en": "services found", "pt": "serviços encontrados", "es": "servicios encontrados"}
_FOUND_LABELS = {"en": "Found", "pt": "Encontrei", "es": "Encontré"}


def _format_summary(discovery: dict, lang: str) -> str:
    name = discovery.get("name", "Your Business")
    desc = discovery.get("description", "")
    address = discovery.get("address", "")
    phone = discovery.get("phone", "")
    services = discovery.get("services", [])

    lines: list[str] = [
        f"✅ {_FOUND_LABELS.get(lang, 'Found')} {name}!",
    ]
    if desc:
        lines.append(desc)
    if address:
        lines.append(f"📍 {address}")
    if phone:
        lines.append(f"📞 {phone}")

    svc_label = _SVC_LABELS.get(lang, _SVC_LABELS["en"])
    lines.append(f"📋 {len(services)} {svc_label}")

    for svc in services[:8]:
        parts = [f"  • {svc.get('name', '')}"]
        dur = svc.get("duration", "")
        price = svc.get("price", "")
        if dur:
            parts.append(dur)
        if price:
            parts.append(price)
        lines.append(" — ".join(parts))

    lines.append(_msg("confirm_prompt", lang))
    return "\n".join(lines)


# ── currency / greeting helpers ──────────────────────────────────────────────

_CURRENCY = {"pt": "EUR", "es": "EUR", "en": "USD", "fr": "EUR", "de": "EUR"}
_GREETINGS = {
    "en": "Hello, thank you for contacting {name}! How can I help you today?",
    "pt": "Olá! Bem-vindo ao {name}. Como posso ajudá-lo hoje?",
    "es": "¡Hola! Bienvenido a {name}. ¿Cómo puedo ayudarte hoy?",
}


# ══════════════════════════════════════════════════════════════════════════════
#  Onboarding Service
# ══════════════════════════════════════════════════════════════════════════════


class OnboardingService:
    """State machine for Entry B onboarding.

    Each incoming WhatsApp message is routed to the handler matching
    the session's ``currentStep``.  Handlers advance the step, persist
    state in Firestore, and send replies via the whatsmeow bridge.
    """

    def __init__(self) -> None:
        self.wa = WhatsmeowClient()
        self.ai = AIService()

    # ── main entry point ──────────────────────────────────────────────────

    async def handle_message(
        self,
        phone: str,
        body: str,
        push_name: str,
        message_id: str,
    ) -> None:
        """Route an incoming WhatsApp message to the correct handler."""
        phone = db._clean_phone(phone)

        # 1. Check for existing onboarding session
        session = db.get_onboarding_session(phone)
        logger.debug("Step 1: got session %s", session)
        if session:
            # Dedup — skip already-processed message
            # if session.get("lastMessageId") == message_id:
            #     print("Duplicate message ID — skipping", message_id)
            #     logger.debug("Duplicate message %s for %s — skipping", message_id, phone)
            #     return

            # Already completed?
            if session.get("currentStep") == "complete":
                logger.info("Session already complete for %s", phone)
                biz = db.get_business_by_owner_phone(phone)
                if biz:
                    lang = session.get("language", "en")
                    await self._send(phone, _msg("already_onboarded", lang, name=biz.get("name", "")))
                    return

            # Resume current step
            logger.debug("Resuming session for %s", phone)
            await self._resume(session, phone, body, message_id)
            return

        # 2. Check if already a registered business owner
        existing_biz = db.get_business_by_owner_phone(phone)
        logger.debug("Step 2: got existing biz %s", existing_biz)
        if existing_biz:
            lang = self.ai.detect_language(phone)
            await self._send(phone, _msg("already_onboarded", lang, name=existing_biz.get("name", "")))
            return
        logger.debug("Step 2: no existing biz")
        # 3. Brand-new user → begin onboarding
        await self._start_new(phone, body, push_name, message_id)

    # ── session lifecycle ─────────────────────────────────────────────────

    async def _start_new(
        self, phone: str, body: str, push_name: str, message_id: str
    ) -> None:
        lang = self.ai.detect_language(phone)
        now = datetime.utcnow().isoformat()

        session_data = {
            "ownerPhone": phone,
            "pushName": push_name or "",
            "currentStep": "awaiting_website",
            "language": lang,
            "discovery": {
                "name": "",
                "website": "",
                "address": "",
                "phone": "",
                "businessType": "",
                "description": "",
                "services": [],
                "hours": "",
                "staff": [],
            },
            "pairingSessionId": None,
            "businessId": None,
            "lastMessageId": message_id,
            "timestamps": {
                "startedAt": now,
                "lastActivityAt": now,
            },
        }
        db.upsert_onboarding_session(phone, session_data)

        await self._send(phone, _msg("welcome", lang))
        await asyncio.sleep(1.5)
        await self._send(phone, _msg("gdpr", lang))

        logger.info("Onboarding started for %s (lang=%s, pushName=%s)", phone, lang, push_name)

    async def _resume(
        self, session: dict, phone: str, body: str, message_id: str
    ) -> None:
        # Stamp activity
        db.upsert_onboarding_session(phone, {
            "lastMessageId": message_id,
            "timestamps.lastActivityAt": datetime.utcnow().isoformat(),
        })

        step = session.get("currentStep", "awaiting_website")
        logger.debug("Onboarding resume step: %s", step)
        handler = {
            "awaiting_website": self._step_awaiting_website,
            "awaiting_business_name": self._step_awaiting_business_name,
            "awaiting_services": self._step_awaiting_services,
            "awaiting_confirm": self._step_awaiting_confirm,
            "awaiting_pairing_done": self._step_awaiting_pairing_done,
            "awaiting_calendar": self._step_awaiting_calendar,
            "awaiting_forwarding": self._step_awaiting_forwarding,
        }.get(step)

        if handler:
            logger.debug("Routing to handler for step %s", step)
            await handler(session, phone, body)
        else:
            logger.warning("Unknown onboarding step '%s' for %s", step, phone)

    # ── step handlers ─────────────────────────────────────────────────────

    async def _step_awaiting_website(
        self, session: dict, phone: str, body: str
    ) -> None:
        lang = session.get("language", "en")

        if _is_no_website(body):
            await self._send(phone, _msg("ask_name", lang))
            db.upsert_onboarding_session(phone, {"currentStep": "awaiting_business_name"})
            return

        if _looks_like_url(body):
            url = body.strip()
            await self._send(phone, _msg("looking_up", lang))

            extracted = await self.ai.scrape_website(url)

            if extracted and extracted.get("name"):
                discovery = {
                    "name": extracted.get("name", ""),
                    "website": url,
                    "address": extracted.get("address", ""),
                    "phone": extracted.get("phone", ""),
                    "businessType": extracted.get("businessType", "other"),
                    "description": extracted.get("description", ""),
                    "services": extracted.get("services", []),
                    "hours": extracted.get("hours", ""),
                    "staff": extracted.get("staff", []),
                }
                db.upsert_onboarding_session(phone, {
                    "discovery": discovery,
                    "currentStep": "awaiting_confirm",
                })
                await self._send(phone, _format_summary(discovery, lang))
            else:
                # Scraping failed → fall back to manual
                db.upsert_onboarding_session(phone, {
                    "discovery.website": url,
                    "currentStep": "awaiting_business_name",
                })
                await self._send(phone, _msg("scrape_fail", lang))
            return

        # Unrecognised input
        await self._send(phone, _msg("retry_website", lang))

    async def _step_awaiting_business_name(
        self, session: dict, phone: str, body: str
    ) -> None:
        lang = session.get("language", "en")
        name = body.strip()

        if len(name) < 2:
            await self._send(phone, _msg("ask_name", lang))
            return

        db.upsert_onboarding_session(phone, {
            "discovery.name": name,
            "currentStep": "awaiting_services",
        })
        await self._send(phone, _msg("ask_services", lang))

    async def _step_awaiting_services(
        self, session: dict, phone: str, body: str
    ) -> None:
        lang = session.get("language", "en")

        services = await self.ai.parse_services_text(body, lang)
        if not services:
            services = [{"name": body.strip(), "duration": "", "price": ""}]

        db.upsert_onboarding_session(phone, {
            "discovery.services": services,
            "currentStep": "awaiting_confirm",
        })

        # Re-fetch full session to build summary with all discovery fields
        updated = db.get_onboarding_session(phone)
        discovery = updated.get("discovery", {}) if updated else session.get("discovery", {})
        await self._send(phone, _format_summary(discovery, lang))

    async def _step_awaiting_confirm(
        self, session: dict, phone: str, body: str
    ) -> None:
        lang = session.get("language", "en")

        if _is_yes(body):
            await self._create_business_and_pair(session, phone)
            return

        if _is_fix(body):
            db.upsert_onboarding_session(phone, {"currentStep": "awaiting_website"})
            await self._send(phone, _msg("retry_website", lang))
            return

        if _is_restart(body):
            db.upsert_onboarding_session(phone, {
                "currentStep": "awaiting_website",
                "discovery": {
                    "name": "", "website": "", "address": "", "phone": "",
                    "businessType": "", "description": "", "services": [],
                    "hours": "", "staff": [],
                },
            })
            await self._send(phone, _msg("welcome", lang))
            return

        # Re-prompt
        await self._send(phone, _msg("confirm_prompt", lang))

    async def _step_awaiting_pairing_done(
        self, session: dict, phone: str, body: str
    ) -> None:
        lang = session.get("language", "en")

        if _is_done(body):
            business_id = session.get("businessId")
            pairing_sid = session.get("pairingSessionId")

            if business_id and pairing_sid:
                try:
                    db.update_business_doc(business_id, {
                        "waSessionId": pairing_sid,
                        "waPhoneNumber": phone,
                    })
                except Exception as exc:
                    logger.error("Failed to update business WA info: %s", exc)

            await self._send(phone, _msg("pairing_success", lang))
            await asyncio.sleep(1.5)
            await self._advance_to_calendar(session, phone)
            return

        if _is_new_code(body):
            await self._send_pairing_code(session, phone)
            return

        if _is_skip(body):
            await self._send(phone, _msg("pairing_skip", lang))
            await asyncio.sleep(1)
            await self._advance_to_calendar(session, phone)
            return

        # Re-prompt
        await self._send(phone, _msg("pairing_code_sent", lang))

    async def _step_awaiting_calendar(
        self, session: dict, phone: str, body: str
    ) -> None:
        lang = session.get("language", "en")

        if _is_skip(body):
            await self._send(phone, _msg("calendar_skip", lang))
            await asyncio.sleep(1)
            await self._advance_to_forwarding(session, phone)
            return

        # Any other response → treat as skip for now
        # (OAuth callback auto-advances via separate endpoint)
        await self._send(phone, _msg("calendar_skip", lang))
        await asyncio.sleep(1)
        await self._advance_to_forwarding(session, phone)

    async def _step_awaiting_forwarding(
        self, session: dict, phone: str, body: str
    ) -> None:
        lang = session.get("language", "en")

        if _is_done(body) or _is_skip(body):
            await self._complete_onboarding(session, phone)
            return

        if _is_help(body):
            await self._send(phone, _msg("forwarding_help", lang, phone=settings.RECEPTE_PHONE))
            return

        # Re-prompt
        await self._send(phone, _msg("forwarding_step", lang, phone=settings.RECEPTE_PHONE))

    # ── business creation & pairing ──────────────────────────────────────

    async def _create_business_and_pair(self, session: dict, phone: str) -> None:
        lang = session.get("language", "en")
        discovery = session.get("discovery", {})
        biz_name = discovery.get("name", "My Business")

        now = datetime.utcnow().isoformat()
        trial_end = (datetime.utcnow() + timedelta(days=14)).isoformat()

        business_data = {
            "name": biz_name,
            "ownerName": session.get("pushName", ""),
            "ownerPhone": phone,
            "adminPhones": [phone],
            "status": "active",
            "plan": "trial",
            "trialEndsAt": trial_end,
            "createdAt": now,
            "primaryLanguage": lang,
            "supportedLanguages": [lang] if lang != "en" else ["en"],
            "businessType": discovery.get("businessType", "other"),
            "services": discovery.get("services", []),
            "hoursRaw": discovery.get("hours", ""),
            "voiceGender": "female",
            "automations": {
                "winBack": True,
                "dailySummary": True,
                "noShowRecovery": True,
                "reminders24h": True,
                "reminders2h": True,
            },
            "verticalSettings": {
                "businessName": biz_name,
                "description": discovery.get("description", ""),
                "businessType": discovery.get("businessType", "other"),
                "services": discovery.get("services", []),
                "staff": discovery.get("staff", []),
                "faqs": [],
                "hours": discovery.get("hours", ""),
                "currency": _CURRENCY.get(lang, "EUR"),
                "languages": [lang],
                "vibe": "casual",
                "aiPersonality": {
                    "tone": "friendly",
                    "greetingStyle": _GREETINGS.get(lang, _GREETINGS["en"]).format(name=biz_name),
                    "keySellingPoints": [],
                    "upsells": [],
                    "objectionHandlers": [],
                },
                "reviewInsights": {
                    "competitiveAdvantages": [],
                    "commonPraises": [],
                    "commonComplaints": [],
                },
                "verticalFeatures": {},
                "automations": {
                    "winBack": True,
                    "dailySummary": True,
                    "noShowRecovery": True,
                    "reminders24h": True,
                    "reminders2h": True,
                },
            },
        }

        if discovery.get("website"):
            business_data["scrapedUrl"] = discovery["website"]
            business_data["scrapedAt"] = now

        business_id = db.create_business_doc(business_data)

        # Owner record
        db.create_owner_doc(phone, {
            "ownerPhone": phone,
            "ownerName": session.get("pushName", ""),
            "businessId": business_id,
        })

        pairing_session_id = f"biz-{phone}"

        db.upsert_onboarding_session(phone, {
            "businessId": business_id,
            "pairingSessionId": pairing_session_id,
            "currentStep": "awaiting_pairing_done",
        })

        # Pairing instructions → 3-second delay → code
        await self._send(phone, _msg("pairing_intro", lang, name=biz_name))
        await asyncio.sleep(3)

        # Re-fetch session so helpers see updated businessId
        refreshed = db.get_onboarding_session(phone) or session
        refreshed["businessId"] = business_id
        refreshed["pairingSessionId"] = pairing_session_id

        await self._send_pairing_code(refreshed, phone)

    async def _send_pairing_code(self, session: dict, phone: str) -> None:
        lang = session.get("language", "en")
        pairing_sid = session.get("pairingSessionId", f"biz-{phone}")

        try:
            result = await self.wa.generate_pair_code(
                session_id=pairing_sid,
                phone_number=f"+{phone}",
            )
            code = result.get("code", "????-????")
            await self._send(phone, code)
            await asyncio.sleep(1)
            await self._send(phone, _msg("pairing_code_sent", lang))
        except Exception as exc:
            logger.error("Pair-code generation failed for %s: %s", phone, exc)
            # Silent failure → skip to calendar (per spec: never show errors)
            await self._send(phone, _msg("pairing_failed", lang))
            await asyncio.sleep(1)
            await self._advance_to_calendar(session, phone)

    # ── step advancement helpers ──────────────────────────────────────────

    async def _advance_to_calendar(self, session: dict, phone: str) -> None:
        lang = session.get("language", "en")
        biz_id = session.get("businessId", "")
        link = f"{settings.RECEPTE_CALENDAR_BASE_URL}?bizId={biz_id}"

        db.upsert_onboarding_session(phone, {"currentStep": "awaiting_calendar"})
        await self._send(phone, _msg("calendar_step", lang, link=link))

    async def _advance_to_forwarding(self, session: dict, phone: str) -> None:
        lang = session.get("language", "en")
        db.upsert_onboarding_session(phone, {"currentStep": "awaiting_forwarding"})
        await self._send(phone, _msg("forwarding_step", lang, phone=settings.RECEPTE_PHONE))

    async def _complete_onboarding(self, session: dict, phone: str) -> None:
        lang = session.get("language", "en")
        db.upsert_onboarding_session(phone, {
            "currentStep": "complete",
            "timestamps.completedAt": datetime.utcnow().isoformat(),
        })
        await self._send(phone, _msg("complete", lang))
        logger.info("Onboarding complete for %s", phone)

    # ── messaging ─────────────────────────────────────────────────────────

    async def _send(self, phone: str, message: str) -> None:
        try:
            await self.wa.send_message(phone, message)
        except Exception as exc:
            logger.error("Failed to send WA message to %s: %s", phone, exc)
