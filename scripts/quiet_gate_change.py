#!/usr/bin/env python3
"""Make an alert-gate loosening forward-only: pre-mark the back-catalog as delivered.

    python scripts/quiet_gate_change.py --old-min-baths 2.5            # dry run
    python scripts/quiet_gate_change.py --old-min-baths 2.5 --apply

When a gate loosens (e.g. telegram_min_baths 2.5 -> 2.0), every already-stored listing
that clears the NEW bar but failed the OLD one becomes alert-eligible at once — measured
~140 rows, i.e. ~15 chunks of catch-up briefs per recipient in one morning. The owner
chose forward-only: those rows get delivery receipts stamped now, so only listings that
appear (or re-price — the receipt kind embeds the price) from here on actually alert.

Run AFTER preferences.yaml carries the new gate values. Rows that were already unsent and
eligible under the OLD bar are left untouched — they are legitimately pending, not
back-catalog.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import preferences
from kash.eligibility import bath_count
from kash.notifications import listing_delivery_kind, unsent_actionable_listings
from kash.store import Store

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(HERE, "pool.db")
DEFAULT_PREFS = os.path.join(HERE, "preferences.yaml")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--old-min-baths", type=float, required=True,
                    help="the bath minimum BEFORE the change (rows at/above it stay pending)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--prefs", default=DEFAULT_PREFS)
    ap.add_argument("--apply", action="store_true", help="write receipts (default: dry run)")
    args = ap.parse_args()

    from run_update import acquire_lock
    lock = acquire_lock(args.db)
    if lock is None:
        print("another Kash run holds the lock — re-run once it finishes")
        sys.exit(1)

    prefs = preferences.load(args.prefs)
    recipients = [int(r) for r in prefs.get("telegram_recipient_ids") or []]
    store = Store(args.db)

    stamped = 0
    for recipient in recipients:
        backlog = [row for row in unsent_actionable_listings(store, recipient, prefs)
                   if (bath_count(row.get("baths")) or 0) < args.old_min_baths]
        print(f"recipient {recipient}: {len(backlog)} back-catalog rows to quiet")
        for row in backlog[:5]:
            print(f"    e.g. {row.get('street_address')}  baths={row.get('baths')} "
                  f"${row.get('list_price')}")
        if args.apply:
            for row in backlog:
                kind = listing_delivery_kind(row)
                if kind:
                    store.mark_notification_sent(recipient, kind)
                    stamped += 1

    if args.apply:
        print(f"stamped {stamped} delivery receipts — the gate change is forward-only")
    else:
        print("dry run — nothing written. Re-run with --apply.")
    store.close()


if __name__ == "__main__":
    main()
