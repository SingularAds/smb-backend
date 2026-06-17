import asyncio
import sys
import io
import os
from unittest.mock import MagicMock, AsyncMock

# Configure stdout to use UTF-8 to avoid console encoding crashes with emojis
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Ensure the root of the project is in the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.onboarding_service import OnboardingService, _link_request_message
from app import firestore as db

# Mock firestore functions to avoid hitting DB
db.upsert_onboarding_session = MagicMock()
db.update_business_doc = MagicMock()
db.merge_business_doc = MagicMock()
db.get_business_by_id = MagicMock(return_value={
    "calendarConnected": True,
    "ownerName": "John",
    "name": "My Shop",
    "businessType": "Retail",
    "address": "123 Main St, New York, NY",
})

# Mock Whatsmeow client with correct methods
class MockWhatsmeowClient:
    default_device_id = "mock-device-id"
    def __init__(self, *args, **kwargs):
        pass
    async def get_session_status(self, session_id):
        return {"status": "disconnected"}
    async def generate_pair_code(self, session_id, phone_number):
        return {"code": "ABCD-1234"}
    async def logout_session(self, session_id):
        pass
    async def reconnect_session(self, session_id):
        pass
    async def send_image(self, *args, **kwargs):
        pass

import app.services.onboarding_service
app.services.onboarding_service.WhatsmeowClient = MockWhatsmeowClient

async def main():
    service = OnboardingService()
    
    # Capture sent messages
    sent_messages = []
    async def mock_send(phone, msg):
        sent_messages.append(msg)
    service._send = mock_send
    
    # Mock localization to just return the string (English copy verification)
    async def mock_localize_static(text, body, lang):
        return text
    service._localize_static = mock_localize_static

    # Define a generic mock session
    session = {
        "pushName": "John",
        "language": "en",
        "businessId": "mock-biz-id",
        "businessData": {
            "ownerName": "John",
            "name": "My Shop",
            "businessType": "Retail",
            "address": "123 Main St, New York, NY"
        }
    }
    phone = "1234567890"

    print("========================================")
    print("VERIFYING ONBOARDING MESSAGES COPY")
    print("========================================")

    # 1. Welcome
    print("\n--- 1. Welcome Message ---")
    msg1_with_name = _link_request_message("en", "John")
    print("With Name:")
    print(msg1_with_name)
    print("\nWithout Name:")
    msg1_no_name = _link_request_message("en", None)
    print(msg1_no_name)

    # 2. Location
    print("\n--- 2. Location Message ---")
    loc_msg = (
        "Perfect! 📍 Let’s find you on the map. "
        "Tap 📎 → Location → Send Your Current Location. Takes 2 seconds 🙌"
    )
    print(loc_msg)

    # 3. Found / Confirm
    print("\n--- 3. Found / Confirm Message ---")
    sent_messages.clear()
    lead = {
        "name": "My Shop",
        "businessType": "Retail",
        "address": "123 Main St, New York, NY",
    }
    await service._show_lead_confirmation(phone, "yes", "John", "msg-1", lead)
    for msg in sent_messages:
        print(msg)

    # 4. Referrals
    print("\n--- 4. Referrals Message ---")
    sent_messages.clear()
    await service._start_referral_step(session, phone, "John", {})
    for msg in sent_messages:
        print(msg)

    # 5. Live + Connect Choice
    print("\n--- 5. Live + Connect Choice Message ---")
    sent_messages.clear()
    await service._start_pairing_mode_choice(session, phone, "My Shop")
    for msg in sent_messages:
        print(msg)

    # 6. Scam Warning
    print("\n--- 6. Scam Warning Message ---")
    sent_messages.clear()
    await service._start_scam_warning(session, phone)
    for msg in sent_messages:
        print(msg)

    # 7 & 8. Pairing Instructions & Code
    print("\n--- 7 & 8. Pairing Instructions & Code Messages ---")
    sent_messages.clear()
    await service._send_pairing_code(session, phone)
    for i, msg in enumerate(sent_messages, start=7):
        print(f"[Sub-message {i}]:")
        print(msg)
        print("-" * 20)

    # 9. Linked Success
    print("\n--- 9. Linked Success Message ---")
    msg9 = (
        "🎉 Connected! We’re officially a team now 🤝 "
        "From this moment, no customer slips through the cracks 💪"
    )
    print(msg9)

    # 10. Calendar Setup
    print("\n--- 10. Calendar Setup Message ---")
    sent_messages.clear()
    await service._transition_to_calendar_setup(session, phone)
    for msg in sent_messages:
        print(msg)

    # 11. Calendar Connected
    print("\n--- 11. Calendar Connected Message ---")
    sent_messages.clear()
    await service._handle_calendar_setup(session, phone, "done")
    for msg in sent_messages:
        print(msg)

    # 12. Missed Calls Forwarding
    print("\n--- 12. Missed Calls Forwarding Message ---")
    sent_messages.clear()
    service._get_call_forwarding_number = MagicMock(return_value="+1234567890")
    await service._transition_to_call_forwarding(session, phone)
    for msg in sent_messages:
        print(msg)

    # 13. All Set (Completion)
    print("\n--- 13. All Set (Completion) Message ---")
    sent_messages.clear()
    await service._complete_onboarding(session, phone)
    for msg in sent_messages:
        print(msg)

if __name__ == "__main__":
    asyncio.run(main())
