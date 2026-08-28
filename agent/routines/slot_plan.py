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
    cg = importlib.util.spec_from_file_location('_mb', os.path.join(_HERE, 'margin_book.py'))
    m = importlib.util.module_from_spec(cg)
    cg.loader.exec_module(m)
    return m

def _scanner():
    """Reuse basis_scanner's fetchers so plan and scan cannot drift apart."""
    cg = importlib.util.spec_from_file_location('_bs', os.path.join(_HERE, 'basis_scanner.py'))
    m = importlib.util.module_from_spec(cg)
    cg.loader.exec_module(m)
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
    dust_usd: float = Field(default=5.0)
    only_bases: list[str] = Field(default=[], description='HARD ALLOWLIST. When set, the plan may touch ONLY these bases - for entries AND unwinds. Everything else is excluded whatever its classification. Use this on any account that holds positions this run did not open.')
    max_notional_per_line: float = Field(default=0.0, description="Refuse to plan an unwind larger than this notional (0 = no cap). A blunt stop against acting on a position far bigger than this run's own size.")

def _margin_exit_bps(bg: float, ae: float, ad: float) -> float:
    """Interpolate the exit floor linearly from margin health.

        health 1.0 (plenty free)  -> at_full   (-6 bp: picky, we are not forced)
        health 0.0 (none free)    -> at_empty  (-16 bp: pay up, we need the margin back)

    Margin sets URGENCY. The carry adjustment applied afterwards sets PATIENCE. They are
    independent: a funding-negative position on a full account should still not overpay to leave, and a
    well-carrying position on a starved account still has to go.
    """
    h = min(1.0, max(0.0, bg))
    return ad + (ae - ad) * h

def _pair_for(af: str, au: str) -> str:
    """The venue's own trading-pair spelling. Hyperliquid quotes USD, Binance USDT-M quotes USDT."""
    return f'{af}-USD' if 'hyperliquid' in au else f'{af}-USDT'

def _mark_from(by: dict) -> float | None:
    try:
        ac, ax = (float(by.get('amount') or 0), float(by.get('entry_price') or 0))
        cn = float(by.get('unrealized_pnl') or 0)
        return ax + cn / ac if ac and ax > 0 else None
    except Exception:
        return None

async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    am = _scanner()
    client = await get_client(context._chat_id, context=context)
    if not client:
        return 'No server available'
    import aiohttp
    ay = []
    async with aiohttp.ClientSession(timeout=am.TIMEOUT) as cb:
        (bj, bk), (aj, ak) = await asyncio.gather(am._hl_all(cb), am._bin_all(cb))
    ay += [e for e in (bk, ak) if e]
    try:
        ch = await client.portfolio.get_state()
    except Exception as e:
        ch = {}
        ay.append(f'portfolio.get_state: {e}')
    try:
        ca = await client.trading.get_positions()
        bz = ca.get('data', ca) if isinstance(ca, dict) else ca or []
    except Exception as e:
        return '\n'.join(['SLOT PLAN   REFUSED', '', f'  !! positions unreadable: {e}', '', '  The plan is not produced when the book cannot be read. Holdings are half of it:', '  an unreadable endpoint is indistinguishable from a flat account, so proceeding', '  would risk re-entering bases already held and would hide every unwind.', '  Restore the connection and re-run. Do NOT deploy from a partial plan.'])
    cq = [config.maker_venue_hl, config.taker_venue_bin]
    br = _margin_book().compute_venue_margin(ch, bz, cq, config.account_name, config.dust_usd)
    bh = {v: m['health'] for v, m in br.items()}
    ah = min(bh.values()) if bh else None
    if ah is None:
        ag = config.exit_bps_at_no_margin
    else:
        ag = _margin_exit_bps(ah, config.exit_bps_at_full_margin, config.exit_bps_at_no_margin)
    bi: dict[str, dict] = {}
    for p in bz:
        if p.get('account_name') and p['account_name'] != config.account_name:
            continue
        at = p.get('connector_name')
        if at not in (config.maker_venue_hl, config.taker_venue_bin):
            continue
        bt, ac = (_mark_from(p), float(p.get('amount') or 0))
        if bt is None or not ac:
            continue
        bv = abs(ac) * bt
        if bv < config.dust_usd:
            continue
        af = (p.get('trading_pair') or '').split('-')[0].upper()
        h = bi.setdefault(af, {'legs': {}, 'notional': 0.0, 'upnl': 0.0})
        h['legs'][at] = 'LONG' if ac > 0 else 'SHORT'
        h['notional'] += bv
        h['upnl'] += float(p.get('unrealized_pnl') or 0)
    ab = {b.upper() for b in config.only_bases}
    az = []
    if ab:
        for af in list(bi):
            if af.upper() not in ab:
                az.append(af)
                bi.pop(af)
    bd, cl, bw = ([], [], {})
    for af, h in bi.items():
        ck = af in bj and am._bin_symbol(af) in aj
        if not ck:
            bd.append((af, h))
        elif len(h['legs']) < 2:
            cl.append((af, h))
        else:
            bw[af] = h
    cm = []
    for af, h in bw.items():
        bl, ai = (bj.get(af), aj.get(am._bin_symbol(af)))
        ce = h['legs'].get(config.maker_venue_hl)
        cd = h['legs'].get(config.taker_venue_bin)
        if not bl or not ai:
            cm.append({'base': af, 'urgent': False, 'fundsig': None, 'carry': None, 'exit_bps': ag, 'why': 'no funding data', 'notional': h['notional'], 'upnl': h['upnl'], 'legs': h['legs']})
            continue
        if ce and cd and (ce != cd):
            cc = ce == 'SHORT'
        elif ce:
            cc = ce == 'SHORT'
        else:
            cc = cd == 'LONG'
        bc, bb = (bl['funding_bph'], ai['funding_bph'])
        bf = bc - bb if cc else bb - bc
        al = float(ai.get('interval_h') or 8.0)
        bo = max(1.0, al)
        import time as _t
        an, _ = am._leg_carry_bps(bl['rate_per_print'], 1.0, 3600.0 - _t.time() % 3600.0, bo, cc, 1.0)
        ao, _ = am._leg_carry_bps(ai['rate_per_print'], al, max(0.0, (ai.get('next_print_min') or 0) * 60.0), bo, not cc, 1.0)
        aq = an + ao
        ba = ag + aq
        co = bf < config.urgent_fundsig_bph
        cm.append({'base': af, 'urgent': co, 'fundsig': bf, 'carry': aq, 'exit_bps': ba, 'notional': h['notional'], 'upnl': h['upnl'], 'legs': h['legs'], 'short_hl': cc, 'why': 'funding turned against us' if co else 'carrying fine'})
    ap = config.max_notional_per_line
    if ap:
        cm = [u for u in cm if u['notional'] <= ap]
    co = sorted([u for u in cm if u['urgent']], key=lambda u: u['fundsig'] or 0)
    bu = sorted([u for u in cm if not u['urgent']], key=lambda u: u['carry'] or 0)
    ar = am.Config(min_fundsig_bph=config.min_fundsig_bph, min_edge_bps=config.min_edge_bps, min_volume_usd=config.min_volume_usd, slots=config.slots)
    aw = []
    for af in [b for b in bj if am._bin_symbol(b) in aj and am._symbol_ok(b)[0]]:
        bl, ai = (bj[af], aj[am._bin_symbol(af)])
        if not bl['bid'] or not bl['ask']:
            continue
        if bl['volume_usd'] < ar.min_volume_usd or ai['volume_usd'] < ar.min_volume_usd:
            continue
        be = am._all_four(bl, ai)
        v, s, av = be[0]
        cc = (v == 'HL') == (s == 'SELL')
        bc, bb = (bl['funding_bph'], ai['funding_bph'])
        bf = bc - bb if cc else bb - bc
        al = float(ai.get('interval_h') or 8.0)
        bo = max(1.0, al)
        import time as _t
        an, _ = am._leg_carry_bps(bl['rate_per_print'], 1.0, 3600.0 - _t.time() % 3600.0, bo, cc, 1.0)
        ao, _ = am._leg_carry_bps(ai['rate_per_print'], al, max(0.0, (ai.get('next_print_min') or 0) * 60.0), bo, not cc, 1.0)
        aq = an + ao
        if ab and af.upper() not in ab:
            continue
        if bf >= ar.min_fundsig_bph and aq > 0 and (av >= ar.min_edge_bps):
            bq = config.maker_venue_hl if v == 'HL' else config.taker_venue_bin
            ci = config.taker_venue_bin if v == 'HL' else config.maker_venue_hl
            aw.append({'base': af, 'dir': f'{v}.{s}', 'edge': av, 'carry': aq, 'fundsig': bf, 'held': af in bi, 'maker_connector': bq, 'maker_trading_pair': _pair_for(af, bq), 'taker_connector': ci, 'taker_trading_pair': _pair_for(af, ci), 'maker_side_str': s})
    aw.sort(key=lambda r: -r['carry'])
    bx, cp = ([], 0)
    for u in co:
        if cp >= config.slots:
            break
        bx.append(('UNWIND-URGENT', u))
        cp += 1
    for e in aw:
        if cp >= config.slots:
            break
        bx.append(('ADD' if e['held'] else 'ENTER', e))
        cp += 1
    for u in bu:
        if cp >= config.slots:
            break
        bx.append(('UNWIND-NORMAL', u))
        cp += 1
    L = [f'SLOT PLAN   {cp}/{config.slots} slots allocated']
    if ab:
        L += ['', f'  ALLOWLIST ACTIVE: only {', '.join(sorted(ab))} may be touched.', f'  {len(az)} held base(s) excluded by it and untouchable' + (f': {', '.join(az[:10])}' if az else '') + ('...' if len(az) > 10 else '')]
    if cl:
        cj = sum((h['notional'] for _, h in cl))
        L += ['', f'  !! UNHEDGED: {len(cl)} position(s) on tradeable bases, ${cj:,.0f} notional - {', '.join((b for b, _ in cl[:8]))}' + (' ...' if len(cl) > 8 else ''), '  One leg only on a base this strategy CAN trade. In a DEDICATED account (the race)', '  that means one thing: our own failed hedge - naked directional risk on a book that', '  should be flat. Re-hedge or close it before opening anything new; there is no', '  operator during the race.', f'  CAVEAT on a shared account: only {', '.join((v.split('_')[0] for v in cq))} are', '  read here, so a leg hedged on any OTHER venue is invisible and shows up in this', '  list. Confirm against the full venue set before acting on a dev box.']
    if bd:
        cj = sum((h['notional'] for _, h in bd))
        L += ['', f'  FOREIGN: {len(bd)} position(s), ${cj:,.0f} notional - {', '.join((b for b, _ in bd[:8]))}' + (' ...' if len(bd) > 8 else ''), '  On bases NOT listed on both venues, so this strategy could never have opened', '  them. EXCLUDED from the plan and never traded.']
    if ay:
        L += ['', '!! ' + ' | '.join(ay)]
    L += ['', f'  order: urgent unwinds -> enter/add -> normal unwinds', f'  urgent when fundsig < {config.urgent_fundsig_bph} bp/h.  entry gate fundsig >= {config.min_fundsig_bph}, carry > 0.', f'  exit_bps = margin_base {ag:+.1f} + carry.  margin_base scales {config.exit_bps_at_full_margin:+.0f} (free) -> {config.exit_bps_at_no_margin:+.0f} (starved)', f"  on the BINDING venue's health" + (f' ({ah * 100:.0f}%: ' + ', '.join((f'{v.split('_')[0]} {h * 100:.0f}%' for v, h in bh.items())) + ')' if ah is not None else ' (UNREADABLE -> assumed starved)') + '. Carry then adds patience.', '', f'  {'slot':<6}{'action':<15}{'base':<10}{'dir/legs':<18}{'carry':>8}{'fundsig':>9}{'edge':>7}{'exit_bps':>10}{'notional':>11}']
    for i, (aa, r) in enumerate(bx, 1):
        if aa.startswith('UNWIND'):
            bp = '+'.join((f'{s[0]}{v.split('_')[0][:3]}' for v, s in r['legs'].items()))
            c = f'{r['carry']:>8.3f}' if r['carry'] is not None else f'{'-':>8}'
            f = f'{r['fundsig']:>9.3f}' if r['fundsig'] is not None else f'{'-':>9}'
            L.append(f'  {i:<6}{aa:<15}{r['base']:<10}{bp:<18}{c}{f}{'-':>7}{r['exit_bps']:>10.1f}{r['notional']:>11,.0f}')
        else:
            L.append(f'  {i:<6}{aa:<15}{r['base']:<10}{r['dir']:<18}{r['carry']:>8.3f}{r['fundsig']:>9.3f}{r['edge']:>7.1f}{'-':>10}{'-':>11}')
            L.append(f'        -> maker {r['maker_connector']} {r['maker_trading_pair']} {r['maker_side_str']} | taker {r['taker_connector']} {r['taker_trading_pair']}')
    if not bx:
        L.append('  (nothing to do - no urgent unwind, no candidate clears the gate)')
    cf = config.slots - cp
    L.append('')
    L.append(f'  {len(co)} urgent unwind(s), {len(aw)} entry candidate(s), {len(bu)} normal unwind(s) available. {cf} slot(s) idle.')
    if cf and bu:
        L.append('  Idle slots with normal unwinds available means the plan ran out of BOTH candidates and holdings - check for a data error.')
    return '\n'.join(L)
