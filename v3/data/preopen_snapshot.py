"""v3/data/preopen_snapshot.py — NSE pre-open auction capture.

NSE runs a call-auction pre-open session 09:00–09:15 IST. By ~09:08–09:12
the indicative equilibrium price is published — that price IS, within a
few points, where continuous trading opens at 09:15.

There is no free GIFT Nifty feed (the daily-report `gift_nifty` field is a
mislabelled stale `^NSEI` proxy). The pre-open auction is the real,
fetchable open-predictor and it's *better* than GIFT Nifty — it's the
actual NSE equilibrium, not an offshore proxy.

HONEST SCOPE: the pre-open price has NO alpha — every desk sees it. This
module is situational-awareness only: it lets the v3 runners (which start
at 09:12) know the expected open + gap a couple of minutes early and
pre-classify the day (gap-and-go vs gap-fill) instead of reacting cold.

Captured once per morning by news.runner (which is already alive from
09:00 — no new cron). Writes v3/cache/preopen_signal.json:

    {
      "ts": "2026-05-19T09:10:03+05:30",
      "NIFTY":     {"preopen": 23510.0, "prev_close": 23643.5,
                    "gap_pts": -133.5, "gap_pct": -0.56,
                    "day_type_hint": "gap-fill-watch"},
      "BANKNIFTY": {...}
    }

day_type_hint heuristic (weak — calibrate later, not alpha):
    |gap| < 0.25%  → "small-gap-likely-fills"
    |gap| > 0.60%  → "large-gap-trend-risk"
    else           → "neutral"
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from datetime import datetime

import pytz

IST  = pytz.timezone('Asia/Kolkata')
ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT  = ROOT / 'v3' / 'cache' / 'preopen_signal.json'

log = logging.getLogger('preopen_snapshot')

# NIFTY index symbol + the cash index to read prev close from
_INSTRUMENTS = {
    'NIFTY':     {'symbol': 'NIFTY',     'exchange': 'NSE', 'segment': 'CASH'},
    'BANKNIFTY': {'symbol': 'BANKNIFTY', 'exchange': 'NSE', 'segment': 'CASH'},
}


def _get_groww():
    """Self-contained Groww auth with 3-try retry (mirrors v3 runner pattern)."""
    import pyotp
    from growwapi import GrowwAPI
    env = {}
    for line in (ROOT / 'token.env').read_text().splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k, _, v = line.partition('=')
            env[k.strip()] = v.strip()
    last_err = None
    for attempt in range(3):
        try:
            totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
            tok  = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
            return GrowwAPI(token=tok)
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(5)
    raise last_err


def _day_type_hint(gap_pct: float) -> str:
    a = abs(gap_pct)
    if a < 0.25:
        return 'small-gap-likely-fills'
    if a > 0.60:
        return 'large-gap-trend-risk'
    return 'neutral'


def capture(groww=None) -> dict | None:
    """Fetch the pre-open indicative for NIFTY + BANKNIFTY, write JSON.

    `groww` — optional pre-authed GrowwAPI instance (news.runner can pass
    its own to avoid a second auth). If None, authenticates fresh.

    Returns the payload dict, or None on total failure.
    """
    try:
        g = groww or _get_groww()
    except Exception as e:
        log.warning("preopen capture: Groww auth failed: %s", e)
        return None

    payload: dict = {'ts': datetime.now(IST).isoformat()}
    ok_any = False

    for name, spec in _INSTRUMENTS.items():
        try:
            q = g.get_quote(trading_symbol=spec['symbol'],
                            exchange=spec['exchange'], segment=spec['segment'])
            ohlc = (q or {}).get('ohlc', {}) or {}
            prev_close = float(ohlc.get('close', 0) or 0)
            # During pre-open, `open` may be the indicative; if not yet set,
            # fall back to last-traded proxy via day_change off prev close.
            preopen = float(ohlc.get('open', 0) or 0)
            if preopen <= 0:
                dc = float(q.get('day_change', 0) or 0)
                preopen = prev_close + dc
            if prev_close <= 0 or preopen <= 0:
                log.warning("preopen %s: incomplete quote %s", name, q)
                continue
            gap_pts = preopen - prev_close
            gap_pct = gap_pts / prev_close * 100.0
            payload[name] = {
                'preopen':       round(preopen, 2),
                'prev_close':    round(prev_close, 2),
                'gap_pts':       round(gap_pts, 1),
                'gap_pct':       round(gap_pct, 3),
                'day_type_hint': _day_type_hint(gap_pct),
            }
            ok_any = True
            log.info("preopen %s: %.1f (gap %+.1f / %+.2f%%) → %s",
                     name, preopen, gap_pts, gap_pct,
                     payload[name]['day_type_hint'])
        except Exception as e:
            log.warning("preopen %s: get_quote failed: %s", name, e)

    if not ok_any:
        return None

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str))
    return payload


def read_latest(max_age_min: int = 30) -> dict | None:
    """Helper for consumers (v3 runners) — returns today's preopen payload
    if it exists and is fresh, else None."""
    if not OUT.exists():
        return None
    try:
        d = json.loads(OUT.read_text())
        ts = datetime.fromisoformat(d['ts'])
        age = (datetime.now(IST) - ts).total_seconds() / 60.0
        return d if age <= max_age_min else None
    except Exception:
        return None


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    r = capture()
    print(json.dumps(r, indent=2) if r else 'capture failed')
