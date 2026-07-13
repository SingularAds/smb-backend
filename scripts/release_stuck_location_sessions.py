"""One-off: release onboarding sessions stuck in the location_request jail.

Context (prod bug 2026-07-13): sessions parked at currentStep="location_request"
re-sent the same static location prompt on every message, forever. The runtime
fix (locationPromptCount + 12h stale-gate TTL in onboarding_service.py) heals
each session on its NEXT inbound message. This script heals them NOW, so no
candidate sees even one more repeated prompt.

Surgical by design — it does NOT change onboarding flow or behavior:
  * Touches ONLY docs where currentStep == "location_request" (the one jailed
    step). Pairing / billing / complete / post_onboarding sessions are never
    read, let alone written.
  * Writes ONLY two fields via the same production upsert (Firestore merge):
        currentStep         -> "conversing"
        locationPromptCount -> 0
    Everything else — conversationHistory, askedForLocation,
    pendingPlacesQuery, language, attribution, timestamps — is untouched.
  * "conversing" is the normal default step: the AI answers the candidate's
    next message with full history. If the candidate later shares a location
    anyway, the conversing path routes it to _handle_location_share, which
    resumes the saved pendingPlacesQuery — the exact same resume path as the
    gate itself. askedForLocation stays True, so Places will fall back to
    text search and can never re-trap the session.
  * Sends no messages. Read-only unless --apply is passed.

Usage:
    .venv/Scripts/python scripts/release_stuck_location_sessions.py           # dry run (default)
    .venv/Scripts/python scripts/release_stuck_location_sessions.py --apply   # actually release
"""
import os
import sys

# Windows consoles default to cp1252 — emoji in session data crashes print().
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

from app.firebase import init_firebase  # noqa: E402

init_firebase()

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from app import firestore as db  # noqa: E402


def _mask(phone: str) -> str:
    """Mask all but the last 4 digits for log output."""
    return ("*" * max(len(phone) - 4, 0)) + phone[-4:] if phone else "?"


def main() -> None:
    apply_changes = "--apply" in sys.argv

    snapshot = (
        db._db()
        .collection("onboarding_sessions")
        .where(filter=FieldFilter("currentStep", "==", "location_request"))
        .stream()
    )

    stuck = list(snapshot)
    print(f"Found {len(stuck)} session(s) stuck at currentStep='location_request'\n")

    for doc in stuck:
        data = doc.to_dict() or {}
        phone = doc.id
        last_activity = (
            (data.get("timestamps") or {}).get("lastActivityAt")
            or data.get("lastActivityAt")
            or data.get("createdAt")
            or "?"
        )
        print(
            f"  phone={_mask(phone)}  lastActivity={last_activity}  "
            f"pendingPlacesQuery={data.get('pendingPlacesQuery')!r}  "
            f"historyLen={len(data.get('conversationHistory') or [])}"
        )
        if apply_changes:
            # Same production upsert (Firestore merge) — only these two fields.
            db.upsert_onboarding_session(phone, {
                "currentStep": "conversing",
                "locationPromptCount": 0,
            })
            print(f"    -> released to 'conversing'")

    if not apply_changes:
        print("\nDRY RUN — nothing written. Re-run with --apply to release these sessions.")
    else:
        print(f"\nDone — {len(stuck)} session(s) released.")


if __name__ == "__main__":
    main()
