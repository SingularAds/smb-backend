"""Live e2e test of the onboarding analyzer against production Firestore.

What it does (safe on prod):
  1. Archive round-trip on a FAKE phone (15550009999) → then deletes it.
  2. Finds a real dropped registration session (read-only pick).
  3. Runs the analyzer with the OpenAI provider (writes onboarding_analyses doc
     — the feature's intended behavior).
  4. Re-runs → must be served from cache (no LLM call).
  5. force=True with provider switched to "anthropic" → verifies the Claude
     path live and that one .env line switches providers.
  6. Appends feedback to the stored analysis.

Run: python scratch/test_analyzer_live.py
"""
import asyncio
import json
import os
import sys

workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, workspace_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(workspace_root, ".env"))

from app.firebase import init_firebase
init_firebase()

from app import firestore as db
from app.config import settings
from app.services import onboarding_analyzer_service as svc

TEST_PHONE = "15550009999"


def step(msg):
    print(f"\n=== {msg}")


async def main():
    # ── 1. Archive round-trip on a fake phone ────────────────────────────────
    step("1. transcript archive round-trip (fake phone)")

    def _purge():
        ref = db._db().collection("onboarding_transcripts").document(TEST_PHONE)
        for d in ref.collection("messages").stream():
            d.reference.delete()
        ref.delete()

    _purge()  # idempotent — clear leftovers from any previous run
    db.append_onboarding_transcript_message(TEST_PHONE, {
        "role": "user", "content": "hello, test", "ts": "2026-07-14T10:00:00",
        "step": "conversing",
    })
    db.append_onboarding_transcript_message(TEST_PHONE, {
        "role": "assistant", "content": "hi! (test)", "ts": "2026-07-14T10:00:05",
        "step": "conversing", "mode": "demo",
    })
    msgs = db.get_onboarding_transcript(TEST_PHONE)
    assert len(msgs) == 2, f"expected 2 archived msgs, got {len(msgs)}"
    assert msgs[0]["role"] == "user" and msgs[1]["mode"] == "demo"
    print(f"   archived + read back {len(msgs)} messages, ordered by ts OK")
    _purge()
    assert db.get_onboarding_transcript(TEST_PHONE) == []
    print("   cleaned up OK")

    # ── 2. Pick a real dropped session (read-only) ───────────────────────────
    step("2. picking a real dropped session")
    from app.services.analytics_service import is_test_session, is_registration_session
    candidate = None
    for doc in db._db().collection("onboarding_sessions").limit(300).stream():
        s = doc.to_dict()
        s["id"] = doc.id
        if is_test_session(doc.id, s) or not is_registration_session(s):
            continue
        history = s.get("conversationHistory") or []
        owner_msgs = sum(1 for m in history if isinstance(m, dict) and m.get("role") == "user")
        if owner_msgs >= 4 and not s.get("businessId"):
            candidate = doc.id
            break
    assert candidate, "no suitable dropped session found"
    print(f"   candidate: {candidate} ({owner_msgs} owner msgs)")

    # ── 3. Fresh analysis (OpenAI provider) ──────────────────────────────────
    step("3. fresh analysis — provider=openai")
    settings.ONBOARDING_ANALYZER_PROVIDER = "openai"
    result = await svc.analyze_onboarding_session(candidate, force=True)
    assert result["cached"] is False
    a = result["analysis"]
    assert a["outcome"] in ("completed", "dropped", "still_active")
    assert a["summary"] and a["customerIntent"]
    print(f"   provider={result['provider']} model={result['model']} src={result['transcriptSource']}")
    print(f"   outcome={a['outcome']} reason={a['dropOffReason']} confidence={a['confidence']}")
    print(f"   summary: {a['summary'][:180]}")
    print(f"   recommendations: {len(a['recommendations'])} | evidence: {len(a['evidence'])}")

    # ── 4. Cache hit ─────────────────────────────────────────────────────────
    step("4. repeat call — must be served from cache")
    cached = await svc.analyze_onboarding_session(candidate)
    assert cached["cached"] is True, "expected cache hit"
    assert cached["analysis"]["summary"] == a["summary"]
    print("   cache hit, no LLM call OK")

    # ── 5. Provider switch → Claude ──────────────────────────────────────────
    step("5. force re-run — provider=anthropic (Claude)")
    settings.ONBOARDING_ANALYZER_PROVIDER = "anthropic"
    try:
        claude_res = await svc.analyze_onboarding_session(candidate, force=True)
        ca = claude_res["analysis"]
        assert claude_res["provider"] == "anthropic"
        assert ca["outcome"] in ("completed", "dropped", "still_active")
        print(f"   provider={claude_res['provider']} model={claude_res['model']}")
        print(f"   outcome={ca['outcome']} reason={ca['dropOffReason']} confidence={ca['confidence']}")
        print(f"   summary: {ca['summary'][:180]}")
        print("   Claude provider path works OK")
    finally:
        settings.ONBOARDING_ANALYZER_PROVIDER = "openai"

    # ── 6. Feedback ──────────────────────────────────────────────────────────
    step("6. feedback append")
    await svc.record_analysis_feedback(candidate, True, "live e2e test feedback")
    stored = db.get_onboarding_analysis(candidate)
    assert stored and any(f.get("note") == "live e2e test feedback" for f in stored.get("feedback", []))
    print(f"   feedback stored ({len(stored['feedback'])} entr{'y' if len(stored['feedback'])==1 else 'ies'}) OK")

    print("\nALL LIVE CHECKS PASSED")


asyncio.run(main())
