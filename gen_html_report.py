"""
gen_html_report.py — Hawala v2 Pre-Market HTML Report

Uses the exact same CSS and design as market_report_2026-04-13.html.
Populated entirely with live data from data/fetch_report_data.py.

Design changes vs template:
  - Snap cards: BankNifty · Nifty 50 · India VIX · S&P 500
  - Signal grid: only algo filters (VIX, S&P, FPI, Gap, DOW, Overall)
  - Expected Scenario box: computed from data
  - Key News + Events Calendar included
  - No trade params, no paper trade tracker

Usage:
    from gen_html_report import build_html
    html = build_html(data)
    with open("market_report_2026-04-20.html", "w") as f:
        f.write(html)
"""

import html as _html_mod


# ── Helpers ───────────────────────────────────────────────────────────────────

def _e(text):
    """HTML-escape a value."""
    return _html_mod.escape(str(text))


def _pct_class(val):
    """Return CSS class based on sign of numeric value."""
    try:
        v = float(str(val).replace("%", "").replace("+", ""))
        return "green" if v >= 0 else "red"
    except:
        return "muted"


def _sign_pct(val):
    """Format as +X.XX% or -X.XX%."""
    try:
        v = float(str(val).replace("%", "").replace("+", ""))
        return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"
    except:
        return str(val)


def _sign_num(val, dec=0):
    """Format number with leading +/- sign."""
    try:
        v = float(val)
        s = f"{v:,.{dec}f}"
        return ("+" + s) if v >= 0 else s
    except:
        return str(val)


def _fmt(val, dec=2, comma=True):
    try:
        v = float(val)
        return f"{v:,.{dec}f}" if comma else f"{v:.{dec}f}"
    except:
        return str(val)


def _snap_class(chg_val):
    """up / dn / neu for snap card CSS class."""
    try:
        v = float(str(chg_val).replace("%", "").replace("+", ""))
        if v > 0.05:  return "up"
        if v < -0.05: return "dn"
        return "neu"
    except:
        return "neu"


def _impact_class(impact):
    return {"high": "red", "medium": "yellow", "low": "muted"}.get(impact, "muted")


# ── CSS (verbatim from template + additions) ──────────────────────────────────

CSS = """
    :root {
      --bg: #0d0f14;
      --surface: #141720;
      --surface2: #1c2030;
      --border: #2a2f42;
      --text: #e4e8f0;
      --text-muted: #8890a8;
      --green: #22c55e;
      --red: #ef4444;
      --yellow: #f59e0b;
      --blue: #3b82f6;
      --purple: #a855f7;
      --accent: #6366f1;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 12px;
      line-height: 1.4;
    }

    /* ── Header ─────────────────────────────────────────────── */
    .header {
      background: linear-gradient(135deg, #1a1d2e 0%, #0f1219 100%);
      border-bottom: 1px solid var(--border);
      padding: 14px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .header-left h1 {
      font-size: 18px; font-weight: 700; color: #fff; letter-spacing: -0.3px;
    }
    .header-left h1 span { color: var(--accent); }
    .header-left p { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
    .header-right { text-align: right; }
    .header-right .date-badge {
      background: var(--accent); color: #fff;
      padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600;
    }
    .header-right .gen-time { font-size: 10px; color: var(--text-muted); margin-top: 3px; }

    /* ── Layout ─────────────────────────────────────────────── */
    .container { max-width: 1200px; margin: 0 auto; padding: 18px 24px 16px; }

    /* ── Section headings ───────────────────────────────────── */
    .section-title {
      font-size: 10px; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.9px; color: var(--text-muted);
      border-left: 3px solid var(--accent);
      padding-left: 7px; margin-bottom: 8px;
    }

    /* ── Quick snapshot row ─────────────────────────────────── */
    .snap-grid {
      display: grid; grid-template-columns: repeat(4, 1fr);
      gap: 8px; margin-bottom: 16px;
    }
    .snap-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 8px; padding: 11px 14px;
      position: relative; overflow: hidden;
    }
    .snap-card::before {
      content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 3px;
    }
    .snap-card.up::before  { background: var(--green); }
    .snap-card.dn::before  { background: var(--red); }
    .snap-card.neu::before { background: var(--yellow); }
    .snap-card.info::before{ background: var(--blue); }
    .snap-card .label {
      font-size: 9px; font-weight: 600; text-transform: uppercase;
      letter-spacing: 0.7px; color: var(--text-muted);
    }
    .snap-card .value { font-size: 18px; font-weight: 700; margin: 3px 0 1px; color: #fff; }
    .snap-card .change { font-size: 11px; font-weight: 600; }
    .snap-card .sub { font-size: 9px; color: var(--text-muted); margin-top: 2px; }
    .up .change { color: var(--green); }
    .dn .change { color: var(--red); }
    .neu .change { color: var(--yellow); }

    /* ── Two/three-column grid ──────────────────────────────── */
    .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
    .three-col { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; margin-bottom: 16px; }

    /* ── Table cards ────────────────────────────────────────── */
    .card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 8px; padding: 12px 14px;
    }
    .card table { width: 100%; border-collapse: collapse; }
    .card table thead th {
      font-size: 9px; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.6px; color: var(--text-muted);
      padding: 0 0 6px; border-bottom: 1px solid var(--border);
    }
    .card table thead th:last-child  { text-align: right; }
    .card table thead th:nth-child(2){ text-align: right; }
    .card table thead th:nth-child(3){ text-align: right; }
    .card table tbody tr { border-bottom: 1px solid rgba(42,47,66,0.5); }
    .card table tbody tr:last-child { border-bottom: none; }
    .card table tbody td { padding: 5px 0; font-size: 11px; }
    .card table tbody td:nth-child(2){ text-align: right; font-weight: 600; }
    .card table tbody td:nth-child(3){ text-align: right; font-weight: 600; }
    .card table tbody td:last-child  { text-align: right; font-weight: 600; }

    .green { color: var(--green); }
    .red   { color: var(--red);   }
    .yellow{ color: var(--yellow);}
    .blue  { color: var(--blue);  }
    .muted { color: var(--text-muted); }

    /* ── Alert/news ─────────────────────────────────────────── */
    .alert-box {
      background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.3);
      border-radius: 8px; padding: 10px 14px; margin-bottom: 16px;
    }
    .alert-box.warning { background: rgba(245,158,11,0.08); border-color: rgba(245,158,11,0.3); }
    .alert-box.info    { background: rgba(59,130,246,0.08);  border-color: rgba(59,130,246,0.3); }
    .alert-box.success { background: rgba(34,197,94,0.08);   border-color: rgba(34,197,94,0.3); }
    .alert-title { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.7px; margin-bottom: 4px; }
    .alert-box         .alert-title { color: var(--red); }
    .alert-box.warning .alert-title { color: var(--yellow); }
    .alert-box.info    .alert-title { color: var(--blue); }
    .alert-box.success .alert-title { color: var(--green); }
    .alert-body { font-size: 11px; color: var(--text); line-height: 1.5; }

    /* ── Signal grid ────────────────────────────────────────── */
    .signal-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 8px; margin-bottom: 16px; }
    .signal-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 8px; padding: 10px 12px;
      display: flex; align-items: center; gap: 10px;
    }
    .signal-icon {
      width: 30px; height: 30px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 13px; flex-shrink: 0;
    }
    .signal-icon.pass { background: rgba(34,197,94,0.15); }
    .signal-icon.fail { background: rgba(239,68,68,0.15); }
    .signal-icon.warn { background: rgba(245,158,11,0.15); }
    .signal-info .label { font-size: 9px; color: var(--text-muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; }
    .signal-info .val   { font-size: 12px; font-weight: 700; margin-top: 1px; }
    .signal-info .status{ font-size: 10px; margin-top: 1px; }
    .signal-info .val.pass, .signal-info .status.pass { color: var(--green); }
    .signal-info .val.fail, .signal-info .status.fail { color: var(--red);   }
    .signal-info .val.warn, .signal-info .status.warn { color: var(--yellow);}

    /* ── Sentiment gauge ────────────────────────────────────── */
    .sentiment-row { display: flex; gap: 12px; margin-bottom: 16px; }
    .sentiment-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; flex: 1; }
    .gauge-bar {
      width: 100%; height: 6px;
      background: linear-gradient(to right, #ef4444 0%, #f59e0b 40%, #22c55e 100%);
      border-radius: 3px; margin: 7px 0 3px; position: relative;
    }
    .gauge-needle {
      width: 10px; height: 10px; border-radius: 50%;
      background: #fff; border: 2px solid var(--bg);
      position: absolute; top: -2px; transform: translateX(-50%);
    }
    .gauge-labels { display: flex; justify-content: space-between; font-size: 9px; color: var(--text-muted); }

    /* ── News list ───────────────────────────────────────────── */
    .news-list { list-style: none; padding: 0; }
    .news-list li { padding: 6px 0; border-bottom: 1px solid rgba(42,47,66,0.5); font-size: 11px; line-height: 1.4; }
    .news-list li:last-child { border-bottom: none; }
    .news-tag {
      display: inline-block; font-size: 8px; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.5px;
      padding: 1px 5px; border-radius: 3px; margin-right: 5px;
    }
    .news-tag.macro  { background: rgba(239,68,68,0.2);  color: #f87171; }
    .news-tag.energy { background: rgba(245,158,11,0.2); color: #fbbf24; }
    .news-tag.india  { background: rgba(99,102,241,0.2); color: #818cf8; }
    .news-tag.crypto { background: rgba(168,85,247,0.2); color: #c084fc; }
    .news-tag.global { background: rgba(59,130,246,0.2); color: #60a5fa; }

    /* ── Footer ─────────────────────────────────────────────── */
    .footer {
      border-top: 1px solid var(--border); padding: 10px 24px;
      display: flex; justify-content: space-between; align-items: center;
      font-size: 10px; color: var(--text-muted);
    }
    .footer .logo { font-weight: 700; color: var(--accent); }

    /* ── Page-break control ─────────────────────────────────── */
    .snap-grid, .snap-card,
    .card, .alert-box,
    .signal-grid, .signal-card,
    .sentiment-row, .sentiment-card,
    .two-col, .three-col,
    .news-list li,
    .section-title {
      break-inside: avoid;
      page-break-inside: avoid;
    }
    .section-title {
      break-after: avoid;
      page-break-after: avoid;
    }
    .page-break-before {
      break-before: page;
      page-break-before: always;
      margin-top: 18px;
    }
"""


# ── Section builders ──────────────────────────────────────────────────────────

def _snap_cards(data):
    bn      = data.get("banknifty_analysis", {})
    nf_row  = next((r for r in data.get("india_markets",[]) if "Nifty 50" in r.get("name","")), {})
    vix_row = data.get("india_vix", {})
    sp_row  = next((r for r in data.get("us_markets",[])    if "S&P"     in r.get("name","")), {})

    bn_close = bn.get("prev_close", "—")
    gap_pts  = bn.get("gap_pts", "—")

    # BankNifty card
    try:
        gp = float(gap_pts)
        gap_str  = f"{'+' if gp>=0 else ''}{gp:,.0f} pts gap estimate"
        gap_sub  = f"Prev close {float(bn_close):,.0f} · {'Gap-Up ↑' if gp>=0 else 'Gap-Down ↓'}"
        bn_cls   = "up" if gp >= 0 else "dn"
        bn_chg   = f"{'+' if gp>=0 else ''}{gp:,.0f} pts ({'+' if gp>=0 else ''}{gp/float(bn_close)*100:.2f}%)"
    except:
        gap_str = "—";  gap_sub = "Gap estimate"; bn_cls = "neu"; bn_chg = "—"

    try:
        bn_val = f"{float(bn_close):,.0f}"
    except:
        bn_val = str(bn_close)

    # Nifty card
    nf_price = nf_row.get("price","—")
    nf_chg   = nf_row.get("chg_pct","—")
    nf_pts   = nf_row.get("chg_pts","—")
    nf_cls   = _snap_class(nf_chg)
    try:
        nf_val = f"{float(nf_price):,.2f}"
        nf_ch_str = _sign_pct(nf_chg)
        nf_sub = f"Chg {_sign_num(nf_pts,1)} pts · Prev close"
    except:
        nf_val = str(nf_price); nf_ch_str = str(nf_chg); nf_sub = "Prev close"

    # VIX card
    vix_val  = vix_row.get("price","—")
    vix_chg  = vix_row.get("chg_pct","—")
    try:
        vv = float(vix_val)
        vix_cls = "neu" if vv < 19 else "dn"
        vix_sub = f"Below 19 → ✅ Trades ALLOWED" if vv < 19 else f"Above 19 → ⚠ Caution"
        vix_ch_str = _sign_pct(vix_chg)
    except:
        vix_cls = "neu"; vix_sub = "—"; vix_ch_str = str(vix_chg)

    # S&P card
    sp_price = sp_row.get("price","—")
    sp_chg   = sp_row.get("chg_pct","—")
    sp_cls   = _snap_class(sp_chg)
    try:
        sp_val    = f"{float(sp_price):,.2f}"
        sp_ch_str = _sign_pct(sp_chg)
        sp_sub    = "US prev session · Overnight move"
    except:
        sp_val = str(sp_price); sp_ch_str = str(sp_chg); sp_sub = "Overnight move"

    return f"""
  <div class="section-title">Quick Snapshot</div>
  <div class="snap-grid">
    <div class="snap-card {bn_cls}">
      <div class="label">Bank Nifty (Prev Close)</div>
      <div class="value">{_e(bn_val)}</div>
      <div class="change">{_e(bn_chg)}</div>
      <div class="sub">{_e(gap_sub)}</div>
    </div>
    <div class="snap-card {nf_cls}">
      <div class="label">Nifty 50 (Prev Close)</div>
      <div class="value">{_e(nf_val)}</div>
      <div class="change">{_e(nf_ch_str)}</div>
      <div class="sub">{_e(nf_sub)}</div>
    </div>
    <div class="snap-card {vix_cls}">
      <div class="label">India VIX (Prev Close)</div>
      <div class="value">{_e(vix_val)}</div>
      <div class="change">{_e(vix_ch_str)}</div>
      <div class="sub">{_e(vix_sub)}</div>
    </div>
    <div class="snap-card {sp_cls}">
      <div class="label">S&amp;P 500 (US, Prev Session)</div>
      <div class="value">{_e(sp_val)}</div>
      <div class="change">{_e(sp_ch_str)}</div>
      <div class="sub">{_e(sp_sub)}</div>
    </div>
  </div>"""


def _india_table(data):
    rows = data.get("india_markets", [])[:7]
    prev = data.get("prev_day_label","Prev")
    date_str = data.get("date_str","")
    tbody = ""
    for r in rows:
        price = r.get("price","—"); chg = r.get("chg_pts","—"); chgp = r.get("chg_pct","—")
        cc  = _pct_class(chgp)
        try: pf = f"{float(price):,.2f}"
        except: pf = str(price)
        try: cf = f"{'+' if float(chg)>=0 else ''}{float(chg):,.1f}"
        except: cf = str(chg)
        bold = "strong" if r.get("name") in ("Bank Nifty","Nifty 50","Sensex") else "span"
        tbody += f"""
          <tr>
            <td><{bold}>{_e(r.get('name',''))}</{bold}></td>
            <td>{_e(pf)}</td>
            <td class="{cc}">{_e(cf)}</td>
            <td class="{cc}">{_e(_sign_pct(chgp))}</td>
          </tr>"""
    return f"""
    <div class="card">
      <table>
        <thead><tr><th>Index</th><th>Close</th><th>Change</th><th>Chg %</th></tr></thead>
        <tbody>{tbody}
        </tbody>
      </table>
    </div>"""


def _global_table(data):
    us     = data.get("us_markets",[])
    asian  = data.get("asian_markets",[])
    europe = data.get("europe_markets",[])
    all_mkts = us + asian + europe
    tbody = ""
    for r in all_mkts[:11]:
        price = r.get("price","—"); chgp = r.get("chg_pct","—")
        cc = _pct_class(chgp)
        try: pf = f"{float(price):,.2f}"
        except: pf = str(price)
        tbody += f"""
          <tr>
            <td>{_e(r.get('name',''))}</td>
            <td>{_e(pf)}</td>
            <td class="{cc}">{_e(_sign_pct(chgp))}</td>
          </tr>"""
    return f"""
    <div class="card">
      <div class="section-title" style="margin-bottom:10px;">Global Indices</div>
      <table>
        <thead><tr><th>Market</th><th>Level</th><th>Change</th></tr></thead>
        <tbody>{tbody}
        </tbody>
      </table>
    </div>"""


def _commodities_table(data):
    commod = data.get("commodities_spot",[])
    tbody = ""
    for r in commod:
        chgp = r.get("chg_pct","—"); cc = _pct_class(chgp)
        tbody += f"""
          <tr>
            <td>{_e(r.get('name',''))}</td>
            <td>{_e(r.get('price','—'))}</td>
            <td class="{cc}">{_e(_sign_pct(chgp))}</td>
          </tr>"""
    return f"""
    <div class="card">
      <div class="section-title" style="margin-bottom:10px;">Commodities (Pre-Market)</div>
      <table>
        <thead><tr><th>Commodity</th><th>Price</th><th>Change</th></tr></thead>
        <tbody>{tbody}
        </tbody>
      </table>
    </div>"""


def _currency_crypto_table(data):
    curr   = data.get("currencies",[])
    crypto = data.get("crypto",[])
    tbody = ""
    for r in curr:
        chgp = r.get("chg_pct","—"); cc = _pct_class(chgp)
        tbody += f"""
          <tr>
            <td>{_e(r.get('pair',''))}</td>
            <td>{_e(r.get('rate','—'))}</td>
            <td class="{cc}">{_e(_sign_pct(chgp))}</td>
          </tr>"""
    for cr in crypto:
        chg24 = cr.get("chg_pct_24h","—"); cc = _pct_class(chg24)
        try:
            price_str = f"${float(cr['price_usd']):,.0f}"
        except:
            price_str = str(cr.get("price_usd","—"))
        tbody += f"""
          <tr>
            <td>{_e(cr.get('symbol',''))} (24h)</td>
            <td>{_e(price_str)}</td>
            <td class="{cc}">{_e(_sign_pct(chg24))}</td>
          </tr>"""
    return f"""
    <div class="card">
      <div class="section-title" style="margin-bottom:10px;">Currency &amp; Crypto (Pre-Market)</div>
      <table>
        <thead><tr><th>Pair / Asset</th><th>Rate</th><th>Change</th></tr></thead>
        <tbody>{tbody}
        </tbody>
      </table>
    </div>"""


def _sentiment_row(data):
    fg_score  = data.get("fear_greed_val","—")
    fg_label  = data.get("fear_greed_label","—")
    cfg_score = data.get("crypto_fg_score","—")
    cfg_label = data.get("crypto_fg_label","—")
    vix_row   = data.get("india_vix",{})
    vix_val   = vix_row.get("price","—")

    def _fg_color_cls(score):
        try:
            s = float(score)
            if s <= 25: return "red"
            if s <= 44: return "yellow"
            if s <= 55: return "muted"
            return "green"
        except:
            return "muted"

    try:
        vv = float(vix_val)
        vix_cls   = "green" if vv < 19 else "red"
        vix_status = f"Below 19 — Trades ENABLED" if vv < 19 else "Above 19 — Caution"
    except:
        vix_cls = "muted"; vix_status = "—"

    def _needle_pct(score):
        try: return f"{min(max(float(score),0),100):.0f}%"
        except: return "50%"

    return f"""
  <div class="section-title">Market Sentiment</div>
  <div class="sentiment-row">
    <div class="sentiment-card">
      <div class="section-title" style="margin-bottom:8px;">CNN Fear &amp; Greed (Equities)</div>
      <div style="font-size:22px; font-weight:800; color: var(--{_fg_color_cls(fg_score)});">{_e(fg_score)}</div>
      <div style="font-size:11px; font-weight:600; color: var(--{_fg_color_cls(fg_score)}); margin-bottom:6px;">{_e(fg_label)}</div>
      <div class="gauge-bar">
        <div class="gauge-needle" style="left:{_needle_pct(fg_score)};"></div>
      </div>
      <div class="gauge-labels"><span>Extreme Fear</span><span>Neutral</span><span>Extreme Greed</span></div>
    </div>
    <div class="sentiment-card">
      <div class="section-title" style="margin-bottom:8px;">Crypto Fear &amp; Greed</div>
      <div style="font-size:28px; font-weight:800; color: var(--{_fg_color_cls(cfg_score)});">{_e(cfg_score)}</div>
      <div style="font-size:12px; font-weight:600; color: var(--{_fg_color_cls(cfg_score)}); margin-bottom:8px;">{_e(cfg_label)}</div>
      <div class="gauge-bar">
        <div class="gauge-needle" style="left:{_needle_pct(cfg_score)};"></div>
      </div>
      <div class="gauge-labels"><span>Extreme Fear</span><span>Neutral</span><span>Extreme Greed</span></div>
    </div>
    <div class="sentiment-card">
      <div class="section-title" style="margin-bottom:8px;">India VIX Trend</div>
      <div style="font-size:22px; font-weight:800; color: var(--{vix_cls});">{_e(vix_val)}</div>
      <div style="font-size:11px; font-weight:600; color: var(--{vix_cls}); margin-bottom:6px;">{_e(vix_status)}</div>
      <div style="font-size:11px; color: var(--text-muted); margin-top:6px;">
        Threshold: 19.0 &nbsp;|&nbsp; Below = trades enabled<br>
        {"⬇ Declining — volatility cooling" if vix_cls == "green" else "⬆ Elevated — trade with caution"}
      </div>
    </div>
  </div>"""


def _signal_grid(data):
    sig = data.get("hawala_signal",{})

    def _pf(ok):
        if ok is None: return "warn"
        return "pass" if ok else "fail"

    def _icon(ok):
        if ok is None: return "⚠️"
        return "✅" if ok else "❌"

    gap_pts   = sig.get("gap_pts","—")
    gap_strat = sig.get("gap_strategy","—")
    gap_dir   = sig.get("gap_dir","FLAT")
    dow_name  = sig.get("dow_name","—")
    overall   = sig.get("overall","—")
    reason    = sig.get("reason","")

    try:
        gp = float(gap_pts)
        gap_val_str = f"{'+' if gp>=0 else ''}{gp:,.0f} pts {gap_dir}"
        gap_status  = f"Routes to: {gap_strat}"
    except:
        gap_val_str = str(gap_pts); gap_status = gap_strat

    fii_val = sig.get("fii_net","—")
    try:
        fii_str = f"₹{float(fii_val):,.0f} Cr"
        fii_pf  = _pf(sig.get("fii_pass"))
    except:
        fii_str = "Data pending"; fii_pf = "warn"

    overall_pf  = "pass" if overall != "NO TRADE" else "fail"
    overall_icon = "✅" if overall != "NO TRADE" else "❌"

    dow_pf   = "fail" if sig.get("dow_blocked") else "pass"
    dow_icon = "❌"  if sig.get("dow_blocked") else "✅"
    dow_val  = f"{dow_name} — {'EXCLUDED' if sig.get('dow_blocked') else 'ALLOWED'}"
    dow_sub  = "Mon & Thu excluded by DOW filter" if sig.get("dow_blocked") else "Trading day — DOW filter passes"

    chips = [
        {
            "label":  "India VIX Filter",
            "val":    f"{sig.get('vix_val','—')} — {'PASS' if sig.get('vix_pass') else 'FAIL'}",
            "status": _sign_pct(sig.get("vix_val","—")),
            "sub":    f"Threshold: Skip if VIX &gt; {sig.get('vix_thresh',19)}",
            "pf":     _pf(sig.get("vix_pass")),
            "icon":   _icon(sig.get("vix_pass")),
        },
        {
            "label":  "S&amp;P Overnight Move",
            "val":    f"{_sign_pct(sig.get('sp_chg','—'))} — {'PASS' if sig.get('sp_pass') else 'FAIL'}",
            "status": f"Threshold: Skip if S&amp;P &lt; {sig.get('sp_thresh',-1.5)}%",
            "pf":     _pf(sig.get("sp_pass")),
            "icon":   _icon(sig.get("sp_pass")),
        },
        {
            "label":  "FPI Net Flow (Prev Day)",
            "val":    fii_str,
            "status": f"Threshold: Skip if FPI &lt; −₹{abs(int(sig.get('fii_thresh',-3000))):,} Cr",
            "pf":     fii_pf,
            "icon":   _icon(sig.get("fii_pass")),
        },
        {
            "label":  "GIFT Nifty Gap",
            "val":    gap_val_str,
            "status": gap_status,
            "pf":     "warn",
            "icon":   "📊",
        },
        {
            "label":  "DOW Filter",
            "val":    dow_val,
            "status": dow_sub,
            "pf":     dow_pf,
            "icon":   dow_icon,
        },
        {
            "label":  "Overall Signal",
            "val":    overall,
            "status": reason,
            "pf":     overall_pf,
            "icon":   overall_icon,
        },
    ]

    html = '\n  <div class="section-title">Hawala v2 — Pre-Market Filter Check</div>\n  <div class="signal-grid">'
    for chip in chips:
        pf = chip["pf"]
        html += f"""
    <div class="signal-card">
      <div class="signal-icon {pf}">{chip['icon']}</div>
      <div class="signal-info">
        <div class="label">{chip['label']}</div>
        <div class="val {pf}">{_e(chip['val'])}</div>
        <div class="status {pf}">{chip['status']}</div>
      </div>
    </div>"""
    html += "\n  </div>"
    return html


def _scenario_box(data):
    sig      = data.get("hawala_signal",{})
    overall  = sig.get("overall","—")
    scenario = data.get("scenario_text","")

    if overall == "NO TRADE":
        kind = ""; title = f"⛔ No Trade Today — {sig.get('reason','')}"
    elif overall in ("ORB","OPTIONS_ORB"):
        kind = "success"; title = "📊 Expected Opening Scenario"
    else:
        kind = "warning"; title = "📊 Expected Opening Scenario"

    return f"""
  <div class="alert-box {kind}">
    <div class="alert-title">{_e(title)}</div>
    <div class="alert-body">{_e(scenario)}</div>
  </div>"""


def _news_section(data):
    news = data.get("news_items",[])
    if not news:
        return ""
    items_html = ""
    for item in news[:6]:
        tag = item.get("tag","macro")
        hl  = item.get("headline","")
        items_html += f"""
      <li>
        <span class="news-tag {_e(tag)}">{_e(tag.upper())}</span>
        {_e(hl)}
      </li>"""
    return f"""
  <div class="section-title">Key News &amp; Events</div>
  <div class="card" style="margin-bottom:24px;">
    <ul class="news-list">{items_html}
    </ul>
  </div>"""


def _data_freshness_section(data):
    """
    Reads each v3 signal-input cache directly from disk and reports the last
    available date versus the expected lag-1 trading day.

    Colour coding:
      green  = fresh (last expected trading day)
      yellow = 1–2 calendar days behind expected
      red    = 3+ days behind, or file missing
    """
    import pickle, pathlib, datetime as _dt, pandas as _pd

    ROOT = pathlib.Path(__file__).parent

    today_str = data.get("date_iso", _dt.date.today().isoformat())
    try:
        today = _dt.date.fromisoformat(today_str)
    except ValueError:
        today = _dt.date.today()

    # Last expected trading day before today (weekday only — no NSE holiday table)
    expected = today - _dt.timedelta(days=1)
    while expected.weekday() >= 5:
        expected -= _dt.timedelta(days=1)

    def _status(last_str):
        """Return (css_class, icon, date_label) tuple."""
        if last_str is None:
            return "red", "✕", "MISSING"
        try:
            last = _dt.date.fromisoformat(str(last_str)[:10])
        except ValueError:
            return "red", "✕", "PARSE ERROR"
        delta = (expected - last).days
        if delta <= 0:
            return "green", "✓", str(last)
        if delta <= 2:
            return "yellow", "⚠", f"{last}  (+{delta}d)"
        return "red", "✕", f"{last}  (+{delta}d)"

    def _last_dict(path):
        """Max key < today from a pickle whose keys are date strings."""
        if not path.exists():
            return None
        try:
            with open(path, "rb") as fh:
                d = pickle.load(fh)
            prev = [k for k in d.keys() if str(k) < today_str]
            return max(prev) if prev else None
        except Exception:
            return None

    def _last_df_pickle(path, date_col="date"):
        """Max date column < today from a pickled DataFrame."""
        if not path.exists():
            return None
        try:
            with open(path, "rb") as fh:
                df = pickle.load(fh)
            sub = df[df[date_col].astype(str) < today_str]
            return str(sub[date_col].max()) if not sub.empty else None
        except Exception:
            return None

    # FII cash (CSV)
    fii_cash_last = None
    try:
        p = ROOT / "fii_data.csv"
        if p.exists():
            cf = _pd.read_csv(p)
            cf["date"] = _pd.to_datetime(cf["date"]).dt.date
            prev = cf[cf["date"] < today]
            fii_cash_last = str(prev["date"].max()) if not prev.empty else None
    except Exception:
        pass

    entries = [
        ("FII Cash",              fii_cash_last,
         "fii_data.csv",                           "FII Signature"),
        ("FII F&amp;O",          _last_dict(ROOT / "trade_logs/_fii_fo_cache.pkl"),
         "_fii_fo_cache.pkl",                      "FII Signature"),
        ("PCR / Bhavcopy (N)",   _last_dict(ROOT / "v3/cache/bhavcopy_NIFTY_all.pkl"),
         "bhavcopy_NIFTY_all.pkl",                 "PCR · Strike Defense"),
        ("PCR / Bhavcopy (BN)",  _last_dict(ROOT / "v3/cache/bhavcopy_BN_all.pkl"),
         "bhavcopy_BN_all.pkl",                    "PCR · Strike Defense"),
        ("Candles 1m — NIFTY",   _last_df_pickle(ROOT / "v3/cache/candles_1m_NIFTY.pkl"),
         "candles_1m_NIFTY.pkl",                   "OI Quadrant · Basis"),
        ("Candles 1m — BANKNIFTY", _last_df_pickle(ROOT / "v3/cache/candles_1m_BANKNIFTY.pkl"),
         "candles_1m_BANKNIFTY.pkl",               "OI Quadrant · Basis"),
        ("Option OI — NIFTY",    _last_dict(ROOT / "v3/cache/option_oi_1m_NIFTY.pkl"),
         "option_oi_1m_NIFTY.pkl",                 "OI Velocity · Strike Defense"),
        ("Option OI — BANKNIFTY", _last_dict(ROOT / "v3/cache/option_oi_1m_BANKNIFTY.pkl"),
         "option_oi_1m_BANKNIFTY.pkl",             "OI Velocity · Strike Defense"),
    ]

    rows_html = ""
    for label, last, filename, signals in entries:
        css, icon, date_lbl = _status(last)
        rows_html += f"""
          <tr>
            <td><strong>{label}</strong></td>
            <td style="font-family:monospace;font-size:10px;color:var(--text-muted);">{filename}</td>
            <td style="font-size:10px;color:var(--text-muted);">{signals}</td>
            <td class="{css}" style="font-weight:700;text-align:center;">{icon}</td>
            <td class="{css}" style="font-weight:600;">{date_lbl}</td>
          </tr>"""

    all_fresh = all(_status(last)[0] == "green" for _, last, _, _ in entries)
    any_missing = any(_status(last)[0] == "red" for _, last, _, _ in entries)

    if all_fresh:
        summary_cls = "success"
        summary_txt = f"All caches up to date &mdash; expected lag-1: <strong>{expected}</strong>"
    elif any_missing:
        summary_cls = ""  # red alert-box
        summary_txt = f"One or more caches are stale or missing. Expected lag-1: <strong>{expected}</strong>"
    else:
        summary_cls = "warning"
        summary_txt = f"Some caches are slightly behind. Expected lag-1: <strong>{expected}</strong>"

    return f"""
  <div class="section-title">v3 Signal Data Freshness</div>
  <div class="alert-box {summary_cls}" style="margin-bottom:12px;">
    <div class="alert-title">{'✅ All Fresh' if all_fresh else ('⚠ Partial Staleness' if not any_missing else '❌ Stale / Missing Data')}</div>
    <div class="alert-body">{summary_txt}</div>
  </div>
  <div class="card" style="margin-bottom:24px;">
    <table>
      <thead>
        <tr>
          <th>Cache</th>
          <th>File</th>
          <th>Signals Using This</th>
          <th style="text-align:center;">Status</th>
          <th>Last Available Date</th>
        </tr>
      </thead>
      <tbody>{rows_html}
      </tbody>
    </table>
  </div>"""


def _events_calendar(data):
    events = data.get("events_calendar",[])
    if not events:
        events = [
            {"time":"9:00 AM",  "event":"Pre-Open Session — NSE / BSE",                    "impact":"medium"},
            {"time":"9:15 AM",  "event":"Market Open — BankNifty gap signal confirmation",  "impact":"high"},
            {"time":"9:20 AM",  "event":"First 5-min candle close — Hawala v2 entry check", "impact":"high"},
            {"time":"9:30 AM",  "event":"ORB window closes",                                "impact":"medium"},
            {"time":"3:30 PM",  "event":"Market Close — Log trade outcome",                 "impact":"medium"},
        ]
    rows_html = ""
    for ev in events:
        ic = _impact_class(ev.get("impact","medium"))
        rows_html += f"""
        <tr>
          <td>{_e(ev.get('time',''))}</td>
          <td>{_e(ev.get('event',''))}</td>
          <td class="{ic}">{_e(ev.get('impact','').capitalize())}</td>
        </tr>"""
    return f"""
  <div class="section-title">Today's Events Calendar</div>
  <div class="card" style="margin-bottom:24px;">
    <table>
      <thead><tr><th>Time (IST)</th><th>Event</th><th>Expected Impact</th></tr></thead>
      <tbody>{rows_html}
      </tbody>
    </table>
  </div>"""


def _fii_dii_section(data):
    rows = data.get("fii_dii", [])
    if not rows:
        return ""
    tbody = ""
    totals = {"buy": 0.0, "sell": 0.0, "net": 0.0}
    for r in rows:
        cat  = r.get("category", "—")
        buy  = r.get("buy", "—")
        sell = r.get("sell", "—")
        net  = r.get("net", "—")
        try:
            bf = float(buy);  sf = float(sell);  nf = float(net)
            totals["buy"] += bf;  totals["sell"] += sf;  totals["net"] += nf
            nc = "green" if nf >= 0 else "red"
            buy_s  = f"₹{bf:,.2f} Cr"
            sell_s = f"₹{sf:,.2f} Cr"
            net_s  = f"{'+'if nf>=0 else ''}₹{nf:,.2f} Cr"
        except:
            nc = "muted"; buy_s = str(buy); sell_s = str(sell); net_s = str(net)
        tbody += f"""
          <tr>
            <td><strong>{_e(cat)}</strong></td>
            <td>{_e(buy_s)}</td>
            <td>{_e(sell_s)}</td>
            <td class="{nc}">{_e(net_s)}</td>
          </tr>"""
    # totals row
    tnc = "green" if totals["net"] >= 0 else "red"
    tbody += f"""
          <tr style="border-top:1px solid var(--border); font-weight:700;">
            <td>Total</td>
            <td>₹{totals['buy']:,.2f} Cr</td>
            <td>₹{totals['sell']:,.2f} Cr</td>
            <td class="{tnc}">{'+'if totals['net']>=0 else ''}₹{totals['net']:,.2f} Cr</td>
          </tr>"""
    return f"""
  <div class="section-title">FII / DII Activity (Prev Session)</div>
  <div class="card" style="margin-bottom:24px;">
    <table>
      <thead><tr><th>Category</th><th>Buy (₹ Cr)</th><th>Sell (₹ Cr)</th><th>Net (₹ Cr)</th></tr></thead>
      <tbody>{tbody}
      </tbody>
    </table>
  </div>"""


def _nifty_levels_section(data):
    bn  = data.get("banknifty_analysis", {})
    nf  = data.get("nifty_analysis", {})

    def _pivots_html(label, analysis):
        pc = analysis.get("pivots_classic", {})
        pf = analysis.get("pivots_fib", {})
        if not pc and not pf:
            return ""
        close = analysis.get("prev_close", "—")
        try:
            close_f = float(close)
            close_str = f"{close_f:,.2f}"
        except:
            close_str = str(close)

        def _level_row(name, val, color="muted"):
            try:
                vf = float(val)
                return f'<tr><td>{name}</td><td class="{color}" style="text-align:right;font-weight:600;">{vf:,.2f}</td></tr>'
            except:
                return f'<tr><td>{name}</td><td class="muted" style="text-align:right;">{val}</td></tr>'

        rows_c = (
            _level_row("R3", pc.get("R3"), "red") +
            _level_row("R2", pc.get("R2"), "red") +
            _level_row("R1", pc.get("R1"), "red") +
            f'<tr style="background:rgba(99,102,241,0.1);"><td><strong>Prev Close</strong></td><td style="text-align:right;font-weight:700;color:var(--accent);">{close_str}</td></tr>' +
            f'<tr><td>Pivot (PP)</td><td style="text-align:right;font-weight:600;color:var(--blue);">{_fmt(pc.get("PP","—"))}</td></tr>' +
            _level_row("S1", pc.get("S1"), "green") +
            _level_row("S2", pc.get("S2"), "green") +
            _level_row("S3", pc.get("S3"), "green")
        )
        rows_f = (
            _level_row("R3", pf.get("R3"), "red") +
            _level_row("R2", pf.get("R2"), "red") +
            _level_row("R1", pf.get("R1"), "red") +
            f'<tr style="background:rgba(99,102,241,0.1);"><td><strong>Pivot (PP)</strong></td><td style="text-align:right;font-weight:700;color:var(--accent);">{_fmt(pf.get("PP","—"))}</td></tr>' +
            _level_row("S1", pf.get("S1"), "green") +
            _level_row("S2", pf.get("S2"), "green") +
            _level_row("S3", pf.get("S3"), "green")
        )
        return f"""
    <div class="card">
      <div class="section-title" style="margin-bottom:10px;">{label}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
        <div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;color:var(--text-muted);margin-bottom:6px;">Classic Pivots</div>
          <table style="width:100%;border-collapse:collapse;">
            <tbody>{rows_c}</tbody>
          </table>
        </div>
        <div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;color:var(--text-muted);margin-bottom:6px;">Fibonacci Pivots</div>
          <table style="width:100%;border-collapse:collapse;">
            <tbody>{rows_f}</tbody>
          </table>
        </div>
      </div>
    </div>"""

    bn_html = _pivots_html("BankNifty — Supports &amp; Resistances", bn)
    nf_html = _pivots_html("Nifty 50 — Supports &amp; Resistances", nf)

    if not bn_html and not nf_html:
        return ""

    return f"""
  <div class="section-title">Key Levels (Classic &amp; Fibonacci Pivots)</div>
  <div class="two-col" style="margin-bottom:24px;">
{bn_html}
{nf_html}
  </div>"""


def _oi_chart_section(data):
    bn = data.get("banknifty_analysis", {})
    nf = data.get("nifty_analysis", {})

    def _oi_card(label, analysis):
        oc  = analysis.get("option_chain", {})
        atm = oc.get("atm", 0)
        pcr = oc.get("pcr", "—")
        expiry = oc.get("near_expiry", "—")

        # Prefer pre-built strike_chain; fall back to merging legacy top-3 lists
        chain = oc.get("strike_chain", [])
        if not chain:
            ce_strikes = oc.get("top_ce_strikes", [])
            pe_strikes = oc.get("top_pe_strikes", [])
            strike_map = {}
            for s in ce_strikes:
                k = s.get("strike", 0)
                strike_map.setdefault(k, {"ce_oi": 0, "pe_oi": 0})
                strike_map[k]["ce_oi"] = s.get("oi", 0)
            for s in pe_strikes:
                k = s.get("strike", 0)
                strike_map.setdefault(k, {"ce_oi": 0, "pe_oi": 0})
                strike_map[k]["pe_oi"] = s.get("oi", 0)
            chain = [{"strike": k, "ce_oi": v["ce_oi"], "pe_oi": v["pe_oi"]}
                     for k, v in sorted(strike_map.items(), reverse=True)]

        strikes_sorted = [r["strike"] for r in chain]
        max_oi = max((max(r["ce_oi"], r["pe_oi"]) for r in chain), default=1) or 1

        try:
            pcr_f = float(pcr)
            pcr_cls = "green" if pcr_f > 1.2 else ("red" if pcr_f < 0.8 else "yellow")
            pcr_str = f"{pcr_f:.2f}"
        except:
            pcr_cls = "muted"; pcr_str = str(pcr)

        if not chain:
            no_data = f"""
      <div style="color:var(--text-muted);font-size:12px;padding:16px 0;">
        Option chain data unavailable — market closed or NSE API returned no data.
      </div>"""
            body = no_data
        else:
            bar_rows = ""
            for row in chain:
                strike = row["strike"]
                ce_pct = row["ce_oi"] / max_oi * 100
                pe_pct = row["pe_oi"] / max_oi * 100
                atm_style = "font-weight:700;color:var(--accent);" if strike == atm else ""
                ce_lakh = f"{row['ce_oi']:.1f}L"
                pe_lakh = f"{row['pe_oi']:.1f}L"
                bar_rows += f"""
        <tr style="font-size:11px;">
          <td style="text-align:right;padding:3px 6px;color:var(--red);width:60px;">{ce_lakh}</td>
          <td style="padding:3px 4px;width:90px;">
            <div style="background:rgba(239,68,68,0.15);border-radius:2px;height:10px;width:100%;display:flex;justify-content:flex-end;">
              <div style="background:var(--red);border-radius:2px;height:10px;width:{ce_pct:.1f}%;"></div>
            </div>
          </td>
          <td style="text-align:center;padding:3px 8px;{atm_style}">{strike}</td>
          <td style="padding:3px 4px;width:90px;">
            <div style="background:rgba(34,197,94,0.15);border-radius:2px;height:10px;">
              <div style="background:var(--green);border-radius:2px;height:10px;width:{pe_pct:.1f}%;"></div>
            </div>
          </td>
          <td style="padding:3px 6px;color:var(--green);width:60px;">{pe_lakh}</td>
        </tr>"""
            body = f"""
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-muted);margin-bottom:6px;">
        <span style="color:var(--red);font-weight:600;">← CALL OI (Resistance)</span>
        <span>Strike</span>
        <span style="color:var(--green);font-weight:600;">PUT OI (Support) →</span>
      </div>
      <table style="width:100%;border-collapse:collapse;">{bar_rows}
      </table>"""

        pcr_label = "Bullish" if pcr_str != "—" and float(pcr_str if pcr_str != "—" else 1) > 1.2 else (
            "Bearish" if pcr_str != "—" and float(pcr_str if pcr_str != "—" else 1) < 0.8 else "Neutral"
        ) if pcr_str != "—" else "—"

        return f"""
    <div class="card">
      <div class="section-title" style="margin-bottom:8px;">{label}</div>
      <div style="display:flex;gap:24px;margin-bottom:12px;">
        <div><div style="font-size:10px;color:var(--text-muted);">Near Expiry</div><div style="font-weight:600;font-size:13px;">{_e(str(expiry))}</div></div>
        <div><div style="font-size:10px;color:var(--text-muted);">ATM Strike</div><div style="font-weight:600;font-size:13px;">{_e(str(atm)) if atm else '—'}</div></div>
        <div><div style="font-size:10px;color:var(--text-muted);">PCR</div><div style="font-weight:700;font-size:16px;" class="{pcr_cls}">{_e(pcr_str)}</div></div>
        <div><div style="font-size:10px;color:var(--text-muted);">Sentiment</div><div style="font-weight:600;font-size:13px;" class="{pcr_cls}">{_e(pcr_label)}</div></div>
      </div>
{body}
    </div>"""

    bn_card = _oi_card("BankNifty Option Chain — OI by Strike", bn)
    nf_card = _oi_card("Nifty 50 Option Chain — OI by Strike", nf)

    return f"""
  <div class="section-title">Open Interest Profile &amp; PCR</div>
  <div class="two-col" style="margin-bottom:24px;">
{bn_card}
{nf_card}
  </div>"""


# ── Healthcheck block (May 2026) ──────────────────────────────────────────────
# Reads v3/cache/healthcheck_latest.json (written by ops/healthcheck.py at 07:25).
# Renders a compact status block at top of newsletter — green on all-pass,
# red on any fail. Always visible so user catches infra issues before market open.

def _healthcheck_block() -> str:
    import json, pathlib
    p = pathlib.Path(__file__).resolve().parent / 'v3' / 'cache' / 'healthcheck_latest.json'
    if not p.exists():
        return ('<div class="alert alert-yellow"><b>⚠ Healthcheck data missing.</b>'
                ' Did ops/healthcheck.py run at 07:25 IST?</div>')
    try:
        d = json.loads(p.read_text())
    except Exception as e:
        return f'<div class="alert alert-yellow"><b>⚠ Healthcheck JSON unreadable:</b> {_e(str(e))}</div>'

    n_pass, n_warn, n_fail = d.get('n_pass',0), d.get('n_warn',0), d.get('n_fail',0)
    overall = d.get('overall','UNKNOWN')

    # All-pass: compact green banner
    if overall == 'PASS' and n_warn == 0:
        return (f'<div class="alert alert-green">'
                f'<b>✓ Healthcheck PASS</b> — {n_pass} checks green '
                f'<span style="opacity:0.7">({_e(d.get("ts","")[:16])})</span></div>')

    # Otherwise: detailed block with failed/warning rows
    fails = [c for c in d.get('checks',[]) if c.get('status') == 'FAIL']
    warns = [c for c in d.get('checks',[]) if c.get('status') == 'WARN']

    banner_class = 'alert-red' if n_fail > 0 else 'alert-yellow'
    banner_emoji = '✗' if n_fail > 0 else '⚠'
    parts = [f'<div class="alert {banner_class}">'
             f'<b>{banner_emoji} Healthcheck — {n_fail} FAIL · {n_warn} WARN · '
             f'{n_pass} PASS</b><br>']
    if fails:
        parts.append('<b>Failures:</b><ul style="margin:6px 0 6px 18px;padding:0">')
        for c in fails[:15]:
            parts.append(f'<li>[{_e(c["cat"])}] <b>{_e(c["label"])}</b>: '
                          f'{_e(c["detail"][:200])}</li>')
        if len(fails) > 15:
            parts.append(f'<li>… and {len(fails)-15} more failures</li>')
        parts.append('</ul>')
    if warns:
        parts.append('<b>Warnings:</b><ul style="margin:6px 0 6px 18px;padding:0">')
        for c in warns[:10]:
            parts.append(f'<li>[{_e(c["cat"])}] <b>{_e(c["label"])}</b>: '
                          f'{_e(c["detail"][:200])}</li>')
        if len(warns) > 10:
            parts.append(f'<li>… and {len(warns)-10} more warnings</li>')
        parts.append('</ul>')
    parts.append(f'<div style="opacity:0.7;font-size:11px">'
                  f'Generated {_e(d.get("ts","")[:19])} · '
                  f'See ops/healthcheck.py for what each check covers.</div>')
    parts.append('</div>')
    return _autoheal_block() + "\n".join(parts)


def _autoheal_block() -> str:
    """Render the autoheal routine's result — what was auto-fixed at 06:55,
    what still needs a human. Reads v3/cache/autoheal_latest.json."""
    import json, pathlib
    p = pathlib.Path(__file__).resolve().parent / 'v3' / 'cache' / 'autoheal_latest.json'
    if not p.exists():
        return ''   # autoheal not yet run — healthcheck block alone suffices
    try:
        d = json.loads(p.read_text())
    except Exception:
        return ''

    fixed   = [f for f in d.get('fixed', []) if not f.get('dry_run')]
    failed  = d.get('failed_to_fix', [])
    unfix   = d.get('unfixable', [])
    streak  = d.get('green_streak', 0)
    stable  = d.get('stable', False)
    needs_human = failed + unfix

    # All-clean: compact green line
    if not fixed and not needs_human:
        tag = f' · ✅ STABLE ({streak}d clean)' if stable else f' · streak {streak}d'
        return (f'<div class="alert alert-green">'
                f'<b>🔧 Autoheal: nothing to fix</b>{tag}</div>')

    cls = 'alert-red' if needs_human else 'alert-yellow'
    parts = [f'<div class="alert {cls}">'
             f'<b>🔧 Autoheal — {len(fixed)} auto-fixed · '
             f'{len(needs_human)} need a human</b><br>']
    if fixed:
        parts.append('<b>Auto-fixed at 06:55:</b><ul style="margin:6px 0 6px 18px">')
        for f in fixed:
            parts.append(f'<li>{_e(f.get("label",""))}</li>')
        parts.append('</ul>')
    if needs_human:
        parts.append('<b>Needs a human:</b><ul style="margin:6px 0 6px 18px">')
        for u in needs_human:
            lbl = u.get('label',''); det = u.get('detail','')
            parts.append(f'<li><b>{_e(lbl)}</b>: {_e(det[:160])}</li>')
        parts.append('</ul>')
    parts.append(f'<div style="opacity:0.7;font-size:11px">'
                  f'pre-fix={_e(d.get("pre_fix_overall",""))} → '
                  f'post-fix={_e(d.get("post_fix_overall",""))} · '
                  f'green streak {streak}d</div></div>')
    return "\n".join(parts)


# ── Master HTML builder ───────────────────────────────────────────────────────

def build_html(data: dict) -> str:
    date_str  = data.get("date_str","")
    gen_at    = data.get("generated_at","")
    prev_day  = data.get("prev_day_label","Prev")

    # Determine macro alert (if large crude move or extreme VIX)
    macro_alert = ""
    for row in data.get("commodities_spot",[]):
        if "Brent" in row.get("name",""):
            try:
                chg = float(str(row.get("chg_pct","0")).replace("%","").replace("+",""))
                if abs(chg) > 3:
                    macro_alert = f"""
  <div class="alert-box">
    <div class="alert-title">⚠ Elevated Commodity Risk — Brent Crude {row.get('chg_pct','')}</div>
    <div class="alert-body">
      Brent Crude moved {row.get('chg_pct','')} to {row.get('price','')}.
      Watch OMCs (HPCL, BPCL, IOC) and aviation stocks for sector impact at open.
      This may widen BankNifty's intraday range — factor into stop sizing.
    </div>
  </div>"""
            except:
                pass
            break

    vix_val = data.get("india_vix",{}).get("price","—")
    try:
        if float(vix_val) > 22 and not macro_alert:
            macro_alert = f"""
  <div class="alert-box warning">
    <div class="alert-title">⚠ Elevated Volatility — India VIX at {vix_val}</div>
    <div class="alert-body">
      India VIX is above 22, indicating elevated intraday volatility.
      Consider wider stops and reduced position sizing today.
    </div>
  </div>"""
    except:
        pass

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Hawala v2 — Pre-Market Report | {_e(date_str)}</title>
  <style>{CSS}
  .alert {{ padding:10px 14px; margin:8px 0 14px; border-radius:6px;
            font-size:13px; line-height:1.5; }}
  .alert b {{ font-weight:700; }}
  .alert ul {{ list-style:disc; }}
  .alert-green  {{ background:#0d3a1d; color:#cfe8d8; border:1px solid #1f8048; }}
  .alert-yellow {{ background:#3d3110; color:#f0d99b; border:1px solid #b58a2c; }}
  .alert-red    {{ background:#3e1414; color:#f5c2c2; border:1px solid #c63838; }}
  </style>
</head>
<body>

<!-- ── HEADER ──────────────────────────────────────────────────── -->
<div class="header">
  <div class="header-left">
    <h1><span>Hawala</span> v2 — Pre-Market Intelligence</h1>
    <p>Automated Daily Briefing</p>
  </div>
  <div class="header-right">
    <div class="date-badge">{_e(date_str)}</div>
    <div class="gen-time">Generated at {_e(gen_at)} · Data cutoff {_e(gen_at)}</div>
  </div>
</div>

<div class="container">

{_healthcheck_block()}

{macro_alert}

{_snap_cards(data)}

  <!-- ── INDIA + GLOBAL ──────────────────────────────────────── -->
  <div class="section-title">India Indices — Previous Close ({_e(prev_day)})</div>
  <div class="two-col">
{_india_table(data)}
{_global_table(data)}
  </div>

  <!-- ── COMMODITIES + CURRENCY ──────────────────────────────── -->
  <div class="two-col">
{_commodities_table(data)}
{_currency_crypto_table(data)}
  </div>

{_sentiment_row(data)}

<div class="page-break-before"></div>
{_signal_grid(data)}

{_scenario_box(data)}

{_fii_dii_section(data)}

{_nifty_levels_section(data)}

<div class="page-break-before"></div>
{_oi_chart_section(data)}

{_news_section(data)}

{_events_calendar(data)}

{_data_freshness_section(data)}

</div>

<!-- ── FOOTER ─────────────────────────────────────────────────── -->
<div class="footer">
  <div><span class="logo">Hawala v2</span></div>
  <div>Data: NSE · Yahoo Finance · CoinGecko · CNN Fear &amp; Greed</div>
  <div>Auto-generated at {_e(gen_at)} · <span style="color:var(--red)">⚠ Not financial advice</span></div>
</div>

</body>
</html>"""


if __name__ == "__main__":
    import json, sys, pathlib
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            d = json.load(f)
    else:
        from data.fetch_report_data import fetch_all
        d = fetch_all()
    date_iso = d.get("date_iso","test")
    out = pathlib.Path(f"market_report_{date_iso}.html")
    out.write_text(build_html(d))
    print(f"✅  HTML written → {out}")
