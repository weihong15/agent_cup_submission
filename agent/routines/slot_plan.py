"""THE PLAN - what to do with each of the N controller slots, this tick.

Merges held positions (what to unwind) with scan candidates (what to enter) into ONE ordered
allocation, because they compete for the same slot budget. Priority:

    1. URGENT unwinds   - funding has turned against the position
    2. ENTER / ADD      - not split: adding to a held base and opening a new one are the same act
    3. NORMAL unwinds   - only with slots left over, and only worth doing to free margin

Every number here is computed, not judged. The agent executes the plan top-down; it does not
re-rank, re-gate or re-size it.

THE EXIT THRESHOLD IS CARRY-ADJUSTED, which is the one piece of real arithmetic:

    exit_threshold_bps = base_exit_bps + carry_bps

`base_exit_bps` is negative - the most we will pay to get out. Carry shifts it by what holding
is worth over the horizon:

  * carry +6 bp (a large settlement is due, often because the legs settle on different clocks)
    -> -10 + 6 = -4 bp. We become PATIENT: only leave on a good price, because waiting pays.
  * carry -6 bp (the position PAYS funding at each settlement)
    -> -10 - 6 = -16 bp. We become AGGRESSIVE: pay more to get out, because waiting costs.

Note this is NOT `fill_guard`'s "bleed", which is a different fault entirely. A bleed is bad
FILLS - the maker filling worse than its hedge - and the guard answers it by flipping or killing
the CONTROLLER. Negative carry is a property of the POSITION and has nothing to do with fills: a
held position with no controller running produces no fills at all, so the guard can never see it.
Stopping a controller does not close a position either - perp_xemm keeps positions on stop - so a
position paying funding persists across ticks until an unwind line is planned for it.

So a position can be flagged urgent on the funding RATE and still be exited patiently, when a
payment is about to land. The two signals do different jobs and are not collapsed.
"""
import asyncio
import importlib.util
import logging
import os
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes
from config_manager import get_client
logger = logging.getLogger(__name__)
CATEGORY = 'Monitoring'
_HERE = os.path.dirname(os.path.abspath(__file__))

def _margin_book():
    """margin_book's shared margin maths - one implementation, two callers."""
    ck = importlib.util.spec_from_file_location('_mb', os.path.join(_HERE, 'margin_book.py'))
    m = importlib.util.module_from_spec(ck)
    ck.loader.exec_module(m)
    return m

def _scanner():
    """Reuse basis_scanner's fetchers so plan and scan cannot drift apart."""
    ck = importlib.util.spec_from_file_location('_bs', os.path.join(_HERE, 'basis_scanner.py'))
    m = importlib.util.module_from_spec(ck)
    ck.loader.exec_module(m)
    return m

class Config(BaseModel):
    """The ordered slot allocation: urgent unwinds, then enters/adds, then normal unwinds."""
    slots: int = Field(default=6, description='Total concurrent controller budget')
    account_name: str = Field(default='master_account')
    maker_venue_hl: str = Field(default='hyperliquid_perpetual')
    taker_venue_bin: str = Field(default='binance_perpetual')
    min_fundsig_bph: float = Field(default=0.3, description='Entry funding gate, bp/h')
    min_edge_bps: float = Field(default=0.0, description='Entry execution floor, bp')
    min_volume_usd: float = Field(default=1500000.0)
    urgent_fundsig_bph: float = Field(default=-0.1, description='Held position with fundsig below this = URGENT unwind')
    exit_bps_at_full_margin: float = Field(default=-6.0, description='Exit floor when margin is plentiful (health 100%): we can afford to be picky.')
    exit_bps_at_no_margin: float = Field(default=-16.0, description='Exit floor when margin is exhausted (health 0%): we must get out, and pay for it.')
    entry_bps_at_full_margin: float = Field(default=10.0, description='Entry edge floor when margin is plentiful (health 100%): idle capital is worth deploying, so take a decent edge rather than hold out.')
    entry_bps_at_no_margin: float = Field(default=22.0, description='Entry edge floor when margin is nearly gone (health 0%): the last dollars only go to an exceptional edge.')
    margin_floor_pct: float = Field(default=0.1, description='Below this margin health on the BINDING venue, plan no new entries - unwinds only.')
    size_usd: float = Field(default=150.0, description='Baseline notional per entry, in QUOTE currency (USD). Emitted as a ready total_amount in base units, already rounded to whole min_notional slices.')
    size_scaled_usd: float = Field(default=900.0, description='The scaled-up notional, used when a pair has already realized well.')
    min_notional: float = Field(default=15.0, description='Per-order minimum in quote. total_amount is rounded DOWN to a whole number of these, so a partial fill can never strand an unplaceable tail.')
    max_base_notional_quote: float = Field(default=0.0, description='Optional cap on TOTAL notional per base (0 = uncapped, the default). Left OFF deliberately: a base that keeps qualifying is a winner, and capping it would force capital into worse candidates. Margin, not a per-base cap, is what bounds exposure.')
    dust_usd: float = Field(default=5.0)
    only_bases: list[str] = Field(default=[], description='HARD ALLOWLIST. When set, the plan may touch ONLY these bases - for entries AND unwinds. Everything else is excluded whatever its classification. Use this on any account that holds positions this run did not open.')
    max_notional_per_line: float = Field(default=0.0, description="Refuse to plan an unwind larger than this notional (0 = no cap). A blunt stop against acting on a position far bigger than this run's own size.")

def _margin_scaled_bps(bi: float, af: float, ae: float) -> float:
    """Linear interpolation of a bps floor against margin health. Used for BOTH floors.

    Exit:  health 1.0 -> -6 bp (picky, we are not forced) ... 0.0 -> -16 bp (pay up, get out)
    Entry: health 1.0 -> +10 bp (deploy idle capital)     ... 0.0 -> +22 bp (last dollars only
           go to an exceptional edge)

    The two run in OPPOSITE directions on purpose. As margin drains, leaving gets easier to
    justify and entering gets harder, so the book drifts toward flat by itself instead of
    slamming into `margin_floor_pct` and stopping dead.

    For the exit, margin sets URGENCY and the carry adjustment applied afterwards sets
    PATIENCE - independent: a funding-negative position on a full account should still not
    overpay to leave, and a well-carrying position on a starved account still has to go.
    """
    h = min(1.0, max(0.0, bi))
    return ae + (af - ae) * h

def _pair_for(ag: str, av: str) -> str:
    """The venue's own trading-pair spelling. Hyperliquid quotes USD, Binance USDT-M quotes USDT."""
    return f'{ag}-USD' if 'hyperliquid' in av else f'{ag}-USDT'

def _mark_from(ca: dict) -> float | None:
    try:
        ad, ay = (float(ca.get('amount') or 0), float(ca.get('entry_price') or 0))
        cr = float(ca.get('unrealized_pnl') or 0)
        return ay + cr / ad if ad and ay > 0 else None
    except Exception:
        return None

async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    an = _scanner()
    client = await get_client(context._chat_id, context=context)
    if not client:
        return 'No server available'
    import aiohttp
    ba = []
    async with aiohttp.ClientSession(timeout=an.TIMEOUT) as ce:
        (bl, bo), (ak, al) = await asyncio.gather(an._hl_all(ce), an._bin_all(ce))
    ba += [e for e in (bo, al) if e]
    try:
        cl = await client.portfolio.get_state()
    except Exception as e:
        cl = {}
        ba.append(f'portfolio.get_state: {e}')
    try:
        cd = await client.trading.get_positions()
        cb = cd.get('data', cd) if isinstance(cd, dict) else cd or []
    except Exception as e:
        return '\n'.join(['SLOT PLAN   REFUSED', '', f'  !! positions unreadable: {e}', '', '  The plan is not produced when the book cannot be read. Holdings are half of it:', '  an unreadable endpoint is indistinguishable from a flat account, so proceeding', '  would risk re-entering bases already held and would hide every unwind.', '  Restore the connection and re-run. Do NOT deploy from a partial plan.'])
    cv = [config.maker_venue_hl, config.taker_venue_bin]
    bu = _margin_book().compute_venue_margin(cl, cb, cv, config.account_name, config.dust_usd)
    bj = {v: m['health'] for v, m in bu.items()}
    ai = min(bj.values()) if bj else None
    if ai is None:
        ah = config.exit_bps_at_no_margin
    else:
        ah = _margin_scaled_bps(ai, config.exit_bps_at_full_margin, config.exit_bps_at_no_margin)
    az = config.entry_bps_at_no_margin if ai is None else _margin_scaled_bps(ai, config.entry_bps_at_full_margin, config.entry_bps_at_no_margin)
    bk: dict[str, dict] = {}
    for p in cb:
        if p.get('account_name') and p['account_name'] != config.account_name:
            continue
        au = p.get('connector_name')
        if au not in (config.maker_venue_hl, config.taker_venue_bin):
            continue
        bv, ad = (_mark_from(p), float(p.get('amount') or 0))
        if bv is None or not ad:
            continue
        bx = abs(ad) * bv
        if bx < config.dust_usd:
            continue
        ag = (p.get('trading_pair') or '').split('-')[0].upper()
        h = bk.setdefault(ag, {'legs': {}, 'notional': 0.0, 'upnl': 0.0})
        h['legs'][au] = 'LONG' if ad > 0 else 'SHORT'
        h['notional'] += bx
        h['upnl'] += float(p.get('unrealized_pnl') or 0)
    ab = {b.upper() for b in config.only_bases}
    bb = []
    if ab:
        for ag in list(bk):
            if ag.upper() not in ab:
                bb.append(ag)
                bk.pop(ag)
    bf, cp, by = ([], [], {})
    for ag, h in bk.items():
        co = ag in bl and an._bin_symbol(ag) in ak
        if not co:
            bf.append((ag, h))
        elif len(h['legs']) < 2:
            cp.append((ag, h))
        else:
            by[ag] = h
    cq = []
    for ag, h in by.items():
        bp, aj = (bl.get(ag), ak.get(an._bin_symbol(ag)))
        ch = h['legs'].get(config.maker_venue_hl)
        cg = h['legs'].get(config.taker_venue_bin)
        if not bp or not aj:
            cq.append({'base': ag, 'urgent': False, 'fundsig': None, 'carry': None, 'exit_bps': ah, 'why': 'no funding data', 'notional': h['notional'], 'upnl': h['upnl'], 'legs': h['legs']})
            continue
        if ch and cg and (ch != cg):
            cf = ch == 'SHORT'
        elif ch:
            cf = ch == 'SHORT'
        else:
            cf = cg == 'LONG'
        be, bd = (bp['funding_bph'], aj['funding_bph'])
        bh = be - bd if cf else bd - be
        am = float(aj.get('interval_h') or 8.0)
        bq = max(1.0, am)
        import time as _t
        ao, _ = an._leg_carry_bps(bp['rate_per_print'], 1.0, 3600.0 - _t.time() % 3600.0, bq, cf, 1.0)
        ap, _ = an._leg_carry_bps(aj['rate_per_print'], am, max(0.0, (aj.get('next_print_min') or 0) * 60.0), bq, not cf, 1.0)
        ar = ao + ap
        bc = ah + ar
        cs = bh < config.urgent_fundsig_bph
        cq.append({'base': ag, 'urgent': cs, 'fundsig': bh, 'carry': ar, 'exit_bps': bc, 'notional': h['notional'], 'upnl': h['upnl'], 'legs': h['legs'], 'short_hl': cf, 'why': 'funding turned against us' if cs else 'carrying fine'})
    aq = config.max_notional_per_line
    if aq:
        cq = [u for u in cq if u['notional'] <= aq]
    cs = sorted([u for u in cq if u['urgent']], key=lambda u: u['fundsig'] or 0)
    bw = sorted([u for u in cq if not u['urgent']], key=lambda u: u['carry'] or 0)
    at = an.Config(min_fundsig_bph=config.min_fundsig_bph, min_edge_bps=config.min_edge_bps, min_volume_usd=config.min_volume_usd, slots=config.slots)
    ax = []
    for ag in [b for b in bl if an._bin_symbol(b) in ak and an._symbol_ok(b)[0]]:
        bp, aj = (bl[ag], ak[an._bin_symbol(ag)])
        if not bp['bid'] or not bp['ask']:
            continue
        if bp['volume_usd'] < at.min_volume_usd or aj['volume_usd'] < at.min_volume_usd:
            continue
        bg = an._all_four(bp, aj)
        v, s, aw = bg[0]
        cf = (v == 'HL') == (s == 'SELL')
        be, bd = (bp['funding_bph'], aj['funding_bph'])
        bh = be - bd if cf else bd - be
        am = float(aj.get('interval_h') or 8.0)
        bq = max(1.0, am)
        import time as _t
        ao, _ = an._leg_carry_bps(bp['rate_per_print'], 1.0, 3600.0 - _t.time() % 3600.0, bq, cf, 1.0)
        ap, _ = an._leg_carry_bps(aj['rate_per_print'], am, max(0.0, (aj.get('next_print_min') or 0) * 60.0), bq, not cf, 1.0)
        ar = ao + ap
        if ab and ag.upper() not in ab:
            continue
        if ai is not None and ai < config.margin_floor_pct:
            continue
        if config.max_base_notional_quote:
            ac = bk.get(ag, {}).get('notional', 0.0)
            if ac + config.size_usd > config.max_base_notional_quote:
                continue
        if bh >= at.min_fundsig_bph and ar > 0 and (aw >= az):
            bt = config.maker_venue_hl if v == 'HL' else config.taker_venue_bin
            cm = config.taker_venue_bin if v == 'HL' else config.maker_venue_hl
            cc = (bp if v == 'HL' else aj)['mark'] or 0.0

            def _amt(ct):
                ci = max(1, int(ct // config.min_notional))
                return ci * config.min_notional / cc if cc else 0.0
            ax.append({'px': cc, 'amount_base': _amt(config.size_usd), 'amount_scaled': _amt(config.size_scaled_usd), 'base': ag, 'dir': f'{v}.{s}', 'edge': aw, 'carry': ar, 'fundsig': bh, 'held': ag in bk, 'maker_connector': bt, 'maker_trading_pair': _pair_for(ag, bt), 'taker_connector': cm, 'taker_trading_pair': _pair_for(ag, cm), 'maker_side_str': s, 'entry_floor_bps': az})
    ax.sort(key=lambda r: -r['carry'])
    bz, cu = ([], 0)
    for u in cs:
        if cu >= config.slots:
            break
        bz.append(('UNWIND-URGENT', u))
        cu += 1
    for e in ax:
        if cu >= config.slots:
            break
        bz.append(('ADD' if e['held'] else 'ENTER', e))
        cu += 1
    for u in bw:
        if cu >= config.slots:
            break
        bz.append(('UNWIND-NORMAL', u))
        cu += 1
    L = [f'SLOT PLAN   {cu}/{config.slots} slots allocated']
    if ab:
        L += ['', f'  ALLOWLIST ACTIVE: only {', '.join(sorted(ab))} may be touched.', f'  {len(bb)} held base(s) excluded by it and untouchable' + (f': {', '.join(bb[:10])}' if bb else '') + ('...' if len(bb) > 10 else '')]
    if cp:
        cn = sum((h['notional'] for _, h in cp))
        L += ['', f'  !! UNHEDGED: {len(cp)} position(s) on tradeable bases, ${cn:,.0f} notional - {', '.join((b for b, _ in cp[:8]))}' + (' ...' if len(cp) > 8 else ''), '  One leg only on a base this strategy CAN trade. In a DEDICATED account (the race)', '  that means one thing: our own failed hedge - naked directional risk on a book that', '  should be flat. Re-hedge or close it before opening anything new; there is no', '  operator during the race.', f'  CAVEAT on a shared account: only {', '.join((v.split('_')[0] for v in cv))} are', '  read here, so a leg hedged on any OTHER venue is invisible and shows up in this', '  list. Confirm against the full venue set before acting on a dev box.']
    if bf:
        cn = sum((h['notional'] for _, h in bf))
        L += ['', f'  FOREIGN: {len(bf)} position(s), ${cn:,.0f} notional - {', '.join((b for b, _ in bf[:8]))}' + (' ...' if len(bf) > 8 else ''), '  On bases NOT listed on both venues, so this strategy could never have opened', '  them. EXCLUDED from the plan and never traded.']
    if ba:
        L += ['', '!! ' + ' | '.join(ba)]
    if ai is not None and ai < config.margin_floor_pct:
        L += ['', f'  !! MARGIN {ai * 100:.0f}% < floor {config.margin_floor_pct * 100:.0f}% - NO new entries this tick, unwinds only.']
    L += ['', f'  order: urgent unwinds -> enter/add -> normal unwinds', f'  urgent when fundsig < {config.urgent_fundsig_bph} bp/h.  entry gate fundsig >= {config.min_fundsig_bph}, carry > 0.', f'  entry floor {az:.1f} bp - slides {config.entry_bps_at_full_margin:.0f} (free) -> {config.entry_bps_at_no_margin:.0f} (starved)', f'  exit_bps = margin_base {ah:+.1f} + carry.  margin_base scales {config.exit_bps_at_full_margin:+.0f} (free) -> {config.exit_bps_at_no_margin:+.0f} (starved)', f"  on the BINDING venue's health" + (f' ({ai * 100:.0f}%: ' + ', '.join((f'{v.split('_')[0]} {h * 100:.0f}%' for v, h in bj.items())) + ')' if ai is not None else ' (UNREADABLE -> assumed starved)') + '. Carry then adds patience.', '', f'  {'slot':<6}{'action':<15}{'base':<10}{'dir/legs':<18}{'carry':>8}{'fundsig':>9}{'edge':>7}{'exit_bps':>10}{'notional':>11}']
    for i, (aa, r) in enumerate(bz, 1):
        if aa.startswith('UNWIND'):
            br = '+'.join((f'{s[0]}{v.split('_')[0][:3]}' for v, s in r['legs'].items()))
            c = f'{r['carry']:>8.3f}' if r['carry'] is not None else f'{'-':>8}'
            f = f'{r['fundsig']:>9.3f}' if r['fundsig'] is not None else f'{'-':>9}'
            L.append(f'  {i:<6}{aa:<15}{r['base']:<10}{br:<18}{c}{f}{'-':>7}{r['exit_bps']:>10.1f}{r['notional']:>11,.0f}')
        else:
            L.append(f'  {i:<6}{aa:<15}{r['base']:<10}{r['dir']:<18}{r['carry']:>8.3f}{r['fundsig']:>9.3f}{r['edge']:>7.1f}{'-':>10}{'-':>11}')
            L.append(f'        -> maker {r['maker_connector']} {r['maker_trading_pair']} {r['maker_side_str']} | taker {r['taker_connector']} {r['taker_trading_pair']}')
            L.append(f'        -> min_price_edge_bps {r['entry_floor_bps']:.1f} (rest floor)')
            L.append(f'        -> total_amount {r['amount_base']:.6g} base (${config.size_usd:.0f}) | scaled {r['amount_scaled']:.6g} (${config.size_scaled_usd:.0f}) @ {r['px']:.6g}')
    if not bz:
        L.append('  (nothing to do - no urgent unwind, no candidate clears the gate)')
    cj = config.slots - cu
    L.append('')
    L.append(f'  {len(cs)} urgent unwind(s), {len(ax)} entry candidate(s), {len(bw)} normal unwind(s) available. {cj} slot(s) idle.')
    if cj and bw:
        L.append('  Idle slots with normal unwinds available means the plan ran out of BOTH candidates and holdings - check for a data error.')
    return '\n'.join(L)
