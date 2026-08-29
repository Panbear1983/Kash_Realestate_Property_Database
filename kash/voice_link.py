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

# Everything except the on/off switch, which each caller decides the default for.
_BASE = {"voice": "en-US-AndrewNeural", "rate": "+0%", "max_chars": 2000}

# Two callers, two defaults, deliberately:
#
#   Chat replies default OFF. Speaking every answer is a running change to how the bot
#   behaves, so a person turns that on for themselves with /voice on.
#
#   The daily report defaults ON. It is one short briefing a day attached to a push the
#   recipient already receives, and the owner's instruction is that the morning report
#   always carries it.
#
# In both cases an explicit `/voice off` still wins: a stored preference overrides the
# default, so "always" means "unless this person asked me to stop", never "regardless".
_OFF = {"enabled": False, **_BASE}

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


def prefs_for(user_id: int, *, default_enabled: bool = False) -> dict:
    """One recipient's voice settings, falling back to `default_enabled` if they have none.

    A stored preference always wins over the default, which is what keeps `/voice off`
    meaningful even where voice is on by default.
    """
    defaults = {"enabled": bool(default_enabled), **_BASE}
    module = renderer(quiet=True)
    if module is None:
        return dict(_OFF)          # nothing can be rendered anyway
    try:
        return module.get_prefs(BOT_NAME, int(user_id), defaults)
    except Exception:  # noqa: BLE001
        return dict(_OFF)


def wants_voice(user_id: int, *, default_enabled: bool = False) -> bool:
    return bool(prefs_for(user_id, default_enabled=default_enabled).get("enabled"))


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
