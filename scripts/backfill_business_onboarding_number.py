"""One-time backfill: denormalize ``onboardingNumber``/``onboardingDeviceId``
onto ``businesses`` docs (from the owner's onboarding session).

WHY: analytics attributes a business to a global onboarding number. That used
to be derived ONLY via ownerPhone → onboarding_sessions, so a deleted session
(new-biz confirm wipe, or manual cleanup during testing) silently turned the
business "unattributed" — visible in the Accounts table but missing from the
per-number dashboard filter (prod case 2026-07-23: 919905252720 / "Patna
edits"). Business creation now stamps both fields at source
(onboarding_service); this script heals businesses created before that.

Two modes, combinable:
  * DEFAULT: for each business missing onboardingNumber, copy the number/device
    its owner's onboarding session captured. Safe — the session's number is the
    immutable capture, never a live registry lookup.
  * --pin ownerPhone:number[:device] (repeatable): explicit stamp for ORPHANS —
    businesses whose session was deleted, so there is nothing to copy from.
    Use only when you positively know which number onboarded that owner
    (e.g. from onboarding_transcripts + the config that was live that day).

⚠️  NEVER bulk-stamp orphans from the live device→number registry: the number
    behind a device id can be re-pointed (smba has already changed numbers),
    so "today's registry" applied to old businesses corrupts history.

Safety:
  * dry-run by default; pass --commit to actually write.
  * only fills businesses missing onboardingNumber; never overwrites.

Examples:
  python scripts/backfill_business_onboarding_number.py                # dry-run
  python scripts/backfill_business_onboarding_number.py --commit
  python scripts/backfill_business_onboarding_number.py \
      --pin 919905252720:919801111352:smba --commit
"""
import argparse
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, workspace_root)

from dotenv import load_dotenv

load_dotenv(os.path.join(workspace_root, ".env"))

from app.firebase import init_firebase  # noqa: E402
from firebase_admin import firestore  # noqa: E402


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def parse_pin(raw: str) -> tuple[str, str, str]:
    """'ownerPhone:number[:device]' → (owner_digits, number_digits, device)."""
    parts = raw.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError(
            f"--pin must be ownerPhone:number[:device], got {raw!r}"
        )
    owner, number = _digits(parts[0]), _digits(parts[1])
    device = parts[2].strip() if len(parts) == 3 else ""
    if not owner or not number:
        raise argparse.ArgumentTypeError(f"--pin needs phone digits: {raw!r}")
    return owner, number, device


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pin", action="append", type=parse_pin, default=[],
                    help="ownerPhone:number[:device] explicit stamp for a "
                         "business whose session no longer exists (repeatable)")
    ap.add_argument("--commit", action="store_true",
                    help="Actually write (default: dry-run)")
    args = ap.parse_args()

    init_firebase()
    db = firestore.client()
    pins = {owner: (number, device) for owner, number, device in args.pin}

    print(f"Mode: {'COMMIT' if args.commit else 'DRY-RUN'}")
    if pins:
        print("Pins:")
        for owner, (number, device) in pins.items():
            print(f"   {owner} -> number={number} device={device or '(none)'}")

    # Owner phone → (number, device) captured on the onboarding session.
    session_capture: dict[str, tuple[str, str]] = {}
    for d in db.collection("onboarding_sessions").stream():
        s = d.to_dict() or {}
        number = _digits(s.get("onboardingNumber"))
        if not number:
            continue
        device = (s.get("onboardingDeviceId") or "").strip()
        owner = _digits(s.get("ownerPhone")) or _digits(d.id)
        if owner:
            session_capture[owner] = (number, device)
    print(f"Sessions with a captured number: {len(session_capture)}")

    stamped = skipped_has = skipped_no_source = 0
    for d in db.collection("businesses").stream():
        b = d.to_dict() or {}
        if _digits(b.get("onboardingNumber")):
            skipped_has += 1
            continue
        owner = _digits(b.get("ownerPhone"))
        source = "session"
        capture = session_capture.get(owner)
        if capture is None and owner in pins:
            capture, source = pins[owner], "pin"
        if capture is None:
            skipped_no_source += 1
            continue
        number, device = capture
        update = {"onboardingNumber": number}
        if device and not (b.get("onboardingDeviceId") or "").strip():
            update["onboardingDeviceId"] = device
        stamped += 1
        print(f"  {'STAMP' if args.commit else 'WOULD STAMP'} businesses/{d.id} "
              f"({b.get('name')!r}, owner={owner}) <- {update} [{source}]")
        if args.commit:
            d.reference.update(update)

    print(
        f"\nDone. stamped={stamped} skipped_has_number={skipped_has} "
        f"skipped_no_session_or_pin={skipped_no_source}"
    )
    if not args.commit:
        print("Dry-run only — re-run with --commit to write.")


if __name__ == "__main__":
    main()
