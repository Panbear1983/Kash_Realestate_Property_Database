#!/usr/bin/env bash
# Install or remove the daily Kash update job (macOS launchd).
#
#   bash scripts/install_scheduler.sh            # install + load (runs daily 07:00)
#   bash scripts/install_scheduler.sh uninstall  # unload + remove
#   launchctl start com.kash.update              # trigger a run right now (to test)
#
# Generates the plist with the correct python3/codex PATH so the scheduled run can both
# scrape and auto-rank (Codex needs to be on PATH under launchd).
set -euo pipefail

LABEL="com.kash.update"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOGDIR="$REPO/logs"

if [ "${1:-install}" = "uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Uninstalled ${LABEL}."
  exit 0
fi

PY="$(command -v python3)"
BINPATHS="$(dirname "$PY")"
if command -v codex >/dev/null 2>&1; then
  BINPATHS="${BINPATHS}:$(dirname "$(command -v codex)")"
fi
BINPATHS="${BINPATHS}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>${REPO}/run_update.py</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>${BINPATHS}</string></dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>${LOGDIR}/update.log</string>
  <key>StandardErrorPath</key><string>${LOGDIR}/update.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed ${LABEL} — runs daily at 07:00."
echo "  logs:        ${LOGDIR}/update.log"
echo "  test now:    launchctl start ${LABEL}"
echo "  disable:     bash scripts/install_scheduler.sh uninstall"
