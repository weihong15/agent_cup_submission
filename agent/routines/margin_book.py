"""Margin and position book - per venue free margin, hedged-pair grouping, unwind priority.

The binding constraint in this strategy is MARGIN, not cost. Positions are held for days and
closing is expensive, so margin is only released by unwinding something. This routine reports
how much room is left and, only when that room is short, what closing would buy.

Free margin is computed rather than trusted:

    initial_margin_i = |amount_i| x mark_i / leverage_i
    free             = equity - sum(initial_margin_i)
    health           = free / equity

The venue's own "available" figure is shown alongside it. They should agree; a persistent
divergence means unrealized PnL or a venue-side margin rule is doing something the simple
formula does not model, and the SMALLER of the two is the one to trust.

Facts only. No sizing, no ranking of what to enter - the agent decides.
"""
import logging
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes
from config_manager import get_client
logger = logging.getLogger(__name__)
CATEGORY = 'Monitoring'

class Config(BaseModel):
    """Per-venue free margin, hedged-pair grouping, and unwind priority when margin is short."""
    account_name: str = Field(default='master_account', description='Account to report on')
    venues: list[str] = Field(default=['hyperliquid_perpetual', 'binance_perpetual'], description='Venues in the race. Others are reported as FOREIGN, never acted on.')
    margin_floor_pct: float = Field(default=0.1, description='Free margin below this fraction of equity = prioritize unwinding')
    dust_usd: float = Field(default=5.0, description='Ignore positions below this notional')
    imbalance_tol: float = Field(default=0.25, description='Leg imbalance below this is not worth reporting. A perp_xemm book is imbalanced by construction while a session is mid-fill.')

def _mark_from(bm: dict) -> float | None:
    """Current mark, derived from entry and unrealized PnL - no extra API call.

    unrealized = (mark - entry) x amount, with amount SIGNED, so this holds for both sides.
    """
    try:
        ab = float(bm.get('amount') or 0)
        am = float(bm.get('entry_price') or 0)
        cb = float(bm.get('unrealized_pnl') or 0)
        if ab == 0 or am <= 0:
            return None
        return am + cb / ab
    except Exception:
        return None

def _base_of(bz: str) -> str:
    return (bz or '').split('-')[0].upper()

def compute_venue_margin(bw, bn, venues, account_name, dust_usd=5.0):
    """{venue: {equity, used_im, computed, venue_avail, effective, health}} for `venues`.

    Shared with slot_plan so the margin number that drives the exit threshold is the SAME one
    reported here - two implementations of this would drift and nobody would notice until an
    exit priced itself off a figure the operator never saw.

        initial_margin_i = |amount_i| x mark_i / leverage_i
        computed         = equity - SUM(initial_margin_i)
        effective        = min(computed, venue_reported_available)
        health           = effective / equity

    `effective` takes the smaller because the two disagree in BOTH directions on live venues:
    Hyperliquid charges unrealized losses this formula ignores (venue < computed), Binance can
    report cross-margin availability above this venue's equity (venue > computed). The lower
    one is what actually stops a fill.

    A venue absent from the result is UNREADABLE, which is not the same as flat.
    """
    ao, ae = ({}, {})
    for aa, ak in (bw or {}).items():
        if aa != account_name:
            continue
        for aj, by in (ak or {}).items():
            an = ad = 0.0
            for t in by or []:
                bo = float(t.get('price') or 0)
                an += float(t.get('value') or 0)
                ac = t.get('available_units')
                if ac is not None:
                    ad += float(ac) * bo
            ao[aj] = an
            ae[aj] = ad
    cc: dict[str, float] = {}
    for p in bn or []:
        if p.get('account_name') and p['account_name'] != account_name:
            continue
        aj = p.get('connector_name')
        if aj not in venues:
            continue
        bg, ab = (_mark_from(p), float(p.get('amount') or 0))
        if bg is None or not ab:
            continue
        bj = abs(ab) * bg
        if bj < dust_usd:
            continue
        bd = float(p.get('leverage') or 0) or 1.0
        cc[aj] = cc.get(aj, 0.0) + bj / bd
    bk = {}
    for v in venues:
        an = ao.get(v)
        if an is None:
            continue
        u = cc.get(v, 0.0)
        ai = an - u
        cd = ae.get(v)
        al = ai if cd is None else min(ai, cd)
        bk[v] = {'equity': an, 'used_im': u, 'computed': ai, 'venue_avail': cd, 'effective': al, 'health': al / an if an else 0.0}
    return bk

async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    client = await get_client(context._chat_id, context=context)
    if not client:
        return 'No server available'
    ap = []
    try:
        bw = await client.portfolio.get_state()
    except Exception as e:
        bw = {}
        ap.append(f'portfolio.get_state: {e}')
    try:
        bq = await client.trading.get_positions()
        bn = bq.get('data', bq) if isinstance(bq, dict) else bq or []
    except Exception as e:
        bn = []
        ap.append(f'trading.get_positions: {e}')
    bf = compute_venue_margin(bw, bn, config.venues, config.account_name, config.dust_usd)
    ao = {v: m['equity'] for v, m in bf.items()}
    ae = {v: m['venue_avail'] for v, m in bf.items()}
    bl: dict[str, list] = {}
    ag: dict[str, list] = {}
    for p in bn:
        if p.get('account_name') and p['account_name'] != config.account_name:
            continue
        aj = p.get('connector_name') or '?'
        bg = _mark_from(p)
        ab = float(p.get('amount') or 0)
        if bg is None or ab == 0:
            continue
        bj = abs(ab) * bg
        if bj < config.dust_usd:
            continue
        bd = float(p.get('leverage') or 0) or 1.0
        bs = {'venue': aj, 'pair': p.get('trading_pair'), 'base': _base_of(p.get('trading_pair')), 'side': 'LONG' if ab > 0 else 'SHORT', 'amount': ab, 'notional': bj, 'leverage': bd, 'im': bj / bd, 'upnl': float(p.get('unrealized_pnl') or 0)}
        bl.setdefault(aj, []).append(bs)
        ag.setdefault(bs['base'], []).append(bs)
    br = {v for v in config.venues if v in ao or v in bl}
    ca = [v for v in config.venues if v not in br]
    L = ['MARGIN BOOK']
    if ap:
        L += ['', '!! ' + ' | '.join(ap)]
    if ca:
        L += ['', f'  !! UNREADABLE VENUE: {', '.join(ca)}', '     No equity and no positions came back. Either credentials are rejected', '     (Binance answers -2015 on a non-whitelisted IP) or the connector is', '     disconnected. Hedge state CANNOT be judged while a leg is invisible:', '     treat the book as unknown, enter nothing, and fix the connection first.']
    L += ['', '  computed = equity - SUM(notional/leverage).  EFFECTIVE = min(computed, venue).', '']
    L.append(f'  {'venue':<24}{'equity':>11}{'used_IM':>10}{'computed':>11}{'venue':>11}{'EFFECTIVE':>12}{'health':>9}{'#pos':>6}')
    cf = {}
    for v in config.venues:
        bt = bl.get(v, [])
        an = ao.get(v)
        cc = sum((r['im'] for r in bt))
        if an is None:
            L.append(f'  {v:<24}{'NO DATA':>11}{cc:>10,.0f}{'-':>11}{'-':>11}{'-':>12}{'-':>9}{len(bt):>6}')
            continue
        ai = an - cc
        ce = ae.get(v)
        al = ai if ce is None else min(ai, ce)
        aw = al / an if an else 0.0
        cf[v] = aw
        aq = '  <-- LOW' if aw < config.margin_floor_pct else ''
        L.append(f'  {v:<24}{an:>11,.0f}{cc:>10,.0f}{ai:>11,.0f}{(ce if ce is not None else 0):>11,.0f}{al:>12,.0f}{aw * 100:>8.1f}%{len(bt):>6}{aq}')
    ar = [v for v in bl if v not in config.venues]
    if ar:
        L.append('')
        for v in ar:
            bt = bl[v]
            L.append(f'  FOREIGN {v}: {len(bt)} positions, ${sum((r['notional'] for r in bt)):,.0f} notional - not part of this run, never act on it')
    bp = {b: bu for b, bu in ag.items() if any((r['venue'] in config.venues for r in bu))}
    L += ['', f'  POSITIONS BY BASE ({len(bp)} on race venues)', '']
    L.append(f'  {'base':<9}{'legs':<6}{'net_notional':>14}{'gross':>12}{'IM':>11}{'upnl':>11}  state')
    bh = []
    for af, bu in sorted(bp.items(), key=lambda ba: -sum((r['notional'] for bb in [ba] for r in ba[1]))):
        az = [r for r in bu if r['venue'] in config.venues]
        bi = sum((r['notional'] * (1 if r['side'] == 'LONG' else -1) for r in az))
        at = sum((r['notional'] for r in az))
        ax = sum((r['im'] for r in az))
        cb = sum((r['upnl'] for r in az))
        bv = {r['venue']: r['side'] for r in az}
        if ca:
            bx = f'unknown - {', '.join((v.split('_')[0] for v in ca))} unreadable'
        elif len(az) == 1:
            bx = f'NAKED on {az[0]['venue'].split('_')[0]} - unhedged'
            bh.append(af)
        elif len(set(bv.values())) == 1:
            bx = 'SAME SIDE both venues - NOT hedged'
            bh.append(af)
        else:
            ay = abs(bi) / at if at else 0
            bx = 'hedged' if ay < config.imbalance_tol else f'hedged, {ay * 100:.0f}% SKEWED'
        L.append(f'  {af:<9}{len(az):<6}{bi:>14,.0f}{at:>12,.0f}{ax:>11,.0f}{cb:>11,.0f}  {bx}')
    if bh:
        L += ['', f'  !! UNHEDGED: {', '.join(bh)} - a single leg is a directional bet.']
        L.append('     Do not size into these. Report to the operator; do not hand-hedge.')
        L.append('     (Only fully unpaired legs are listed. A partial fill skew is expected')
        L.append(f'      mid-session and is not reported below {config.imbalance_tol * 100:.0f}%.)')
    be = [v for v, h in cf.items() if h < config.margin_floor_pct]
    L.append('')
    if ca:
        L.append(f'  UNWIND: cannot judge - {', '.join(ca)} unreadable, so neither')
        L.append('  margin nor hedge state is known. Restore the connection before deciding.')
    elif not be:
        L.append(f'  UNWIND: not needed. Every race venue is above the {config.margin_floor_pct * 100:.0f}% floor.')
        L.append('  Closing costs the spread twice and frees margin you do not need yet -')
        L.append('  only unwind for margin, or to free a slot for a clearly better pair.')
    else:
        L.append(f'  UNWIND PRIORITY - margin short on: {', '.join(be)}')
        L.append('  Ranked by margin freed. Closing a hedged base means closing BOTH legs;')
        L.append('  closing one leg leaves a directional position, which is worse than being full.')
        L.append('')
        L.append(f'  {'base':<9}{'IM_freed':>11}{'gross':>12}{'upnl':>11}  legs')
        ah = []
        for af, bu in bp.items():
            az = [r for r in bu if r['venue'] in config.venues]
            if any((r['venue'] in be for r in az)):
                ah.append((af, sum((r['im'] for r in az)), sum((r['notional'] for r in az)), sum((r['upnl'] for r in az)), az))
        for af, ax, at, cb, az in sorted(ah, key=lambda x: -x[1])[:10]:
            bc = ' + '.join((f'{r['side'][0]}{r['venue'].split('_')[0][:3]}' for r in az))
            L.append(f'  {af:<9}{ax:>11,.0f}{at:>12,.0f}{cb:>11,.0f}  {bc}')
    return '\n'.join(L)
