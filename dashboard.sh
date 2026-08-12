#!/usr/bin/env bash
# Launch the Kash dashboard from anywhere:  ./dashboard.sh   (or double-click in Finder
# via "Open With > Terminal"). Extra args pass through to dashboard.py.
#
# What this adds over `python dashboard.py`:
#   - runs from any directory (resolves the repo from its own location)
#   - fails with a clear message when the python/textual install is missing
#   - a UTF-8 locale and a color-capable TERM, so Textual's borders and shading render
#   - exec's python: Ctrl+C and terminal resizes reach the app directly
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Override with KASH_PYTHON=/path/to/python3 if the default isn't the one with textual.
PY="${KASH_PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "dashboard.sh: python3 not found on PATH (set KASH_PYTHON=/path/to/python3)" >&2
    exit 1
fi
if ! "$PY" -c "import textual" >/dev/null 2>&1; then
    echo "dashboard.sh: '$PY' can't import 'textual' (wrong python or not installed)." >&2
    echo "  Install it:  $PY -m pip install textual pyyaml requests" >&2
    echo "  Or point at another interpreter:  KASH_PYTHON=/path/to/python3 ./dashboard.sh" >&2
    exit 1
fi

# Textual needs UTF-8 for box drawing and a color-capable TERM; only fill gaps, never
# override a terminal that already declares itself.
if [ -z "${LANG:-}" ] && [ -z "${LC_ALL:-}" ]; then
    export LANG="en_US.UTF-8"
fi
case "${TERM:-}" in
    ""|dumb) export TERM="xterm-256color" ;;
esac

exec "$PY" dashboard.py "$@"
