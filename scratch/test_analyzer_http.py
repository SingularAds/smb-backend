"""HTTP-level e2e test of the analyzer endpoints against a local server.

Prereq: uvicorn app.main:app --port 8765 (DISABLE_SCHEDULER=true).
Run: python scratch/test_analyzer_http.py
"""
import os
import sys
import time

import httpx

workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, workspace_root)
from dotenv import load_dotenv
load_dotenv(os.path.join(workspace_root, ".env"))

BASE = "http://127.0.0.1:8765"
KEY = os.environ["ANALYTICS_ADMIN_KEY"]
HEADERS = {"x-admin-key": KEY}
PHONE = "120363155094250235"  # dropped session used by the live service test

# Wait for server readiness
for i in range(60):
    try:
        r = httpx.get(f"{BASE}/api/v1/health", timeout=2)
        if r.status_code < 500:
            break
    except Exception:
        pass
    time.sleep(1)
else:
    sys.exit("server did not become ready")
print("server ready")

client = httpx.Client(base_url=BASE, timeout=120)

# 1. Auth guard: no key -> 401
r = client.post(f"/api/v1/analytics/onboarding-sessions/{PHONE}/analyze")
assert r.status_code == 401, r.status_code
print("1. analyze without admin key -> 401 OK")

# 2. Analyze (fresh or cached depending on fingerprint) -> 200 with contract fields
r = client.post(f"/api/v1/analytics/onboarding-sessions/{PHONE}/analyze", headers=HEADERS)
assert r.status_code == 200, (r.status_code, r.text[:300])
body = r.json()
for field in ("analysis", "provider", "model", "promptVersion", "analyzedAt", "cached", "messageCount"):
    assert field in body, f"missing {field}"
a = body["analysis"]
assert a["outcome"] in ("completed", "dropped", "still_active")
print(f"2. analyze -> 200 (cached={body['cached']}, provider={body['provider']}, outcome={a['outcome']}) OK")

# 3. Repeat -> must be cache hit now
r = client.post(f"/api/v1/analytics/onboarding-sessions/{PHONE}/analyze", headers=HEADERS)
assert r.status_code == 200 and r.json()["cached"] is True
print("3. repeat analyze -> cached=true OK")

# 4. Unknown phone -> 404
r = client.post("/api/v1/analytics/onboarding-sessions/19999999990001/analyze", headers=HEADERS)
assert r.status_code == 404, (r.status_code, r.text[:200])
print("4. unknown phone -> 404 OK")

# 5. Feedback -> 200
r = client.post(
    f"/api/v1/analytics/onboarding-sessions/{PHONE}/analysis-feedback",
    headers=HEADERS, json={"helpful": False, "note": "http e2e test"},
)
assert r.status_code == 200 and r.json()["ok"] is True
print("5. feedback -> 200 OK")

# 6. Feedback for phone with no analysis -> 404
r = client.post(
    "/api/v1/analytics/onboarding-sessions/19999999990001/analysis-feedback",
    headers=HEADERS, json={"helpful": True},
)
assert r.status_code == 404
print("6. feedback unknown phone -> 404 OK")

# 7. Analyzer context: GET, PUT content, GET back, PUT empty (leave prod clean)
r = client.get("/api/v1/analytics/analyzer-context", headers=HEADERS)
assert r.status_code == 200 and "content" in r.json()
original = r.json()["content"]
r = client.put("/api/v1/analytics/analyzer-context", headers=HEADERS,
               json={"content": "TEST marketing context (http e2e)"})
assert r.status_code == 200 and r.json()["chars"] > 0
r = client.get("/api/v1/analytics/analyzer-context", headers=HEADERS)
assert "TEST marketing context" in r.json()["content"]
r = client.put("/api/v1/analytics/analyzer-context", headers=HEADERS,
               json={"content": original})
assert r.status_code == 200
print("7. analyzer-context GET/PUT round-trip (restored) OK")

# 8. Built dashboard served
r = client.get("/dashboard/")
assert r.status_code == 200 and "html" in r.headers.get("content-type", "")
print("8. dashboard index served OK")

print("\nALL HTTP CHECKS PASSED")
