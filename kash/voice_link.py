"""Optional link to the Telegram voice renderer, which lives with the transport.

Turning text into a voice bubble belongs next to `sendVoice`, which is in the sibling
Hermes_Telegram_Bridge repo — its `run_bridge.py` already reaches into this project the
other way, so the symmetry is deliberate rather than accidental coupling.

Everything here degrades to None. A missing bridge checkout, a missing ffmpeg, a missing
edge-tts, or a user who never switched voice on must cost the morning report its voice
note and absolutely nothing else: the written report is the product, the audio is a
convenience on top of it.

Rendering only. Sending stays in run_update.py, like every other Telegram push here.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_PATH = os.environ.get(
    "HERMES_BRIDGE_PATH", os.path.join(os.path.dirname(HERE), "Hermes_Telegram_Bridge"))

# The key the bridge files preferences under is "<bot name>:<telegram user id>", so the
# daily job has to name the same bot the chat runs on to read the same settings.
BOT_NAME = "Kash_Realestate_Property_bot"

# Voice is opt-in: a recipient with no stored preference gets a written report only,
# exactly as before this existed. Nobody is spoken to because a default said so.
_OFF = {"enabled": False, "voice": "en-US-AndrewNeural", "rate": "+0%", "max_chars": 2000}

_cached: list = []          # [] = not tried yet, [None] = tried and unavailable


def renderer(quiet: bool = False):
    """The bridge's voice module, or None with a one-line reason printed."""
    if _cached:
        return _cached[0]

    def unavailable(reason):
        if not quiet:
            print(f"[voice] unavailable: {reason}")
        _cached.append(None)
        return None

    if not os.path.isdir(BRIDGE_PATH):
        return unavailable(f"no bridge checkout at {BRIDGE_PATH}")
    if BRIDGE_PATH not in sys.path:
        sys.path.insert(0, BRIDGE_PATH)
    try:
        from bridge import voice as module
    except Exception as e:  # noqa: BLE001
        return unavailable(f"cannot import the bridge voice module ({e})")
    _cached.append(module)
    return module


def prefs_for(user_id: int) -> dict:
    """One recipient's voice settings, or the off-by-default set."""
    module = renderer(quiet=True)
    if module is None:
        return dict(_OFF)
    try:
        return module.get_prefs(BOT_NAME, int(user_id), dict(_OFF))
    except Exception:  # noqa: BLE001
        return dict(_OFF)


def wants_voice(user_id: int) -> bool:
    return bool(prefs_for(user_id).get("enabled"))


def render(text: str, prefs: dict) -> bytes | None:
    """Opus/Ogg bytes for `text` in this recipient's voice, or None if anything is missing."""
    module = renderer()
    if module is None or not (text or "").strip():
        return None
    reason = module.unavailable_reason(prefs.get("voice"))
    if reason:
        print(f"[voice] unavailable: {reason}")
        return None
    speakable = module.speakable(text, int(prefs.get("max_chars") or 2000))
    return module.render_ogg(speakable, prefs.get("voice"), prefs.get("rate", "+0%"))
