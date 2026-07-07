"""Send a synthetic whatsmeow-bridge webhook POST at a locally-running backend.

Drives the REAL /whatsmeow-webhook endpoint end-to-end (dedup, routing,
onboarding/customer-AI, real Anthropic/OpenAI calls, real outbound WhatsApp
send through the already-paired bridge) — the only thing "synthetic" is that
this script plays the role of the Go bridge for one HTTP POST.

Run against the ISOLATED test instance (see scripts/run_local_test.md), never
against the live production port, so a bad reply during testing can't reach a
real customer twice or race the live process's own dedup cache.

Usage:
    .venv/Scripts/python scripts/send_test_webhook.py --scenario greeting
    .venv/Scripts/python scripts/send_test_webhook.py --scenario language_switch
    .venv/Scripts/python scripts/send_test_webhook.py --scenario cta_yes
    .venv/Scripts/python scripts/send_test_webhook.py --scenario custom --body "..." --device-id smba

    # target a different port / phone:
    .venv/Scripts/python scripts/send_test_webhook.py --scenario greeting --port 8010 --phone 918294746282
"""
import argparse
import os
import sys
import time
import uuid

import httpx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from app.config import settings  # noqa: E402

# Default test phone — the number visibly used for onboarding/demo testing in
# your own production logs (push_name Abhishek). Override with --phone.
DEFAULT_PHONE = "918294746282"
DEFAULT_DEVICE = settings.WHATSMEOW_ONBOARDING_DEVICE_ID or settings.WHATSMEOW_DEFAULT_DEVICE_ID or "smba"

SCENARIOS = {
    # Issue 1a: session language was sticky — this should now get a Portuguese
    # reply even though earlier turns in the session may have been English.
    "language_switch": "Olá! Posso ter mais informações sobre isso?",
    # Issue 1b: bare "Hi" as a first-ever message — greeting + link request.
    "greeting": "Hi",
    # Issue 1b: a bare "yes" answering a previously-sent onboarding CTA.
    # Only meaningful as a SECOND call after one that set onboardingCtaOffered=True
    # (e.g. run --scenario greeting, then re-run this once a sales reply was sent).
    "cta_yes": "Yes",
}


def build_payload(body: str, device_id: str, phone: str, msg_id: str) -> dict:
    return {
        "event": "message",
        "device_id": device_id,
        "phone": "",  # device's own number — left blank, never equals `phone` below
        "payload": {
            "from": f"{phone}@s.whatsapp.net",
            "chat_id": phone,
            "body": body,
            "message_id": msg_id,
            "push_name": "Local-Test",
            "timestamp": int(time.time()),
            "message_type": "text",
            "is_from_me": False,
            "is_group": False,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", choices=[*SCENARIOS, "custom"], default="greeting")
    ap.add_argument("--body", default=None, help="Message body for --scenario custom")
    ap.add_argument("--port", type=int, default=8010, help="Local TEST instance port (not 8000/prod)")
    ap.add_argument("--phone", default=DEFAULT_PHONE, help="Customer/owner phone (chat_id)")
    ap.add_argument("--device-id", default=DEFAULT_DEVICE, help="smba (onboarding) or biz-<...> (a paired business)")
    args = ap.parse_args()

    if args.port == 8000:
        print("Refusing to target port 8000 — that's the live production instance. "
              "Start a second instance on another port first (see scripts/run_local_test.md).")
        sys.exit(1)

    body = args.body if args.scenario == "custom" else SCENARIOS[args.scenario]
    if not body:
        print("--body is required for --scenario custom")
        sys.exit(1)

    msg_id = uuid.uuid4().hex.upper()
    payload = build_payload(body, args.device_id, args.phone, msg_id)

    url = f"http://127.0.0.1:{args.port}/whatsmeow-webhook"
    headers = {"X-Webhook-Secret": settings.WEBHOOK_SECRET or settings.X_WEBHOOK_SECRET or ""}

    print(f"POST {url}")
    print(f"  device_id={args.device_id!r} phone={args.phone!r} msg_id={msg_id}")
    print(f"  body={body!r}")

    resp = httpx.post(url, json=payload, headers=headers, timeout=10)
    print(f"  -> {resp.status_code} {resp.text}")
    print("\nWatch the test instance's console for [ONBOARDING-RESPONSE] / [WEBHOOK] "
          "log lines, and check WhatsApp on the phone number above for the actual reply.")


if __name__ == "__main__":
    main()
