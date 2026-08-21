#!/usr/bin/env bash
# Refresh the public site's class library and push it to GitHub.
# Run weekly by ~/Library/LaunchAgents/com.peloton-picker.refresh.plist,
# or by hand any time: ./refresh.sh

set -uo pipefail

PYTHON=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
GIT=/usr/bin/git
CATEGORIES="cycling strength pilates yoga stretching cardio running walking hiking"

cd "$(dirname "$0")" || exit 1
mkdir -p "$HOME/.peloton-picker"
LOG="$HOME/.peloton-picker/refresh.log"

# keep the log from growing forever
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 200000 ]; then
  tail -c 80000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exec >> "$LOG" 2>&1
echo
echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="

# shellcheck disable=SC2086
"$PYTHON" peloton_picker.py export \
  --categories $CATEGORIES \
  --pages 2 \
  --generated "$(date +%Y-%m-%d)" \
  --out classes.json \
  --only-if-changed
STATUS=$?

if [ "$STATUS" -eq 3 ]; then
  echo "Catalogue unchanged — nothing to push."
  exit 0
fi
if [ "$STATUS" -ne 0 ]; then
  echo "Export failed (exit $STATUS). Leaving the published library as it is."
  exit "$STATUS"
fi

if "$GIT" diff --quiet -- classes.json; then
  echo "No git change detected."
  exit 0
fi

"$GIT" add classes.json
"$GIT" commit -m "Refresh class library ($(date +%Y-%m-%d))" || exit 0
"$GIT" push origin main && echo "Pushed. The site updates in a minute or two."
