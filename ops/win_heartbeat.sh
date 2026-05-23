#!/usr/bin/env bash
# ops/win_heartbeat.sh — Mac → Windows liveness check via SSH.
#
# Runs every 10 minutes from Mac cron. SSHes into the Windows production
# box and verifies it responds within 5s. Tracks transition state and
# Telegram-alerts on:
#   • up   → down  (Windows became unreachable for 2 checks in a row)
#   • down → up    (Windows recovered)
#
# Why 2 consecutive failures, not 1: a single missed ping happens from
# transient Wi-Fi / NAT timing. 2 consecutive (20 min unreachable) is a
# real outage. Reduces false alarms.
#
# State file: ~/.hawala_win_heartbeat.json
#   { status, consecutive_failures, last_check_ts, last_alert_ts }
#
# Cron line:
#   */10 * * * * /Users/subhransubaboo/Claude\ Projects/Hawala\ v2/Hawala\ v2/ops/win_heartbeat.sh >> /tmp/win_heartbeat.log 2>&1

set -u

SSH_HOST="hawala-win"
SSH_TIMEOUT=5
STATE_FILE="${HOME}/.hawala_win_heartbeat.json"
TOKEN_ENV="/Users/subhransubaboo/Claude Projects/Hawala v2/Hawala v2/token.env"
THRESHOLD_FAILURES=2   # alert after this many consecutive failures
LOG_TAG="[win-hb $(date +%H:%M:%S)]"

# ─── 1. Probe ─────────────────────────────────────────────────────────────────
if ssh -o ConnectTimeout=${SSH_TIMEOUT} -o BatchMode=yes \
       -o StrictHostKeyChecking=accept-new "${SSH_HOST}" \
       "powershell -c \"'alive'\"" >/dev/null 2>&1; then
    UP=1
else
    UP=0
fi

# ─── 2. Load state ───────────────────────────────────────────────────────────
read_state() {
    /opt/anaconda3/bin/python3 - <<PY
import json, pathlib
p = pathlib.Path("${STATE_FILE}")
if not p.exists():
    print('up|0||')
else:
    d = json.loads(p.read_text())
    print(f"{d.get('status','up')}|{d.get('consecutive_failures',0)}|"
          f"{d.get('last_alert_ts','')}|{d.get('first_failure_ts','')}")
PY
}

IFS='|' read -r PREV_STATUS PREV_FAILS LAST_ALERT FIRST_FAIL < <(read_state)

# ─── 3. Update transitions ───────────────────────────────────────────────────
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
ALERT_MSG=""

if (( UP == 0 )); then
    NEW_FAILS=$((PREV_FAILS + 1))
    NEW_STATUS="down"
    if [[ -z "$FIRST_FAIL" || "$PREV_STATUS" == "up" ]]; then
        FIRST_FAIL="$NOW"
    fi
    # Alert when crossing the threshold (only once per outage)
    if (( NEW_FAILS == THRESHOLD_FAILURES )); then
        DOWN_MIN=$(( (NEW_FAILS - 1) * 10 ))
        ALERT_MSG="🚨 <b>Hawala Windows DOWN</b>%0A"
        ALERT_MSG+="${SSH_HOST} unreachable for ${DOWN_MIN}+ minutes.%0A"
        ALERT_MSG+="First failure: <code>${FIRST_FAIL}</code>%0A"
        ALERT_MSG+="Sent from Mac heartbeat. Tomorrow morning's crons will NOT fire."
    fi
    echo "${LOG_TAG} DOWN consecutive=${NEW_FAILS}"
else
    NEW_STATUS="up"
    NEW_FAILS=0
    if [[ "$PREV_STATUS" == "down" ]]; then
        # Recovery
        ALERT_MSG="✅ <b>Hawala Windows RECOVERED</b>%0A"
        ALERT_MSG+="${SSH_HOST} reachable again at <code>${NOW}</code>%0A"
        ALERT_MSG+="Was down since <code>${FIRST_FAIL}</code>"
        FIRST_FAIL=""
    fi
    echo "${LOG_TAG} UP"
fi

# ─── 4. Persist state ────────────────────────────────────────────────────────
/opt/anaconda3/bin/python3 - <<PY
import json, pathlib
p = pathlib.Path("${STATE_FILE}")
p.write_text(json.dumps({
    'status': "${NEW_STATUS}",
    'consecutive_failures': ${NEW_FAILS},
    'last_check_ts': "${NOW}",
    'first_failure_ts': "${FIRST_FAIL}",
    'last_alert_ts': "${LAST_ALERT}",
}, indent=2))
PY

# ─── 5. Fire Telegram alert if needed ────────────────────────────────────────
if [[ -n "$ALERT_MSG" ]]; then
    # Pull MACRO bot creds from token.env
    TG_TOKEN=$(grep "^TELEGRAM_BOT_TOKEN_MACRO=" "$TOKEN_ENV" | cut -d= -f2-)
    TG_CHATS=$(grep "^TELEGRAM_CHAT_IDS_MACRO="   "$TOKEN_ENV" | cut -d= -f2-)
    if [[ -z "$TG_TOKEN" || -z "$TG_CHATS" ]]; then
        echo "${LOG_TAG} ALERT SUPPRESSED (no MACRO bot creds in $TOKEN_ENV)"
    else
        for cid in $(echo "$TG_CHATS" | tr ',' ' '); do
            curl -fsS -m 10 \
                "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
                -d "chat_id=${cid}" \
                -d "text=${ALERT_MSG}" \
                -d "parse_mode=HTML" >/dev/null \
                && echo "${LOG_TAG} alert sent to chat ${cid}" \
                || echo "${LOG_TAG} alert FAILED to chat ${cid}"
        done
        # update last_alert_ts so we know we sent something
        /opt/anaconda3/bin/python3 - <<PY
import json, pathlib
p = pathlib.Path("${STATE_FILE}")
d = json.loads(p.read_text())
d['last_alert_ts'] = "${NOW}"
p.write_text(json.dumps(d, indent=2))
PY
    fi
fi
