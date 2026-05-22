# Hawala Live Footprint — Reading & Entry Guide

*Last updated: May 21, 2026 · For NIFTY / BANKNIFTY futures · ₹1L capital base*

---

## TL;DR — Where to enter

**Footprint NEVER originates a trade.** Your existing v3 / ORB / VWAP / VP-Trail
signal is the trade. Footprint and DOM are a **confirm-or-veto** layer.

The 3-question pre-entry checklist:

1. **Strategy signal**: does v3 / ORB / VP-Trail say go? (If no → don't trade)
2. **Footprint context**: is the recent cell flow + CVD aligned with your direction?
3. **DOM permission**: is there NO fat opposing wall within 2-3 ticks of your entry?

All three yes → take it. Any "no" → wait or skip.

**Where the order goes:**
- LONG: enter at-or-above the inside ask (top of `sellBook` in DOM)
- SHORT: enter at-or-below the inside bid (top of `buyBook` in DOM)
- Visible wall between you and target ⇒ expect it to slow you down; size accordingly.

---

## Part 1 — Anatomy of the viewer

```
┌─────────────────────────────────────────────────┬─────┬─────┐
│                                                 │     │     │
│           MAIN FOOTPRINT                        │ ses │ live│
│           (candles with two-column cells)       │ vol │ DOM │
│                                                 │ prof│     │
│                                                 │     │     │
├─────────────────────────────────────────────────┴─────┴─────┤
│           DELTA HISTOGRAM (per-candle buy − sell)           │
└─────────────────────────────────────────────────────────────┘
```

| Pane | What it is | Updates |
|---|---|---|
| Main footprint | Per-candle, per-price cells (sell │ buy) | 5 Hz on tick |
| Session vol profile | Sum of all volume per price level today | 5 Hz on tick |
| Live DOM | Top-5 resting bids (green right) / asks (red left) | 1 Hz on snapshot |
| Delta histogram | Per-candle delta (qty) coloured by sign | 5 Hz on tick |

---

## Part 2 — Reading each pane

### 2.1 Main footprint cells

Each price level inside each candle has two halves:

```
  ┌──────┬──────┐
  │ SELL │ BUY  │   ← red on left, green on right
  │  237 │  29  │   ← qty (or tick-count if qty=0)
  └──────┴──────┘
```

- Colour intensity = that side's qty relative to the busiest cell in the same candle
- **Bold yellow ring + bold text** = imbalance (one side ≥ 3× other AND ≥ 4 ticks)
- **Yellow ring around a whole row** = that's the candle's POC (point of control)
- Faint green band across the candle = value area (70% of candle's volume)
- The wick + body of the candle is in the centre gutter between the columns

What each cell tells you in plain English:

| Cell example | Meaning |
|---|---|
| `237 × 29` (yellow ring on sell) | Sellers crossed the spread 8× harder than buyers here → strong supply |
| `0 × 387` (yellow ring on buy) | Nobody wanted to sell at this price; only buyers crossed → strong demand |
| `45 × 50` | Balanced — informational, no edge |
| `0 × 0` | Price touched the level but no trades happened in that direction |

### 2.2 Session volume profile (middle right pane)

Vertical histogram. For every price level the bot has seen today:
- Red bars to the left of axis = total sell volume executed at that price
- Green bars to the right = total buy volume executed at that price

This is your **"where the day parked its money"** view. Where the bars are
fattest = the day's "fair value zone". Where they're thinnest = inefficient
levels that price often revisits.

### 2.3 Live DOM (rightmost pane)

Updates every 1 second with the top-5 resting bid + ask:
- Green bars right of axis = resting **bids** (someone wants to BUY)
- Red bars left of axis = resting **asks** (someone wants to SELL)
- Bar width = qty (lots)
- Colour saturation = size relative to the biggest visible level
- Number outside the bar = qty

This is the only place you see **unexecuted intent**. Cancellations and
spoofing live here.

### 2.4 Delta histogram (bottom)

One bar per candle, height = buy_qty − sell_qty. Green up, red down.
Cumulative line of these = CVD (cumulative volume delta), which is the
"intent" track of the day:
- CVD rising + price rising → trend day, ride it
- CVD rising + price flat or falling → bears absorbing, watch for reversal
- CVD falling + price rising → bulls absorbing, watch for reversal
- CVD flat → balanced auction, fade extremes

---

## Part 3 — The 5 patterns that actually matter

### Pattern 1: Stacked imbalances + break (continuation signal)

```
price ↑
     [   sell  │  buy   ]
     [    23   │  142*  ]    ← imbalance
     [    15   │  238*  ]    ← imbalance
     [     8   │  189*  ]    ← imbalance     ← stacked!
     [   ...   │  ...   ]
                             ← if next candle breaks ABOVE this,
                               it's a strong continuation long
```

**What it means**: ≥3 same-side imbalances at adjacent prices = a wall of
aggressive flow chewed through resting opposite orders. The level becomes
support (if buy-stacked) or resistance (if sell-stacked).

**Entry**: enter on the break of the top of the stack +1 tick (LONG for buy-stack).
**Stop**: just below the bottom of the stack.
**Target**: 1× the stack's range from entry (measured move).

This is the single highest-quality footprint signal.

### Pattern 2: Absorption at level (reversal signal)

```
delta = +800 (huge)
candle body = 4 ticks (tiny)
candle visited the SAME price 5 times
```

**What it means**: Aggressive buyers (or sellers) dumped massive flow into a
level, but the price didn't move. Someone with deeper pockets sat on the
other side absorbing it. They usually win.

**Entry**: counter-trend after the absorption candle closes. Wait for a
small confirmation candle (Pattern 4) before pulling the trigger.
**Stop**: beyond the absorption candle's extreme.
**Target**: previous swing in the absorption direction.

Visible in the viewer as the **purple X marker** on the candle (when implemented; for now look manually for big delta + small body).

### Pattern 3: CVD divergence (reversal signal)

```
price:  H1 ─── H2 (higher high)
CVD  :  ↑       ↓ (lower or flat) ← divergence
```

**What it means**: Price made a new high but the cumulative delta did NOT.
Buyers are running out of fuel. Sellers are quietly absorbing.

**Entry**: short the next bearish candle after the divergent high. Mirror
for longs at lows.
**Stop**: above the divergent high.
**Target**: middle of the prior range / VWAP / value area.

Requires patience — divergences often persist for 2-5 candles before resolving.

### Pattern 4: POC migration (trend strength)

```
candle 1 POC = 23,640
candle 2 POC = 23,645   ↑
candle 3 POC = 23,650   ↑   POC is climbing → buyers in control
candle 4 POC = 23,655   ↑
```

**What it means**: The "fair value" of each candle keeps moving the same
direction. Strong trend.

**Use**: confirmation only. If your v3/ORB signal says LONG and POCs are
climbing → take it bigger. If POCs flat → take it smaller. If POCs
moving against your signal → SKIP.

### Pattern 5: DOM wall break (momentum signal)

```
DOM before:  ASK level 1 = 480 lots @ 23,650   ← huge wall
             …trade tape shows 480 lots traded at 23,650 in 2 seconds
DOM after :  ASK level 1 = 30 lots @ 23,650    ← wall consumed
             ↓
             price punches through to 23,655+
```

**What it means**: A large resting order got eaten. The buyer was patient
and aggressive enough to consume it. Continuation is highly likely.

**Entry**: market or aggressive limit on the break.
**Stop**: just back inside the broken level.
**Target**: next visible wall in the DOM, or 1× the breaker's range.

This is the **DOM's killer feature**.

---

## Part 4 — Patterns that are noise (don't trade)

| Pattern | Why it's noise |
|---|---|
| Single imbalanced cell (not stacked) | One outlier ≠ signal |
| Large CVD on its own | Direction without confirming price action = nothing |
| Small DOM levels (qty < 30) | Background liquidity churn |
| First 5 minutes of session | Opening auction noise, classifier accuracy drops |
| Last 10 minutes of session | Position-squaring distorts everything |
| Lunch hour (12:00–13:30) | Low volume, easy to misinterpret |
| Footprint patterns < 5 ticks from spot | Spread + slippage eats the edge |

---

## Part 5 — The decision framework

Step by step, every time you consider an entry:

### Step 1: Trigger
A strategy you trust (ORB, VWAP, VP-Trail, v3 signal score) generates a signal.
If no strategy says go → close the laptop. Footprint alone is not a signal.

### Step 2: Pre-entry checklist (15 seconds)

```
[ ] Is recent CVD direction aligned with my trade direction?
[ ] In the last 2-3 candles, are imbalances on my side outnumbering the other?
[ ] Is the candle I'm entering on showing positive (for long) / negative (for short) delta?
[ ] In the live DOM, is the inside qty on my side ≥ the opposing inside qty?
[ ] Is there NO opposing wall (large red for long / large green for short) within 3 ticks?
[ ] Are we OUTSIDE the noise windows (first 5 min / last 10 min / lunch)?
```

5 of 6 yes → take it.
4 of 6 yes → take it smaller (half size).
≤ 3 yes → skip.

### Step 3: Order placement

Use the DOM live values:
- LONG: limit order at the inside ask + 0 to 1 tick.
- SHORT: limit order at the inside bid − 0 to 1 tick.
- If the move is fast and you've waited → market order, but accept slippage.
- If you see a wall between you and your target → either size down or expect the wall to slow the move.

### Step 4: Stop placement

Always tie to a footprint structure, not a percentage:
- **Pattern-1 trade** (stacked break): stop = opposite extreme of the stack.
- **Pattern-2 trade** (absorption fade): stop = beyond absorption candle high/low.
- **Pattern-3 trade** (CVD divergence): stop = beyond the divergent extreme.
- **Pattern-4 trade** (POC trend): stop = previous candle's POC.
- **Pattern-5 trade** (wall break): stop = inside the broken level.

If your strategy's default stop is tighter, use the tighter one.

### Step 5: Exit
- Default target = 1× mother range or 1R, whichever your strategy says.
- Footprint over-rides: if CVD flips against you mid-trade → close half. If
  Pattern 2 (absorption) forms against you → close full.
- Auto exit at 15:25 IST (5 mins before close — don't get caught in the close auction).

---

## Part 6 — Honest caveats

- **Tick classification is ~87% accurate** (Lee-Ready algorithm). Don't bet the
  farm on a single cell — look at clusters of 3+ cells for any signal.
- **Spoofing is real.** Walls in the DOM can disappear right before they're
  hit. Use wall break (Pattern 5) only when the print actually consumed it,
  not just when the wall vanished.
- **Footprint is a confirmation, not a magic wand.** Your existing strategy
  edge is what makes money. Footprint refines entries by maybe +3–7% WR if
  used correctly.
- **NIFTY moves ~50 pts a typical session.** Don't try to scalp 5-tick
  footprint moves — slippage + brokerage eats the edge.
- **Lunch hour is treacherous.** Volume drops to ⅓ — classifier accuracy
  drops, imbalances become misleading. Avoid.
- **News kills footprint.** When a headline drops the order book empties for
  3-5 seconds. Don't trade off footprint during news; wait for the dust.

---

## Part 7 — Quick-reference cheat sheet

### Symbol legend

| Symbol | Meaning |
|---|---|
| Red box left | sell aggressors (lifted asks) |
| Green box right | buy aggressors (hit bids) |
| Bold yellow ring | imbalance ≥ 3× |
| Yellow row ring | POC of that candle |
| Pale green band | value area (70% volume) |
| Centre coloured bar | candle body (red/green by close) |
| Centre vertical line | candle wick (high-low) |
| Right DOM green bar | resting BID (size = bar width) |
| Right DOM red bar | resting ASK (size = bar width) |
| Bottom green bar | candle delta positive |
| Bottom red bar | candle delta negative |

### Pattern → action table

| If you see | Direction | What to do |
|---|---|---|
| Stacked imbalance ≥3 same-side adjacent | Same as imbalance | Enter on break with 1× target |
| Huge delta + tiny body | Opposite of delta | Fade after confirmation candle |
| Price high + lower CVD | Short | Wait for bearish confirm candle |
| POC migration steady | Same direction | Trend in progress — take strategy signals bigger |
| DOM wall consumed live | Same as break | Enter on break, stop inside level |
| Single imbalanced cell | — | Ignore, not enough evidence |
| First/last 10 min, lunch | — | Don't trade footprint here |
| News headline live | — | Wait 30-60s for book to refill |

### Trade-sizing knobs by footprint quality

| Footprint context | Size |
|---|---|
| 5–6 checklist yes | full size |
| 4 checklist yes | half size |
| ≤3 checklist yes | skip |
| Footprint disagrees with strategy | skip (footprint veto) |
| Footprint agrees + Pattern 1 or 5 present | full size + can trail wider |

### Stop = footprint structure, never a fixed %

| Pattern | Stop |
|---|---|
| Stacked break | Other end of the stack |
| Absorption fade | Beyond absorption candle extreme |
| CVD divergence | Beyond divergent swing high/low |
| POC trend | Previous POC |
| Wall break | Inside the broken level |

### What NOT to do (etched in memory)

1. Don't trade footprint without a strategy signal.
2. Don't trade single-cell imbalances.
3. Don't fade big moves just because delta is extreme.
4. Don't ignore stops — footprint can be wrong 30% of the time.
5. Don't trust DOM walls absolutely — spoofing.
6. Don't trade in noise windows (open, lunch, close).
7. Don't trade during live news.
8. Don't increase position size mid-trade because footprint "looks good".

---

## Appendix — One real example from today

Looking at the May 21, 2026 chart (NIFTY 5m):

- **12:55 candle**: huge negative delta (−150ish), sellers 227×21 stacked at top.
  POC at 23,580. **Signal: sellers active near 23,605, but absorption visible
  at 23,580.**
- **13:00 candle**: 142×9 stacked (buyers reload) at 23,590-23,595.
  **Signal: Pattern 1 — stacked buy imbalance.**
- **13:15 candle**: breaks above 23,605 with momentum. Pattern 5 fires.
  **If a strategy signal said LONG here → strong confirmation, full size.**
- **13:30 candle**: big positive delta +490 (the day's biggest), POC migration
  upward to 23,640. **Pattern 4 confirms the trend.**
- **14:00–14:20**: range, no clear pattern → no trade.
- **14:30 candle**: positive delta breakout to 23,665. **Pattern 5 again** —
  if you were already long, this is where you add OR trail your stop up.

Result: a long initiated at ~23,600 with 23,580 stop and 23,660 target would
have netted +60 pts. R:R was ~3:1. That's a footprint-confirmed strategy
trade.

---

## Where to go from here

- **Phase 3** of the footprint pipeline (next): shadow-log these features at
  every v3 entry for 2 weeks. Then we'll know which checklist items actually
  predict outcomes and can drop the noise.
- **Phase 4**: wire `footprint_veto.py` into v3 entries — auto-skip trades
  where the checklist fails. That's the goal.

Until then: use this manually. Watch the viewer alongside your existing
signals. Log the checklist mentally before each trade. Review weekly.

— *built for Hawala, ₹1L, NIFTY/BANKNIFTY futures*
