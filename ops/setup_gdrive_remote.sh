#!/usr/bin/env bash
# ops/setup_gdrive_remote.sh — One-shot OAuth setup for the GDrive backup
# remote. Run this ONCE; afterward the nightly launchd backup just works.
#
# Usage:
#   bash ops/setup_gdrive_remote.sh
#
# What it does:
#   1. Verifies rclone is installed
#   2. Runs `rclone config` interactively (you click through browser OAuth)
#   3. Creates the gdrive:hawala/ target folder
#   4. Triggers an immediate test backup so you can confirm it works
#
# After this completes, the launchd plist at
# ~/Library/LaunchAgents/com.hawala.backup.plist (already installed) will
# fire weekdays at 22:30 IST automatically.

set -o pipefail

if ! command -v rclone >/dev/null 2>&1; then
    echo "rclone not installed. Run: brew install rclone" >&2
    exit 2
fi

echo "=== rclone GDrive remote setup ==="
echo ""
echo "About to launch interactive rclone config."
echo "When prompted:"
echo "  - n) for new remote"
echo "  - name: gdrive"
echo "  - storage / type: drive   (or the number that appears for 'Google Drive')"
echo "  - client_id / client_secret: leave BLANK (press Enter)"
echo "  - scope: 1   (full Drive access)"
echo "  - service_account_file: leave BLANK"
echo "  - Edit advanced config: n"
echo "  - Use auto config: y    (opens your browser for OAuth)"
echo "    → authorize, then return to this terminal"
echo "  - Configure as team drive: n"
echo "  - Save / Yes this is OK: y"
echo "  - Quit: q"
echo ""
read -p "Press Enter to begin..." _

rclone config

echo ""
echo "=== verifying remote ==="
if ! rclone listremotes | grep -q '^gdrive:'; then
    echo "✗ remote 'gdrive' was not created. Re-run this script." >&2
    exit 3
fi
echo "  ✓ remote 'gdrive' configured"

echo ""
echo "=== creating gdrive:hawala/ folder ==="
rclone mkdir gdrive:hawala || true
rclone lsd gdrive:hawala
echo "  ✓ folder exists"

echo ""
echo "=== running first test backup ==="
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/ops/backup_to_gdrive.sh"

echo ""
echo "=== ALL DONE ==="
echo "Verify: rclone ls gdrive:hawala"
echo "Nightly auto-backup: weekdays 22:30 IST via com.hawala.backup (launchd)"
