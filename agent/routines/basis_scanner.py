"""Cross-venue funding scanner - Hyperliquid vs Binance perpetuals.

Ranks funding-arb candidates. Funding is the earner: under a forced close at the deadline the
price edge is a basis position that is handed back if the basis has not moved, whereas funding
is banked at every settlement and cannot be clawed back. So funding gates and ranks; the price
edge is only an execution-quality floor.

BULK ONLY - five requests for the entire market, no per-symbol calls:

    HL   POST /info {"type":"metaAndAssetCtxs"}   funding, 24h volume, impact prices, mid
    BIN  GET  /fapi/v1/premiumIndex               funding rate, mark, next settlement
    BIN  GET  /fapi/v1/fundingInfo                per-symbol funding interval
    BIN  GET  /fapi/v1/ticker/bookTicker          best bid/ask + size, all symbols
    BIN  GET  /fapi/v1/ticker/24hr                24h quote volume, all symbols

Per-symbol order books were the first design and are gone: ~2 calls per base to evaluate ~200
bases, to find the handful that clear a funding bar the bulk data already answers.

Prices: Binance from the book touch; Hyperliquid from `impactPxs`, the executable price for
size rather than the touch. Its spread runs 2-6x the touch spread, so every HL-side edge here
is UNDERSTATED. That bias is deliberate - a gate should skip a marginal trade rather than
invent one - but it means these edges are a floor, not a forecast.
"""
import asyncio
import logging
import time
import aiohttp
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes
logger = logging.getLogger(__name__)
CATEGORY = 'Market Data'
HL_URL = 'https://api.hyperliquid.xyz/info'
BIN = 'https://fapi.binance.com/fapi/v1'
TIMEOUT = aiohttp.ClientTimeout(total=25)
_HL_MULT_PREFIXES = ('k', 'm')
_BIN_MULT_PREFIXES = ('1000', '1M', '1MB')

class Config(BaseModel):
    """Funding-arb candidates across Hyperliquid and Binance perps, ranked by carry."""
    universe: list[str] = Field(default=[], description='Bases to scan. EMPTY = every base both venues list.')
    min_fundsig_bph: float = Field(default=0.3, description="Funding gate, bp/HOUR, signed for the row's direction.")
    min_edge_bps: float = Field(default=0.0, description='Execution floor: skip if the price edge is worse than this.')
    min_volume_usd: float = Field(default=1500000.0, description='Minimum 24h traded volume on BOTH venues.')
    min_max_leverage: float = Field(default=3.0, description="Skip a base whose venue-permitted MAX leverage is below this. Only Hyperliquid publishes it, and it is the binding side (Binance offers more on every pair we trade), so the filter reads HL's number. Set at 3 to exclude only genuinely capped names: leverage and funding quality pull OPPOSITE ways here - the 20x+ majors are efficient and their funding rarely diverges, while the pairs that clear the funding gate cap at 3-5x. A higher bar buys headroom that cannot be filled.")
    slots: int = Field(default=6, description='Concurrent controller budget.')
    hold_hours: float = Field(default=0.0, description='Hold horizon. 0 = max(interval_a, interval_b).')
    decay: float = Field(default=1.0, description='Per-hour retention on each settlement (1.0 = none).')
    rank_by: str = Field(default='carry', description="'carry' or 'edge'.")

def _bin_symbol(ae: str) -> str:
    return f'{ae.upper()}USDT'

def _symbol_ok(ae: str) -> tuple[bool, str]:
    """ASCII-only, and no contract multiplier. Both failures are silent and expensive.

    A non-ASCII base becomes a controller-config name, which the deploy validates against
    [A-Za-z0-9_-]+ and REJECTS THE WHOLE COHORT over - every controller in the deploy.
    A multiplier mismatch (1000PEPE vs kPEPE) is a 1000x hedge error that never announces itself.
    """
    if not ae.isascii() or not ae.replace('_', '').replace('-', '').isalnum():
        return (False, 'non-ASCII or non-alphanumeric')
    b = ae.upper()
    if any((b.startswith(p.upper()) for p in _BIN_MULT_PREFIXES)):
        return (False, 'binance-style multiplier')
    if ae[:1] in _HL_MULT_PREFIXES and ae[1:2].isupper():
        return (False, 'hyperliquid-style multiplier')
    return (True, '')

async def _hl_all(cj) -> tuple[dict, str | None]:
    try:
        async with cj.post(HL_URL, json={'type': 'metaAndAssetCtxs'}) as r:
            au = await r.json()
        universe, at = (au[0]['universe'], au[1])
    except Exception as e:
        return ({}, f'hyperliquid metaAndAssetCtxs failed: {e}')
    cb = {}
    for bq, c in zip(universe, at):
        try:
            bf = c.get('impactPxs') or []
            ai, ad = (float(bf[0]), float(bf[1])) if len(bf) == 2 else (None, None)
            cb[bq['name'].upper()] = {'max_leverage': float(bq.get('maxLeverage') or 0), 'rate_per_print': float(c.get('funding') or 0), 'funding_bph': float(c.get('funding') or 0) * 10000.0, 'mark': float(c.get('markPx') or 0) or None, 'mid': float(c.get('midPx') or 0) or None, 'bid': ai, 'ask': ad, 'volume_usd': float(c.get('dayNtlVlm') or 0)}
        except Exception:
            continue
    return (cb, None)

async def _bin_all(cj) -> tuple[dict, str | None]:
    """premiumIndex + fundingInfo + bookTicker + ticker/24hr, merged per symbol."""
    try:
        async with cj.get(f'{BIN}/fundingInfo') as r:
            bg = await r.json()
        bi = {e['symbol'].upper(): float(e['fundingIntervalHours']) for e in bg if e.get('symbol') and e.get('fundingIntervalHours')}
    except Exception as e:
        logger.warning('binance fundingInfo failed, assuming 8h: %s', e)
        bi = {}

    async def _get(cc):
        async with cj.get(f'{BIN}/{cc}') as r:
            return await r.json()
    try:
        ce, ao, cq = await asyncio.gather(_get('premiumIndex'), _get('ticker/bookTicker'), _get('ticker/24hr'))
    except Exception as e:
        return ({}, f'binance bulk fetch failed: {e}')
    ap = {e['symbol'].upper(): e for e in ao if isinstance(e, dict)}
    cv = {e['symbol'].upper(): e for e in cq if isinstance(e, dict)}
    bz = time.time() * 1000
    cb = {}
    for e in ce if isinstance(ce, list) else []:
        try:
            co = e['symbol'].upper()
            aj, cu = (ap.get(co), cv.get(co))
            if not aj:
                continue
            by = e.get('nextFundingTime')
            br = (float(by) - bz) / 60000.0 if by else None
            be = bi.get(co, 8.0)
            cb[co] = {'rate_per_print': float(e.get('lastFundingRate') or 0), 'funding_bph': float(e.get('lastFundingRate') or 0) * 10000.0 / be, 'mark': float(e.get('markPrice') or 0) or None, 'bid': float(aj['bidPrice']), 'ask': float(aj['askPrice']), 'bid_qty': float(aj.get('bidQty') or 0), 'ask_qty': float(aj.get('askQty') or 0), 'next_print_min': br, 'interval_h': be, 'volume_usd': float((cu or {}).get('quoteVolume') or 0)}
        except Exception:
            continue
    return (cb, None)

def _leg_carry_bps(cg, bh, bx, bd, bj, decay):
    """(carry bps, settlements collected) for ONE leg over the hold.

    Funding pays at DISCRETE timestamps. Hold four hours on an 8h venue, cross no print, and
    you collect exactly nothing - amortising the rate to a per-hour figure reports a steady
    trickle where the real cashflow is a staircase.

    A SHORT receives when the rate is positive; a LONG receives when it is negative.

    `decay` is per-HOUR, so two settlements the same hours out are discounted equally however
    many times their venue has printed. It cancels out of the NET whenever both legs share an
    interval (their prints land together), so it can only reorder pairs on different clocks.
    """
    cd = cg if bj else -cg
    t = bx / 3600.0
    if t <= 1e-09:
        t += bh
    cb, n = (0.0, 0)
    while t <= bd + 1e-09:
        cb += cd * decay ** t
        n += 1
        t += bh
    return (cb * 10000.0, n)

def _all_four(az: dict, ak: dict) -> list[tuple[str, str, float]]:
    """(maker_venue, maker_side, edge_bps) for all four placements, best first.

    There are four, and they pair up: `maker HL SELL / taker BIN BUY` and
    `maker BIN BUY / taker HL SELL` are the SAME economic trade (short HL, long BIN) and differ
    only in WHICH LEG RESTS. The basis picks the direction; the venue is a free lever for fill
    quality, fee and depth.
    """

    def e(bs, ct, cm):
        if cm == 'BUY':
            return (ct['bid'] - bs['bid']) / bs['bid'] * 10000.0
        return (bs['ask'] - ct['ask']) / bs['ask'] * 10000.0
    return sorted([('HL', 'BUY', e(az, ak, 'BUY')), ('HL', 'SELL', e(az, ak, 'SELL')), ('BIN', 'BUY', e(ak, az, 'BUY')), ('BIN', 'SELL', e(ak, az, 'SELL'))], key=lambda x: -x[2])

async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    cp = time.time()
    async with aiohttp.ClientSession(timeout=TIMEOUT) as cj:
        (az, bb), (ak, am) = await asyncio.gather(_hl_all(cj), _bin_all(cj))
    ch = []
    if config.universe:
        aq = []
        for b in config.universe:
            ca, cw = _symbol_ok(b)
            (aq if ca else ch).append(b if ca else f'{b} ({cw})')
    else:
        aq = [b for b in az if _bin_symbol(b) in ak and _symbol_ok(b)[0]]
    bw = len(aq)
    cs = 0
    bm = 0
    ci = []
    for ae in aq:
        h, b = (az.get(ae), ak.get(_bin_symbol(ae)))
        if not (h and b) or not h['bid'] or (not h['ask']):
            continue
        if h['volume_usd'] < config.min_volume_usd or b['volume_usd'] < config.min_volume_usd:
            cs += 1
            continue
        if config.min_max_leverage and h.get('max_leverage', 0) < config.min_max_leverage:
            bm += 1
            continue
        ax = _all_four(h, b)
        ah, ag, af = ax[0]
        ac, ab, aa = ax[1]
        ck = (ah == 'HL') == (ag == 'SELL')
        bc, an = (1.0, float(b.get('interval_h') or 8.0))
        bd = config.hold_hours or max(bc, an)
        ba, bu = _leg_carry_bps(h['rate_per_print'], bc, 3600.0 - time.time() % 3600.0, bd, ck, config.decay)
        al, bt = _leg_carry_bps(b['rate_per_print'], an, max(0.0, (b.get('next_print_min') or 0) * 60.0), bd, not ck, config.decay)
        ar = ba + al
        aw, av = (h['funding_bph'], b['funding_bph'])
        ay = aw - av if ck else av - aw
        bo = (b['mark'] - h['mark']) / h['mark'] * 10000.0 if h['mark'] and b['mark'] else None
        ci.append({'base': ae, 'dir': f'{ah}.{ag}', 'best': af, 'alt': f'{ac}.{ab}', 'alt_edge': aa, 'mark_basis': bo, 'carry': ar, 'fund_signed': ay, 'hold_h': bd, 'n_hl': bu, 'n_bn': bt, 'vol_hl': h['volume_usd'], 'vol_bn': b['volume_usd'], 'max_lev': h.get('max_leverage', 0), 'next_bin_min': b.get('next_print_min'), 'qualifies': ay >= config.min_fundsig_bph and ar > 0 and (af >= config.min_edge_bps)})
    key = (lambda r: (not r['qualifies'], -r['best'], -r['carry'])) if config.rank_by == 'edge' else lambda r: (not r['qualifies'], -r['carry'], -r['fund_signed'])
    ci.sort(key=key)
    bv = sum((1 for r in ci if r['qualifies']))
    cl = [r for r in ci if r['qualifies']][:config.slots] + [r for r in ci if not r['qualifies']][:6]
    L = [f'FUNDING SCAN  hyperliquid <-> binance   ({time.time() - cp:.1f}s, 5 bulk calls)']
    if bb:
        L.append(f'!! {bb}')
    if am:
        L.append(f'!! {am}')
    L += ['', f'  universe {bw} on both -> {cs} below ${config.min_volume_usd / 1000000.0:.1f}M vol -> {bm} under {config.min_max_leverage:.0f}x max leverage -> {len(ci)} priced -> {bv} qualify', '', f'  GATE: fund_signed >= {config.min_fundsig_bph} bp/h AND carry > 0 AND edge >= {config.min_edge_bps} bp. Ranked by {config.rank_by}.', '  carry   = TOTAL bp over hold H = max(intervals); prints = settlements crossed (hl/bin).', "  fundsig = naive per-hour rate, signed for this row's direction.", '  edge    = price edge at placement. HL side uses impact prices, so it is UNDERSTATED.', '  best/alt= the same trade rested on either venue - pick on fill quality and depth.', '']
    L.append(f'  {'':<4}{'base':<10}{'best':<9}{'edge':>7}  {'alt':<9}{'edge':>7}{'carry':>8}{'fundsig':>9}{'H':>4}{'prints':>7}{'mkbasis':>9}{'vol_hl':>11}{'vol_bin':>11}')
    bl = False
    cn = 0
    for r in cl:
        if not r['qualifies'] and (not bl):
            L.append('  ' + '-' * 58 + '  below: fails the gate')
            bl = True
        cr = f'  {cn + 1}.' if r['qualifies'] else '   -'
        if r['qualifies']:
            cn += 1
        bp = f'{r['mark_basis']:.1f}' if r['mark_basis'] is not None else '-'
        cf = f'{r['n_hl']}/{r['n_bn']}'
        L.append(f'  {cr:<4}{r['base']:<10}{r['dir']:<9}{r['best']:>7.1f}  {r['alt']:<9}{r['alt_edge']:>7.1f}{r['carry']:>8.3f}{r['fund_signed']:>9.3f}{r['hold_h']:>4.0f}{cf:>7}{bp:>9}{r['vol_hl'] / 1000000.0:>10.1f}M{r['vol_bn'] / 1000000.0:>10.1f}M')
    if not bv:
        L.append('  (nothing qualifies - no funding differential worth the round trip. Correct to sit out.)')
    if ch:
        L += ['', '  REJECTED symbols: ' + ', '.join(ch)]
    return '\n'.join(L)
