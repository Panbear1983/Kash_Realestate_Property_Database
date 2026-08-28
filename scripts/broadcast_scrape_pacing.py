#!/usr/bin/env python3
"""One-time Telegram broadcast: the 2026-08-28 scrape pacing update, in plain terms.

    python scripts/broadcast_scrape_pacing.py            # dry run
    python scripts/broadcast_scrape_pacing.py --apply    # send once per allowed user

Same mechanics as broadcast_upgrade.py (allowed users only, ledger-idempotent, marked
only after a real send) under its own ledger key, so it can never collide with or
re-send the earlier announcement.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_update
from broadcast_upgrade import DEFAULT_DB, HERE, broadcast
from kash.store import Store

KIND = "broadcast:2026-08-scrape-pacing"

MESSAGE = """Robo Kash update — the nightly listing search just got steadier.

WHAT HAPPENED
In August, Kash's search budget ran out around the 20th of the month
(it was shared with another project), so for a few nights near the end
of the month Kash couldn't search Zillow at all.

WHAT CHANGED
Kash now searches on its own dedicated account and spreads its monthly
search budget evenly across all 30 days instead of using it up early.
No more quiet week at the end of the month. Three more Staten Island
areas are covered too — Tottenville (10307) plus the 10311 and 10313
ZIP codes.

WHAT TO EXPECT
For the next few nights you'll see a burst of new listings while Kash
catches up on the wider search. After that it settles to a steady
trickle — usually 5 to 8 new homes a day — because that's about how
many new listings Staten Island actually produces in the target price
range. A quiet day doesn't mean Kash stopped looking. It means Kash
already knows every home on the market and is re-checking them for
price drops and sold notices, which is exactly when it sends an alert.

Nothing changes in how you talk to Kash. Type help anytime."""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="send (default: dry run)")
    args = ap.parse_args()

    run_update.load_dotenv(os.path.join(HERE, ".env"))
    lock = run_update.acquire_lock(args.db)
    if lock is None:
        print("another Kash run holds the lock — re-run once it finishes")
        sys.exit(1)

    store = Store(args.db)
    outcomes = broadcast(store, apply=args.apply, kind=KIND, message=MESSAGE)
    if not outcomes:
        print("no allowed chat users in the access table — nothing to send")
        return
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"{mode} — {KIND}")
    for uid, outcome in sorted(outcomes.items()):
        print(f"  {uid}: {outcome}")


if __name__ == "__main__":
    main()
