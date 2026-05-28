# Hawala — Pre-Trade Checklist

*The single source of truth for whether a trade happens. Read it every time.
Written after the 2026-05-27 −₹42k day. Its whole job is to make that day
impossible to repeat.*

---

## The one rule that contains all the others

> **A trade may ONLY originate from the system (v3 runner or VP-Trail).
> The viewer never originates a trade. My eyes never originate a trade.**

If the system did not fire, there is no trade. Full stop. The chart looking
"ready to break", a level being "tested twice", a feeling — none of these are
signals. They are how the ₹42k disappeared.

---

## GATE 0 — Is there even a signal? (most days end here)

- [ ] Did a **v3 runner** (NIFTY / BANKNIFTY) print a `[PAPER] ENTER` this session?
- [ ] OR did **VP-Trail** print a new entry?

**If NO to both → NO TRADE. Close the terminal.** This is the correct outcome
on most days, especially low-volatility ones. The system being silent *is* the
instruction: sit out. Waiting is the job.

*(The morning brief tells you this each day. If it says "nothing fired," you are
done — do not go looking on the chart for something to do.)*

---

## GATE 1 — Hard NEVER rules (any one fails → NO TRADE)

- [ ] **NOT** a naked option bought on its **expiry day** to hold > 10 min.
      (Theta destroys it regardless of direction. This was the 11am trade.)
- [ ] **NOT** an instrument the system doesn't cover with data
      (don't trade SENSEX discretionarily; if v3/VP-Trail didn't signal it, skip).
- [ ] **NOT** within **30 minutes** of a loss today (no revenge / tilt window).
- [ ] **NOT** a reversal of a thesis I held < 30 min ago
      (if I'm flipping long↔short fast, I have no thesis — I have emotion).
- [ ] **NOT** sized by "what others made" or a target P&L. Size by the rules below only.
- [ ] Order will be a **LIMIT** order, not MARKET (no slippage-bleeding on illiquid strikes).

---

## GATE 2 — The 4 numbers (must be writable in 30 seconds, BEFORE entry)

If you cannot fill these in, you do not understand the trade → NO TRADE.

1. **Entry trigger:** the system signal + price. _________________
2. **Profit target:** _________________
3. **Invalidation (stop):** price/level where the thesis is WRONG. _________________
4. **Time stop:** if neither hit by ____ , exit anyway. _________________

A stop is not optional. The 76100 CE went −56% with no stop because there were
no numbers. Numbers first, or no trade.

---

## GATE 3 — Viewer validation (can only VETO or shrink, never create)

Open the viewer in **Clean mode**. The system already proposed the trade; now
check whether the context argues against it:

- [ ] **Level check:** Is the entry running straight into a fat **HVN / value
      edge / pivot-confluence** that will block it? (resistance for a long,
      support for a short) → reduces conviction or veto.
- [ ] **Vacuum check:** Is the *stop* on the far side of an **LVN gap** that
      price could slice through fast? → widen mental risk or skip.
- [ ] **Positioning check:** Does the **composite** (FLOW / RESTING / INST /
      MACRO) actively *contradict* the trade direction? → veto.
      *(Note: positioning is still unvalidated — use it only to veto, never to
      confirm. A green composite is not a reason to trade; a red one against you
      is a reason to pass.)*
- [ ] **Absorption against you:** Is there 🅐 absorption at the level showing
      the other side defending? → veto a breakout-style entry.

Validation passes = the context does not scream "no." It does **not** mean
"good trade" — it means "the system's trade isn't obviously walking into a wall."

---

## GATE 4 — Sizing & exposure (until 20 profitable trades logged)

- [ ] **Max 1 lot** per trade. No exceptions for "this one's great."
- [ ] **Max 2 trades** per day. After the 2nd entry, terminal closes.
- [ ] **Max 5% of remaining capital** at risk per trade (~₹2,900 on ₹58k).
- [ ] Daily loss limit: **stop for the day after −₹3,000** realized.

After 20 profitable trades following every rule, revisit sizing — not before.

---

## What the experimental tools are FOR (and are NOT)

- **▲▼ entry markers, positioning composite, absorption/reversal badges:**
  unvalidated heuristics. **Study them. Do not trade them.** They become inputs
  only after the leading-direction / footprint regression proves they precede
  favorable outcomes (≈3 weeks of data). Treating a scored ▲ as a signal is the
  same error as the 11am chart-read.
- **Volume profile, pivots, footprint, DOM:** context for GATE 3 validation.
  They explain *why* a system trade is good or bad. They do not *make* trades.

---

## The mantra

**System fires → 4 numbers → viewer doesn't veto → 1 lot → done.**
**No signal → no trade → close the terminal.**

Most days are "close the terminal." That is success, not boredom.
