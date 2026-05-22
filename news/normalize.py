"""Headline normalization for matching, dedup, and event-key building."""
from __future__ import annotations

import hashlib
import re


_STOPWORDS = frozenset("""
a an the and or but if then else when at by for from in into of off on onto out over to up
with without via amid as is are was were be been being has have had do does did
this that these those it its their there here he she him her his hers they them
i we us our you your my mine ours yours
new old say says said reports report reportedly per according amid
will would could should may might can must shall
not no nor only also just very more most less least many much few several
today tomorrow yesterday morning evening night week month year
hits hit hitting near record high low high lows highs ahead eyes seen seeing eye
rise rises rose fall falls fell drop drops dropped jump jumps jumped
gain gains gained surge surges surged tumble tumbles tumbled climb climbs climbed
slip slips slipped slide slides slid slump slumps slumped soar soars soared
plunge plunges plunged crash crashes crashed sharp sharply
above below across over under amid mid late early
exclusive breaking analysis opinion comment column update updates news live
percent points pts bps stock stocks share shares market markets index indices
year-on-year quarter quarterly monthly weekly daily session intraday
report says people sources told insider — - msn bloomberg reuters axios cnbc bbc wsj ft
""".split())


_PUNCT_RE  = re.compile(r"[^\w\s]+")
_WS_RE     = re.compile(r"\s+")
_TOKEN_RE  = re.compile(r"[a-z0-9$%]+")


# ── Anchor vocabulary (May 2026 cleanup) ─────────────────────────────────────
# An event_key MUST contain at least one of these tokens to be considered
# well-formed. Headlines that produce fragment-only keys like
# `cards_doesn_iran_learns_trump` (40% of historic alerted keys) are usually
# noise; this gate rejects them at the source.
#
# Vocabulary sources: keywords.yml triggers + entities + tier-1 macro proper nouns.
ANCHOR_TOKENS = frozenset({
    # monetary policy
    'fed','fomc','powell','rbi','mpc','ecb','boj','boe','rate','rates',
    'cpi','inflation','repo',
    # geopolitical actors / themes
    'iran','israel','russia','ukraine','china','korea','hormuz','gaza',
    'taiwan','war','strike','strikes','sanctions','tariff','tariffs',
    'ceasefire','peace','nuclear','missile','trump','biden','putin','xi','modi',
    # commodities
    'oil','crude','brent','wti','opec','gas','gold',
    # india macro
    'india','nifty','sensex','banknifty','rupee','fii','dii','monsoon',
    'sebi','gst','gdp','rating','moody','fitch',
    'nse','bse','exchange','circular','probe','fine','penalty',
    'agm','qip','buyback','dividend','results','earnings',
    # crisis / markets
    'default','bankrupt','crisis','collapse','crash','rout',
    'sp500','nasdaq','dow','yen','dollar','treasury','vix',
    # corporate megacap (matches keywords.yml entities)
    'reliance','hdfc','icici','sbi','infosys','tcs','bharti','kotak',
    'axis','larsen','itc','bajaj','maruti',
})

# Sentinel returned by event_key() when no anchor is present.
NO_ANCHOR = "_NO_ANCHOR_"


def is_valid_event_key(event_key: str) -> bool:
    """Reject event_keys with no anchor token (= probably a fragment)."""
    if not event_key or event_key == NO_ANCHOR:
        return False
    return bool(set(event_key.split('_')) & ANCHOR_TOKENS)


# Compound-token pre-normalization — protect well-known indices/tickers
# from being shredded by the punctuation-stripping regex below.
_COMPOUND_FIXUPS = [
    (re.compile(r"\bs\s*&\s*p\s*500\b",  re.I), "sp500"),
    (re.compile(r"\bs\s*&\s*p\b",        re.I), "sp500"),
    (re.compile(r"\bdow\s+jones\b",      re.I), "dow"),
    (re.compile(r"\bl\s*&\s*t\b",        re.I), "larsen"),
    (re.compile(r"\bm\s*&\s*a\b",        re.I), "mergers"),
]


def normalize(text: str) -> str:
    """Lowercase, strip punctuation (except $ % which carry meaning), collapse whitespace."""
    if not text:
        return ""
    s = text.lower()
    for pat, rep in _COMPOUND_FIXUPS:
        s = pat.sub(rep, s)
    # Keep $ and % since they signal price levels and figures
    s = re.sub(r"[^\w\s$%]+", " ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def tokens(text: str) -> list[str]:
    """Tokenize a normalized string into a list of tokens (preserves order, keeps $ %).
    Routes through normalize() first so compound tokens (S&P → sp500, L&T → larsen) survive."""
    if not text:
        return []
    return _TOKEN_RE.findall(normalize(text))


def content_tokens(text: str) -> list[str]:
    """Return tokens with stopwords removed, length>=3 (or numeric/$%)."""
    out = []
    for t in tokens(text):
        if t in _STOPWORDS:
            continue
        if len(t) < 3 and not (t.startswith("$") or t.endswith("%") or t.isdigit()):
            continue
        out.append(t)
    return out


def headline_hash(text: str) -> str:
    """SHA-1 of normalized headline — for Layer-1 exact-dedup."""
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


def event_key(text: str, k: int = 5) -> str:
    """Build a coarse event-key from the top-K most distinctive content tokens.

    Strategy: take content tokens, dedupe preserving first-seen order, take up
    to K, sort alphabetically, join with `_`. Two headlines about the same
    event tend to share the same set of distinctive nouns/verbs.
    """
    seen, out = set(), []
    for t in content_tokens(text):
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= k * 2:  # gather a bit more, in case top tokens are very generic
            break
    if not out:
        return ""
    # Drop the most generic — markets, says, today — already handled by stopwords;
    # for now, just take first K (they're in headline order, which weighs the lead).
    chosen = sorted(out[:k])
    key = "_".join(chosen)
    # Anchor gate — fragment-only keys are routed to digest, not live alerts.
    if not is_valid_event_key(key):
        return NO_ANCHOR
    return key


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two token sets (0..1)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def token_set(text: str) -> set[str]:
    """Content-token set for Jaccard comparison."""
    return set(content_tokens(text))


def near_pair(text: str, a: str, b: str, window: int = 6) -> bool:
    """True if tokens `a` and `b` both appear within `window` tokens of each other."""
    toks = tokens(text)
    a_pos = [i for i, t in enumerate(toks) if t == a]
    b_pos = [i for i, t in enumerate(toks) if t == b]
    if not a_pos or not b_pos:
        return False
    for i in a_pos:
        for j in b_pos:
            if abs(i - j) <= window:
                return True
    return False
