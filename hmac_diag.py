"""
Run after triggering a Chatwoot webhook to find which secret Chatwoot uses.

  python hmac_diag.py
"""
import base64, hashlib, hmac, json, os, tempfile

diag_path = os.path.join(tempfile.gettempdir(), "chatwoot_hmac_diag.json")
with open(diag_path) as f:
    d = json.load(f)

body = base64.b64decode(d["body_b64"])
received = d["received_sig"]

print(f"Body length  : {len(body)} bytes")
print(f"Received sig : {received}\n")

# ── All candidate secrets to test ────────────────────────────────────────────
# Add any extra secrets you want to check below.
candidates = {
    "CHATWOOT_HMAC_SECRET (configured)": d["configured_secret"],
    # Fill these in from your Chatwoot .env / dashboard:
    "CHATWOOT_WEBSITE_TOKEN": "ek99zU5gKjF2GRbZdmd8cgtM",
    "CHATWOOT_ACCESS_TOKEN":  "KAx6BXtURzkkSxLM8J1bXUGJ",
    # Add any other candidates you want to try:
    # "another": "...",
}

found = False
for label, secret in candidates.items():
    if not secret:
        continue
    computed = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    match = computed == received
    if match:
        found = True
        print(f"✅ MATCH  {label!r}")
        print(f"   → Set CHATWOOT_HMAC_SECRET={secret!r} in .env")
    else:
        print(f"❌        {label!r}")
        print(f"          computed : {computed}")

if not found:
    print(
        "\nNo candidate matched. Chatwoot may be using an inbox-level HMAC token "
        "not shown in webhook settings.\n"
        "Go to Chatwoot → Settings → Inboxes → [your inbox] → Configuration → "
        "look for 'HMAC Token' and add it to candidates above."
    )
else:
    print("\nUpdate CHATWOOT_HMAC_SECRET in .env to the matched value and restart.")
