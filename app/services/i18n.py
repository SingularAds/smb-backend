"""i18n — customer-facing message templates for multi-language support.

Used by visit_service, referral_service, and notification_service to ensure
all automated messages sent to customers match the customer's own language.

Supported languages: en, pt, es, fr, de, it
Fallback: en
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------
_CUSTOMER_MSGS: dict[str, dict[str, str]] = {

    # ── Visit / check-in ────────────────────────────────────────────────────

    "visit_thanks": {
        "en": "✅ *Thank you, {name}!*\n\nSo glad you came in for *{service}* at *{biz}*! We hope to see you again soon. 😊",
        "pt": "✅ *Obrigado(a), {name}!*\n\nQue bom ter-te cá para *{service}* em *{biz}*! Esperamos ver-te em breve. 😊",
        "es": "✅ *¡Gracias, {name}!*\n\n¡Qué alegría que hayas venido a *{service}* en *{biz}*! Esperamos verte pronto. 😊",
        "fr": "✅ *Merci, {name}!*\n\nRavi(e) de vous avoir reçu(e) pour *{service}* chez *{biz}* ! À très bientôt. 😊",
        "de": "✅ *Danke, {name}!*\n\nSchön, dass Sie für *{service}* bei *{biz}* vorbeigekommen sind! Bis bald. 😊",
        "it": "✅ *Grazie, {name}!*\n\nChe piacere averti da *{biz}* per *{service}*! Speriamo di rivederti presto. 😊",
    },

    "visit_noshow": {
        "en": (
            "👋 *No worries, {name}!*\n\n"
            "Thanks for letting us know. We'd love to have you visit *{biz}* for *{service}* soon!\n\n"
            "Would you like to reschedule? Just tell us a date and time that works for you. 📅"
        ),
        "pt": (
            "👋 *Sem problema, {name}!*\n\n"
            "Obrigado por nos avisares. Adorávamos receber-te em *{biz}* para *{service}* em breve!\n\n"
            "Gostarias de remarcar? Diz-nos uma data e hora que te convenha. 📅"
        ),
        "es": (
            "👋 *¡Sin problema, {name}!*\n\n"
            "Gracias por avisarnos. ¡Nos encantaría tenerte en *{biz}* para *{service}* pronto!\n\n"
            "¿Te gustaría reprogramar? Solo dinos una fecha y hora que te convenga. 📅"
        ),
        "fr": (
            "👋 *Pas de souci, {name}!*\n\n"
            "Merci de nous avoir prévenus. Nous serions ravis de vous accueillir chez *{biz}* "
            "pour *{service}* prochainement !\n\n"
            "Voulez-vous reprogrammer ? Donnez-nous une date et une heure qui vous conviennent. 📅"
        ),
        "de": (
            "👋 *Kein Problem, {name}!*\n\n"
            "Danke für die Benachrichtigung. Wir würden Sie gerne bald wieder bei *{biz}* "
            "für *{service}* begrüßen!\n\n"
            "Möchten Sie einen neuen Termin vereinbaren? Nennen Sie uns Datum und Uhrzeit. 📅"
        ),
        "it": (
            "👋 *Nessun problema, {name}!*\n\n"
            "Grazie per averci avvisato. Ci farebbe piacere averti da *{biz}* per *{service}* presto!\n\n"
            "Vorresti riprogrammare? Dicci una data e un orario che ti vanno bene. 📅"
        ),
    },

    # ── Referral — customer-facing ──────────────────────────────────────────

    "referral_already_customer": {
        "en": "👋 Welcome back! You're already one of our valued customers at *{biz}*. We look forward to seeing you again! 😊",
        "pt": "👋 Bem-vindo(a) de volta! Já és um(a) dos nossos clientes em *{biz}*. Até breve! 😊",
        "es": "👋 ¡Bienvenido(a) de nuevo! Ya eres uno de nuestros clientes en *{biz}*. ¡Hasta pronto! 😊",
        "fr": "👋 Bienvenue à nouveau ! Vous êtes déjà l'un de nos précieux clients chez *{biz}*. À très bientôt ! 😊",
        "de": "👋 Willkommen zurück! Sie sind bereits einer unserer geschätzten Kunden bei *{biz}*. Bis bald! 😊",
        "it": "👋 Bentornato(a)! Sei già uno dei nostri clienti affezionati di *{biz}*. A presto! 😊",
    },

    "referral_already_referred": {
        "en": "👋 Hi! You were referred by *{referrer}*. We already have your *{pct}% discount* saved for your first visit. See you soon! 😊",
        "pt": "👋 Olá! Foste indicado(a) por *{referrer}*. Já temos o teu desconto de *{pct}%* guardado para a tua primeira visita. Até breve! 😊",
        "es": "👋 ¡Hola! Fuiste referido(a) por *{referrer}*. Ya tenemos tu *descuento del {pct}%* guardado para tu primera visita. ¡Hasta pronto! 😊",
        "fr": "👋 Bonjour ! Vous avez été recommandé(e) par *{referrer}*. Votre *remise de {pct}%* est déjà enregistrée pour votre première visite. À bientôt ! 😊",
        "de": "👋 Hallo! Sie wurden von *{referrer}* empfohlen. Ihr *{pct}% Rabatt* ist bereits für Ihren ersten Besuch gespeichert. Bis bald! 😊",
        "it": "👋 Ciao! Sei stato(a) segnalato(a) da *{referrer}*. Il tuo *sconto del {pct}%* è già salvato per la tua prima visita. A presto! 😊",
    },

    "referral_welcome": {
        "en": (
            "👋 *Welcome to {biz}!*\n\n"
            "You were referred by *{referrer}* — thank you for coming! 🎉\n\n"
            "Your *{pct}% discount* is saved for your first visit. The team will apply it at checkout."
        ),
        "pt": (
            "👋 *Bem-vindo(a) a {biz}!*\n\n"
            "Foste indicado(a) por *{referrer}* — obrigado por vires! 🎉\n\n"
            "O teu desconto de *{pct}%* está guardado para a tua primeira visita. A equipa vai aplicá-lo na caixa."
        ),
        "es": (
            "👋 *¡Bienvenido(a) a {biz}!*\n\n"
            "Fuiste referido(a) por *{referrer}* — ¡gracias por venir! 🎉\n\n"
            "Tu *descuento del {pct}%* está guardado para tu primera visita. El equipo lo aplicará en la caja."
        ),
        "fr": (
            "👋 *Bienvenue chez {biz}!*\n\n"
            "Vous avez été recommandé(e) par *{referrer}* — merci de votre visite ! 🎉\n\n"
            "Votre *remise de {pct}%* est enregistrée pour votre première visite. L'équipe l'appliquera à la caisse."
        ),
        "de": (
            "👋 *Willkommen bei {biz}!*\n\n"
            "Sie wurden von *{referrer}* empfohlen — danke, dass Sie gekommen sind! 🎉\n\n"
            "Ihr *{pct}% Rabatt* ist für Ihren ersten Besuch gespeichert. Das Team wendet ihn an der Kasse an."
        ),
        "it": (
            "👋 *Benvenuto(a) da {biz}!*\n\n"
            "Sei stato(a) segnalato(a) da *{referrer}* — grazie per essere venuto(a)! 🎉\n\n"
            "Il tuo *sconto del {pct}%* è salvato per la tua prima visita. Il team lo applicherà alla cassa."
        ),
    },

    "referral_reward": {
        "en": (
            "🎉 *Great news!*\n\n"
            "*{friend}* just completed their first visit at *{biz}*!\n\n"
            "As a thank-you, you've earned a *{pct}% discount* on your next visit. "
            "The team will apply it at checkout. 😊"
        ),
        "pt": (
            "🎉 *Ótimas notícias!*\n\n"
            "*{friend}* acabou de completar a primeira visita em *{biz}*!\n\n"
            "Como agradecimento, ganhaste um desconto de *{pct}%* na tua próxima visita. "
            "A equipa vai aplicá-lo na caixa. 😊"
        ),
        "es": (
            "🎉 *¡Excelentes noticias!*\n\n"
            "*{friend}* acaba de completar su primera visita en *{biz}*!\n\n"
            "Como agradecimiento, has ganado un *descuento del {pct}%* en tu próxima visita. "
            "El equipo lo aplicará en la caja. 😊"
        ),
        "fr": (
            "🎉 *Super nouvelle !*\n\n"
            "*{friend}* vient de compléter sa première visite chez *{biz}* !\n\n"
            "En guise de remerciement, vous avez gagné une *remise de {pct}%* sur votre prochaine visite. "
            "L'équipe l'appliquera à la caisse. 😊"
        ),
        "de": (
            "🎉 *Tolle Neuigkeiten!*\n\n"
            "*{friend}* hat gerade den ersten Besuch bei *{biz}* abgeschlossen!\n\n"
            "Als Dankeschön haben Sie einen *{pct}% Rabatt* auf Ihren nächsten Besuch verdient. "
            "Das Team wendet ihn an der Kasse an. 😊"
        ),
        "it": (
            "🎉 *Ottime notizie!*\n\n"
            "*{friend}* ha appena completato la sua prima visita da *{biz}*!\n\n"
            "Come ringraziamento, hai guadagnato uno *sconto del {pct}%* sulla tua prossima visita. "
            "Il team lo applicherà alla cassa. 😊"
        ),
    },

    # ── Booking confirm (SMS — extra languages for notification_service) ────

    "booking_confirmed": {
        "en": "✅ Your booking is confirmed!\n📍 {biz}\n📅 {service} on {dt}",
        "pt": "✅ Sua marcação foi confirmada!\n📍 {biz}\n📅 {service} on {dt}\nObrigado!",
        "es": "✅ ¡Tu cita está confirmada!\n📍 {biz}\n📅 {service} on {dt}\n¡Gracias!",
        "fr": "✅ Votre rendez-vous est confirmé!\n📍 {biz}\n📅 {service} on {dt}\nMerci!",
        "de": "✅ Ihre Buchung ist bestätigt!\n📍 {biz}\n📅 {service} am {dt}\nVielen Dank!",
        "it": "✅ La tua prenotazione è confermata!\n📍 {biz}\n📅 {service} il {dt}\nGrazie!",
    },

    "booking_rescheduled": {
        "en": "🔁 Your appointment was rescheduled!\n📍 {biz}\n📅 {service} on {dt}",
        "pt": "🔁 Sua marcação foi remarcada!\n📍 {biz}\n📅 {service} on {dt}\nObrigado!",
        "es": "🔁 ¡Tu cita fue reprogramada!\n📍 {biz}\n📅 {service} on {dt}\n¡Gracias!",
        "fr": "🔁 Votre rendez-vous a été reprogrammé !\n📍 {biz}\n📅 {service} le {dt}\nMerci!",
        "de": "🔁 Ihr Termin wurde umgebucht!\n📍 {biz}\n📅 {service} am {dt}\nVielen Dank!",
        "it": "🔁 Il tuo appuntamento è stato riprogrammato!\n📍 {biz}\n📅 {service} il {dt}\nGrazie!",
    },

    "booking_cancelled": {
        "en": "❌ Your appointment was cancelled\n📍 {biz}\n📅 {service} on {dt}\nFeel free to rebook anytime!",
        "pt": "❌ Sua marcação foi cancelada\n📍 {biz}\n📅 {service} on {dt}\nSe mudar de ideias, nos contacte!",
        "es": "❌ Tu cita fue cancelada\n📍 {biz}\n📅 {service} on {dt}\n¡Si cambias de opinión, contáctanos!",
        "fr": "❌ Votre rendez-vous a été annulé\n📍 {biz}\n📅 {service} le {dt}\nN'hésitez pas à réserver à nouveau !",
        "de": "❌ Ihr Termin wurde storniert\n📍 {biz}\n📅 {service} am {dt}\nSie können jederzeit neu buchen!",
        "it": "❌ Il tuo appuntamento è stato cancellato\n📍 {biz}\n📅 {service} il {dt}\nPuoi riprenotare in qualsiasi momento!",
    },

    "complaint_acknowledged": {
        "en": "Hi {name},\nWe've received your feedback and will look into it.\nThank you — {biz}",
        "pt": "Olá {name},\nRecebemos o seu feedback e vamos analisá-lo.\nObrigado por nos contactar — {biz}",
        "es": "Hola {name},\nHemos recibido tu comentario y lo analizaremos.\nGracias por contactarnos — {biz}",
        "fr": "Bonjour {name},\nNous avons bien reçu votre avis et allons l'examiner.\nMerci de nous avoir contactés — {biz}",
        "de": "Hallo {name},\nWir haben Ihr Feedback erhalten und werden es prüfen.\nVielen Dank — {biz}",
        "it": "Ciao {name},\nAbbiamo ricevuto il tuo feedback e lo esamineremo.\nGrazie per averci contattato — {biz}",
    },

    # ── Reminder timing words ────────────────────────────────────────────────

    "reminder_today": {
        "en": "is TODAY",
        "pt": "é HOJE",
        "es": "es HOY",
        "fr": "est AUJOURD'HUI",
        "de": "ist HEUTE",
        "it": "è OGGI",
    },

    "reminder_tomorrow": {
        "en": "is TOMORROW",
        "pt": "é AMANHÃ",
        "es": "es MAÑANA",
        "fr": "est DEMAIN",
        "de": "ist MORGEN",
        "it": "è DOMANI",
    },

    "reminder_in_days": {
        "en": "is in {n} days",
        "pt": "é em {n} dias",
        "es": "es en {n} días",
        "fr": "est dans {n} jours",
        "de": "ist in {n} Tagen",
        "it": "è tra {n} giorni",
    },

    "reminder_body": {
        "en": (
            "{emoji} Hey {name}! Friendly reminder:\n"
            "Your appointment {timing}!\n"
            "📍 {biz}\n"
            "{svc_emoji} {service}\n"
            "🕐 {dt}\n"
            "See you soon! 😊"
        ),
        "pt": (
            "{emoji} Olá {name}! Lembrete amigável:\n"
            "A sua marcação {timing}!\n"
            "📍 {biz}\n"
            "{svc_emoji} {service}\n"
            "🕐 {dt}\n"
            "Esperamos vê-lo(a) em breve! 😊"
        ),
        "es": (
            "{emoji} ¡Hola {name}! Recordatorio amistoso:\n"
            "¡Tu cita {timing}!\n"
            "📍 {biz}\n"
            "{svc_emoji} {service}\n"
            "🕐 {dt}\n"
            "¡Nos vemos pronto! 😊"
        ),
        "fr": (
            "{emoji} Bonjour {name}! Petit rappel :\n"
            "Votre rendez-vous {timing} !\n"
            "📍 {biz}\n"
            "{svc_emoji} {service}\n"
            "🕐 {dt}\n"
            "À très bientôt ! 😊"
        ),
        "de": (
            "{emoji} Hallo {name}! Freundliche Erinnerung:\n"
            "Ihr Termin {timing}!\n"
            "📍 {biz}\n"
            "{svc_emoji} {service}\n"
            "🕐 {dt}\n"
            "Bis bald! 😊"
        ),
        "it": (
            "{emoji} Ciao {name}! Piccolo promemoria:\n"
            "Il tuo appuntamento {timing}!\n"
            "📍 {biz}\n"
            "{svc_emoji} {service}\n"
            "🕐 {dt}\n"
            "A presto! 😊"
        ),
    },
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def t(key: str, lang: str, **kwargs: object) -> str:
    """Return translated message for *key* in *lang*, formatted with **kwargs.

    Falls back to English when the language is not covered.

    Args:
        key:    A key in ``_CUSTOMER_MSGS``.
        lang:   ISO-639-1 language code (``"pt"``, ``"es"``, …).
                Only the first two characters are used so ``"pt-BR"`` also works.
        **kwargs: Variables to interpolate into the template string.
    """
    lang2 = (lang or "en")[:2].lower()
    bucket = _CUSTOMER_MSGS.get(key, {})
    template = bucket.get(lang2) or bucket.get("en") or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError):
            return template
    return template


def get_customer_language(business_id: str, phone: str, fallback: str = "en") -> str:
    """Look up the stored language for a customer, falling back to *fallback*.

    Imports firestore lazily to avoid circular imports.
    """
    try:
        from app import firestore as _db
        customer = _db.get_customer_by_phone(business_id, phone)
        if customer:
            lang = (customer.get("language") or fallback).strip()
            return lang[:2].lower() if lang else fallback
    except Exception:
        pass
    return fallback
