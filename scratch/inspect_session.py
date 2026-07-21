import asyncio
import os
import sys

# Add app to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Load dotenv if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app.firebase import init_firebase
init_firebase()

from app.firestore import get_onboarding_session, get_business_by_owner_phone, get_recepte_lead_by_phone, get_website_lead_by_phone

async def main():
    phone = "917696794756"
    print("--- FIRESTORE LOOKUP FOR PHONE:", phone, "---")
    
    session = get_onboarding_session(phone)
    print("\n[Onboarding Session]:")
    if session:
        for k, v in session.items():
            if k == "conversationHistory":
                print(f"  {k}: {len(v)} turns")
                for i, turn in enumerate(v):
                    role = turn.get('role')
                    content = turn.get('content', '')
                    # Safe print for windows console
                    safe_content = content.encode('ascii', 'replace').decode('ascii')
                    print(f"    {i+1}. {role}: {safe_content}")
            else:
                print(f"  {k}: {repr(v)}")
    else:
        print("  None")

if __name__ == "__main__":
    asyncio.run(main())
