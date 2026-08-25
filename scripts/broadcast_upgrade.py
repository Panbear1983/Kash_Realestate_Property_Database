#!/usr/bin/env python3
"""One-time Telegram broadcast announcing the 2026-08 conversation upgrade.

    python scripts/broadcast_upgrade.py            # dry run: who would receive it
    python scripts/broadcast_upgrade.py --apply    # actually send

Recipients are the ALLOWED chat users in the access table (deliberately NOT
`telegram_recipient_ids`, which is the listing-alert list). Idempotent via the
notification_delivery ledger: a user is marked only after every chunk reports "sent", so a
partial failure is retried by simply re-running --apply, and a second run after success
sends nothing. Run manually on deploy — never scheduled.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_update
from kash.access import Access
from kash.notifications import split_text
from kash.store import Store

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(HERE, "pool.db")

KIND = "broadcast:2026-08-upgrade"

MESSAGE = """Robo Kash just got smarter. Here's what's new:

BIGGER QUESTIONS
You can now ask for counts, averages, and comparisons:
- "How many 3-bedroom homes are there in each neighborhood under $700k?"
- "What's the average price in Great Kills?"
- "Show me homes in Great Kills or Annadale" (either-or works now)

IT GIVES ITS OPINION
Ask "show homes under $750k and tell me which is the best value" and you'll
get the list plus the reasoning — why one stands out from the rest.

IT REMEMBERS THE CONVERSATION
If Kash asks "what's your budget?", just answer: "under 800k."
It keeps what you already told it and finishes the search. No starting over.

SPELLING DOESN'T MATTER
"Anadale" finds Annadale. Kash will tell you what it assumed.

FOR CONTRIBUTORS
Have a listing sheet or a screenshot? Just send the file to this chat —
no commands needed. Kash reads it, shows you what it found, and after
you confirm it goes to the owner for review. Nothing changes until the
owner approves — and you'll get a message here when your submission is
approved or rejected.

WHAT HASN'T CHANGED
- Kash still can't change or delete anything on its own — every edit
  goes through the owner's review.
- Private info stays private.
- Everything you already do still works the same.

Just talk to it like a person. If an answer misses, say it a different way.
Type help anytime."""


def broadcast(store, *, apply=False, push=None) -> dict[int, str]:
    """Send MESSAGE once to every allowed chat user. Returns {user_id: outcome}."""
    if push is None:
        push = run_update.push_telegram
    # uid 0 is the dashboard's internal actor row, not a Telegram user — sending to it
    # would fail forever and re-report on every retry. Real Telegram ids are positive.
    recipients = [int(r["telegram_user_id"]) for r in Access(store).all()
                  if r.get("status") == "allowed" and int(r["telegram_user_id"]) > 0]
    parts = split_text(MESSAGE)
    outcomes: dict[int, str] = {}
    for uid in recipients:
        if store.notification_sent(uid, KIND):
            outcomes[uid] = "already sent"
            continue
        if not apply:
            outcomes[uid] = f"would send ({len(parts)} part(s))"
            continue
        result = "sent"
        for part in parts:
            outcome = push(part, [uid]).get(uid)
            if outcome != "sent":
                result = outcome or "fail"
                break
        if result == "sent":
            # Marked only on full success: a failed or partial send stays unmarked and is
            # retried by the next --apply.
            store.mark_notification_sent(uid, KIND)
        outcomes[uid] = result
    return outcomes


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
    outcomes = broadcast(store, apply=args.apply)
    if not outcomes:
        print("no allowed chat users in the access table — nothing to send")
        return
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"{mode} — {KIND}")
    for uid, outcome in sorted(outcomes.items()):
        print(f"  {uid}: {outcome}")


if __name__ == "__main__":
    main()
