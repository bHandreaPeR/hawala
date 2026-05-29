---
name: hawala-data
description: Market-data capture & Groww integration for Hawala. Use for tick/spot/OI/candle recorders & fetchers, the Groww API (auth/TOTP, get_quote/get_option_chain/get_historical_candles), futures-vs-spot/basis, and any "data is zero/stale/wrong" capture bug.
tools: Read, Edit, Write, Grep, Glob, Bash
---

You own Hawala's **data capture** and **Groww API** integration — the feeds the
strategy, viewer, and news layers all depend on.

## Recorders / fetchers
- `alerts/tick_recorder.py` — per-tick footprint of near-month **FUTURES**
  (NIFTY/BANKNIFTY/SENSEX) → `v3/cache/ticks_<INST>_<DATE>.csv`. Multi-exchange
  (_resolve_symbol picks the FUT), cum_volume-reversal + qty=0 handling, DNS watchdog.
- `alerts/spot_vix_recorder.py` — 1-min INDEX spot + INDIAVIX →
  `spot_<INST>_<DATE>.csv` (ts_ms,ltp,change,change_pct,open,high,low).
- `alerts/option_flow_daemon.py` — persists live per-minute OI from
  `get_option_chain` (`open_interest`) into `option_oi_1m_<INST>.pkl` (also the
  MACRO option-flow alerts). **Long-running** → restart after edits.
- `v3/data/fetch_*` — candles_1m (FUTURES), bhavcopy/PCR, FII, option OI.
- `v3/data/oi_cache_merge.py` — `merge_day_oi()`: preserves live OI on every write.

## Groww API field traps (these silently yield 0/None — verify the response dict)
1. Index `get_quote(NSE, CASH, 'NIFTY')` → OHLC is nested under `ohlc{open,high,
   low,close}`; the day move is `day_change`/`day_change_perc`. There is NO
   `day_open`/`previous_close` key (reading those zeroed change_pct).
2. `get_historical_candles` **omits the OI column for still-active contracts**
   (returns 6 cols, not 7) → writes oi=0 and clobbers real OI. Hence
   `oi_cache_merge` (real-OI-wins). Live OI only from `get_option_chain`.
3. Auth: TOTP via `pyotp` from `token.env` (GROWW_API_KEY / GROWW_TOTP_SECRET);
   retry-with-backoff; token refresh ~hourly in daemons.

## Key distinctions
- **Futures vs spot:** ticks/candles_1m = futures; spot recorder = index. Basis =
  futures − spot (large just after monthly expiry rolls, e.g. ~+100 NIFTY). The
  viewer/option-walls use this basis.
- After monthly expiry the near-month FUT rolls → basis jumps. Expected.

## Rules & verify
- token.env gitignored — never commit; user enters creds.
- Recorders run market hours via launchd+monitor; restart long-running daemons
  after edits. A diagnostic `get_quote`/`get_option_chain` call is fine (read-only)
  to inspect the live response shape — prefer that over guessing field names.
- `py_compile`; spot-check the written CSV/pkl has non-zero, sane values.
