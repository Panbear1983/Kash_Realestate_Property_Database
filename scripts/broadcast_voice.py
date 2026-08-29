#!/usr/bin/env python3
"""One-time Telegram broadcast: Robo Kash can now speak its answers.

    python scripts/broadcast_voice.py                    # dry run
    python scripts/broadcast_voice.py --apply            # send the text announcement
    python scripts/broadcast_voice.py --apply --voice    # ...and demonstrate it, spoken

Same mechanics as broadcast_upgrade.py (allowed users only, ledger-idempotent, marked
only after a real send) under its own ledger key, so re-running --apply after a partial
failure retries only who is still missing and a second run after success sends nothing.

`--voice` follows the text with the same words as a voice note, so the announcement
demonstrates itself. Worth being clear about why that is allowed: a recipient's /voice
setting governs whether their own *answers* come back spoken — it is not a subscription,
and it does not gate a push we choose to make. Telegram needs only an open chat with the
bot, which every allowed user here already has. The voice note is therefore best-effort
decoration on top of the text; the text is what the ledger records, so a failed render
never blocks or re-sends the announcement.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_update
from broadcast_upgrade import DEFAULT_DB, HERE, broadcast
from kash import voice_link
from kash.store import Store

KIND = "broadcast:2026-08-voice"
KASH_ID = 5143942438          # the contributor — this note is addressed to him alone

MESSAGE = """Hi Kash, Robo Kash here.

I have mounted myself a creepy cyborg voice, scavenged off the
second-hand market — just the way you like it. It lets you play my
replies instead of reading through a long list of them, handy when you
are driving, or when an answer runs to a dozen listings.

The written message is always the full answer, with the clickable
listing links. Nothing else about how you talk to me has changed."""


# Fred, the classic MacinTalk robot, because the message calls it a creepy cyborg voice
# and a demo has to sound like its own description. Kash's stored preference is set to
# Fred to match, so the text's promise holds: turn it on and this is what you get, and
# `/voice set andrew` moves you to a human one.
SPOKEN_VOICE = "Fred"

# Its own script rather than a reading of MESSAGE: a voice note lands right under the
# text, so repeating it word for word is tedious. This one only has to prove the feature
# is real and sound like its own description doing it.
#
# The announcement deliberately does NOT explain the /voice commands. Kash's voice is
# switched on for him instead, so it simply starts happening — without that, the message
# would promise a voice he had no way to enable. `/voice` on its own still prints the
# full command list if he goes looking.
SPOKEN_MESSAGE = """Hi Kash, Robo Kash here.

I have mounted myself a creepy cyborg voice, scavenged off the second-hand
market — just the way you like it. This is it.

It lets you play my replies instead of reading through a long list of them,
handy when you are driving, or when an answer runs to a dozen listings."""


def send_voice_note(chat_id: int, text: str) -> str:
    """Best-effort voice note. Never raises — the text has already been delivered.

    Rendering comes from kash/voice_link.py, the shared optional link to the bridge's
    speech module. Unlike the daily report, this deliberately ignores per-user voice
    preferences: it is an announcement, and it has to reach someone who has not
    switched voice on.
    """
    import requests
    tok = os.environ.get("KASH_BOT_TOKEN")
    if not tok:
        return "skipped (no KASH_BOT_TOKEN)"
    try:
        ogg = voice_link.render(text, {"voice": SPOKEN_VOICE, "rate": "+0%", "max_chars": 3000})
        if not ogg:
            return "render failed"
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendVoice",
                          data={"chat_id": int(chat_id)},
                          files={"voice": ("announcement.ogg", ogg, "audio/ogg")},
                          timeout=120)
        return "sent" if r.json().get("ok") else r.json().get("description", "fail")
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="send (default: dry run)")
    ap.add_argument("--voice", action="store_true",
                    help="also send the announcement as a voice note")
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
    for chat_id, outcome in outcomes.items():
        print(f"  {chat_id}: {outcome}")

    if not args.voice:
        return
    # Only follow up where the text actually landed this run. "already sent" means a
    # previous run announced it, and re-sending audio then would be a duplicate.
    fresh = [cid for cid, outcome in outcomes.items() if outcome == "sent"]
    if not args.apply:
        print("  voice note: would follow the text for "
              f"{sorted(outcomes)} (dry run — nothing rendered)")
        return
    if not fresh:
        print("  voice note: skipped — no text was sent this run")
        return
    if voice_link.renderer() is None:
        return
    for chat_id in fresh:
        print(f"  {chat_id} voice note: {send_voice_note(chat_id, SPOKEN_MESSAGE)}")


if __name__ == "__main__":
    main()
