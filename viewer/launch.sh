#!/usr/bin/env bash
# viewer/launch.sh — open the live footprint in a native-feel Chrome app window.
# Server should already be running (cron); this just opens the window.

set -e
PORT="${HAWALA_VIEWER_PORT:-8765}"
URL="http://127.0.0.1:${PORT}/"

# Wait briefly for server (e.g. if launched from same script as the server)
for i in 1 2 3 4 5; do
  if curl -fsS --max-time 1 "${URL}config" >/dev/null 2>&1; then break; fi
  sleep 1
done

# Chrome "app mode" — no tabs, no URL bar, native-feel window.
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [[ ! -x "$CHROME" ]]; then
  echo "Chrome not found at $CHROME — opening in default browser instead."
  open "$URL"
  exit 0
fi

PROFILE_DIR="$HOME/.hawala_viewer_profile"
mkdir -p "$PROFILE_DIR"

"$CHROME" \
  --app="$URL" \
  --user-data-dir="$PROFILE_DIR" \
  --window-size=1400,900 \
  --no-first-run --no-default-browser-check \
  >/dev/null 2>&1 &

echo "viewer launched at $URL  (pid=$!)"
