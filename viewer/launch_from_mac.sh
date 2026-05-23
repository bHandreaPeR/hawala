#!/usr/bin/env bash
# viewer/launch_from_mac.sh — Open the Windows-hosted footprint viewer in a
# native-feel Chrome app window ON MAC.
#
# Requires: ~/.ssh/config has a 'hawala-win' Host entry with
#   LocalForward 8765 localhost:8765
#
# Flow:
#   1. Check if SSH tunnel to Windows already open (port 8765 locally LISTENs)
#   2. If not, open a background SSH tunnel (-fN = no shell, background)
#   3. Wait up to 5s for the tunnel to be ready
#   4. Launch Chrome in --app mode pointing at localhost:8765
#
# When done viewing: just close the Chrome window. The SSH tunnel stays open
# for next time (or kill via `pkill -f "ssh.*hawala-win"`).

set -e
PORT=8765
URL="http://localhost:${PORT}/"

# 1. Is local port already forwarded?
if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    echo "tunnel already up on :${PORT}"
else
    echo "opening SSH tunnel hawala-win :${PORT} -> localhost :${PORT} ..."
    ssh -fN hawala-win 2>&1 | tee /tmp/hawala_tunnel.log
fi

# 2. Wait briefly for tunnel + remote server to respond
for i in 1 2 3 4 5; do
    if curl -fsS --max-time 1 "${URL}config" >/dev/null 2>&1; then
        echo "viewer responds — opening Chrome"
        break
    fi
    sleep 1
done

# 3. Open Chrome --app for native-feel window
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [[ ! -x "$CHROME" ]]; then
    echo "Chrome not found at $CHROME — opening in default browser"
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
