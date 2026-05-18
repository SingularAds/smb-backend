"""V1 API Router Exports"""

from app.api.v1 import businesses, bookings, calendar, customers, health, reminders, vapi, voice, webhooks, whatsapp

__all__ = [
    "voice",
    "vapi",
    "bookings",
    "businesses",
    "customers",
    "webhooks",
    "health",
    "whatsapp",
    "calendar",
    "reminders",
]
