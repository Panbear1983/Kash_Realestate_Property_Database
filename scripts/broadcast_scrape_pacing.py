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
KASH_ID = 5143942438          # the contributor — this note is addressed to him alone

MESSAGE = """Hi Kash, quick update from Robo Kash.

Last month my search budget ran out early, so I went quiet for a few
nights. That's fixed: I now have my own budget, spread evenly across
the whole month. I'm also covering Tottenville and the 10311/10313
ZIPs now.

Expect a burst of new listings from me over the next few nights, then
a steady 5 to 8 new homes a day. Quiet days are normal: Staten Island
only adds a handful of new listings daily, and I keep re-checking the
homes I already know for price drops and sold notices — that's when
I'll alert you."""


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
    outcomes = broadcast(store, apply=args.apply, kind=KIND, message=MESSAGE,
                         recipients=[KASH_ID])
    if not outcomes:
        print("no allowed chat users in the access table — nothing to send")
        return
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"{mode} — {KIND}")
    for uid, outcome in sorted(outcomes.items()):
        print(f"  {uid}: {outcome}")


if __name__ == "__main__":
    main()
