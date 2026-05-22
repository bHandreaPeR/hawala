"""Curated news feeds with source-tier weighting.

Tier weights influence per-headline confidence and final score.
- Tier-1 (1.0): wire services + agenda-setters
- Tier-2 (0.7): mainstream business press
- Tier-3 (0.4): topical Google News queries (broad coverage, more noise)
- Direct  (1.0): primary government / central-bank pages
"""
from __future__ import annotations


FEEDS: list[dict] = [
    # ── Tier-1 — agenda-setters ───────────────────────────────────────────────
    {"name": "Axios",       "tier": 1.0, "type": "rss",
     "url":  "https://api.axios.com/feed/"},
    {"name": "Reuters",     "tier": 1.0, "type": "rss",
     "url":  "https://news.google.com/rss/search?q=when:2h+reuters+markets+OR+economy+OR+geopolitical&hl=en&gl=US&ceid=US:en"},
    {"name": "Bloomberg",   "tier": 1.0, "type": "rss",
     "url":  "https://news.google.com/rss/search?q=when:2h+bloomberg+markets+OR+fed+OR+economy&hl=en&gl=US&ceid=US:en"},
    {"name": "FT",          "tier": 1.0, "type": "rss",
     "url":  "https://news.google.com/rss/search?q=when:2h+%22financial+times%22+markets&hl=en&gl=US&ceid=US:en"},
    {"name": "WSJ",         "tier": 1.0, "type": "rss",
     "url":  "https://news.google.com/rss/search?q=when:2h+%22wall+street+journal%22+markets+OR+fed&hl=en&gl=US&ceid=US:en"},
    {"name": "Moneycontrol", "tier": 1.0, "type": "rss",
     "url":  "https://www.moneycontrol.com/rss/latestnews.xml"},

    # ── Tier-2 — mainstream business press ────────────────────────────────────
    {"name": "CNBC",            "tier": 0.7, "type": "rss",
     "url":  "https://www.cnbc.com/id/10000664/device/rss/rss.html"},
    {"name": "BBC Business",    "tier": 0.7, "type": "rss",
     "url":  "https://feeds.bbci.co.uk/news/business/rss.xml"},
    {"name": "NY Times",        "tier": 0.7, "type": "rss",
     "url":  "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"},
    {"name": "Economic Times",  "tier": 0.7, "type": "rss",
     "url":  "https://economictimes.indiatimes.com/rssfeedstopstories.cms"},
    {"name": "Livemint",        "tier": 0.7, "type": "rss",
     "url":  "https://www.livemint.com/rss/markets"},
    {"name": "NDTV Profit",     "tier": 0.7, "type": "rss",
     "url":  "https://www.ndtvprofit.com/feed"},

    # ── Tier-3 (domain-filtered) — Google News restricted to TRUSTED outlets ─
    # The previous broad queries (`q=when:1h+iran+OR+israel...`) surfaced
    # whatever ranked in Google News — including partisan blogs and clickbait.
    # Replaced with `site:` filters that only return items from editorially
    # accountable wire services / broadsheets. Each query is scoped to the
    # last 60 min for freshness.
    {"name": "GN: rates",       "tier": 0.5, "type": "rss",
     "url":  "https://news.google.com/rss/search?q=when:1h+(site:reuters.com+OR+site:bloomberg.com+OR+site:ft.com+OR+site:wsj.com+OR+site:cnbc.com+OR+site:axios.com)+(federal+reserve+OR+rbi+OR+rate+cut+OR+rate+hike)&hl=en&gl=US&ceid=US:en"},
    {"name": "GN: geopolitics", "tier": 0.5, "type": "rss",
     "url":  "https://news.google.com/rss/search?q=when:1h+(site:reuters.com+OR+site:bloomberg.com+OR+site:ft.com+OR+site:wsj.com+OR+site:apnews.com+OR+site:bbc.com)+(iran+OR+israel+OR+russia+ukraine+OR+china+taiwan+OR+ceasefire)&hl=en&gl=US&ceid=US:en"},
    {"name": "GN: oil",         "tier": 0.5, "type": "rss",
     "url":  "https://news.google.com/rss/search?q=when:1h+(site:reuters.com+OR+site:bloomberg.com+OR+site:ft.com+OR+site:wsj.com+OR+site:cnbc.com)+(oil+OR+crude+OR+opec+OR+brent)&hl=en&gl=US&ceid=US:en"},
    {"name": "GN: india",       "tier": 0.5, "type": "rss",
     "url":  "https://news.google.com/rss/search?q=when:1h+(site:reuters.com+OR+site:bloomberg.com+OR+site:moneycontrol.com+OR+site:economictimes.indiatimes.com+OR+site:livemint.com+OR+site:thehindubusinessline.com)+(nifty+OR+sensex+OR+rbi+OR+sebi)&hl=en-IN&gl=IN&ceid=IN:en"},

    # ── Direct — primary sources ──────────────────────────────────────────────
    {"name": "RBI",  "tier": 1.0, "type": "html",
     "url":  "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"},
    {"name": "Fed press",   "tier": 1.0, "type": "rss",
     "url":  "https://www.federalreserve.gov/feeds/press_all.xml"},

    # ── Fast wire-style (geopolitics + macro) ────────────────────────────────
    {"name": "ForexLive",   "tier": 1.0, "type": "rss",
     "url":  "https://www.forexlive.com/feed/"},
    {"name": "Investing news", "tier": 0.7, "type": "rss",
     "url":  "https://www.investing.com/rss/news.rss"},
    {"name": "Investing econ", "tier": 0.7, "type": "rss",
     "url":  "https://www.investing.com/rss/news_25.rss"},

    # ── Geopolitical fast feeds ──────────────────────────────────────────────
    {"name": "BBC World",   "tier": 0.7, "type": "rss",
     "url":  "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Al Jazeera",  "tier": 0.7, "type": "rss",
     "url":  "https://www.aljazeera.com/xml/rss/all.xml"},

    # ── India fast — extra ───────────────────────────────────────────────────
    {"name": "ET Markets",  "tier": 0.7, "type": "rss",
     "url":  "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
    {"name": "Mint money",  "tier": 0.7, "type": "rss",
     "url":  "https://www.livemint.com/rss/money"},

    # ── REMOVED (May 2026 trust cleanup) ─────────────────────────────────────
    # The following feeds were removed because they routinely surfaced
    # partisan / low-trust / clickbait content:
    #   - Truth Social (trumpstruth.org) — Trump posts. Frequently contain
    #     misinformation; market-moving content gets re-reported by Reuters/
    #     Bloomberg within minutes anyway. Removed entirely.
    #   - GN: breaking-30m — `q=breaking OR exclusive` surfaced random tabloid
    #     and aggregator content. Replaced by the domain-filtered tier-1
    #     fast-paths above (GN: bloomberg-30m, GN: reuters-30m).
    #   - GN: india-30m — same problem, India-side. The Indian primary feeds
    #     (Moneycontrol, ET, Mint, Hindu BL, Zerodha Pulse) cover this window
    #     directly without the GN noise floor.

    # ── X/Twitter via Nitter.net — financial wire accounts ──────────────────
    # Note: nitter mirrors are unreliable. We poll one mirror; if it dies the
    # healthcheck makes it visible and we can swap to another instance.
    {"name": "X: DeItaone",      "tier": 0.7, "type": "rss",
     "url":  "https://nitter.net/DeItaone/rss"},
    {"name": "X: FirstSquawk",   "tier": 0.7, "type": "rss",
     "url":  "https://nitter.net/FirstSquawk/rss"},

    # ── Wire-style market commentary (fast forex/macro) ─────────────────────
    {"name": "FXStreet",         "tier": 0.7, "type": "rss",
     "url":  "https://www.fxstreet.com/rss/news"},
    {"name": "MarketWatch top",  "tier": 0.7, "type": "rss",
     "url":  "https://feeds.marketwatch.com/marketwatch/topstories/"},
    {"name": "Yahoo Finance",    "tier": 0.7, "type": "rss",
     "url":  "https://finance.yahoo.com/news/rssindex"},

    # ── NSE corporate filings (JSON API) ────────────────────────────────────
    # Catches results, board changes, regulatory action, AGM notices, etc.
    # before they hit press. Special parser in scraper.py (type="nse_json").
    {"name": "NSE filings",      "tier": 1.0, "type": "nse_json",
     "url":  "https://www.nseindia.com/api/corporate-announcements?index=equities"},

    # ── Bloomberg / Reuters domain-filtered Google News (last 30m) ──────────
    {"name": "GN: bloomberg-30m", "tier": 1.0, "type": "rss",
     "url":  "https://news.google.com/rss/search?q=when:30m+site:bloomberg.com&hl=en&gl=US&ceid=US:en"},
    {"name": "GN: reuters-30m",   "tier": 1.0, "type": "rss",
     "url":  "https://news.google.com/rss/search?q=when:30m+site:reuters.com&hl=en&gl=US&ceid=US:en"},

    # ── Indian financial aggregator ─────────────────────────────────────────
    # Zerodha Pulse aggregates trusted Indian financial sources with low noise.
    # Tier-1 — curated, fast, and India-focused. Adds ~25 fresh items per pull.
    {"name": "Zerodha Pulse",    "tier": 1.0, "type": "rss",
     "url":  "https://pulse.zerodha.com/feed.php"},

    # ── BSE corporate announcements (primary) ───────────────────────────────
    # Parallel to NSE filings — catches BSE-listed companies whose filings
    # don't always appear on the NSE feed (different listings, dual-listings).
    {"name": "BSE filings",      "tier": 1.0, "type": "rss",
     "url":  "https://www.bseindia.com/data/xml/notices.xml"},

    # ── SEBI press releases (regulatory actions) ────────────────────────────
    # SEBI fines, probe announcements, and policy circulars routinely move
    # individual stocks 3–10% on the day. Primary source — Tier-1.
    {"name": "SEBI press",       "tier": 1.0, "type": "rss",
     "url":  "https://www.sebi.gov.in/sebirss.xml"},

    # ── Indian business TV / quality press ──────────────────────────────────
    {"name": "CNBC TV18",        "tier": 0.8, "type": "rss",
     "url":  "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"},
    {"name": "Hindu BusinessLine", "tier": 0.8, "type": "rss",
     "url":  "https://www.thehindubusinessline.com/markets/feeder/default.rss"},
    {"name": "BusinessLine econ", "tier": 0.8, "type": "rss",
     "url":  "https://www.thehindubusinessline.com/economy/feeder/default.rss"},

    # ── Exchange + regulator X/Twitter handles (official) ───────────────────
    # Via Nitter mirror — fragile but high signal when working. Official
    # accounts tweet AGM dates, trading-halt notices, regulatory circulars
    # before they hit RSS. Tier-1 because direct from source.
    {"name": "X: NSEIndia",      "tier": 1.0, "type": "rss",
     "url":  "https://nitter.net/NSEIndia/rss"},
    {"name": "X: BSEIndia",      "tier": 1.0, "type": "rss",
     "url":  "https://nitter.net/BSEIndia/rss"},
    {"name": "X: RBI",           "tier": 1.0, "type": "rss",
     "url":  "https://nitter.net/RBI/rss"},

    # ── Fast wire-style X/Twitter handles ──────────────────────────────────
    # Indian + global breaking-news handles. Tier-0.7 — fast but more noise
    # than primary wires.
    {"name": "X: CNBCTV18Live",  "tier": 0.7, "type": "rss",
     "url":  "https://nitter.net/CNBCTV18Live/rss"},
    {"name": "X: LiveSquawk",    "tier": 0.7, "type": "rss",
     "url":  "https://nitter.net/LiveSquawk/rss"},
]
