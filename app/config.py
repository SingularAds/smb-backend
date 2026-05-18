"""Application Configuration"""

from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Application
    APP_NAME: str = "BoomReception"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    # Transport security controls.
    # Keep dev ergonomics while preventing accidental insecure production traffic.
    ALLOW_INSECURE_TRANSPORT: bool = True
    ENABLE_SSLIP_DNS_OVERRIDE: bool = True
    
    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 4
    
    # Firebase / Firestore
    GOOGLE_APPLICATION_CREDENTIALS: str = "./serviceAccount.json"
    FIRESTORE_PROJECT_ID: str = "smbaicallz"
    
    # Security
    SECRET_KEY: str = "change-this-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    API_SECRET: str = ""  # Set in .env — used for x-api-key / Authorization: Bearer <key>
    
    # CORS
    ALLOWED_ORIGINS: List[str] = [
        "http://localhost:3000",
        "https://smbaicallz.web.app"
    ]
    ALLOWED_METHODS: List[str] = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
    ALLOWED_HEADERS: List[str] = ["*"]
    
    # Twilio
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_TEST_NUMBER: str = ""
    
    # Anthropic
    ANTHROPIC_API_KEY: str = ""
    
    # ElevenLabs
    ELEVENLABS_API_KEY: str = ""
    
    # Google Calendar
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_PLACES_API_KEY: str = ""   # Google Places / Maps API key

    # Apify (Instagram & Google Places scraping) [OPTIONAL]
    APIFY_API_KEY: str = ""                                    # Apify API token
    APIFY_INSTAGRAM_ACTOR_ID: str = "apify~instagram-scraper" # Instagram profile actor
    APIFY_GOOGLE_PLACES_ACTOR_ID: str = "compass~crawler-google-places"  # Maps fallback actor
    GOOGLE_CREDENTIALS_FILE: str = "./credentials.json"  # service account for owner calendar
    GOOGLE_CALENDAR_ID: str = "primary"                  # owner calendar ID or email
    GOOGLE_SERVICE_ACCOUNT_CALENDAR_ID: str = "primary"  # calendar ID for service account
    GOOGLE_CALENDAR_TIMEZONE: str = "Europe/Lisbon"      # timezone for calendar events
    BUSINESS_TIMEZONE: str = "Europe/Lisbon"             # local timezone for booking display & reminders
    
    # Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    
    # SMTP (Email)
    SMTP_HOST: str = "sandbox.smtp.mailtrap.io"
    SMTP_PORT: int = 2525
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True

    # VAPI
    VAPI_SECRET: str = ""                    # x-vapi-secret header value
    VAPI_API_KEY: str = ""                   # VAPI dashboard API key (for provisioning)
    VAPI_DEFAULT_ASSISTANT_ID: str = ""      # fallback assistant when business not found
    VAPI_DEFAULT_BUSINESS_ID: str = ""       # optional fallback business for VAPI booking/slots
    VAPI_AUTHENTICATION_HEADER_NAME: str = "Authorization"  # header name sent by VAPI
    VAPI_AUTHENTICATION_SECRET_KEY: str = ""               # expected header value

    # Twilio (outbound SMS notifications)
    TWILIO_FROM_NUMBER: str = ""             # E.164 number used as SMS sender

    # WhatsApp / whatsmeow Bridge
    WHATSMEOW_API_BASE_URL: str = ""         # e.g. https://91-99-169-109.sslip.io/whatsmeow
    WHATSMEOW_API_USERNAME: str = ""         # Bridge basic-auth username
    WHATSMEOW_API_PASSWORD: str = ""         # Bridge basic-auth password
    WHATSMEOW_DEFAULT_DEVICE_ID: str = "smba"  # Legacy default device/session ID (kept for compatibility)
    WHATSMEOW_ONBOARDING_DEVICE_ID: str = ""   # Optional explicit global/onboarding device/session ID
    WHATSMEOW_GLOBAL_NUMBER: str = ""          # Optional global onboarding WhatsApp number (digits, no +)
    WEBHOOK_SECRET: str = ""                 # Webhook secret for validation
    X_WEBHOOK_SECRET: str = ""               # Alias (header: X-Webhook-Secret)

    # Speech-to-Text (Deepgram)
    DEEPGRAM_API_KEY: str = ""               # Deepgram Nova-3 API key

    # Text-to-Speech (Cartesia)
    CARTESIA_API_KEY: str = ""               # Cartesia sonic-multilingual API key

    # Google OAuth (Calendar connect)
    GOOGLE_REDIRECT_URI: str = "http://localhost:3002/auth/google/callback"  # Override in .env for production
    BASE_URL: str = "http://localhost:3002"    # Public base URL of this server

    # Recepte global settings
    RECEPTE_PHONE: str = "911111111111"      # Recepte WhatsApp number (no +)
    RECEPTE_CALENDAR_BASE_URL: str = "https://recepte.co/connect-calendar"

    # Call-forwarding destination numbers, keyed by country calling code.
    # JSON object stored as a string, e.g.:
    #   CALL_FORWARDING_NUMBERS_JSON='{"351": "+351200010001", "1": "+12125550100", "44": "+441234567890"}'
    # The country code is matched against the leading digits of the owner's WhatsApp phone number.
    CALL_FORWARDING_NUMBERS_JSON: str = "{}"
    # Optional single fallback number used when the owner's country code is not in the map above.
    CALL_FORWARDING_DEFAULT_NUMBER: str = ""

    # Feature Flags
    ENABLE_DAILY_SUMMARIES: bool = True
    ENABLE_REMINDERS: bool = True
    ENABLE_QA_RUNNER: bool = False
    SMS_GATEWAY_PORT_3002: bool = False      # Temporary: send SMS via port 3002 instead of Twilio (testing only)
    
    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 60
    RATE_LIMIT_PER_HOUR: int = 1000
    
    class Config:
        import os as _os
        env_file = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".env")
        case_sensitive = True


settings = Settings()
