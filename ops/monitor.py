"""ops/monitor.py — Hawala production watchdog.

Watches every production process, detects wedges (process alive but data
stale), auto-restarts failed processes, escalates to MACRO Telegram if
recovery fails N times in an hour.

Designed as the safety net ON TOP of every other reliability fix:
  - WS auto-reconnect inside tick_recorder catches the easy cases
  - This monitor catches the hard cases (DNS blips, network drops,
    silent WS-callback-dead-but-process-alive)
  - launchd KeepAlive on the monitor itself catches monitor crashes

Watch targets (each can be tuned via the TARGETS list below):

  ┌────────────────────────┬─────────────────────────────────────────────┐
  │ Name                   │ Health signal                               │
  ├────────────────────────┼─────────────────────────────────────────────┤
  │ tick_recorder          │ NIFTY tick CSV mtime < 120 s                │
  │ viewer.live_server     │ HTTP 200 on /config within 3 s              │
  │ option_flow_daemon     │ log mtime < 5 min                           │
  │ vp_live_daemon         │ log mtime < 5 min                           │
  │ vp_paper_executor      │ log mtime < 10 min                          │
  │ index_1m_intraday      │ log mtime < 5 min                           │
  └────────────────────────┴─────────────────────────────────────────────┘

Recovery flow per target:
  1. Health-check failed
  2. Log WARN, kill the wedged process (SIGTERM, fallback SIGKILL after 3s)
  3. Re-run the documented `restart_cmd`
  4. Wait `grace_period_s`, re-check
  5. If still failed → record restart, escalate after threshold

Cooldown: max 3 restarts per target per hour. Beyond that, monitor stops
auto-restarting and Telegrams CRITICAL — needs human eyes.

Market hours: 09:10-15:40 IST Mon-Fri (lenient margins around the actual
09:15-15:30 session so we cover pre-market warmup and EOD flush). Outside
this window, health checks are skipped — recorders are MEANT to exit at
EOD, so freshness alarms would fire spuriously.

Run:   python -m ops.monitor
       python -m ops.monitor --once       # single check pass, exit
       python -m ops.monitor --dry-run    # check but don't restart

Install as launchd agent so it survives crashes + reboots:
  cp ops/com.hawala.monitor.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.hawala.monitor.plist
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Callable, Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PYTHON  = '/opt/anaconda3/bin/python3'
LOG_DIR = ROOT / 'logs' / 'trade_bot'
LOG_DIR.mkdir(parents=True, exist_ok=True)

CHECK_INTERVAL_S       = 30
GRACE_AFTER_RESTART_S  = 45     # let the new process warm up before re-checking
MAX_RESTARTS_PER_HOUR  = 3      # beyond this, escalate + stop restarting
MARKET_OPEN  = dtime(9, 10)
# IMPORTANT: must be EARLIER than every monitored daemon's END_HHMM
# (currently 15:35 for tick_recorder, spot_vix_recorder, vp_paper_executor,
# index_1m_intraday — they self-exit at that point by design). Otherwise the
# 15:35-15:40 window sees "process dead during market hours" → restart →
# daemon immediately re-exits → escalation → false-positive Telegram alert.
# 15:33 gives a 2-min buffer before any daemon's clean shutdown.
MARKET_CLOSE = dtime(15, 33)


# ─── Logging (flushing — heartbeats reach disk immediately) ──────────────────
class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record); self.flush()


def _setup_logging() -> logging.Logger:
    log = logging.getLogger('monitor')
    log.setLevel(logging.INFO); log.handlers.clear()
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh = _FlushingFileHandler(LOG_DIR / 'monitor.log', mode='a')
    fh.setFormatter(fmt); log.addHandler(fh)
    sh = logging.StreamHandler(); sh.setFormatter(fmt); log.addHandler(sh)
    return log


log = _setup_logging()


# ─── Telegram (MACRO bot — same envelope as autoheal/healthcheck) ───────────
def _load_telegram() -> tuple[str, list[str]]:
    env = {}
    try:
        for ln in open(ROOT / 'token.env'):
            if '=' in ln:
                k, _, v = ln.strip().partition('=')
                env[k] = v
    except Exception:
        return '', []
    tok = env.get('TELEGRAM_BOT_TOKEN_MACRO', '').strip()
    chats = [c.strip() for c in env.get('TELEGRAM_CHAT_IDS_MACRO', '').split(',') if c.strip()]
    return tok, chats


def _tg_send(msg: str) -> None:
    tok, chats = _load_telegram()
    if not tok or not chats:
        log.warning('telegram disabled — no creds in token.env')
        return
    try:
        from alerts.telegram import send as tg_send
        for cid in chats:
            tg_send(tok, cid, msg)
    except Exception as e:
        log.warning('telegram send failed: %s', e)


# ─── Health-check primitives ─────────────────────────────────────────────────
def _file_age_seconds(p: pathlib.Path) -> Optional[float]:
    if not p.exists():
        return None
    return time.time() - p.stat().st_mtime


def _tick_csv_age(inst: str) -> Optional[float]:
    """Age (seconds) since the latest tick row's ts_ms — not file mtime.
    File mtime can lie because Python buffers writes; the row's ts is truth."""
    today = datetime.now().strftime('%Y%m%d')
    p = ROOT / 'v3' / 'cache' / f'ticks_{inst}_{today}.csv'
    if not p.exists() or p.stat().st_size < 200:
        return None
    try:
        # Read just the last ~1 KB
        with p.open('rb') as f:
            size = p.stat().st_size
            f.seek(max(0, size - 1024))
            tail = f.read().decode('utf-8', errors='ignore')
        lines = [ln for ln in tail.split('\n') if ',' in ln and ln[0].isdigit()]
        if not lines:
            return None
        last_ts_ms = int(lines[-1].split(',')[0])
        return time.time() - (last_ts_ms / 1000.0)
    except Exception as e:
        log.warning('tick_csv_age %s err: %s', inst, e)
        return None


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _process_running(pattern: str) -> Optional[int]:
    """Return PID if a python process matching this module-pattern is alive."""
    try:
        out = subprocess.check_output(
            ['pgrep', '-f', pattern], text=True, timeout=5).strip()
        if not out:
            return None
        # Filter to Python processes only (avoid matching pgrep itself or shells)
        for pid_s in out.split('\n'):
            try:
                pid = int(pid_s)
                cmd = subprocess.check_output(
                    ['ps', '-p', str(pid), '-o', 'comm='], text=True, timeout=3).strip()
                if 'python' in cmd.lower():
                    return pid
            except Exception:
                continue
    except subprocess.CalledProcessError:
        return None
    except Exception as e:
        log.warning('pgrep err: %s', e)
    return None


# ─── Target definition ──────────────────────────────────────────────────────
@dataclass
class Target:
    name:          str
    proc_pattern:  str
    healthy:       Callable[[], bool]
    why_unhealthy: Callable[[], str]
    restart_cmd:   str                       # shell command
    market_hours_only: bool = True
    grace_period_s: int = GRACE_AFTER_RESTART_S


@dataclass
class TargetState:
    last_healthy_ts: float = 0.0
    last_restart_ts: float = 0.0
    restart_history: deque = field(default_factory=lambda: deque(maxlen=10))
    escalated: bool = False


def _market_hours_now() -> bool:
    """True only if we're in the intraday trading window of a NSE trading day.
    Honours weekends + the holiday list in ops/market_holidays.json — so on
    holidays (e.g. 2026-05-28), market_hours_only health checks correctly skip
    instead of triggering restart cascades against silent-by-design daemons."""
    from ops.market_calendar import is_trading_day
    now = datetime.now()
    if not is_trading_day(now.date()):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


# ─── Restart helper ─────────────────────────────────────────────────────────
def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(3)
        # Verify gone; SIGKILL if still alive
        try:
            os.kill(pid, 0)         # signal 0 = existence check
            log.warning('PID %d ignored SIGTERM, SIGKILLing', pid)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass
    except Exception as e:
        log.warning('kill pid=%d err: %s', pid, e)


def _restart(target: Target) -> bool:
    """Kill any matching processes, then run restart_cmd. Returns True on success."""
    log.info('restarting %s', target.name)
    pid = _process_running(target.proc_pattern)
    if pid:
        log.info('  killing wedged %s (pid=%d)', target.name, pid)
        _kill(pid)
        time.sleep(2)
    # Spawn new
    try:
        subprocess.Popen(['/bin/bash', '-c', target.restart_cmd],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        log.error('  spawn %s failed: %s', target.name, e)
        return False
    return True


# ─── Watch targets ──────────────────────────────────────────────────────────
def _build_targets() -> list[Target]:
    cwd = str(ROOT)
    nohup = 'nohup caffeinate -i'

    def restart(module: str) -> str:
        return (f'cd "{cwd}" && {nohup} {PYTHON} -m {module} '
                f'> /dev/null 2>&1 &')

    return [
        # tick_recorder — the most critical; wedges on DNS/WS drops
        Target(
            name='tick_recorder',
            proc_pattern=r'alerts\.tick_recorder$',
            healthy=lambda: (
                # During market hours, NIFTY ticks must be fresher than 2 min.
                # Before market: just check the process is alive.
                _process_running(r'alerts\.tick_recorder$') is not None
                and (not _market_hours_now()
                     or (lambda a: a is not None and a < 120)(_tick_csv_age('NIFTY')))),
            why_unhealthy=lambda: (
                'process dead' if _process_running(r'alerts\.tick_recorder$') is None
                else (lambda a: f'NIFTY ticks stale ({a:.0f}s)' if a is not None
                      else 'NIFTY tick CSV empty/missing (no rows written)')(
                          _tick_csv_age('NIFTY'))),
            restart_cmd=restart('alerts.tick_recorder'),
        ),
        # viewer — HTTP endpoint must respond
        Target(
            name='viewer',
            proc_pattern=r'viewer\.live_server',
            healthy=lambda: _http_ok('http://127.0.0.1:8765/config'),
            why_unhealthy=lambda: 'HTTP /config not responding',
            restart_cmd=(
                f'cd "{cwd}" && {nohup} {PYTHON} -m viewer.live_server '
                f'--host 127.0.0.1 --port 8765 > logs/macro_bot/viewer.log 2>&1 &'),
            market_hours_only=False,
        ),
        # option_flow_daemon
        Target(
            name='option_flow_daemon',
            proc_pattern=r'alerts\.option_flow_daemon',
            healthy=lambda: (
                _process_running(r'alerts\.option_flow_daemon') is not None
                and (lambda a: a is not None and a < 300)(
                    _file_age_seconds(ROOT / 'logs' / 'macro_bot' / 'option_flow.log'))),
            why_unhealthy=lambda: (
                'process dead' if _process_running(r'alerts\.option_flow_daemon') is None
                else 'log stale > 5 min'),
            restart_cmd=(
                f'cd "{cwd}" && {nohup} {PYTHON} -m alerts.option_flow_daemon '
                f'--mode daemon > logs/macro_bot/option_flow.log 2>&1 &'),
        ),
        # vp_live_daemon
        Target(
            name='vp_live_daemon',
            proc_pattern=r'alerts\.vp_live_daemon',
            healthy=lambda: (
                _process_running(r'alerts\.vp_live_daemon') is not None
                and (lambda a: a is not None and a < 300)(
                    _file_age_seconds(ROOT / 'logs' / 'trade_bot' / 'vp_live_daemon.log'))),
            why_unhealthy=lambda: (
                'process dead' if _process_running(r'alerts\.vp_live_daemon') is None
                else 'log stale > 5 min'),
            restart_cmd=(
                f'cd "{cwd}" && {nohup} {PYTHON} -m alerts.vp_live_daemon '
                f'--mode daemon > /dev/null 2>&1 &'),
        ),
        # vp_paper_executor
        Target(
            name='vp_paper_executor',
            proc_pattern=r'alerts\.vp_paper_executor',
            healthy=lambda: (
                _process_running(r'alerts\.vp_paper_executor') is not None
                and (lambda a: a is not None and a < 600)(
                    _file_age_seconds(LOG_DIR / 'vp_paper_executor.log'))),
            why_unhealthy=lambda: (
                'process dead' if _process_running(r'alerts\.vp_paper_executor') is None
                else 'log stale > 10 min'),
            restart_cmd=restart('alerts.vp_paper_executor'),
        ),
        # index_1m_intraday
        Target(
            name='index_1m_intraday',
            proc_pattern=r'alerts\.index_1m_intraday',
            healthy=lambda: (
                _process_running(r'alerts\.index_1m_intraday') is not None
                and (lambda a: a is not None and a < 300)(
                    _file_age_seconds(LOG_DIR / 'index_1m_intraday.log'))),
            why_unhealthy=lambda: (
                'process dead' if _process_running(r'alerts\.index_1m_intraday') is None
                else 'log stale > 5 min'),
            restart_cmd=restart('alerts.index_1m_intraday'),
        ),
        # spot_vix_recorder — polls spot indices + VIX every 60 s; ~60-s
        # cadence + 5-min grace = log mtime threshold of 5 min is safe.
        Target(
            name='spot_vix_recorder',
            proc_pattern=r'alerts\.spot_vix_recorder',
            healthy=lambda: (
                _process_running(r'alerts\.spot_vix_recorder') is not None
                and (lambda a: a is not None and a < 300)(
                    _file_age_seconds(LOG_DIR / 'spot_vix_recorder.log'))),
            why_unhealthy=lambda: (
                'process dead' if _process_running(r'alerts\.spot_vix_recorder') is None
                else 'log stale > 5 min'),
            restart_cmd=restart('alerts.spot_vix_recorder'),
        ),
        # signal_validator — only writes a log line when a signal fires, so a
        # quiet validator (no signals all day, the common case) is HEALTHY.
        # Process-alive check only; no log-freshness requirement.
        Target(
            name='signal_validator',
            proc_pattern=r'alerts\.signal_validator',
            healthy=lambda: _process_running(r'alerts\.signal_validator') is not None,
            why_unhealthy=lambda: 'process dead',
            restart_cmd=restart('alerts.signal_validator'),
        ),
    ]


# ─── Main loop ──────────────────────────────────────────────────────────────
def _check_one(t: Target, state: TargetState, dry_run: bool) -> None:
    if t.market_hours_only and not _market_hours_now():
        return  # skip — daemon may have legitimately exited

    if t.healthy():
        state.last_healthy_ts = time.time()
        return

    # Within grace period after recent restart? skip
    if time.time() - state.last_restart_ts < t.grace_period_s:
        return

    # why_unhealthy() may raise (e.g. f-string formatting None) — don't let
    # that abort the restart logic. Today (2026-05-28) the tick_recorder
    # wedged for 6 hours because _tick_csv_age returned None on a
    # header-only CSV, the f-string in its why_unhealthy raised TypeError,
    # the outer try/except caught it, and no restart ever fired.
    try:
        reason = t.why_unhealthy()
    except Exception as e:
        reason = f'unhealthy (why_unhealthy raised: {e})'
    log.warning('UNHEALTHY %s — %s', t.name, reason)

    # Already escalated? do not auto-restart
    if state.escalated:
        return

    # Count restarts in last hour
    now = time.time()
    state.restart_history = deque(
        [ts for ts in state.restart_history if now - ts < 3600], maxlen=10)

    if len(state.restart_history) >= MAX_RESTARTS_PER_HOUR:
        state.escalated = True
        msg = (f'🔴 <b>CRITICAL — {t.name}</b>\n'
               f'Hit {MAX_RESTARTS_PER_HOUR} restarts in 1 h.\n'
               f'Reason: <code>{reason}</code>\n'
               f'Monitor STOPPED auto-restarting this target.\n'
               f'Needs human eyes. Check '
               f'<code>logs/trade_bot/monitor.log</code>.')
        log.error('ESCALATED %s — %d restarts/hr exceeded', t.name,
                  MAX_RESTARTS_PER_HOUR)
        _tg_send(msg)
        return

    if dry_run:
        log.info('[dry-run] would restart %s', t.name)
        return

    if _restart(t):
        state.restart_history.append(now)
        state.last_restart_ts = now
        msg = (f'🟡 <b>auto-restart — {t.name}</b>\n'
               f'Reason: <code>{reason}</code>\n'
               f'Attempt {len(state.restart_history)}/{MAX_RESTARTS_PER_HOUR} this hour.')
        _tg_send(msg)


_running = True


def _signal_handler(signum, frame):
    global _running
    log.info('signal %s — shutting down', signum)
    _running = False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--once',    action='store_true', help='single check pass + exit')
    ap.add_argument('--dry-run', action='store_true', help='detect, don’t restart')
    ap.add_argument('--interval', type=int, default=CHECK_INTERVAL_S)
    args = ap.parse_args()

    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    log.info('monitor boot — interval=%ds dry_run=%s once=%s',
             args.interval, args.dry_run, args.once)

    targets = _build_targets()
    states  = {t.name: TargetState() for t in targets}

    last_summary_ts = 0.0
    SUMMARY_INTERVAL = 600   # every 10 minutes log a one-line all-clear

    while _running:
        for t in targets:
            try:
                _check_one(t, states[t.name], args.dry_run)
            except Exception as e:
                log.exception('check %s err: %s', t.name, e)

        # Periodic heartbeat so the user can see the monitor is alive even when
        # everything is fine
        if time.time() - last_summary_ts >= SUMMARY_INTERVAL:
            healthy = [t.name for t in targets
                       if (t.market_hours_only and not _market_hours_now()) or t.healthy()]
            log.info('heartbeat — %d/%d healthy: %s',
                     len(healthy), len(targets), ', '.join(healthy))
            last_summary_ts = time.time()

        if args.once:
            break
        time.sleep(args.interval)

    log.info('monitor exited cleanly')


if __name__ == '__main__':
    main()
