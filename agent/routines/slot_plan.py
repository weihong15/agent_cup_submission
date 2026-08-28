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
    ch = importlib.util.spec_from_file_location('_mb', os.path.join(_HERE, 'margin_book.py'))
    m = importlib.util.module_from_spec(ch)
    ch.loader.exec_module(m)
    return m

def _scanner():
    """Reuse basis_scanner's fetchers so plan and scan cannot drift apart."""
    ch = importlib.util.spec_from_file_location('_bs', os.path.join(_HERE, 'basis_scanner.py'))
    m = importlib.util.module_from_spec(ch)
    ch.loader.exec_module(m)
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
    margin_floor_pct: float = Field(default=0.1, description='Below this margin health on the BINDING venue, plan no new entries - unwinds only.')
    size_hint_quote: float = Field(default=40.0, description='The size an entry would deploy. Used only to test a base against max_base_notional_quote before proposing it.')
    max_base_notional_quote: float = Field(default=0.0, description='Optional cap on TOTAL notional per base (0 = uncapped, the default). Left OFF deliberately: a base that keeps qualifying is a winner, and capping it would force capital into worse candidates. Margin, not a per-base cap, is what bounds exposure.')
    dust_usd: float = Field(default=5.0)
    only_bases: list[str] = Field(default=[], description='HARD ALLOWLIST. When set, the plan may touch ONLY these bases - for entries AND unwinds. Everything else is excluded whatever its classification. Use this on any account that holds positions this run did not open.')
    max_notional_per_line: float = Field(default=0.0, description="Refuse to plan an unwind larger than this notional (0 = no cap). A blunt stop against acting on a position far bigger than this run's own size.")

def _margin_exit_bps(bh: float, af: float, ae: float) -> float:
    """Interpolate the exit floor linearly from margin health.

        health 1.0 (plenty free)  -> at_full   (-6 bp: picky, we are not forced)
        health 0.0 (none free)    -> at_empty  (-16 bp: pay up, we need the margin back)

    Margin sets URGENCY. The carry adjustment applied afterwards sets PATIENCE. They are
    independent: a funding-negative position on a full account should still not overpay to leave, and a
    well-carrying position on a starved account still has to go.
    """
    h = min(1.0, max(0.0, bh))
    return ae + (af - ae) * h

def _pair_for(ag: str, av: str) -> str:
    """The venue's own trading-pair spelling. Hyperliquid quotes USD, Binance USDT-M quotes USDT."""
    return f'{ag}-USD' if 'hyperliquid' in av else f'{ag}-USDT'

def _mark_from(bz: dict) -> float | None:
    try:
        ad, ay = (float(bz.get('amount') or 0), float(bz.get('entry_price') or 0))
        co = float(bz.get('unrealized_pnl') or 0)
        return ay + co / ad if ad and ay > 0 else None
    except Exception:
        return None

async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    an = _scanner()
    client = await get_client(context._chat_id, context=context)
    if not client:
        return 'No server available'
    import aiohttp
    az = []
    async with aiohttp.ClientSession(timeout=an.TIMEOUT) as cc:
        (bk, bl), (ak, al) = await asyncio.gather(an._hl_all(cc), an._bin_all(cc))
    az += [e for e in (bl, al) if e]
    try:
        ci = await client.portfolio.get_state()
    except Exception as e:
        ci = {}
        az.append(f'portfolio.get_state: {e}')
    try:
        cb = await client.trading.get_positions()
        ca = cb.get('data', cb) if isinstance(cb, dict) else cb or []
    except Exception as e:
        return '\n'.join(['SLOT PLAN   REFUSED', '', f'  !! positions unreadable: {e}', '', '  The plan is not produced when the book cannot be read. Holdings are half of it:', '  an unreadable endpoint is indistinguishable from a flat account, so proceeding', '  would risk re-entering bases already held and would hide every unwind.', '  Restore the connection and re-run. Do NOT deploy from a partial plan.'])
    cr = [config.maker_venue_hl, config.taker_venue_bin]
    bt = _margin_book().compute_venue_margin(ci, ca, cr, config.account_name, config.dust_usd)
    bi = {v: m['health'] for v, m in bt.items()}
    ai = min(bi.values()) if bi else None
    if ai is None:
        ah = config.exit_bps_at_no_margin
    else:
        ah = _margin_exit_bps(ai, config.exit_bps_at_full_margin, config.exit_bps_at_no_margin)
    bj: dict[str, dict] = {}
    for p in ca:
        if p.get('account_name') and p['account_name'] != config.account_name:
            continue
        au = p.get('connector_name')
        if au not in (config.maker_venue_hl, config.taker_venue_bin):
            continue
        bu, ad = (_mark_from(p), float(p.get('amount') or 0))
        if bu is None or not ad:
            continue
        bw = abs(ad) * bu
        if bw < config.dust_usd:
            continue
        ag = (p.get('trading_pair') or '').split('-')[0].upper()
        h = bj.setdefault(ag, {'legs': {}, 'notional': 0.0, 'upnl': 0.0})
        h['legs'][au] = 'LONG' if ad > 0 else 'SHORT'
        h['notional'] += bw
        h['upnl'] += float(p.get('unrealized_pnl') or 0)
    ab = {b.upper() for b in config.only_bases}
    ba = []
    if ab:
        for ag in list(bj):
            if ag.upper() not in ab:
                ba.append(ag)
                bj.pop(ag)
    be, cm, bx = ([], [], {})
    for ag, h in bj.items():
        cl = ag in bk and an._bin_symbol(ag) in ak
        if not cl:
            be.append((ag, h))
        elif len(h['legs']) < 2:
            cm.append((ag, h))
        else:
            bx[ag] = h
    cn = []
    for ag, h in bx.items():
        bo, aj = (bk.get(ag), ak.get(an._bin_symbol(ag)))
        cf = h['legs'].get(config.maker_venue_hl)
        ce = h['legs'].get(config.taker_venue_bin)
        if not bo or not aj:
            cn.append({'base': ag, 'urgent': False, 'fundsig': None, 'carry': None, 'exit_bps': ah, 'why': 'no funding data', 'notional': h['notional'], 'upnl': h['upnl'], 'legs': h['legs']})
            continue
        if cf and ce and (cf != ce):
            cd = cf == 'SHORT'
        elif cf:
            cd = cf == 'SHORT'
        else:
            cd = ce == 'LONG'
        bd, bc = (bo['funding_bph'], aj['funding_bph'])
        bg = bd - bc if cd else bc - bd
        am = float(aj.get('interval_h') or 8.0)
        bp = max(1.0, am)
        import time as _t
        ao, _ = an._leg_carry_bps(bo['rate_per_print'], 1.0, 3600.0 - _t.time() % 3600.0, bp, cd, 1.0)
        ap, _ = an._leg_carry_bps(aj['rate_per_print'], am, max(0.0, (aj.get('next_print_min') or 0) * 60.0), bp, not cd, 1.0)
        ar = ao + ap
        bb = ah + ar
        cp = bg < config.urgent_fundsig_bph
        cn.append({'base': ag, 'urgent': cp, 'fundsig': bg, 'carry': ar, 'exit_bps': bb, 'notional': h['notional'], 'upnl': h['upnl'], 'legs': h['legs'], 'short_hl': cd, 'why': 'funding turned against us' if cp else 'carrying fine'})
    aq = config.max_notional_per_line
    if aq:
        cn = [u for u in cn if u['notional'] <= aq]
    cp = sorted([u for u in cn if u['urgent']], key=lambda u: u['fundsig'] or 0)
    bv = sorted([u for u in cn if not u['urgent']], key=lambda u: u['carry'] or 0)
    at = an.Config(min_fundsig_bph=config.min_fundsig_bph, min_edge_bps=config.min_edge_bps, min_volume_usd=config.min_volume_usd, slots=config.slots)
    ax = []
    for ag in [b for b in bk if an._bin_symbol(b) in ak and an._symbol_ok(b)[0]]:
        bo, aj = (bk[ag], ak[an._bin_symbol(ag)])
        if not bo['bid'] or not bo['ask']:
            continue
        if bo['volume_usd'] < at.min_volume_usd or aj['volume_usd'] < at.min_volume_usd:
            continue
        bf = an._all_four(bo, aj)
        v, s, aw = bf[0]
        cd = (v == 'HL') == (s == 'SELL')
        bd, bc = (bo['funding_bph'], aj['funding_bph'])
        bg = bd - bc if cd else bc - bd
        am = float(aj.get('interval_h') or 8.0)
        bp = max(1.0, am)
        import time as _t
        ao, _ = an._leg_carry_bps(bo['rate_per_print'], 1.0, 3600.0 - _t.time() % 3600.0, bp, cd, 1.0)
        ap, _ = an._leg_carry_bps(aj['rate_per_print'], am, max(0.0, (aj.get('next_print_min') or 0) * 60.0), bp, not cd, 1.0)
        ar = ao + ap
        if ab and ag.upper() not in ab:
            continue
        if ai is not None and ai < config.margin_floor_pct:
            continue
        if config.max_base_notional_quote:
            ac = bj.get(ag, {}).get('notional', 0.0)
            if ac + config.size_hint_quote > config.max_base_notional_quote:
                continue
        if bg >= at.min_fundsig_bph and ar > 0 and (aw >= at.min_edge_bps):
            br = config.maker_venue_hl if v == 'HL' else config.taker_venue_bin
            cj = config.taker_venue_bin if v == 'HL' else config.maker_venue_hl
            ax.append({'base': ag, 'dir': f'{v}.{s}', 'edge': aw, 'carry': ar, 'fundsig': bg, 'held': ag in bj, 'maker_connector': br, 'maker_trading_pair': _pair_for(ag, br), 'taker_connector': cj, 'taker_trading_pair': _pair_for(ag, cj), 'maker_side_str': s})
    ax.sort(key=lambda r: -r['carry'])
    by, cq = ([], 0)
    for u in cp:
        if cq >= config.slots:
            break
        by.append(('UNWIND-URGENT', u))
        cq += 1
    for e in ax:
        if cq >= config.slots:
            break
        by.append(('ADD' if e['held'] else 'ENTER', e))
        cq += 1
    for u in bv:
        if cq >= config.slots:
            break
        by.append(('UNWIND-NORMAL', u))
        cq += 1
    L = [f'SLOT PLAN   {cq}/{config.slots} slots allocated']
    if ab:
        L += ['', f'  ALLOWLIST ACTIVE: only {', '.join(sorted(ab))} may be touched.', f'  {len(ba)} held base(s) excluded by it and untouchable' + (f': {', '.join(ba[:10])}' if ba else '') + ('...' if len(ba) > 10 else '')]
    if cm:
        ck = sum((h['notional'] for _, h in cm))
        L += ['', f'  !! UNHEDGED: {len(cm)} position(s) on tradeable bases, ${ck:,.0f} notional - {', '.join((b for b, _ in cm[:8]))}' + (' ...' if len(cm) > 8 else ''), '  One leg only on a base this strategy CAN trade. In a DEDICATED account (the race)', '  that means one thing: our own failed hedge - naked directional risk on a book that', '  should be flat. Re-hedge or close it before opening anything new; there is no', '  operator during the race.', f'  CAVEAT on a shared account: only {', '.join((v.split('_')[0] for v in cr))} are', '  read here, so a leg hedged on any OTHER venue is invisible and shows up in this', '  list. Confirm against the full venue set before acting on a dev box.']
    if be:
        ck = sum((h['notional'] for _, h in be))
        L += ['', f'  FOREIGN: {len(be)} position(s), ${ck:,.0f} notional - {', '.join((b for b, _ in be[:8]))}' + (' ...' if len(be) > 8 else ''), '  On bases NOT listed on both venues, so this strategy could never have opened', '  them. EXCLUDED from the plan and never traded.']
    if az:
        L += ['', '!! ' + ' | '.join(az)]
    if ai is not None and ai < config.margin_floor_pct:
        L += ['', f'  !! MARGIN {ai * 100:.0f}% < floor {config.margin_floor_pct * 100:.0f}% - NO new entries this tick, unwinds only.']
    L += ['', f'  order: urgent unwinds -> enter/add -> normal unwinds', f'  urgent when fundsig < {config.urgent_fundsig_bph} bp/h.  entry gate fundsig >= {config.min_fundsig_bph}, carry > 0.', f'  exit_bps = margin_base {ah:+.1f} + carry.  margin_base scales {config.exit_bps_at_full_margin:+.0f} (free) -> {config.exit_bps_at_no_margin:+.0f} (starved)', f"  on the BINDING venue's health" + (f' ({ai * 100:.0f}%: ' + ', '.join((f'{v.split('_')[0]} {h * 100:.0f}%' for v, h in bi.items())) + ')' if ai is not None else ' (UNREADABLE -> assumed starved)') + '. Carry then adds patience.', '', f'  {'slot':<6}{'action':<15}{'base':<10}{'dir/legs':<18}{'carry':>8}{'fundsig':>9}{'edge':>7}{'exit_bps':>10}{'notional':>11}']
    for i, (aa, r) in enumerate(by, 1):
        if aa.startswith('UNWIND'):
            bq = '+'.join((f'{s[0]}{v.split('_')[0][:3]}' for v, s in r['legs'].items()))
            c = f'{r['carry']:>8.3f}' if r['carry'] is not None else f'{'-':>8}'
            f = f'{r['fundsig']:>9.3f}' if r['fundsig'] is not None else f'{'-':>9}'
            L.append(f'  {i:<6}{aa:<15}{r['base']:<10}{bq:<18}{c}{f}{'-':>7}{r['exit_bps']:>10.1f}{r['notional']:>11,.0f}')
        else:
            L.append(f'  {i:<6}{aa:<15}{r['base']:<10}{r['dir']:<18}{r['carry']:>8.3f}{r['fundsig']:>9.3f}{r['edge']:>7.1f}{'-':>10}{'-':>11}')
            L.append(f'        -> maker {r['maker_connector']} {r['maker_trading_pair']} {r['maker_side_str']} | taker {r['taker_connector']} {r['taker_trading_pair']}')
    if not by:
        L.append('  (nothing to do - no urgent unwind, no candidate clears the gate)')
    cg = config.slots - cq
    L.append('')
    L.append(f'  {len(cp)} urgent unwind(s), {len(ax)} entry candidate(s), {len(bv)} normal unwind(s) available. {cg} slot(s) idle.')
    if cg and bv:
        L.append('  Idle slots with normal unwinds available means the plan ran out of BOTH candidates and holdings - check for a data error.')
    return '\n'.join(L)
