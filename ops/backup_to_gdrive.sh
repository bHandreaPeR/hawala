#!/usr/bin/env bash
# ops/backup_to_gdrive.sh — Nightly backup of Hawala state to Google Drive.
#
# What gets backed up (everything that can't be regenerated):
#   - v3/cache/ticks_*.csv          (today + last 30 trading days)
#   - v3/cache/depth_*.csv          (today + last 30 trading days)
#   - v3/cache/candles_1m_*.pkl     (full history — used by daemons at boot)
#   - v3/cache/positioning_*.ndjson (today + last 30 days)
#   - v3/cache/option_flow_*.json   (current state, all instruments)
#   - v3/cache/option_flow_trace_*.ndjson (today + last 30 days)
#   - v3/cache/news_signal.json
#   - v3/cache/*.preBackport        (historical recorder schemas)
#   - news/state/*.json             (clusters, alerted, theme, digest)
#   - trade_logs/*.csv              (vp_paper_journal, footprint_features)
#   - token.env                     (encrypted — credentials)
#   - logs/trade_bot/*.log          (today only; older log rolled into archive)
#   - logs/macro_bot/*.log          (today only)
#
# Tarball + rclone push to gdrive:hawala/YYYY-MM-DD.tar.gz
# Keeps last 60 daily archives in Drive; older ones auto-deleted.
#
# ONE-TIME SETUP (do this first):
#   brew install rclone
#   rclone config       # → "n" new remote → name=gdrive → type=drive
#                       #   → leave client_id/secret blank → scope=drive
#                       #   → auth via browser (one-time OAuth)
#                       #   → root_folder_id=blank → quit
#   rclone mkdir gdrive:hawala
#   rclone lsd gdrive:hawala     # verify
#
# Cron (Mac launchd is cleaner — see ops/com.hawala.backup.plist):
#   30 22 * * 1-5 /Users/subhransubaboo/Claude\ Projects/Hawala\ v2/Hawala\ v2/ops/backup_to_gdrive.sh
#
# Manual run:
#   bash ops/backup_to_gdrive.sh
#   bash ops/backup_to_gdrive.sh --dry-run    (build tarball, skip upload)

set -o pipefail
# (intentionally NOT using `set -u` — bash 3.2 nullglob expansions trip it)

ROOT="/Users/subhransubaboo/Claude Projects/Hawala v2/Hawala v2"
GDRIVE_REMOTE="gdrive:hawala"
KEEP_DAYS=60

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; fi

cd "$ROOT"

TS=$(date +%Y-%m-%d)
LOG_DIR="logs/macro_bot"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/backup-$TS.log"
exec > >(tee -a "$LOG") 2>&1

echo "=== Hawala backup → GDrive [$(date '+%H:%M:%S')] ==="

# ─── 1. Sanity check tools (skip during dry-run so first-time use can verify
#       tarball logic before rclone is installed) ───────────────────────────
if [[ $DRY_RUN -eq 0 ]]; then
    if ! command -v rclone >/dev/null 2>&1; then
        echo "ERROR: rclone not installed. Run: brew install rclone" >&2
        echo "Then: rclone config   (set up a remote called 'gdrive')" >&2
        exit 2
    fi
    if ! rclone listremotes 2>/dev/null | grep -q "^gdrive:"; then
        echo "ERROR: rclone remote 'gdrive' not configured. Run: rclone config" >&2
        exit 2
    fi
fi

# ─── 2. Build tarball ─────────────────────────────────────────────────────────
ARCHIVE_DIR="/tmp/hawala_backup_$$"
mkdir -p "$ARCHIVE_DIR"
TARBALL="$ARCHIVE_DIR/hawala-$TS.tar.gz"

# Build the include-list. Globs that match nothing simply skip silently.
shopt -s nullglob
INCLUDE=()

# Tick + depth CSVs (today + last 30 trading days = ~45 calendar days)
for d in $(seq 0 45); do
    YMD=$(date -v-${d}d +%Y%m%d 2>/dev/null)
    INCLUDE+=( v3/cache/ticks_*_${YMD}.csv v3/cache/depth_*_${YMD}.csv )
    INCLUDE+=( v3/cache/positioning_*_${YMD}.ndjson )
    INCLUDE+=( v3/cache/option_flow_trace_*_${YMD}.ndjson )
    INCLUDE+=( v3/cache/option_flow_intraday_${YMD}.ndjson )
done

# Static state — small, always include
INCLUDE+=( v3/cache/candles_1m_*.pkl )
INCLUDE+=( v3/cache/option_flow_*.json )
INCLUDE+=( v3/cache/news_signal.json )
INCLUDE+=( v3/cache/pcr_daily.csv )
INCLUDE+=( v3/cache/*.preBackport )
INCLUDE+=( news/state/*.json )
INCLUDE+=( trade_logs/*.csv )
INCLUDE+=( token.env )

# Today's logs only (rolling)
for f in logs/trade_bot/*.log logs/macro_bot/*.log; do
    # Skip logs older than 1 day to keep the tarball lean
    if [[ -f "$f" ]] && [[ $(find "$f" -mtime -1 2>/dev/null) ]]; then
        INCLUDE+=( "$f" )
    fi
done

# Existence-filter + de-dup (macOS ships bash 3.2 — no associative arrays;
# use sort -u via a temp file instead).
LISTFILE="$ARCHIVE_DIR/filelist.txt"
: > "$LISTFILE"
if [[ ${#INCLUDE[@]} -gt 0 ]]; then
    for f in "${INCLUDE[@]}"; do
        [[ -e "$f" ]] && printf '%s\n' "$f" >> "$LISTFILE"
    done
fi
sort -u "$LISTFILE" -o "$LISTFILE"
N_FILES=$(wc -l < "$LISTFILE" | tr -d ' ')

echo "files to archive: $N_FILES"
if [[ "$N_FILES" -eq 0 ]]; then
    echo "ERROR: nothing to archive — bailing" >&2
    rm -rf "$ARCHIVE_DIR"; exit 2
fi

# tar -czf with file list (avoids argv-length limits on large globs)
tar -czf "$TARBALL" -T "$LISTFILE"
SIZE=$(du -h "$TARBALL" | cut -f1)
echo "tarball ready: $TARBALL ($SIZE)"

# ─── 3. Upload ──────────────────────────────────────────────────────────────
if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] skipping upload to $GDRIVE_REMOTE"
else
    echo "uploading to $GDRIVE_REMOTE/..."
    if rclone copy "$TARBALL" "$GDRIVE_REMOTE/" --progress; then
        echo "✓ uploaded"
    else
        echo "ERROR: rclone upload failed" >&2
        rm -rf "$ARCHIVE_DIR"; exit 3
    fi
fi

# ─── 4. Prune old archives (keep last KEEP_DAYS days) ───────────────────────
if [[ $DRY_RUN -eq 0 ]]; then
    echo "pruning archives older than $KEEP_DAYS days in $GDRIVE_REMOTE/..."
    rclone delete "$GDRIVE_REMOTE/" --min-age "${KEEP_DAYS}d" \
        --include "hawala-*.tar.gz" || true
fi

# ─── 5. Cleanup local tarball ────────────────────────────────────────────────
rm -rf "$ARCHIVE_DIR"

echo "=== done [$(date '+%H:%M:%S')]  remote: $GDRIVE_REMOTE  size: $SIZE ==="
