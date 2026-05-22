"""
data/fetch_report_data.py — Live data fetcher for the daily pre-market report.

Single entry point:  fetch_all() -> dict

Every source is wrapped in try/except. On failure the field gets "—"
and a warning is printed. The report always generates even with partial data.

Sources used:
  yfinance       — US, Asian, Europe, India indices, commodities, currencies
  CoinGecko      — BTC, ETH, SOL (free API, no key)
  CNN endpoint   — Fear & Greed score
  NSE India API  — FII/DII flows, option chains
  Google News RSS — top headlines
  Groww API      — BN futures (gap pts, ATR14, pivots)  [optional — skipped if no token]
"""

import os
import re
import math
import time
import datetime
import requests
import yfinance as yf
from pathlib import Path


# ── Helpers ────────────────────────────────────────────────────────────────

def _safe(fn, default="—"):
    try:
        return fn()
    except Exception as e:
        print(f"  ⚠ fetch warning: {e}")
        return default


def _pct(new, old):
    if old and old != 0:
        return round((new - old) / old * 100, 2)
    return 0.0


def _fmt_pct(val):
    if val == "—":
        return "—"
    try:
        v = float(val)
        return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"
    except:
        return str(val)


def _ticker_row(ticker_sym, label):
    """Fetch a single yfinance ticker and return a market row dict."""
    try:
        t = yf.Ticker(ticker_sym)
        hist = t.history(period="5d", interval="1d")
        if hist.empty or len(hist) < 2:
            return {"name": label, "price": "—", "chg_pct": "—", "chg_pts": "—"}
        prev = hist["Close"].iloc[-2]
        last = hist["Close"].iloc[-1]
        chg_pts = round(last - prev, 2)
        chg_pct = round(_pct(last, prev), 2)
        return {
            "name":    label,
            "price":   round(last, 2),
            "chg_pts": chg_pts,
            "chg_pct": chg_pct,
        }
    except Exception as e:
        print(f"  ⚠ {label} ({ticker_sym}): {e}")
        return {"name": label, "price": "—", "chg_pct": "—", "chg_pts": "—"}


def _ticker_last(ticker_sym):
    """Return (last_price, prev_price) or (None, None)."""
    try:
        t = yf.Ticker(ticker_sym)
        hist = t.history(period="5d", interval="1d")
        if hist.empty:
            return None, None
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last
        return last, prev
    except:
        return None, None


# ── Fear & Greed ────────────────────────────────────────────────────────────

def _fetch_fear_greed():
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://edition.cnn.com/",
    }
    r = requests.get(url, headers=headers, timeout=10)
    data = r.json()
    score = data["fear_and_greed"]["score"]
    rating = data["fear_and_greed"]["rating"]
    return int(score), rating.upper()


def _fg_label(score):
    if score <= 25:   return "EXTREME FEAR"
    if score <= 44:   return "FEAR"
    if score <= 55:   return "NEUTRAL"
    if score <= 75:   return "GREED"
    return "EXTREME GREED"


# ── Crypto ──────────────────────────────────────────────────────────────────

def _fetch_crypto():
    url = ("https://api.coingecko.com/api/v3/simple/price"
           "?ids=bitcoin,ethereum,solana"
           "&vs_currencies=usd"
           "&include_24hr_change=true")
    r = requests.get(url, timeout=10)
    d = r.json()

    def _row(cg_id, label, sym):
        price  = d.get(cg_id, {}).get("usd", "—")
        chg    = d.get(cg_id, {}).get("usd_24h_change", "—")
        if price != "—":
            price = round(price, 0)
        if chg != "—":
            chg = round(chg, 2)
        return {"name": label, "symbol": sym, "price_usd": price, "chg_pct_24h": chg}

    return [
        _row("bitcoin",  "Bitcoin",  "BTC"),
        _row("ethereum", "Ethereum", "ETH"),
        _row("solana",   "Solana",   "SOL"),
    ]


# ── NSE India ───────────────────────────────────────────────────────────────

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}


def _nse_session():
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    # warm-up cookie
    s.get("https://www.nseindia.com/", timeout=10)
    return s


def _fetch_fii_dii(sess=None):
    try:
        if sess is None:
            sess = _nse_session()
        url = "https://www.nseindia.com/api/fiidiiTradeReact"
        r = sess.get(url, timeout=10)
        data = r.json()
        # data is a list; last entry is most recent
        # Fields: category, buyValue, sellValue, netValue
        result = []
        for row in data[:4]:   # FII Cash, DII Cash, FII F&O, DII F&O
            result.append({
                "category": row.get("category", "—"),
                "buy":      row.get("buyValue",  "—"),
                "sell":     row.get("sellValue", "—"),
                "net":      row.get("netValue",  "—"),
            })
        return result
    except Exception as e:
        print(f"  ⚠ FII/DII: {e}")
        return []


def _fetch_bhav_copy(date_obj):
    """Download NSE F&O bhav copy for a given trading date. Returns parsed rows or []."""
    import io, zipfile, csv as _csv
    date_str = date_obj.strftime("%Y%m%d")
    url = (f"https://nsearchives.nseindia.com/content/fo/"
           f"BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip")
    try:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0"})
        r = sess.get(url, timeout=25)
        if r.status_code != 200 or len(r.content) < 1000:
            return []
        z = zipfile.ZipFile(io.BytesIO(r.content))
        content = z.read(z.namelist()[0]).decode("utf-8")
        return list(_csv.DictReader(io.StringIO(content)))
    except Exception as e:
        print(f"  ⚠ Bhav copy {date_str}: {e}")
        return []


def _fetch_option_chain(symbol, bhav_rows=None):
    """
    Build option chain OI summary from NSE F&O bhav copy.
    Falls back to last 5 trading days if today's file isn't available yet.
    """
    from datetime import date as _date, timedelta as _td
    from collections import defaultdict

    _EMPTY = {"near_expiry": "—", "atm": "—", "pcr": "—",
              "top_ce_strikes": [], "top_pe_strikes": []}

    # Use pre-loaded rows if supplied, otherwise fetch last trading day
    rows = bhav_rows
    if rows is None:
        for offset in range(1, 6):
            d = _date.today() - _td(days=offset)
            if d.weekday() >= 5:
                continue
            rows = _fetch_bhav_copy(d)
            if rows:
                break

    if not rows:
        return _EMPTY

    try:
        # Filter: IDO = Index Derivative Options for this symbol
        sym_rows = [r for r in rows
                    if r["TckrSymb"] == symbol and r["FinInstrmTp"] == "IDO"]
        if not sym_rows:
            return _EMPTY

        expiries   = sorted(set(r["XpryDt"] for r in sym_rows))
        near_exp   = expiries[0] if expiries else "—"
        near_rows  = [r for r in sym_rows if r["XpryDt"] == near_exp]

        underlying = next((float(r["UndrlygPric"]) for r in near_rows
                           if r.get("UndrlygPric")), 0)

        ce_oi: dict = defaultdict(int)
        pe_oi: dict = defaultdict(int)
        for row in near_rows:
            strike = float(row["StrkPric"])
            oi     = int(float(row["OpnIntrst"] or 0))
            if row["OptnTp"] == "CE":
                ce_oi[strike] += oi
            elif row["OptnTp"] == "PE":
                pe_oi[strike] += oi

        tot_ce = sum(ce_oi.values())
        tot_pe = sum(pe_oi.values())
        pcr    = round(tot_pe / tot_ce, 2) if tot_ce else "—"

        step = 100 if "BANK" in symbol else 50
        atm  = round(underlying / step) * step if underlying else 0

        # 9 strikes centred around ATM for the OI chart
        all_strikes = sorted(set(list(ce_oi.keys()) + list(pe_oi.keys())))
        if all_strikes and atm:
            ai = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - atm))
            near_strikes = all_strikes[max(0, ai - 4): ai + 5]
        else:
            near_strikes = all_strikes[:9]

        # Format for HTML renderer: {strike, ce_oi (Lakhs), pe_oi (Lakhs)}
        strike_chain = [
            {
                "strike": int(s),
                "ce_oi":  round(ce_oi.get(s, 0) / 1e5, 1),
                "pe_oi":  round(pe_oi.get(s, 0) / 1e5, 1),
            }
            for s in sorted(near_strikes, reverse=True)
        ]

        # Legacy top-3 lists kept for backward compat
        top_ce = sorted(ce_oi.items(), key=lambda x: -x[1])[:3]
        top_pe = sorted(pe_oi.items(), key=lambda x: -x[1])[:3]

        return {
            "near_expiry":    near_exp,
            "atm":            int(atm),
            "pcr":            pcr,
            "strike_chain":   strike_chain,   # new — used by OI chart
            "top_ce_strikes": [{"strike": int(s), "oi": round(o / 1e5, 1)} for s, o in top_ce],
            "top_pe_strikes": [{"strike": int(s), "oi": round(o / 1e5, 1)} for s, o in top_pe],
        }
    except Exception as e:
        print(f"  ⚠ Option chain parse {symbol}: {e}")
        return _EMPTY


# ── Google News RSS ─────────────────────────────────────────────────────────

import html as _html_lib

def _html_unescape(text):
    return _html_lib.unescape(text)


# Priority RSS sources — each tagged with a default category.
# Trust principle (May 2026): only editorially-accountable wires + broad Google
# News queries restricted to whitelisted domains. Removed:
#   - bare Google News queries that surfaced any ranking site (clickbait risk)
# All broad GN searches below now use `site:` filters limiting to Reuters,
# Bloomberg, FT, WSJ, CNBC, BBC, AP, Moneycontrol, ET, Mint, Hindu BusinessLine.
_NEWS_FEEDS = [
    # ── Tier-1 direct (verified live feeds) ────────────────────────────────
    ("https://feeds.bbci.co.uk/news/business/rss.xml",                                                                                "global"),
    ("https://www.cnbc.com/id/10000664/device/rss/rss.html",                                                                          "global"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",                                                                     "macro"),
    ("https://economictimes.indiatimes.com/rssfeedstopstories.cms",                                                                   "india"),
    ("https://www.livemint.com/rss/markets",                                                                                          "india"),
    ("https://www.thehindubusinessline.com/markets/feeder/default.rss",                                                               "india"),
    ("https://pulse.zerodha.com/feed.php",                                                                                            "india"),

    # ── Domain-filtered Google News queries (trust-whitelist) ─────────────
    # Macro / central banks
    ("https://news.google.com/rss/search?q=(site:reuters.com+OR+site:bloomberg.com+OR+site:ft.com+OR+site:wsj.com+OR+site:cnbc.com+OR+site:apnews.com)+(federal+reserve+OR+rbi+OR+ecb+OR+interest+rate+OR+central+bank)&hl=en&gl=US&ceid=US:en",   "macro"),
    # Geopolitics (kept narrow + whitelisted)
    ("https://news.google.com/rss/search?q=(site:reuters.com+OR+site:bloomberg.com+OR+site:ft.com+OR+site:wsj.com+OR+site:apnews.com+OR+site:bbc.com)+(geopolitical+risk+OR+iran+OR+russia+ukraine+OR+china+tariff+OR+middle+east+OR+ceasefire)&hl=en&gl=US&ceid=US:en", "global"),
    # Oil / energy
    ("https://news.google.com/rss/search?q=(site:reuters.com+OR+site:bloomberg.com+OR+site:ft.com+OR+site:wsj.com+OR+site:cnbc.com)+(oil+OR+crude+OR+opec+OR+brent)&hl=en&gl=US&ceid=US:en",                            "energy"),
    # India macro
    ("https://news.google.com/rss/search?q=(site:reuters.com+OR+site:bloomberg.com+OR+site:moneycontrol.com+OR+site:economictimes.indiatimes.com+OR+site:livemint.com+OR+site:thehindubusinessline.com)+(rbi+OR+sebi+OR+nifty+OR+sensex+OR+india+economy)&hl=en-IN&gl=IN&ceid=IN:en",   "india"),
    # US indices
    ("https://news.google.com/rss/search?q=(site:reuters.com+OR+site:bloomberg.com+OR+site:ft.com+OR+site:wsj.com+OR+site:cnbc.com)+(S%26P500+OR+nasdaq+OR+dow+jones)&hl=en&gl=US&ceid=US:en",                          "global"),
    # Crypto removed — neither a market-mover for our books nor whitelistable
    # without surfacing low-trust crypto media. If we want it back, scope to
    # site:reuters.com OR site:bloomberg.com only.
]

# Map feed URL prefix → display source name (for the news card)
_FEED_SOURCE = {
    "feeds.bbci.co.uk":            "BBC Business",
    "www.cnbc.com":                 "CNBC",
    "rss.nytimes.com":              "NY Times",
    "economictimes.indiatimes":     "Economic Times",
    "www.livemint.com":             "Livemint",
    "www.thehindubusinessline.com": "Hindu BusinessLine",
    "pulse.zerodha.com":            "Zerodha Pulse",
    "news.google.com":              "Google News",
}

# High-signal keywords that boost an item to "must include"
_HIGH_PRIORITY = [
    "rate cut", "rate hike", "interest rate", "federal reserve", "fed ", "rbi ",
    "inflation", "recession", "gdp", "tariff", "sanction", "war", "ceasefire",
    "blockade", "opec", "crude surge", "oil spike", "bitcoin", "crash", "rally",
    "circuit breaker", "default", "debt ceiling", "election", "coup",
]


def _feed_source_name(url: str) -> str:
    """Return a human-readable source label for a feed URL."""
    for key, name in _FEED_SOURCE.items():
        if key in url:
            return name
    return "News"


def _fetch_news():
    """Fetch high-priority global financial & geopolitical news from multiple RSS feeds."""
    HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    seen, results = set(), []
    is_google_news = False

    for url, default_tag in _NEWS_FEEDS:
        source_name = _feed_source_name(url)
        is_google_news = "news.google.com" in url
        try:
            r = requests.get(url, timeout=8, headers=HDR)
            if not r.ok:
                print(f"  ⚠ News feed {source_name} HTTP {r.status_code}")
                continue
            # Extract <item> blocks first, then pull <title> from each
            items_xml = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
            for block in items_xml[:6]:
                title_m = re.search(r"<title>(.*?)</title>", block)
                if not title_m:
                    continue
                raw = _html_unescape(title_m.group(1)).strip()
                # Strip CDATA wrapper if present (e.g. BBC RSS)
                raw = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", raw, flags=re.DOTALL).strip()
                # Google News appends " - Source Name" — strip it; direct feeds are already clean
                if is_google_news:
                    raw = re.sub(r"\s*[-|]\s*[A-Z][^-|]{3,40}$", "", raw).strip()
                # Skip very short, duplicates, or boilerplate
                if len(raw) < 25 or raw in seen:
                    continue
                if any(skip in raw.lower() for skip in ["google news", "rss", "feed"]):
                    continue
                seen.add(raw)
                tag = _classify_news(raw) or default_tag
                # Compute priority score
                hl_lower = raw.lower()
                priority = sum(1 for kw in _HIGH_PRIORITY if kw in hl_lower)
                results.append({"headline": raw, "tag": tag, "source": source_name, "priority": priority})
        except Exception as e:
            print(f"  ⚠ News feed {source_name} ({url[:45]}): {e}")
            continue

    # ── Zerodha Pulse — curated India market headlines ────────────────────────
    for item in _fetch_pulse_news():
        if item["headline"] not in seen:
            seen.add(item["headline"])
            results.append(item)

    if not results:
        return []

    # Sort: high-priority first, then by diversity of tags
    results.sort(key=lambda x: -x["priority"])

    # Ensure tag diversity — pick at most 2 per tag
    tag_counts: dict = {}
    selected = []
    # First pass: high-priority items regardless of tag
    for item in results:
        if item["priority"] >= 2 and len(selected) < 3:
            tag_counts[item["tag"]] = tag_counts.get(item["tag"], 0) + 1
            selected.append(item)
    # Second pass: fill remaining slots with diverse tags
    for item in results:
        if len(selected) >= 7:
            break
        if item in selected:
            continue
        if tag_counts.get(item["tag"], 0) < 2:
            tag_counts[item["tag"]] = tag_counts.get(item["tag"], 0) + 1
            selected.append(item)

    return [{"headline": i["headline"], "tag": i["tag"], "source": i["source"]} for i in selected[:7]]


def _classify_news(headline):
    hl = headline.lower()
    if any(w in hl for w in ["oil", "crude", "gas", "energy", "opec", "brent"]):
        return "energy"
    if any(w in hl for w in ["bitcoin", "crypto", "eth", "solana", "blockchain"]):
        return "crypto"
    if any(w in hl for w in ["us ", "fed", "dollar", "trump", "china", "global", "war"]):
        return "global"
    if any(w in hl for w in ["rbi", "sebi", "india", "sensex", "nifty", "bse", "nse",
                               "rupee", "inflation", "budget"]):
        return "india"
    return "macro"


def _fetch_pulse_news(max_items: int = 30):
    """
    Scrape Zerodha Pulse (pulse.zerodha.com) for the latest curated India market headlines.
    Site is plain PHP/HTML — no JS needed, no auth, no RSS/API.
    Sources: NDTV Business, Economic Times, The Hindu Business (Zerodha-curated).

    Items are returned newest-first; we take the top max_items without date filtering
    because the daily report runs at 07:30 IST when today's news is sparse — yesterday's
    closing session headlines are exactly what the pre-market report needs.

    Returns list of {"headline", "tag", "source", "priority"} dicts.
    """
    HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
    try:
        r = requests.get("https://pulse.zerodha.com/", timeout=10, headers=HDR)
        if not r.ok:
            print(f"  ⚠ Pulse fetch HTTP {r.status_code}")
            return []
    except Exception as e:
        print(f"  ⚠ Pulse fetch: {e}")
        return []

    # Each news item is a <li class="box item"> block (already sorted newest-first)
    item_blocks = re.findall(
        r'<li class="box item"[^>]*>(.*?)</li>', r.text, re.DOTALL)

    results, seen_hl = [], set()

    for block in item_blocks[:max_items]:
        # Headline from <h2 class="title"><a ...>TEXT</a></h2>
        hl_m = re.search(r'<h2 class="title">\s*<a[^>]*>([^<]+)</a>', block)
        if not hl_m:
            continue
        headline = _html_lib.unescape(hl_m.group(1)).strip()
        if len(headline) < 25 or headline in seen_hl:
            continue
        seen_hl.add(headline)

        # Source publication from <span class="feed">— NDTV Business</span>
        src_m = re.search(r'<span class="feed">—\s*([^<]+)</span>', block)
        source = src_m.group(1).strip() if src_m else "Pulse (Zerodha)"

        tag      = _classify_news(headline) or "india"
        priority = sum(1 for kw in _HIGH_PRIORITY if kw in headline.lower())
        # Floor of 2: Pulse is Zerodha-curated specifically for market traders — always
        # treat as high-signal so they compete in the first-pass selection alongside RSS
        priority = max(priority, 2)

        results.append({"headline": headline, "tag": tag,
                        "source": source, "priority": priority})

    print(f"  📰 Pulse: {len(results)} headlines scraped")
    return results


# ── Crypto Fear & Greed (alternative.me) ────────────────────────────────────

def _fetch_crypto_fg():
    url = "https://api.alternative.me/fng/"
    r = requests.get(url, timeout=8)
    d = r.json()["data"][0]
    score = int(d["value"])
    label = d["value_classification"].upper()
    return score, label


# ── Scenario text generation ─────────────────────────────────────────────────

def _generate_scenario_text(sig, bn, nifty, vix_val, sp_chg, commodities):
    gap_pts  = bn.get("gap_pts", "—")
    bn_close = bn.get("prev_close")
    atr14    = bn.get("atr14", 0)
    strategy = sig.get("overall", "NO TRADE")
    gap_dir  = sig.get("gap_dir", "FLAT")
    overall  = strategy

    parts = []

    # Opening range estimate
    try:
        gp = float(gap_pts)
        bc = float(bn_close)
        bn_open = bc + gp
        buf = max(80, abs(gp) * 0.25)
        low  = round(bn_open - buf)
        high = round(bn_open + buf)
        sign = "+" if gp >= 0 else ""
        parts.append(
            f"BankNifty estimated to open around {low:,.0f}–{high:,.0f} "
            f"(gap ~{sign}{gp:.0f} pts vs prev close {bc:,.0f})."
        )
    except:
        pass

    # Strategy context
    if overall == "ORB":
        dir_w = "Gap-Up" if gap_dir == "GAP UP" else "Gap-Down"
        parts.append(
            f"{dir_w} qualifies for ORB strategy. "
            f"Watch 9:15–9:30 AM range for a breakout — "
            f"{'long above ORB high' if gap_dir == 'GAP UP' else 'short below ORB low'} "
            f"with ATR-based stop ({round(0.4 * float(atr14)):,.0f} pts)."
        )
    elif overall == "OPTIONS_ORB":
        opt = "CE" if gap_dir == "GAP UP" else "PE"
        parts.append(
            f"Large gap routes to OPTIONS ORB — buy ATM {opt} at open. "
            f"Max loss = premium paid. Target 2× premium, stop at 50% loss."
        )
    elif overall == "VWAP_REVERSION":
        parts.append(
            "Flat open routes to VWAP Reversion. "
            "Wait for price to deviate 0.25% from VWAP, then fade the move back."
        )
    elif overall == "NO TRADE":
        parts.append(f"No trade today: {sig.get('reason', '')}.")

    # VIX context
    try:
        vv = float(vix_val)
        if vv > 22:
            parts.append(f"India VIX at {vv:.2f} — elevated volatility, widen stops.")
        elif vv < 14:
            parts.append(f"India VIX at {vv:.2f} — low volatility, signals tend to be cleaner.")
    except:
        pass

    # S&P overnight
    try:
        sp = float(str(sp_chg).replace("%", "").replace("+", ""))
        if sp < -2.0:
            parts.append(f"S&P fell {sp_chg} overnight — risk-off, expect broader weakness at open.")
        elif sp > 1.5:
            parts.append(f"S&P gained {sp_chg} — positive risk sentiment may support BankNifty.")
    except:
        pass

    # Oil spike check
    for row in commodities:
        if "Brent" in row.get("name", ""):
            try:
                chg = float(str(row.get("chg_pct", "0")).replace("%", "").replace("+", ""))
                if abs(chg) > 3:
                    dir_w = "surge" if chg > 0 else "drop"
                    parts.append(
                        f"Brent crude {dir_w} of {row['chg_pct']} — "
                        "watch OMCs and aviation stocks for sector impact."
                    )
            except:
                pass
            break

    return " ".join(parts) or "Opening scenario will be clearer at 9:15 AM based on actual gap."


# ── Events calendar ──────────────────────────────────────────────────────────

def _build_events_calendar(sig, bn):
    strategy = sig.get("overall", "NO TRADE")
    gap_dir  = sig.get("gap_dir", "FLAT")

    orb_note = "Watch ORB breakout — entry signal if strategy = ORB" if strategy == "ORB" else "ORB window closes"
    events = [
        {"time": "9:00 AM",  "event": "Pre-Open Session — NSE / BSE",                    "impact": "medium"},
        {"time": "9:15 AM",  "event": "Market Open — BankNifty gap signal confirmation",  "impact": "high"},
        {"time": "9:20 AM",  "event": "First 5-min candle close — Hawala v2 entry check", "impact": "high"},
        {"time": "9:30 AM",  "event": orb_note,                                           "impact": "high" if strategy in ("ORB","OPTIONS_ORB") else "low"},
        {"time": "3:30 PM",  "event": "Market Close — Log trade outcome",                 "impact": "medium"},
    ]
    return events


# ── Pivot calculation ───────────────────────────────────────────────────────

def _classic_pivots(high, low, close):
    pp = round((high + low + close) / 3, 0)
    r1 = round(2 * pp - low, 0)
    s1 = round(2 * pp - high, 0)
    r2 = round(pp + (high - low), 0)
    s2 = round(pp - (high - low), 0)
    r3 = round(high + 2 * (pp - low), 0)
    s3 = round(low - 2 * (high - pp), 0)
    return {"PP": pp, "R1": r1, "R2": r2, "R3": r3,
            "S1": s1, "S2": s2, "S3": s3}


def _fib_pivots(high, low, close):
    pp   = round((high + low + close) / 3, 0)
    rng  = high - low
    r1   = round(pp + 0.382 * rng, 0)
    r2   = round(pp + 0.618 * rng, 0)
    r3   = round(pp + 1.000 * rng, 0)
    s1   = round(pp - 0.382 * rng, 0)
    s2   = round(pp - 0.618 * rng, 0)
    s3   = round(pp - 1.000 * rng, 0)
    return {"PP": pp, "R1": r1, "R2": r2, "R3": r3,
            "S1": s1, "S2": s2, "S3": s3}


# ── ATR14 from yfinance daily ──────────────────────────────────────────────

def _atr14_daily(ticker_sym):
    try:
        t = yf.Ticker(ticker_sym)
        hist = t.history(period="30d", interval="1d")
        if len(hist) < 15:
            return None
        tr_list = []
        for i in range(1, len(hist)):
            h = hist["High"].iloc[i]
            l = hist["Low"].iloc[i]
            pc = hist["Close"].iloc[i-1]
            tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = round(sum(tr_list[-14:]) / 14, 0)
        return atr
    except:
        return None


# ── BankNifty / Nifty deep analysis ────────────────────────────────────────

def _fetch_bn_analysis(usdinr):
    """Fetch BankNifty previous day OHLC from yfinance and compute pivots/ATR14."""
    try:
        t = yf.Ticker("^NSEBANK")
        hist = t.history(period="30d", interval="1d")
        if hist.empty:
            return {}
        last_row  = hist.iloc[-1]
        prev_row  = hist.iloc[-2] if len(hist) >= 2 else last_row
        prev2_row = hist.iloc[-3] if len(hist) >= 3 else prev_row

        last_close = round(float(last_row["Close"]), 0)
        prev_close = round(float(prev_row["Close"]), 0)
        last_high  = round(float(last_row["High"]), 0)
        last_low   = round(float(last_row["Low"]), 0)
        last_open  = round(float(last_row["Open"]), 0)

        day_chg    = round(last_close - prev_close, 0)
        day_chg_pct = round(_pct(last_close, prev_close), 2)

        atr14 = _atr14_daily("^NSEBANK") or 0

        pivots_classic = _classic_pivots(last_high, last_low, last_close)
        pivots_fib     = _fib_pivots(last_high, last_low, last_close)

        # Estimated gap based on GIFT Nifty ratio
        # Approximate BN gap from GIFT: use BN/Nifty ratio ~2.3
        # (will be overridden by gap_pts computed in fetch_all)

        return {
            "prev_close":     last_close,
            "prev_open":      last_open,
            "prev_high":      last_high,
            "prev_low":       last_low,
            "day_chg":        day_chg,
            "day_chg_pct":    day_chg_pct,
            "atr14":          atr14,
            "pivots_classic": pivots_classic,
            "pivots_fib":     pivots_fib,
        }
    except Exception as e:
        print(f"  ⚠ BN analysis: {e}")
        return {}


def _fetch_nifty_analysis():
    try:
        t = yf.Ticker("^NSEI")
        hist = t.history(period="30d", interval="1d")
        if hist.empty:
            return {}
        last_row = hist.iloc[-1]
        prev_row = hist.iloc[-2] if len(hist) >= 2 else last_row

        last_close = round(float(last_row["Close"]), 2)
        prev_close = round(float(prev_row["Close"]), 2)
        last_high  = round(float(last_row["High"]), 2)
        last_low   = round(float(last_row["Low"]), 2)

        day_chg     = round(last_close - prev_close, 2)
        day_chg_pct = round(_pct(last_close, prev_close), 2)

        atr14 = _atr14_daily("^NSEI") or 0

        pivots_classic = _classic_pivots(last_high, last_low, last_close)
        pivots_fib     = _fib_pivots(last_high, last_low, last_close)

        return {
            "prev_close":     last_close,
            "prev_high":      last_high,
            "prev_low":       last_low,
            "day_chg":        day_chg,
            "day_chg_pct":    day_chg_pct,
            "atr14":          atr14,
            "pivots_classic": pivots_classic,
            "pivots_fib":     pivots_fib,
        }
    except Exception as e:
        print(f"  ⚠ Nifty analysis: {e}")
        return {}


# ── Hawala signal logic ─────────────────────────────────────────────────────

def _compute_signal(vix, sp_chg_pct, fii_net_cr, gap_pts, weekday, bn_close):
    """Return signal dict matching config.py MACRO thresholds."""
    from config import MACRO, STRATEGIES, INSTRUMENTS

    vix_thresh   = MACRO["vix_threshold"]   # 19.0
    sp_thresh    = MACRO["sp_threshold"]    # -1.5 %
    fii_thresh   = MACRO["fpi_threshold"]   # -3000 Cr

    vix_pass  = (vix != "—" and float(vix)    <= vix_thresh)  if vix != "—" else None
    sp_pass   = (sp_chg_pct != "—" and float(sp_chg_pct) >= sp_thresh) if sp_chg_pct != "—" else None
    fii_pass  = (fii_net_cr != "—" and float(fii_net_cr)  >= fii_thresh) if fii_net_cr != "—" else None

    # Count failures (None = data unavailable, treat as pass for non-blocking)
    failures = sum(0 if x is None or x else 1
                   for x in [vix_pass, sp_pass, fii_pass])
    macro_blocked = failures >= MACRO["min_filters"]

    # DOW filter
    orb_params    = STRATEGIES["orb"]["params"]
    dow_allow     = orb_params.get("ORB_DOW_ALLOW", [1, 2, 4])
    dow_blocked   = weekday not in dow_allow
    dow_names     = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_name      = dow_names[weekday] if weekday < len(dow_names) else "—"

    # Gap routing
    gap_abs = abs(gap_pts) if gap_pts != "—" else 0
    orb_buf = INSTRUMENTS["BANKNIFTY"]["min_gap"]   # 50 pts
    large_gap_min = orb_params.get("ORB_MAX_GAP_FUTURES", 100)

    if gap_abs < orb_buf:
        gap_strategy = "VWAP_REVERSION"
    elif gap_abs < large_gap_min:
        gap_strategy = "ORB"
    else:
        gap_strategy = "OPTIONS_ORB"

    gap_dir = "GAP UP" if (gap_pts != "—" and gap_pts > 0) else "GAP DOWN" if (gap_pts != "—" and gap_pts < 0) else "FLAT"

    # Overall decision
    if macro_blocked:
        overall = "NO TRADE"
        reason  = f"Macro filters blocked ({failures}/3 failed)"
    elif dow_blocked:
        overall = "NO TRADE"
        reason  = f"{dow_name} excluded by DOW filter"
    else:
        overall = gap_strategy
        reason  = f"{gap_dir} of {gap_abs:.0f} pts → {gap_strategy}"

    return {
        "vix_val":       vix,
        "vix_pass":      vix_pass,
        "vix_thresh":    vix_thresh,
        "sp_chg":        sp_chg_pct,
        "sp_pass":       sp_pass,
        "sp_thresh":     sp_thresh,
        "fii_net":       fii_net_cr,
        "fii_pass":      fii_pass,
        "fii_thresh":    fii_thresh,
        "macro_blocked": macro_blocked,
        "dow_blocked":   dow_blocked,
        "dow_name":      dow_name,
        "gap_pts":       gap_pts,
        "gap_dir":       gap_dir,
        "gap_strategy":  gap_strategy,
        "overall":       overall,
        "reason":        reason,
    }


# ── Master function ─────────────────────────────────────────────────────────

def fetch_all() -> dict:
    """
    Fetch all pre-market data from live sources.
    Returns a unified dict used by gen_report.py and saved as JSON.
    """
    now_ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    date_str = now_ist.strftime("%A, %B %d, %Y")
    date_iso = now_ist.strftime("%Y-%m-%d")
    generated_at = now_ist.strftime("%I:%M %p IST")
    weekday = now_ist.weekday()   # 0=Mon, 4=Fri

    print("=" * 60)
    print(f"  Hawala v2 — fetching report data  ({date_str})")
    print("=" * 60)

    # ── 1. US Markets ─────────────────────────────────────────────
    print("\n[1/9] US markets...")
    sp_row    = _ticker_row("^GSPC",  "S&P 500")
    dow_row   = _ticker_row("^DJI",   "Dow Jones")
    nas_row   = _ticker_row("^IXIC",  "Nasdaq")
    us_vix    = _ticker_row("^VIX",   "US VIX")
    us_markets = [sp_row, dow_row, nas_row, us_vix]

    sp_chg_pct = sp_row.get("chg_pct", "—")

    # ── 2. Asian Markets ──────────────────────────────────────────
    print("[2/9] Asian markets...")
    asian_markets = [
        _ticker_row("^N225",  "Nikkei 225"),
        _ticker_row("^HSI",   "Hang Seng"),
        _ticker_row("^KS11",  "KOSPI"),
        _ticker_row("^AXJO",  "ASX 200"),
    ]

    # ── 3. Europe ─────────────────────────────────────────────────
    print("[3/9] European markets...")
    europe_markets = [
        _ticker_row("^GDAXI", "DAX"),
        _ticker_row("^FTSE",  "FTSE 100"),
        _ticker_row("^FCHI",  "CAC 40"),
    ]

    # ── 4. India Indices ──────────────────────────────────────────
    print("[4/9] India indices...")
    india_markets = [
        _ticker_row("^NSEI",    "Nifty 50"),
        _ticker_row("^BSESN",   "Sensex"),
        _ticker_row("^NSEBANK", "Bank Nifty"),
        _ticker_row("^CNXIT",   "Nifty IT"),
        _ticker_row("^CNXPHARMA", "Nifty Pharma"),
        _ticker_row("NIFTYMIDCAP150.NS", "Nifty Midcap 150"),
    ]
    india_vix_row = _ticker_row("^INDIAVIX", "India VIX")
    vix_val = india_vix_row.get("price", "—")

    # ── 5. Commodities + Currencies ───────────────────────────────
    print("[5/9] Commodities & currencies...")
    brent_last, brent_prev = _ticker_last("BZ=F")
    wti_last,   wti_prev   = _ticker_last("CL=F")
    gold_last,  gold_prev  = _ticker_last("GC=F")
    silv_last,  silv_prev  = _ticker_last("SI=F")
    ng_last,    ng_prev    = _ticker_last("NG=F")

    usdinr_last, usdinr_prev = _ticker_last("USDINR=X")
    eurusd_last, eurusd_prev = _ticker_last("EURUSD=X")
    usdjpy_last, usdjpy_prev = _ticker_last("USDJPY=X")
    gbpusd_last, gbpusd_prev = _ticker_last("GBPUSD=X")
    dxy_last,    dxy_prev    = _ticker_last("DX-Y.NYB")

    usdinr = usdinr_last or 84.0

    # Derived INR cross rates
    eurinr_last  = round(eurusd_last  * usdinr, 2) if eurusd_last  and usdinr_last else None
    eurinr_prev  = round(eurusd_prev  * usdinr, 2) if eurusd_prev  and usdinr_prev else None
    gbpinr_last  = round(gbpusd_last  * usdinr, 2) if gbpusd_last  and usdinr_last else None
    gbpinr_prev  = round(gbpusd_prev  * usdinr, 2) if gbpusd_prev  and usdinr_prev else None

    # Live MCX Gold & Silver from bullions.co.in (actual MCX prices, not formula)
    mcx_gold_val = mcx_gold_chg = mcx_silv_val = mcx_silv_chg = None
    try:
        import re as _re
        _b = requests.Session()
        _b.headers.update({"User-Agent": "Mozilla/5.0"})
        _br = _b.get("https://bullions.co.in/", timeout=12)
        _bidx = _br.text.lower().find("live exchange rate")
        if _bidx >= 0:
            _bsec = _br.text[_bidx:_bidx+3000]
            _brows = _re.findall(
                r'<td class="text-left">(.*?)</td>\s*<td[^>]*><div>([\d,\.]+)</div></td>'
                r'\s*<td[^>]*><div>([\-\d,\.]+)</div></td>\s*<td[^>]*><div>([\-\d\.]+%)</div>',
                _bsec, _re.DOTALL)
            for _bname, _bprice, _bchg, _bchgpct in _brows:
                _bname = _re.sub(r'<.*?>', '', _bname).strip()
                _bprice_f = float(_bprice.replace(",", ""))
                _bchgpct_f = float(_bchgpct.replace("%", ""))
                if "Gold" in _bname and "MCX" in _bname:
                    mcx_gold_val = int(_bprice_f)
                    mcx_gold_chg = _bchgpct_f
                elif "Silver" in _bname and "MCX" in _bname:
                    mcx_silv_val = int(_bprice_f)
                    mcx_silv_chg = _bchgpct_f
        if mcx_gold_val:
            print(f"  ✅ MCX Gold: ₹{mcx_gold_val:,}/10g  Silver: ₹{mcx_silv_val:,}/kg (bullions.co.in)")
    except Exception as _be:
        print(f"  ⚠ bullions.co.in: {_be}")

    # Fallback: crude formula if scrape fails
    if not mcx_gold_val and gold_last and usdinr:
        _DUTY = (1 + 0.06 + 0.05 + 0.035 * 0.06) * (1 + 0.03)
        mcx_gold_val = int(gold_last / 31.1035 * usdinr * _DUTY * 10)
        mcx_silv_val = int(silv_last / 31.1035 * usdinr * _DUTY * 1000) if silv_last else None

    def _mcx_crude(brent_bbl_usd, usdinr_rate):
        if brent_bbl_usd is None:
            return None
        return round(brent_bbl_usd * usdinr_rate, 0)

    mcx_crude_val = _mcx_crude(brent_last, usdinr)

    commodities_spot = [
        {"name": "Brent Crude",  "price": f"${round(brent_last,2)}/bbl" if brent_last else "—",
         "chg_pct": _fmt_pct(_pct(brent_last, brent_prev)) if brent_last and brent_prev else "—"},
        {"name": "WTI Crude",    "price": f"${round(wti_last,2)}/bbl"   if wti_last   else "—",
         "chg_pct": _fmt_pct(_pct(wti_last, wti_prev))   if wti_last   and wti_prev   else "—"},
        {"name": "MCX Crude",    "price": f"Rs.{mcx_crude_val:,.0f}/bbl" if mcx_crude_val else "—",
         "chg_pct": _fmt_pct(_pct(brent_last, brent_prev)) if brent_last and brent_prev else "—"},
        {"name": "Gold (Comex)", "price": f"${round(gold_last,0):,.0f}/oz" if gold_last  else "—",
         "chg_pct": _fmt_pct(_pct(gold_last, gold_prev)) if gold_last  and gold_prev  else "—"},
        {"name": "MCX Gold (10g)", "price": f"₹{mcx_gold_val:,}" if mcx_gold_val else "—",
         "chg_pct": _fmt_pct(mcx_gold_chg) if mcx_gold_chg is not None else _fmt_pct(_pct(gold_last, gold_prev))},
        {"name": "MCX Silver (kg)", "price": f"₹{mcx_silv_val:,}" if mcx_silv_val else "—",
         "chg_pct": _fmt_pct(mcx_silv_chg) if mcx_silv_chg is not None else _fmt_pct(_pct(silv_last, silv_prev))},
        {"name": "Natural Gas",  "price": f"${round(ng_last,2)}/MMBtu"  if ng_last    else "—",
         "chg_pct": _fmt_pct(_pct(ng_last, ng_prev))     if ng_last    and ng_prev    else "—"},
    ]

    currencies = [
        {"pair": "USD / INR", "rate": f"{usdinr_last:.2f}"  if usdinr_last else "—",
         "chg_pct": _fmt_pct(_pct(usdinr_last, usdinr_prev)) if usdinr_last and usdinr_prev else "—"},
        {"pair": "EUR / INR", "rate": f"{eurinr_last:.2f}"  if eurinr_last else "—",
         "chg_pct": _fmt_pct(_pct(eurinr_last, eurinr_prev)) if eurinr_last and eurinr_prev else "—"},
        {"pair": "GBP / INR", "rate": f"{gbpinr_last:.2f}"  if gbpinr_last else "—",
         "chg_pct": _fmt_pct(_pct(gbpinr_last, gbpinr_prev)) if gbpinr_last and gbpinr_prev else "—"},
        {"pair": "EUR / USD", "rate": f"{eurusd_last:.4f}"  if eurusd_last else "—",
         "chg_pct": _fmt_pct(_pct(eurusd_last, eurusd_prev)) if eurusd_last and eurusd_prev else "—"},
        {"pair": "DXY (Dollar)", "rate": f"{dxy_last:.2f}"  if dxy_last   else "—",
         "chg_pct": _fmt_pct(_pct(dxy_last, dxy_prev))     if dxy_last   and dxy_prev    else "—"},
        {"pair": "USD / JPY", "rate": f"{usdjpy_last:.2f}"  if usdjpy_last else "—",
         "chg_pct": _fmt_pct(_pct(usdjpy_last, usdjpy_prev)) if usdjpy_last and usdjpy_prev else "—"},
    ]

    # ── 6. Crypto + Crypto F&G ────────────────────────────────────
    print("[6/9] Crypto...")
    crypto = _safe(_fetch_crypto, [])
    try:
        crypto_fg_score, crypto_fg_label = _fetch_crypto_fg()
    except Exception as e:
        print(f"  ⚠ Crypto F&G: {e}")
        crypto_fg_score, crypto_fg_label = "—", "—"

    # ── 7. Fear & Greed ───────────────────────────────────────────
    print("[7/9] Fear & Greed...")
    try:
        fg_score, fg_label = _fetch_fear_greed()
    except Exception as e:
        print(f"  ⚠ Fear&Greed: {e}")
        fg_score, fg_label = "—", "—"

    # ── 8. FII/DII + Option Chains ────────────────────────────────
    print("[8/9] NSE India (FII/DII + option chains via bhav copy)...")
    nse_sess = None
    try:
        nse_sess = _nse_session()
    except Exception as e:
        print(f"  ⚠ NSE session: {e}")

    fii_dii = _fetch_fii_dii(nse_sess)

    # Download bhav copy once, reuse for both symbols
    from datetime import date as _date_cls, timedelta as _td_cls
    _bhav_rows = None
    for _offset in range(1, 6):
        _d = _date_cls.today() - _td_cls(days=_offset)
        if _d.weekday() >= 5:
            continue
        _bhav_rows = _fetch_bhav_copy(_d)
        if _bhav_rows:
            print(f"  ✅ Bhav copy loaded ({_d}) — {len(_bhav_rows):,} rows")
            break

    bn_chain  = _fetch_option_chain("BANKNIFTY", _bhav_rows)
    nf_chain  = _fetch_option_chain("NIFTY",     _bhav_rows)

    # FII cash net in Cr (for signal)
    fii_net_cr = "—"
    for row in fii_dii:
        cat = str(row.get("category", "")).lower()
        if "fii" in cat and "fo" not in cat and "f&o" not in cat:
            try:
                fii_net_cr = float(str(row["net"]).replace(",", ""))
            except:
                pass
            break

    # ── 9. News + BN/Nifty analysis ───────────────────────────────
    print("[9/9] News + index deep-dive...")
    news_items    = _safe(_fetch_news, [])
    bn_analysis   = _fetch_bn_analysis(usdinr)
    nifty_analysis = _fetch_nifty_analysis()

    # GIFT Nifty — REAL value scraped from sgxnifty.org (GIFT City trades
    # overnight). Replaces the old mislabelled ^NSEI proxy. Falls back to
    # ^NSEI last close only if the scrape fails.
    gift_nifty_pts = "—"
    gift_nifty_asof = ""
    try:
        from data.gift_nifty import get_gift_nifty
        _gn = get_gift_nifty()
        if _gn:
            gift_nifty_pts  = _gn['value']
            gift_nifty_asof = _gn['asof']
    except Exception:
        pass
    if gift_nifty_pts == "—":
        # Fallback — old ^NSEI proxy (clearly worse, but better than nothing)
        try:
            gift_hist = yf.Ticker("^NSEI").history(period="2d", interval="60m")
            if not gift_hist.empty:
                gift_nifty_pts = round(float(gift_hist["Close"].iloc[-1]), 2)
        except Exception:
            gift_nifty_pts = nifty_analysis.get("prev_close", "—")

    # BN gap estimate: GIFT % × BN prev close
    bn_prev = bn_analysis.get("prev_close", None)
    nifty_prev = nifty_analysis.get("prev_close", None)
    gap_pts = "—"
    if gift_nifty_pts != "—" and nifty_prev and nifty_prev > 0 and bn_prev:
        gift_pct = (gift_nifty_pts - nifty_prev) / nifty_prev
        gap_pts  = round(gift_pct * bn_prev, 0)
    bn_analysis["gap_pts"]     = gap_pts
    bn_analysis["gift_nifty"]  = gift_nifty_pts
    bn_analysis["option_chain"] = bn_chain

    nifty_analysis["option_chain"] = nf_chain

    # ── Signal ────────────────────────────────────────────────────
    hawala_signal = _compute_signal(
        vix=vix_val,
        sp_chg_pct=sp_chg_pct,
        fii_net_cr=fii_net_cr,
        gap_pts=gap_pts,
        weekday=weekday,
        bn_close=bn_prev,
    )

    # ── Scenario text + events ────────────────────────────────────
    scenario_text    = _generate_scenario_text(
        hawala_signal, bn_analysis, nifty_analysis,
        vix_val, sp_chg_pct, commodities_spot,
    )
    events_calendar  = _build_events_calendar(hawala_signal, bn_analysis)

    # Get previous trading day label
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    prev_wd = (weekday - 1) % 7
    prev_day_label = dow_names[prev_wd]

    print("\n✅  Data fetch complete.")
    print(f"    BN prev close: {bn_prev}  |  Gap est: {gap_pts} pts")
    print(f"    India VIX: {vix_val}  |  S&P chg: {sp_chg_pct}%")
    print(f"    Signal: {hawala_signal['overall']}")
    print("=" * 60)

    return {
        "date_str":          date_str,
        "date_iso":          date_iso,
        "generated_at":      generated_at,
        "weekday":           weekday,
        "prev_day_label":    prev_day_label,
        "fear_greed_val":    fg_score,
        "fear_greed_label":  fg_label if fg_score != "—" else _fg_label(fg_score),
        "crypto_fg_score":   crypto_fg_score,
        "crypto_fg_label":   crypto_fg_label,
        "gift_nifty":        gift_nifty_pts,
        "gift_nifty_asof":   gift_nifty_asof,
        "india_markets":     india_markets,
        "india_vix":         india_vix_row,
        "us_markets":        us_markets,
        "asian_markets":     asian_markets,
        "europe_markets":    europe_markets,
        "commodities_spot":  commodities_spot,
        # mcx_futures: subset of commodities_spot for the MCX-specific section in the report
        "mcx_futures":       [c for c in commodities_spot if c["name"].startswith("MCX")],
        "crypto":            crypto,
        "currencies":        currencies,
        "fii_dii":           fii_dii,
        "news_items":        news_items,
        "banknifty_analysis": bn_analysis,
        "nifty_analysis":    nifty_analysis,
        "hawala_signal":     hawala_signal,
        "scenario_text":     scenario_text,
        "events_calendar":   events_calendar,
    }


if __name__ == "__main__":
    import json
    data = fetch_all()
    print(json.dumps(data, indent=2, default=str))
