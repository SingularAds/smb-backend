import asyncio
from app.services.onboarding_service import OnboardingService

async def main():
    svc = OnboardingService()

    async def stub_generate_pair_code(session_id, phone_number):
        print(f"[stub] generate_pair_code called session_id={session_id} phone_number={phone_number}")
        return {"code": "1234-5678"}

    async def stub_send(phone, message):
        print(f"[stub send] to={phone} msg={message[:120]}")

    svc.wa.generate_pair_code = stub_generate_pair_code
    svc._send = stub_send

    session = {"pairingSessionId": "biz-test", "businessId": "BIZ-TEST"}
    print("Calling _send_pairing_code (should send code)")
    await svc._send_pairing_code(session, "351912341234")

    print('\nSimulate natural phrase -> _handle_pairing with "please resend the code"')
    await svc._handle_pairing(session, "351912341234", "please resend the code")

    print('\nSimulate phrase -> _handle_pairing with "i didnt get it"')
    await svc._handle_pairing(session, "351912341234", "i didnt get it")


if __name__ == '__main__':
    asyncio.run(main())
