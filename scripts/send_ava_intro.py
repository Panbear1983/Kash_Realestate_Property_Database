#!/usr/bin/env python3
"""One-time: introduce Ava (the new default voice) to Kash, then retire this job.

Scheduled by the owner on 2026-09-10 for 09:00 New York time on 2026-09-10 (21:00 on this
Mac), via ~/Library/LaunchAgents/com.kash.avaintro.plist. Switches Kash's stored voice to
Ava, sends the owner's intro text, then the same words as a voice note in Ava's voice.
Guarded by a marker file so a re-fire can never send twice; removes its own launchd job
at the end. `--dry-run` renders and prints but sends nothing and changes nothing.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
sys.path.insert(0, HERE)
from dashboard import load_dotenv  # noqa: E402
from kash import preferences, voice_link  # noqa: E402

LABEL = "com.kash.avaintro"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
MARKER = os.path.join(HERE, "logs", "ava_intro.sent")
AVA = "en-US-AvaMultilingualNeural"
KASH = 5143942438
PETER = 7512954760

INTRO = ("Hi Kash, I'm Ava, Robo Kash's assistant, a replacement of the nerdy wheelchair "
         "guy's persona. Starting today I'll be reading your morning Staten Island report "
         "and any spoken answers. Talk to you tomorrow morning.")
TEXT = INTRO + ("\n\nIf you'd rather read than listen, send /voice off. To pick another "
                "voice, send /voice voices for the list.")


def main(dry_run: bool) -> int:
    load_dotenv(os.path.join(HERE, ".env"))
    prefs = preferences.load(os.path.join(HERE, "preferences.yaml"))
    recipients = [int(x) for x in prefs.get("telegram_recipient_ids") or []]
    assert KASH in recipients and KASH != PETER, "refusing: Kash's id is not a configured recipient"
    if os.path.exists(MARKER) and not dry_run:
        print("already sent (marker present); nothing to do")
        return 0
    tok = os.environ.get("KASH_BOT_TOKEN")
    if not tok:
        print("no KASH_BOT_TOKEN; aborting")
        return 1
    bridge = voice_link.renderer()
    if bridge is None:
        print("bridge voice module unavailable; aborting")
        return 1
    ogg = bridge.render_ogg(INTRO, AVA, "+0%")
    if not ogg:
        print("voice render failed; aborting before any send")
        return 1
    print(f"rendered {len(ogg)} bytes of Ava audio")
    if dry_run:
        print("DRY RUN — would switch Kash to", AVA, "and send:\n---\n" + TEXT + "\n---")
        return 0

    import requests
    bridge.set_prefs(voice_link.BOT_NAME, KASH, voice=AVA)
    print("Kash's voice set to", bridge.get_prefs(voice_link.BOT_NAME, KASH)["voice"])
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=60,
                      data={"chat_id": KASH, "text": TEXT})
    print("text:", "sent" if r.json().get("ok") else r.json().get("description"))
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendVoice", timeout=120,
                      data={"chat_id": KASH},
                      files={"voice": ("ava_intro.ogg", ogg, "audio/ogg")})
    print("voice:", "sent" if r.json().get("ok") else r.json().get("description"))
    with open(MARKER, "w") as fh:
        fh.write("sent\n")
    # Retire the one-shot job: drop the plist, then unload (which ends this process).
    try:
        os.remove(PLIST)
    except FileNotFoundError:
        pass
    subprocess.Popen(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
