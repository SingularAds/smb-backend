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
    ANALYTICS_ADMIN_KEY: str = ""  # Set in .env — internal analytics dashboard access (x-admin-key header)
    
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
    
    # OpenAI
    OPENAI_API_KEY: str = ""
    
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
    GOOGLE_CALENDAR_TIMEZONE: str = "Europe/Lisbon"      # timezone for calendar events
    BUSINESS_TIMEZONE: str = "Europe/Lisbon"             # local timezone for booking display & reminders
    
    # Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    # Telegram (Daniel escalation alerts)
    TELEGRAM_BOT_TOKEN: str = ""       # Bot token from @BotFather
    TELEGRAM_DANIEL_CHAT_ID: str = ""  # Daniel's personal chat/group ID
    
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
    # Optional: run onboarding on MULTIPLE global numbers at once. Comma-separated
    # "deviceId:number" pairs, e.g. "smba:918968012547, smbb:917696794756". When
    # set, ANY of these numbers runs the same onboarding flow and each owner gets
    # replies back on whichever number they messaged. When EMPTY (default), the
    # system falls back to the single WHATSMEOW_ONBOARDING_DEVICE_ID /
    # WHATSMEOW_DEFAULT_DEVICE_ID + WHATSMEOW_GLOBAL_NUMBER above — i.e. nothing
    # changes until this is filled in. See app/services/global_numbers.py.
    WHATSMEOW_GLOBAL_NUMBERS: str = ""
    WHATSAPP_DEBOUNCE_S: float = 2.0           # Seconds to wait before flushing a rapid-message burst to the LLM
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

    # Dedicated global DEMO number (digits, no +) for the "feel it first" link.
    # This MUST be a SEPARATE number from the onboarding number — e.g. a Twilio
    # demo line — so the wa.me link opens a NEW chat instead of reopening the
    # owner's onboarding chat. When empty, the demo link is simply not shown
    # (we never point it at the onboarding number). The Salão Bella demo persona
    # handles the conversation on this number.
    DEMO_WA_NUMBER: str = ""
    # Bridge device/session ID that the demo number is linked to on the whatsmeow
    # bridge. Incoming messages arriving on THIS device are routed to the Salão
    # Bella demo handler. Must be set (alongside DEMO_WA_NUMBER) for the demo to
    # actually reply — otherwise messages to the demo number reach no handler.
    DEMO_WA_DEVICE_ID: str = ""
    # Public URL of the "feel it first" demo landing page on the website. The
    # trust interstitial links here (instead of a raw wa.me link); the page then
    # opens the WhatsApp demo chat with a localized pre-filled message. The
    # backend appends ?lang=<lang>&n=<demo number> so the page knows which
    # language and which demo number to open.
    DEMO_PAGE_URL: str = "https://recepte.co/demo"

    # Intro VIDEO NOTE (round tap-to-play bubble) sent once, right after Sofia's
    # first greeting (client 2026-07-23 — replaces the old post-greeting privacy
    # note). ONBOARDING_VIDEO_NOTE_URL must point at an ALREADY WhatsApp-ready MP4
    # (H.264/AAC, ideally SQUARE and ≤60s for the round bubble) — e.g. a public or
    # presigned S3 URL. The bridge does NOT transcode video. When empty, no video
    # is sent (the greeting simply stands alone), so this is safe to leave unset
    # until the file is uploaded. ONBOARDING_VIDEO_NOTE_PTV=false sends it as a
    # normal (rectangular) video instead of a round note.
    ONBOARDING_VIDEO_NOTE_URL: str = ""
    ONBOARDING_VIDEO_NOTE_PTV: bool = True

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

    # ── WhatsApp Outbound Guard (anti-ban discipline for PROACTIVE sends) ──
    # Applies to marketing-class messages we initiate (referral invites, CSAT
    # prompts, future campaigns). Transactional messages (booking confirmations,
    # reminders, AI replies to inbound) are NOT gated — they are customer-
    # initiated and are WhatsApp's safest traffic class.
    WA_OUTBOUND_GUARD_ENABLED: bool = True
    # Max proactive messages per device (business number) per calendar day.
    WA_PROACTIVE_DAILY_CAP: int = 50
    # Max proactive touches per contact per rolling 30 days (across ALL mechanics).
    WA_TOUCH_BUDGET_PER_30D: int = 2
    # Randomized human-like delay (seconds) before each proactive send.
    WA_PROACTIVE_JITTER_MIN_S: float = 10.0
    WA_PROACTIVE_JITTER_MAX_S: float = 45.0
    # Local business-hours window for proactive sends (business timezone).
    WA_PROACTIVE_HOUR_START: int = 9
    WA_PROACTIVE_HOUR_END: int = 20
    # Days after WhatsApp pairing during which NO proactive sends happen
    # (number warm-up). Businesses without waPairedAt are treated as warmed.
    WA_WARMUP_DAYS: int = 7
    # Minimum device cooldown (seconds) after WhatsApp 463 (reachout
    # time-locked). The bridge's retry_after is honored if larger.
    WA_463_COOLDOWN_MIN_S: int = 3600

    # ── LLM Observability (Langfuse) ──
    # Wraps every OpenAI call with traces, token cost, latency, and lets you
    # correlate customer CSAT scores back to the exact AI prompt/response.
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"
    LANGFUSE_ENABLED: bool = True

    # ── Product Analytics (PostHog backend) ──
    # The whatsmeow bridge already sends PostHog events; this is the backend
    # counterpart so the full conversion funnel (message → intent → booking)
    # lives in one PostHog project.
    POSTHOG_API_KEY: str = ""
    POSTHOG_HOST: str = "https://app.posthog.com"
    POSTHOG_ENABLED: bool = True

    # ── CSAT (1-5 rating) ──
    # Conversation is considered "ended" after CSAT_IDLE_MINUTES of silence.
    # The sweep job runs every CSAT_SWEEP_INTERVAL_MINUTES.
    CSAT_ENABLED: bool = True
    CSAT_IDLE_MINUTES: int = 30
    CSAT_SWEEP_INTERVAL_MINUTES: int = 5
    CSAT_COOLDOWN_DAYS: int = 7

    # ── Chatwoot (Website AI Assistant) ──
    # Independent pipeline serving the recepte.co website chat widget.
    # Does NOT share state, prompts, or services with the WhatsApp customer AI.
    CHATWOOT_ENABLED: bool = True
    CHATWOOT_BASE_URL: str = "https://app.chatwoot.com"  # Chatwoot Cloud or self-hosted base URL
    CHATWOOT_ACCOUNT_ID: str = ""                        # Numeric account ID (e.g. "169263")
    CHATWOOT_ACCESS_TOKEN: str = ""                      # Agent/bot API access token (api_access_token)
    CHATWOOT_HMAC_SECRET: str = ""                       # Webhook signing secret (X-Chatwoot-Signature)
    CHATWOOT_WEBSITE_TOKEN: str = ""                     # Public website widget token (for inbox identification)
    CHATWOOT_WEBSITE_INBOX_ID: int = 0                   # Numeric inbox ID (optional — used for safety filter)
    CHATWOOT_SUPPORT_TEAM_ID: int = 0                    # Team ID escalations are assigned to (0 = no team assignment)
    CHATWOOT_REQUIRE_SIGNATURE: bool = False             # Reject webhooks lacking a valid HMAC (set True in production)
    CHATWOOT_LLM_MODEL: str = "gpt-4o-mini"              # OpenAI model used for website assistant
    CHATWOOT_HISTORY_LIMIT: int = 10                     # Max messages kept per conversation in Firestore
    CHATWOOT_LEARNINGS_LIMIT: int = 30                   # Max learnings injected into the prompt
    CHATWOOT_LEARNINGS_CACHE_TTL_S: int = 300            # In-process cache TTL for learnings (seconds)
    CHATWOOT_LEARN_COMMAND: str = "/kb-learn"            # Private-note command that saves a learning
    CHATWOOT_PAUSE_COMMAND: str = "/ai-pause"            # Private-note command that pauses AI for this conversation
    CHATWOOT_RESUME_COMMAND: str = "/ai-resume"          # Private-note command that re-enables the AI
    CHATWOOT_HELP_COMMAND: str = "/ai-help"              # Private-note command that lists available agent commands

    class Config:
        import os as _os
        env_file = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".env")
        case_sensitive = True


settings = Settings()
