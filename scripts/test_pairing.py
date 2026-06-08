import asyncio
from app.services.onboarding_service import OnboardingService
from app import firestore as db

async def main():
    svc = OnboardingService()

    async def stub_generate_pair_code(session_id, phone_number):
        print(f"[stub] generate_pair_code called session_id={session_id} phone_number={phone_number}")
        return {"code": "1234-5678"}

    async def stub_send(phone, message):
        # Replace emojis and non-ascii characters to avoid CP1252 print errors on Windows console
        safe_msg = message[:120].encode('ascii', 'replace').decode('ascii')
        print(f"[stub send] to={phone} msg={safe_msg}")

    def stub_upsert_onboarding_session(phone, data):
        print(f"[stub db] upsert_onboarding_session phone={phone} data={data}")
        return None

    def stub_get_onboarding_session(phone):
        print(f"[stub db] get_onboarding_session phone={phone}")
        return {"currentStep": "pairing", "pairingAttemptId": "mock-attempt-id"}

    svc.wa.generate_pair_code = stub_generate_pair_code
    svc._send = stub_send
    db.upsert_onboarding_session = stub_upsert_onboarding_session
    db.get_onboarding_session = stub_get_onboarding_session

    session = {"pairingSessionId": "biz-test", "businessId": "BIZ-TEST"}
    print("Calling _send_pairing_code (should send code)")
    await svc._send_pairing_code(session, "351912341234")

    print('\nSimulate natural phrase -> _handle_pairing with "please resend the code"')
    await svc._handle_pairing(session, "351912341234", "please resend the code")

    print('\nSimulate phrase -> _handle_pairing with "i didnt get it"')
    await svc._handle_pairing(session, "351912341234", "i didnt get it")


if __name__ == '__main__':
    asyncio.run(main())
