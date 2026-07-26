#!/usr/bin/env bash
# Update KASH_BOT_TOKEN in every .env that needs it, without the token ever being echoed,
# logged, or passed on a command line (where `ps` would expose it).
#
#   bash scripts/set_bot_token.sh
#
# Two files carry the token and both must match, or half the system breaks silently:
#   Hermes_Telegram_Bridge/.env    -> the bot that answers Telegram messages (run_bridge.py)
#   Kash_Realestate_Property/.env  -> the nightly 07:00 digest push (run_update.py)
#
# Each file is backed up to <file>.bak before being rewritten.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GITHUB="$(cd "$HERE/.." && pwd)"

TARGETS=(
  "$GITHUB/Hermes_Telegram_Bridge/.env"
  "$HERE/.env"
)

for f in "${TARGETS[@]}"; do
  if [ ! -f "$f" ]; then
    echo "ERROR: missing $f" >&2
    exit 1
  fi
done

# -s: no echo. The value never reaches the terminal, the scrollback, or a transcript.
read -rsp "Paste the new KASH_BOT_TOKEN (input hidden): " NEW_TOKEN
echo

if [ -z "${NEW_TOKEN:-}" ]; then
  echo "ERROR: empty input, nothing changed." >&2
  exit 1
fi

# Telegram tokens look like <bot_id>:<35-char secret>. A loose check catches the usual
# paste mistakes (trailing newline, partial copy, quotes) without being brittle.
if ! printf '%s' "$NEW_TOKEN" | grep -qE '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$'; then
  echo "ERROR: that doesn't look like a Telegram bot token (expected <digits>:<secret>)." >&2
  echo "       Nothing was changed. Re-copy it from BotFather and try again." >&2
  exit 1
fi

BOT_ID="${NEW_TOKEN%%:*}"

for f in "${TARGETS[@]}"; do
  cp -p "$f" "$f.bak"
  tmp="$(mktemp)"
  # Rewrite the KASH_BOT_TOKEN line in place, preserving every other line and their order.
  # The value is handed to python via the environment, never argv, so it stays out of `ps`.
  NEW_TOKEN="$NEW_TOKEN" python3 - "$f" >"$tmp" <<'PY'
import os, sys
token = os.environ["NEW_TOKEN"]
lines, replaced = [], False
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        if line.lstrip().startswith("KASH_BOT_TOKEN="):
            lines.append(f"KASH_BOT_TOKEN={token}\n")
            replaced = True
        else:
            lines.append(line)
if not replaced:                      # key absent entirely — append it
    if lines and not lines[-1].endswith("\n"):
        lines.append("\n")
    lines.append(f"KASH_BOT_TOKEN={token}\n")
sys.stdout.write("".join(lines))
PY
  cat "$tmp" >"$f"        # preserve the original file's permissions and inode
  rm -f "$tmp"
  echo "  updated $f  (backup: $(basename "$f").bak)"
done

unset NEW_TOKEN
echo
echo "Done. Bot ID $BOT_ID written to ${#TARGETS[@]} files; the secret was never printed."
echo "Next: launchctl kickstart -k gui/\$(id -u)/com.kash.telegrambridge"
