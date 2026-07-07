import os
import sys
from dotenv import load_dotenv

# Add workspace root to sys.path so we can import app modules
workspace_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, workspace_root)

# Load environment variables
load_dotenv(os.path.join(workspace_root, ".env"))

from app.firebase import init_firebase
from firebase_admin import firestore

def main():
    try:
        # Initialize Firebase
        init_firebase()
        
        # Get firestore client
        db = firestore.client()
        
        # Fetch onboarding sessions
        sessions_ref = db.collection("onboarding_sessions")
        docs = list(sessions_ref.stream())
        
        print(f"TOTAL_SESSIONS_FOUND: {len(docs)}")
        print("SESSIONS_LIST_START")
        for doc in docs:
            print(f"{doc.id}")
        print("SESSIONS_LIST_END")
        
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
