"""
alerts/vp_signal_alert.py — Format a Telegram alert for a volume-profile
signal event.

The strategy emits a SIGNAL (not a trade) — direction + key levels +
context. Execution (target, stop, trail) is left to the discretionary
trader. The alert is therefore informative, not prescriptive.

Usage from alert_runner.py:

    from alerts.vp_signal_alert import format_signal_alert
    from alerts.telegram import send

    text = format_signal_alert(signal_row, instrument='SENSEX',
                               chart_url='file:///.../trade_explorer.html')
    for cid in TG_CHAT_IDS:
        send(TG_TOKEN, cid, text)

Telegram parse_mode is HTML — keep <b>, <i>, <code>, <a> tags.
"""

from __future__ import annotations

import pandas as pd


_DIR_EMOJI = {'LONG': '🟢⬆️', 'SHORT': '🔴⬇️'}


def _fmt_price(x: float) -> str:
    return f'{x:,.0f}' if abs(x) >= 1000 else f'{x:.2f}'


def format_signal_alert(signal: dict | pd.Series,
                        instrument: str,
                        chart_url: str | None = None) -> str:
    """
    Build the Telegram-ready (HTML parse_mode) alert text.

    `signal` is one row from the DataFrame produced by
    `run_volume_profile(..., signals_only=True)` — either a Series or a
    plain dict.
    """
    s = (signal.to_dict() if isinstance(signal, pd.Series) else dict(signal))

    direction   = s.get('direction', 'LONG')
    fade_kind   = s.get('fade_kind', '?')
    px          = float(s.get('signal_price', 0))
    pierce_ext  = float(s.get('pierce_extreme', 0))
    pierce_pts  = float(s.get('pierce_pts', 0))
    pierce_atr  = float(s.get('pierce_atr', 0))
    bias        = float(s.get('bias_score', 0))
    vah         = float(s.get('vah', 0))
    val         = float(s.get('val', 0))
    poc         = float(s.get('poc', 0))
    atr14       = float(s.get('atr14', 0))
    profile     = s.get('profile_used', 'full')
    regime_st   = s.get('regime_start')
    contract    = s.get('contract', '')
    ts          = pd.Timestamp(s.get('ts'))

    # Distances the trader cares about
    if direction == 'LONG':       # downside thrust failed
        dist_to_poc    = poc - px
        dist_to_vah    = vah - px
        risk_to_pierce = px - pierce_ext       # (positive)
    else:                         # upside thrust failed (SHORT)
        dist_to_poc    = px - poc
        dist_to_val    = px - val
        risk_to_pierce = pierce_ext - px

    emoji = _DIR_EMOJI.get(direction, '')
    profile_tag = ('<b>SUB</b>' if profile == 'sub' else 'full')
    regime_line = ''
    if profile == 'sub' and regime_st and not pd.isna(regime_st):
        regime_line = (f'\n<i>Regime sub-profile active since '
                       f'{pd.Timestamp(regime_st).date()}</i>')

    levels_line = (
        f'<b>VAH</b> {_fmt_price(vah)} · '
        f'<b>POC</b> {_fmt_price(poc)} · '
        f'<b>VAL</b> {_fmt_price(val)}'
    )

    # Build the body
    head = (f'{emoji} <b>{instrument}</b>  {fade_kind}  '
            f'<i>{direction}</i>  '
            f'@ <code>{_fmt_price(px)}</code>')
    sub  = (f'{ts:%Y-%m-%d %H:%M}  ·  {contract}  ·  profile={profile_tag}'
            f'{regime_line}')

    setup = (f'\n<b>Setup</b>: pierce {_fmt_price(pierce_pts)} pts beyond '
             f"{'VAL' if direction=='LONG' else 'VAH'} "
             f'(<code>{pierce_atr:.2f}×ATR</code>, bias {bias:.2f})  '
             f'extreme @ <code>{_fmt_price(pierce_ext)}</code>')
    levels = f'\n<b>Levels</b>: {levels_line}  ·  ATR14 ≈ {_fmt_price(atr14)}'

    if direction == 'LONG':
        guide = (f'\n<b>Targets</b>: VAH {_fmt_price(vah)} '
                 f'(+{_fmt_price(dist_to_vah)})  ·  '
                 f'POC {_fmt_price(poc)} (+{_fmt_price(dist_to_poc)})')
    else:
        guide = (f'\n<b>Targets</b>: VAL {_fmt_price(val)} '
                 f'(-{_fmt_price(dist_to_val)})  ·  '
                 f'POC {_fmt_price(poc)} (-{_fmt_price(dist_to_poc)})')

    risk = (f'\n<b>Stop reference</b>: pierce extreme '
            f'<code>{_fmt_price(pierce_ext)}</code> '
            f'(risk {_fmt_price(risk_to_pierce)} pts)')

    note = ('\n<i>Discretionary execution — pick your own size, '
            'stop, target, trail. This is a directional signal only.</i>')

    chart = (f'\n<a href="{chart_url}">📊 view chart</a>'
             if chart_url else '')

    return head + '\n' + sub + setup + levels + guide + risk + note + chart


# ── Self-test / sample render ─────────────────────────────────────────────────

def _sample() -> str:
    """Render an example alert from the SENSEX Apr 30 signal."""
    sample = {
        'ts':            '2026-04-30 12:00:00',
        'direction':     'LONG',
        'fade_kind':     'FADE_DOWN',
        'signal_price':  76649,
        'pierce_extreme':76273,
        'pierce_pts':    627,
        'pierce_atr':    0.56,
        'bias_score':    0.56,
        'vah':           78250,
        'val':           76900,
        'poc':           77188,
        'atr14':         1115,
        'profile_used':  'sub',
        'regime_start':  '2026-04-08',
        'contract':      'BSE-SENSEX-30Apr26-FUT',
    }
    return format_signal_alert(sample, instrument='SENSEX',
                               chart_url='file:///path/to/trade_explorer.html')


if __name__ == '__main__':
    print(_sample())
