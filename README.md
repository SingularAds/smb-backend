# BoomReception VAPI - Voice AI Calling System

**AI-powered voice receptionist using FastAPI, Twilio, Claude AI, and ElevenLabs TTS**

This is a Python FastAPI implementation focused on voice calling (VAPI) capabilities: intelligent phone call handling, appointment booking via voice, and AI-powered customer conversations.

---

## ✨ Features

- 🎤 **Voice AI Pipeline** - Twilio integration with AI conversation handling
- 🧠 **Claude AI** - Intelligent conversation processing and customer trait extraction
- 🗣️ **ElevenLabs TTS** - Natural multilingual text-to-speech
- 📅 **Booking System** - Voice-based appointment booking
- 👥 **Customer Intelligence** - Automatic customer profiling and preferences
- 🔄 **Real-time Processing** - Async/await architecture for performance

---

## 🚀 Quick Start

### 1. Install Dependencies

```bash
cd dando-vapi-tool
python -m venv venv
.\venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
copy .env.example .env
# Edit .env with your API keys:
# - DATABASE_URL
# - ANTHROPIC_API_KEY
# - TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
# - ELEVENLABS_API_KEY
```

### 3. Start Database

```bash
# Using Docker
docker-compose up -d postgres redis

# Create tables
alembic upgrade head
```

### 4. Run Application

```bash
# Development mode
uvicorn app.main:app --reload --port 8000

# Visit http://localhost:8000/docs for API documentation
```

---

## 📂 Project Structure

```
app/
├── main.py                 # FastAPI application
├── config.py               # Configuration settings
├── database.py             # Database connection
├── models/                 # SQLAlchemy models
│   ├── business.py
│   ├── booking.py
│   ├── customer.py
│   └── conversation.py
├── schemas/                # Pydantic schemas
├── api/v1/                 # API endpoints
│   ├── voice.py           # ✨ Twilio voice webhooks
│   ├── bookings.py
│   ├── customers.py
│   └── webhooks.py
├── services/               # Business logic
│   ├── voice_service.py   # ✨ Call handling
│   └── booking_service.py
└── integrations/           # External APIs
    ├── anthropic_client.py # ✨ Claude AI
    └── elevenlabs_client.py # ✨ TTS
```

---

## 🔌 API Endpoints

### Voice (Twilio VAPI)
- `POST /twilio/voice/webhook` - Handle incoming calls
- `POST /twilio/voice/gather` - Process customer speech
- `POST /twilio/voice/status` - Call status callbacks
- `GET /twilio/voice/tts` - Generate speech audio

### Bookings
- `GET /api/v1/bookings` - List bookings
- `POST /api/v1/bookings` - Create booking
- `PATCH /api/v1/bookings/{id}/confirm` - Confirm booking
- `POST /api/v1/bookings/available-slots` - Get available times

### Customers
- `GET /api/v1/customers` - List customers
- `GET /api/v1/customers/{phone}` - Get customer profile

---

## 🔑 Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DATABASE_URL` | PostgreSQL connection string | ✅ |
| `ANTHROPIC_API_KEY` | Claude AI API key | ✅ |
| `TWILIO_ACCOUNT_SID` | Twilio account SID | ✅ |
| `TWILIO_AUTH_TOKEN` | Twilio auth token | ✅ |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS key | ✅ |
| `SECRET_KEY` | JWT secret for auth | ✅ |
| `REDIS_URL` | Redis for Celery tasks | Optional |

---

## 🐳 Docker Deployment

```bash
# Start all services (API + PostgreSQL + Redis + Celery)
docker-compose up -d

# View logs
docker-compose logs -f api

# Stop everything
docker-compose down
```

---

## 📝 Implementation Checklist

### Core Voice Features
- [x] Twilio webhook handling
- [x] TwiML generation for call flow
- [x] ElevenLabs TTS integration
- [ ] Claude conversation processing
- [ ] Booking creation from voice
- [ ] Customer trait extraction

### Database
- [x] PostgreSQL setup
- [x] SQLAlchemy models
- [x] Alembic migrations
- [ ] Data seeding

### Business Logic
- [ ] Business phone number lookup
- [ ] Conversation history storage
- [ ] Google Calendar integration
- [ ] Reminder scheduling (Celery)

---

## 🛠️ Development

```bash
# Run tests
pytest

# Format code
ruff format .

# Create database migration
alembic revision --autogenerate -m "Description"
alembic upgrade head

# Access PostgreSQL
docker-compose exec postgres psql -U postgres -d boomreception
```

---

## 📚 Documentation

For detailed migration docs from Node.js, see:
- `../boomreception/MIGRATION_README.md` - Full migration blueprint
- `../boomreception/DATABASE_SCHEMA.md` - Database schema
- `../boomreception/API_ENDPOINTS.md` - API specifications

---

## 🎯 Next Steps

1. **Configure Twilio**: Add your Twilio phone number webhook URL: `https://your-domain.com/twilio/voice/webhook`
2. **Implement Business Logic**: Complete the TODO items in `voice_service.py`
3. **Add Google Calendar**: Integrate calendar for availability checking
4. **Setup Celery**: Configure background tasks for reminders
5. **Deploy**: Use Railway, Render, or Google Cloud Run

---

## 📞 Voice Call Flow

```
Incoming Call → Twilio Webhook
    ↓
Voice Service (Generate Greeting)
    ↓
TwiML + ElevenLabs TTS → Customer hears AI greeting
    ↓
Customer Speaks → Twilio Speech Recognition
    ↓
Claude AI Processing (Intent Recognition)
    ↓
If Booking Request → Create Booking
    ↓
Generate Response → TTS → Customer
    ↓
Loop until call ends
```

---

**Built with FastAPI, Twilio, Anthropic Claude, and ElevenLabs** 🚀

---

## 📲 WhatsApp AI Receptionist (Audio + Bookings)

Customers can send **text or voice notes** to the business WhatsApp number.
The AI transcribes audio (Deepgram STT), runs it through Claude with full
booking tools, replies with a voice note (Cartesia TTS), and handles all
booking actions end-to-end.

### Audio pipeline

```
WhatsApp voice note
   ↓  bridge delivers media_url + webhook
Download audio (retry on DNS, 3 attempts)
   ↓  validate bytes are real audio
Deepgram Nova-3 STT  (retry + fallback content-type on 400)
   ↓  transcript
Claude AI with booking tools  (same as text path)
   ↓  text reply
Cartesia TTS → MP3
   ↓
WhatsApp bridge  →  audio voice note to customer
```

---

## 🧪 Testing with cURL

> **Pre-requisite**: FastAPI must be running.
> ```bash
> cd dando-vapi-tool
> uvicorn app.main:app --reload --port 8000
> ```
> Replace `YOUR_DEVICE_ID` with the `waSessionId` of a business that has
> WhatsApp linked (check Firestore → businesses → waSessionId field).
> The webhook secret is set by `WEBHOOK_SECRET` / `X_WEBHOOK_SECRET` in `.env`
> (default `boom2026` for local dev).

---

### 1. Health check

```bash
curl http://localhost:8000/health
```

Expected: `{"status":"ok"}`

---

### 2. WhatsApp text message → AI booking reply

```bash
curl -X POST http://localhost:8000/whatsmeow-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "event": "message",
    "device_id": "YOUR_DEVICE_ID",
    "payload": {
      "chat_id": "919876543210",
      "push_name": "Test Customer",
      "body": "Hi, I want to book a haircut for tomorrow at 3pm",
      "message_id": "txt-test-001",
      "is_from_me": false,
      "is_group": false,
      "message_type": "text"
    }
  }'
```

Expected: `200 OK` → AI sends a WhatsApp reply to the customer confirming
or asking for more details.

---

### 3. WhatsApp audio / voice note → STT → AI booking reply → TTS audio

**Step A** — serve a local audio file (run in a separate terminal):

```bash
# PowerShell — serve the test WAV from the tests folder
cd dando-vapi-tool\tests
python -m http.server 9001
```

**Step B** — POST the webhook with the `media_url` pointing to your local server:

```bash
curl -X POST http://localhost:8000/whatsmeow-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "event": "message",
    "device_id": "YOUR_DEVICE_ID",
    "payload": {
      "chat_id": "919876543210",
      "push_name": "Test Customer",
      "body": "",
      "message_id": "audio-test-001",
      "is_from_me": false,
      "is_group": false,
      "message_type": "audio",
      "media_url": "http://127.0.0.1:9001/_test_audio.wav",
      "mime_type": "audio/wav"
    }
  }'
```

> The first time, the test audio file is not present — run the automated
> E2E test once to download and cache it:
> ```bash
> set PYTHONPATH=c:\path\to\dando-vapi-tool
> set TEST_DEVICE_ID=YOUR_DEVICE_ID
> python tests\e2e_audio_test.py
> ```

**Step C (PTT / voice note format)** — same as Step B but with
`"message_type": "ptt"` and a real `.ogg` from the WhatsApp bridge:

```bash
curl -X POST http://localhost:8000/whatsmeow-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "event": "message",
    "device_id": "YOUR_DEVICE_ID",
    "payload": {
      "chat_id": "919876543210",
      "push_name": "Test Customer",
      "body": "",
      "message_id": "ptt-test-001",
      "is_from_me": false,
      "is_group": false,
      "message_type": "ptt",
      "media_url": "https://YOUR_BRIDGE_HOST/media/AUDIO_FILE_ID",
      "mime_type": "audio/ogg; codecs=opus"
    }
  }'
```

---

### 4. Check available booking slots

```bash
curl -X POST http://localhost:8000/api/v1/vapi/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "type": "tool-calls",
      "toolCallList": [{
        "id": "tc-001",
        "name": "getAvailableSlots",
        "parameters": {
          "businessId": "YOUR_BUSINESS_ID",
          "date": "2026-04-25",
          "durationMinutes": 60
        }
      }],
      "call": {"phoneNumberId": "", "customer": {"number": "+919876543210"}}
    }
  }'
```

---

### 5. Create a booking directly (VAPI tool call)

```bash
curl -X POST http://localhost:8000/api/v1/vapi/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "type": "tool-calls",
      "toolCallList": [{
        "id": "tc-002",
        "name": "createBooking",
        "parameters": {
          "businessId": "YOUR_BUSINESS_ID",
          "customerPhone": "919876543210",
          "customerName": "Test Customer",
          "serviceName": "Haircut",
          "dateTime": "2026-04-25T15:00:00",
          "durationMinutes": 60
        }
      }],
      "call": {"phoneNumberId": "", "customer": {"number": "+919876543210"}}
    }
  }'
```

---

### 6. Automated E2E audio test (Python)

Downloads a real speech sample, spins up a local media server, and fires
the full pipeline end-to-end:

```bash
set PYTHONPATH=c:\path\to\dando-vapi-tool
set TEST_DEVICE_ID=YOUR_DEVICE_ID
set TEST_PHONE=919876543210
python tests\e2e_audio_test.py
```

Check FastAPI logs for:
```
[AUDIO] Downloaded N bytes ... (mime='audio/wav' → effective='audio/wav')
[AUDIO] Transcript for ...: 'We the people ...'
[AUDIO] AI reply for ...: ...
Audio AI reply sent to ... for business ...
```

---

## 🔧 Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `[Errno 11001] getaddrinfo failed` | Windows DNS transient failure | DNS patch now retries 3× automatically |
| `Deepgram 400 corrupt or unsupported data` | Wrong Content-Type or empty bytes | Client now auto-retries with `application/octet-stream` |
| `Failed to download audio` but URL looks valid | DNS failure on first attempt | `download_media` now retries 3× with 2 s delay |
| `Downloaded bytes do not appear to be audio` | Bridge returned error page (expired media URL) | Check bridge logs; media URLs expire after ~60 s on some bridges |
| Audio reply not received | Cartesia API key missing / TTS failed | Check `CARTESIA_API_KEY`; falls back to text reply automatically |

