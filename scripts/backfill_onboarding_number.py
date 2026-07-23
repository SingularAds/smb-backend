"""One-time backfill: stamp the immutable ``onboardingNumber`` on legacy
onboarding_sessions that only recorded a bridge ``onboardingDeviceId``.

WHY: analytics now attributes a session to a global number via the number
captured at onboarding time (see analytics_service.session_number). Sessions
created before that capture existed have only a device id. Attribution derived
from the live device→number registry is unsafe because that registry is
mutable — so those sessions read as "unattributed" until backfilled here.

⚠️  RUN THIS ONLY IN THE ENVIRONMENT WHOSE CONFIG STILL MATCHES THE DEVICE→
    NUMBER BINDING THAT WAS LIVE WHEN THOSE SESSIONS WERE CREATED (i.e. real
    PRODUCTION). Running it from a machine whose .env has re-pointed a device
    to a DIFFERENT number (e.g. a local test box) would stamp the WRONG number
    onto historical sessions and permanently corrupt attribution.

Safety:
  * dry-run by default; pass --commit to actually write.
  * --device/--number let you pin exactly which device maps to which number,
    instead of trusting the ambient registry. STRONGLY recommended.
  * only fills sessions missing onboardingNumber; never overwrites.

Examples:
  python scripts/backfill_onboarding_number.py                       # dry-run, uses registry
  python scripts/backfill_onboarding_number.py --device smba --number 918968012547 --commit
"""
import argparse
import os
import sys

from dotenv import load_dotenv

workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, workspace_root)
load_dotenv(os.path.join(workspace_root, ".env"))

from app.firebase import init_firebase
from firebase_admin import firestore
from app.services import global_numbers


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", help="Only backfill sessions on this device id")
    ap.add_argument("--number", help="Force this number for --device (digits)")
    ap.add_argument("--commit", action="store_true", help="Actually write (default: dry-run)")
    args = ap.parse_args()

    if args.number and not args.device:
        ap.error("--number requires --device")

    init_firebase()
    db = firestore.client()

    forced = "".join(ch for ch in (args.number or "") if ch.isdigit())
    print(f"Mode: {'COMMIT' if args.commit else 'DRY-RUN'}")
    print("Registry device→number:")
    for dev, num in global_numbers.registry():
        print(f"   {dev!r:>10} -> {num!r}")
    if args.device:
        eff = forced or global_numbers.number_for_device(args.device)
        print(f"Scoped to device {args.device!r} -> number {eff!r}")

    docs = list(db.collection("onboarding_sessions").stream())
    stamped = skipped_has = skipped_scope = skipped_nonum = 0
    for d in docs:
        s = d.to_dict() or {}
        dev = (s.get("onboardingDeviceId") or "").strip()
        if not dev:
            continue
        if args.device and dev != args.device:
            skipped_scope += 1
            continue
        if (s.get("onboardingNumber") or "").strip():
            skipped_has += 1
            continue
        number = forced if (args.device and forced) else global_numbers.number_for_device(dev)
        number = "".join(ch for ch in (number or "") if ch.isdigit())
        if not number:
            skipped_nonum += 1
            continue
        stamped += 1
        print(f"  {'WOULD STAMP' if not args.commit else 'STAMP'} {d.id}: device={dev} -> onboardingNumber={number}")
        if args.commit:
            d.reference.update({"onboardingNumber": number})

    print(
        f"\nDone. stamped={stamped} skipped_has_number={skipped_has} "
        f"skipped_out_of_scope={skipped_scope} skipped_no_number_for_device={skipped_nonum}"
    )
    if not args.commit:
        print("Dry-run only — re-run with --commit to write.")


if __name__ == "__main__":
    main()
