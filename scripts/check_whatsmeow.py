import asyncio
import json
import os
import sys
import traceback

# Ensure project root is on sys.path so imports work when running this script directly
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.services.whatsmeow_client import WhatsmeowClient

async def main():
    c = WhatsmeowClient()
    print("BASE_URL:", c.base_url)
    print("DEFAULT_DEVICE:", c.default_device_id)
    try:
        health = await c.health_check()
        print("HEALTH:", json.dumps(health))
    except Exception as e:
        print("HEALTH_ERROR:", repr(e))
    try:
        session_id = "biz-916387400721"
        status = await c.get_session_status(session_id)
        print("SESSION_STATUS:", json.dumps(status))
    except Exception:
        print("SESSION_ERROR:")
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(main())
