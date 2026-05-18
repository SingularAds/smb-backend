"""Quick smoke test for all new owner commands."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.owner.commands import parser, services, handlers, language  # noqa

p = parser.parse_command
tests = [
    ("i want to cancel my booking",   "cancel"),
    ("closed friday",                  "close_day"),
    ("closed 2026-12-25",             "close_day"),
    ("open saturday",                  "close_day"),
    ("change hours Mon-Sat 9-19",     "change_hours"),
    ("change hours Sunday closed",    "change_hours"),
    ("inactive clients 60",           "inactive_clients"),
    ("send outreach Hey come back",   "send_outreach"),
    ("add faq: Walk-ins? | Yes!",     "add_faq"),
    ("add stylist Maria, specialties: color", "add_stylist"),
    ("change vibe to casual",         "change_vibe"),
    ("scan https://mysite.com",       "scan_website"),
    ("turn off auto reply",           "auto_reply_off"),
    ("turn on auto reply",            "auto_reply_on"),
    ("show services",                 "show_services"),
    ("add service Haircut | 30 | 15", "add_service"),
    ("remove service 2",              "remove_service"),
    ("today",                         "today"),
    ("summary",                       "summary"),
    ("help",                          "help"),
]

ok = True
for text, expected in tests:
    result = p(text)
    got = result["type"]
    status = "OK" if got == expected else "FAIL"
    if status == "FAIL":
        ok = False
    print(f"  [{status}] {text!r:45s} => {got} (args={result['args']})")

print()
print("ALL PASSED" if ok else "SOME FAILURES — see above")
sys.exit(0 if ok else 1)
