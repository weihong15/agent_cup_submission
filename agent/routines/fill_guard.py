"""fill_guard - the out-of-band breaker. CONTINUOUS: runs between ticks, not on them.

The shipped executor has no in-flight defence. Its only self-stop is the consecutive
hedge-failure cap, which catches a broken venue, not a bleeding one. Between two 15-minute
ticks an executor can fill repeatedly at a spread that has gone negative and nothing notices.

So this runs on its own clock. Every `interval_sec` it reads the fills of the last window,
computes the volume-weighted maker-vs-taker spread per base, and stops the controllers whose
recent fills are averaging below `kill_threshold_bps`. It stops ONLY those controllers; every
other pair keeps trading.

Where the fills come from, and why not the obvious places:

  * `controller.custom_info` - the PUBLIC perp_xemm controller does not implement
    `get_custom_info()` (only the private fork does), so the bot-status payload carries an
    empty dict.
  * `trading.get_trades()` - reads hummingbot-api's Postgres, which is written by
    `OrdersRecorder` attached to the API's OWN connectors. A bot container places its orders
    in its own process, so **bot fills never reach that table**. It returns empty all race.
  * `/archived-bots/{db}/trades` - `list_databases()` scans the `archived` folder only, so it
    sees a bot only AFTER it is stopped and archived. Useless to a live guard.

What is left, and what actually works, is the bot's own SQLite - the standard Hummingbot
`TradeFill` table at `bots/instances/<instance>/data/<instance>.sqlite`. Condor runs natively
on the host and the API bind-mounts that directory, so the file is directly readable. It is
opened READ-ONLY (`mode=ro`) because the bot is actively writing to it.

Threshold at 0 bp: a session whose recent fills average a negative spread is not paying for its
own execution. A false positive costs only the remainder of the tick window - the pair can be
re-entered next tick - so the rule favours recall over precision. `min_fills` is a sanity guard
against a single-fill artefact, not a quality filter; raising it mostly just delays the verdict.

DRY-RUN BY DEFAULT. Set `arm=True` to let it actually stop controllers.
"""
import asyncio
import glob
import logging
import os
import sqlite3
import time
from collections import defaultdict
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes
from config_manager import get_client
logger = logging.getLogger(__name__)
CATEGORY = 'Monitoring'
CONTINUOUS = True
_FLIPS: dict[str, int] = {}

class Config(BaseModel):
    """Kills perp_xemm controllers whose recent fills average a negative spread."""
    interval_sec: int = Field(default=300, description='Seconds between checks')
    window_sec: int = Field(default=600, description='Look-back for fills. Wider than the interval so a slow pair still has a sample.')
    kill_threshold_bps: float = Field(default=0.0, description='Stop the controller when the window VWAP spread is below this')
    min_fills: int = Field(default=2, description='Sanity guard against a single-fill artefact. Not a quality filter.')
    max_stale_sec: float = Field(default=300.0, description="Fallback only, when the API's active-bot list cannot be read: ignore an instance db not written within this many seconds.")
    maker_connector: str = Field(default='hyperliquid_perpetual', description='Venue that rests')
    taker_connector: str = Field(default='binance_perpetual', description='Venue that hedges')
    instances_dir: str = Field(default='', description='hummingbot-api bots/instances dir. Empty = auto-discover.')
    arm: bool = Field(default=False, description='False = report only. True = actually stop controllers.')
    flip_on_bleed: bool = Field(default=True, description='On a bleed, redeploy the same trade with the legs SWAPPED (rest on the other venue) instead of leaving the slot empty until the next tick.')
    max_flips_per_base: int = Field(default=1, description='Flips allowed per base per run. Stops maker/taker flip-flop.')

def _vwap(cg) -> tuple[float, float]:
    """(vwap, total_base) over a list of {price, amount}."""
    ab = sum((abs(float(t['amount'])) for t in cg))
    if ab <= 0:
        return (0.0, 0.0)
    bq = sum((abs(float(t['amount'])) * float(t['price']) for t in cg))
    return (bq / ab, ab)

def _spread_bps(bd: float, cd: float, bc: bool) -> float:
    """Realized cross-venue spread. Bought low / sold high is positive on both sides."""
    if bd <= 0 or cd <= 0:
        return 0.0
    if bc:
        return (cd - bd) / bd * 10000.0
    return (bd - cd) / bd * 10000.0

def _instance_roots(instances_dir: str) -> list[str]:
    if instances_dir:
        bv = [instances_dir]
    else:
        bv = [os.environ.get('BOTS_PATH', '') and os.path.join(os.environ['BOTS_PATH'], 'bots', 'instances'), os.path.join(os.getcwd(), '..', 'hummingbot-api', 'bots', 'instances'), os.path.join(os.getcwd(), 'bots', 'instances'), os.path.expanduser('~/hummingbot-api/bots/instances')]
    return [r for r in bv if r and os.path.isdir(r)]

def _find_dbs(instances_dir: str, ba: set[str] | None, bf: float) -> tuple[list[str], list[str]]:
    """(dbs worth judging, names skipped). Only sessions that could still be ACTED ON.

    The guard's single action is to kill a RUNNING controller. A finished session has nothing
    to kill, so reading it cannot help - and it can actively harm: instance dirs accumulate,
    every session on a base carries that base's name, and a new controller inherits its dead
    predecessor's fills. That is how a healthy controller gets killed for a bleed it did not
    cause, which is the worst outcome this routine has available.

    Liveness comes from the API's active-bot list, which is authoritative - it already excludes
    bots that are stopping. When that list cannot be fetched the fallback is file mtime, since a
    live session writes continuously; that is weaker but strictly better than reading everything.
    """
    aq, by = ([], [])
    bm = time.time()
    for bu in _instance_roots(instances_dir):
        for path in glob.glob(os.path.join(bu, '*', 'data', '*.sqlite')):
            bk = os.path.basename(os.path.dirname(os.path.dirname(path)))
            if ba is not None:
                if bk in ba:
                    aq.append(path)
                else:
                    by.append(bk)
                continue
            try:
                au = bm - os.path.getmtime(path) <= bf
            except OSError:
                au = False
            (aq if au else by).append(path if au else bk)
        if aq or by:
            break
    return (aq, by)

async def _live_bot_names(client) -> set[str] | None:
    """Bot names the API currently reports as active. None if it cannot be read."""
    try:
        br = await client.bot_orchestration.get_active_bots_status()
        an = (br.get('data') if isinstance(br, dict) else br) or {}
        return set(an.keys()) if isinstance(an, dict) else None
    except Exception as e:
        logger.warning('fill_guard: active-bot list unavailable (%s); falling back to mtime', e)
        return None

def _read_fills(ap: str, bx: float) -> tuple[list[dict], str | None]:
    """TradeFill rows newer than `since_epoch`, as API-shaped dicts.

    READ-ONLY (`mode=ro`): the bot owns this file and is writing to it live. A writable
    handle risks locking the process that is placing our orders, which would be a far worse
    outcome than a missed guard pass.

    The query is shaped to hold that lock as briefly as possible. These databases run in
    `journal_mode=delete`, NOT WAL, so a reader and the bot's commit cannot overlap: a long
    read can delay the process placing our orders. A bare `WHERE timestamp >= ?` is a full
    table SCAN plus a temp B-tree for ORDER BY, because every index is compound and none
    leads with `timestamp`. Filtering on `config_file_path` (there is exactly one per
    instance) hits `tf_config_timestamp_index` as a true range scan on BOTH columns, and
    dropping ORDER BY removes the sort - the caller volume-weights the rows, so their order
    is irrelevant.
    """
    bw: list[dict] = []
    try:
        aj = sqlite3.connect(f'file:{ap}?mode=ro', uri=True, timeout=5.0)
        try:
            ag = aj.execute('SELECT config_file_path FROM TradeFill LIMIT 1').fetchone()
            if not ag:
                return ([], None)
            am = aj.execute('SELECT market, symbol, trade_type, price, amount, timestamp FROM TradeFill WHERE config_file_path = ? AND timestamp >= ?', (ag[0], int(bx * 1000)))
            for be, cc, ci, bp, aa, ch in am.fetchall():
                bw.append({'connector_name': be, 'trading_pair': cc, 'trade_type': ci, 'price': float(bp), 'amount': float(aa), 'timestamp': ch})
        finally:
            aj.close()
    except Exception as e:
        return ([], f'  !! could not read {os.path.basename(ap)}: {e}')
    return (bw, None)

async def _check_once(client, config: Config) -> tuple[list[str], list[dict]]:
    """One pass. Returns (report_lines, controllers_to_kill)."""
    bm = time.time()
    cb = bm - config.window_sec
    ay, aw = ([], [])
    az = await _live_bot_names(client)
    aq, by = _find_dbs(config.instances_dir, az, config.max_stale_sec)
    if not aq:
        av = 'no bot reported active' if az is not None else 'no recently-written instance db'
        bl = f'  nothing to judge - {av}'
        if by:
            bl += f' ({len(by)} inactive session(s) skipped)'
        return ([bl], [])
    cg, bs = ([], [])
    for ao in aq:
        bw, ar = _read_fills(ao, cb)
        cg.extend(bw)
        if ar:
            bs.append(ar)
    if not cg:
        return ([f'  no fills in the last {config.window_sec}s across {len(aq)} instance(s)'] + bs, [])
    af = defaultdict(lambda: {'maker': [], 'taker': []})
    for t in cg:
        ak = t.get('connector_name')
        ab = (t.get('trading_pair') or '').split('-')[0].upper()
        if not ab:
            continue
        if ak == config.maker_connector:
            af[ab]['maker'].append(t)
        elif ak == config.taker_connector:
            af[ab]['taker'].append(t)
    ay.extend(bs)
    if by:
        ay.append(f"  ({len(by)} inactive session(s) skipped - dead sessions cannot be killed and their fills are not this controller's)")
    for ab, ax in sorted(af.items()):
        bh, cf = (ax['maker'], ax['taker'])
        n = len(bh)
        if not bh or not cf:
            ay.append(f'  {ab:<9} one leg only in window (maker={len(bh)} taker={len(cf)}) - no verdict')
            continue
        if n < config.min_fills:
            ay.append(f'  {ab:<9} {n} maker fill(s) < min_fills - no verdict')
            continue
        bc = sum((1 for t in bh if (t.get('trade_type') or '').upper() == 'BUY')) >= len(bh) / 2
        bj, bg = _vwap(bh)
        cj, _ = _vwap(cf)
        bz = _spread_bps(bj, cj, bc)
        ck = 'KILL' if bz < config.kill_threshold_bps else 'ok'
        ay.append(f'  {ab:<9} {n:>3} fills  maker_vwap={bj:<14.8g} taker_vwap={cj:<14.8g} spread={bz:>7.2f}bp  {ck}')
        if ck == 'KILL':
            aw.append({'base': ab, 'spread': bz, 'fills': n})
    return (ay, aw)

async def _flip_controller(client, ad: str, ai: str, ab: str) -> str:
    """Redeploy this controller with maker and taker venues SWAPPED.

    Legitimate mid-tick because it does NOT change the trade. Which leg rests is an EXECUTION
    choice: the direction, the position and therefore the funding are identical either way, so
    no fresh funding read is needed and the tick boundary is not required. Changing direction or
    opening a new base IS a funding decision and stays on the tick, where funding is re-read.

    A bleed usually means our resting order is the one being picked off on that venue; resting
    on the other side of the same trade often is not.
    """
    try:
        ah = await client.controllers.get_bot_controller_configs(ad)
        am = None
        for c in ah if isinstance(ah, list) else []:
            if str(c.get('id', '')).lower() == ai.lower() or ab.lower() in str(c.get('id', '')).lower():
                am = c
                break
        if not am:
            return f'  !! {ab}: no config found to flip'
        at = dict(am)
        at['maker_connector'], at['taker_connector'] = (am.get('taker_connector'), am.get('maker_connector'))
        at['maker_trading_pair'], at['taker_trading_pair'] = (am.get('taker_trading_pair'), am.get('maker_trading_pair'))
        at['maker_side_str'] = 'SELL' if str(am.get('maker_side_str', 'BUY')).upper() == 'BUY' else 'BUY'
        await client.controllers.update_bot_controller_config(ad, ai, at)
        _FLIPS[ab] = _FLIPS.get(ab, 0) + 1
        return f'  FLIPPED {ab}: now rests on {at['maker_connector']} {at['maker_side_str']} (was {am.get('maker_connector')} {am.get('maker_side_str')})'
    except Exception as e:
        return f'  !! {ab}: flip failed: {e}'

async def _stop_controllers(client, aw, config: Config) -> list[str]:
    """Stop only the controllers whose base is in `kills`. Never stops the whole bot.

    There is no per-controller stop endpoint. The mechanism is the controller config's own
    `manual_kill_switch`: setting it true on ONE controller halts that controller and leaves
    every other controller in the same bot running. `bot_orchestration.stop_bot` would take
    all of them down, which is exactly the over-reaction this guard exists to avoid.
    """
    bn = []
    try:
        br = await client.bot_orchestration.get_active_bots_status()
        ae = (br.get('data') if isinstance(br, dict) else br) or {}
    except Exception as e:
        return [f'  !! cannot list bots to stop: {e}']
    ce = {k['base'].lower() for k in aw}
    for ad, info in ae.items() if isinstance(ae, dict) else []:
        bo = (info or {}).get('performance') or {} if isinstance(info, dict) else {}
        for al in bo:
            ai = str(al)
            bb = ai.lower()
            if not any((b in bb for b in ce)):
                continue
            ab = next((b for b in ce if b in bb), ai)
            if config.flip_on_bleed and _FLIPS.get(ab, 0) < config.max_flips_per_base:
                bn.append(await _flip_controller(client, ad, ai, ab))
                continue
            try:
                await client.controllers.update_bot_controller_config(ad, ai, {'manual_kill_switch': True})
                bt = 'bled again after a flip - the pair is wrong, not the venue' if _FLIPS.get(ab, 0) else 'flip disabled'
                bn.append(f'  KILL-SWITCHED {ad}/{ai} ({bt})')
            except Exception as e:
                bn.append(f'  !! failed to kill {ad}/{ai}: {e}')
    if not bn:
        bn.append('  (no running controller matched the killed bases)')
    return bn

async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    client = await get_client(context._chat_id, context=context)
    if not client:
        return 'No server available'
    bi = 'ARMED' if config.arm else 'DRY-RUN (reporting only)'
    logger.info('fill_guard starting: %s, every %ss', bi, config.interval_sec)
    while True:
        try:
            ay, aw = await _check_once(client, config)
            ca = time.strftime('%H:%M:%S')
            ac = [f'[{ca}] fill_guard  window={config.window_sec}s  {bi}'] + ay
            if aw:
                if config.arm:
                    ac += await _stop_controllers(client, aw, config)
                else:
                    ac.append('  WOULD STOP: ' + ', '.join((f'{k['base']} ({k['spread']:.1f}bp)' for k in aw)) + '   - set arm=True to act')
            logger.info('\n'.join(ac))
        except asyncio.CancelledError:
            logger.info('fill_guard cancelled')
            raise
        except Exception as e:
            logger.exception('fill_guard pass failed (continuing): %s', e)
        await asyncio.sleep(config.interval_sec)
