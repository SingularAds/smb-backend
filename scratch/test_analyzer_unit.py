"""Unit tests for the onboarding analyzer service — pure logic, no network.

Run: python scratch/test_analyzer_unit.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, workspace_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(workspace_root, ".env"))

from app.services import onboarding_analyzer_service as svc

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


# ── _parse_analysis ───────────────────────────────────────────────────────────
GOOD = {
    "customerIntent": "Automate bookings for a salon",
    "outcome": "dropped",
    "dropOffStage": "details_collected",
    "dropOffReason": "PRICING_CONCERN",
    "confidence": "high",
    "evidence": ["quanto custa? [how much is it?]"],
    "objections": ["price too high"],
    "frictionPoints": ["pricing asked twice, answered late"],
    "recommendations": ["state pricing earlier"],
    "summary": "Owner engaged then left after the price reveal.",
}
import json

a = svc._parse_analysis(json.dumps(GOOD))
check("parse: plain JSON", a.dropOffReason == "PRICING_CONCERN")

a = svc._parse_analysis("```json\n" + json.dumps(GOOD) + "\n```")
check("parse: fenced JSON", a.outcome == "dropped")

a = svc._parse_analysis("Here is the analysis:\n" + json.dumps(GOOD) + "\nDone.")
check("parse: JSON with prose around it", a.confidence == "high")

try:
    svc._parse_analysis("not json at all")
    check("parse: invalid raises AnalyzerError", False)
except svc.AnalyzerError:
    check("parse: invalid raises AnalyzerError", True)

try:
    bad = dict(GOOD, outcome="vanished")  # not in the Literal
    svc._parse_analysis(json.dumps(bad))
    check("parse: schema violation raises", False)
except svc.AnalyzerError:
    check("parse: schema violation raises", True)

# ── _derive_outcome ───────────────────────────────────────────────────────────
now = datetime.now(timezone.utc)
check("outcome: business doc wins", svc._derive_outcome({}, {"id": "b1"}) == "completed")
recent = {"timestamps": {"lastActivityAt": (now - timedelta(hours=3)).isoformat()}}
check("outcome: active <48h", svc._derive_outcome(recent, None) == "still_active")
old = {"timestamps": {"lastActivityAt": (now - timedelta(days=10)).isoformat()}}
check("outcome: dropped >48h", svc._derive_outcome(old, None) == "dropped")
check("outcome: no session -> dropped", svc._derive_outcome(None, None) == "dropped")

# ── _format_transcript ────────────────────────────────────────────────────────
few = [{"role": "user", "content": f"m{i}"} for i in range(5)]
out = svc._format_transcript(few)
check("transcript: all messages when short", out.count("OWNER") == 5)

many = [
    {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
    for i in range(150)
]
out = svc._format_transcript(many)
check("transcript: head+tail sampling", "messages omitted" in out and "m0" in out and "m149" in out)
check("transcript: omitted count correct", "50 messages omitted" in out)

demo = [{"role": "assistant", "content": "hi", "mode": "demo", "step": "conversing"}] * 1
out = svc._format_transcript(demo + few)
check("transcript: demo tagged", "(conversing/DEMO)" in out)

ts_msg = [{"role": "user", "content": "hello", "ts": "2026-07-10T14:32:00"}]
out = svc._format_transcript(ts_msg)
check("transcript: timestamp rendered", "[2026-07-10 14:32]" in out)

# ── _build_session_facts ──────────────────────────────────────────────────────
session = {
    "currentStep": "conversing",
    "language": "pt",
    "timestamps": {
        "startedAt": (now - timedelta(days=5)).isoformat(),
        "lastActivityAt": (now - timedelta(days=4)).isoformat(),
    },
    "attribution": {"channel": "meta_ads", "campaign": "julho"},
    "businessData": {"businessName": "Salão X"},
}
msgs = [
    {"role": "user", "content": "hi", "ts": (now - timedelta(days=5)).isoformat()},
    {"role": "assistant", "content": "hello!", "ts": (now - timedelta(days=5) + timedelta(minutes=2)).isoformat()},
    {"role": "user", "content": "price?", "ts": (now - timedelta(days=4)).isoformat(), "mode": "demo"},
]
facts = svc._build_session_facts("351911111111", session, None, msgs, "archive", "dropped")
check("facts: outcome present", "Outcome: dropped" in facts)
check("facts: channel present", "channel=meta_ads" in facts)
check("facts: demo count", "Demo-mode messages: 1" in facts)
check("facts: gap computed", "Longest gap" in facts and "unknown (no per-message" not in facts)
check("facts: 24h gap value", "23." in facts or "24." in facts)

legacy_facts = svc._build_session_facts(
    "351911111111", session, None,
    [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}],
    "session_history", "dropped",
)
check("facts: legacy note", "legacy history" in legacy_facts)

# ── provider switch ───────────────────────────────────────────────────────────
from app.config import settings
orig = settings.ONBOARDING_ANALYZER_PROVIDER
settings.ONBOARDING_ANALYZER_PROVIDER = "openai"
p, m = svc._provider_and_model()
check("provider: openai default", p == "openai" and m == settings.ONBOARDING_ANALYZER_OPENAI_MODEL)
settings.ONBOARDING_ANALYZER_PROVIDER = "anthropic"
p, m = svc._provider_and_model()
check("provider: anthropic switch", p == "anthropic" and m == "claude-opus-4-8")
settings.ONBOARDING_ANALYZER_PROVIDER = orig

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
