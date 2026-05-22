"""data/gift_nifty.py — Real GIFT Nifty (ex-SGX Nifty) value scraper.

The daily-report `gift_nifty` field was a MISLABELLED stale `^NSEI` proxy
(yfinance NIFTY-spot last hourly close). That is NOT GIFT Nifty — it caused
the overnight-gap POC's 0.43% error.

sgxnifty.org embeds a live intraday series in a JS var `chartIntradayData`:
    var chartIntradayData = [
        {"date":"2026-05-18 01:46:04","value":"23769"},
        {"date":"2026-05-18 06:25:05","value":"23635.5"},
        ...
    ];
The LAST element is the most recent GIFT Nifty print — GIFT City trades
overnight while NSE is shut, so this is a genuine leading reference for
the next NSE open.

PUBLIC:
    get_gift_nifty() -> dict | None
        {'value': 23561.5, 'asof': '2026-05-18 09:08:04',
         'series_len': 312, 'source': 'sgxnifty.org'}

Falls back to None on any failure — caller must handle (the daily report
should degrade gracefully, not crash).
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.request

log = logging.getLogger('gift_nifty')

_URL = 'https://sgxnifty.org/'
_UA  = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) hawala-gift-fetch'

# tolerate sites with imperfect TLS chains
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# Matches:  chartIntradayData = [ {...}, {...} ]
_SERIES_RE = re.compile(
    r'chartIntradayData\s*=\s*(\[.*?\])\s*;', re.DOTALL)


def get_gift_nifty(timeout: int = 12) -> dict | None:
    """Scrape the latest GIFT Nifty value from sgxnifty.org.

    Returns dict {value, asof, series_len, source} or None on failure.
    """
    try:
        req = urllib.request.Request(_URL, headers={'User-Agent': _UA})
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            html = r.read().decode('utf-8', errors='ignore')
    except Exception as e:
        log.warning("gift_nifty: fetch failed: %s", e)
        return None

    m = _SERIES_RE.search(html)
    if not m:
        log.warning("gift_nifty: chartIntradayData not found in page")
        return None

    try:
        series = json.loads(m.group(1))
    except Exception as e:
        log.warning("gift_nifty: series JSON parse failed: %s", e)
        return None

    if not series:
        log.warning("gift_nifty: series empty")
        return None

    # Last entry = most recent print. Series is chronological.
    last = series[-1]
    try:
        value = float(str(last.get('value', '')).replace(',', ''))
    except (TypeError, ValueError):
        log.warning("gift_nifty: last value unparseable: %s", last)
        return None

    if not (10_000 < value < 60_000):
        # sanity band — GIFT Nifty should be in NIFTY's range
        log.warning("gift_nifty: value %.1f outside sane band", value)
        return None

    return {
        'value':      round(value, 2),
        'asof':       last.get('date', ''),
        'series_len': len(series),
        'source':     'sgxnifty.org',
    }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    r = get_gift_nifty()
    print(json.dumps(r, indent=2) if r else 'fetch failed')
