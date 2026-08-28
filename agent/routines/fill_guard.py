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

def _vwap(ch) -> tuple[float, float]:
    """(vwap, total_base) over a list of {price, amount}."""
    ab = sum((abs(float(t['amount'])) for t in ch))
    if ab <= 0:
        return (0.0, 0.0)
    br = sum((abs(float(t['amount'])) * float(t['price']) for t in ch))
    return (br / ab, ab)

def _spread_bps(be: float, ce: float, bd: bool) -> float:
    """Realized cross-venue spread. Bought low / sold high is positive on both sides."""
    if be <= 0 or ce <= 0:
        return 0.0
    if bd:
        return (ce - be) / be * 10000.0
    return (be - ce) / be * 10000.0

def _instance_roots(instances_dir: str) -> list[str]:
    if instances_dir:
        bw = [instances_dir]
    else:
        ar = os.environ.get('BOTS_PATH', '')
        bw = [os.path.join(ar, 'bots', 'instances') if ar else '', os.path.join(os.getcwd(), '..', 'hummingbot-api', 'bots', 'instances'), os.path.join(os.getcwd(), 'bots', 'instances'), os.path.expanduser('~/hummingbot-api/bots/instances')]
    return [r for r in bw if r and os.path.isdir(r)]

def _find_dbs(instances_dir: str, bb: set[str] | None, bg: float) -> tuple[list[str], list[str]]:
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
    aq, bz = ([], [])
    bn = time.time()
    for bv in _instance_roots(instances_dir):
        for path in glob.glob(os.path.join(bv, '*', 'data', '*.sqlite')):
            bl = os.path.basename(os.path.dirname(os.path.dirname(path)))
            if bb is not None:
                if bl in bb:
                    aq.append(path)
                else:
                    bz.append(bl)
                continue
            try:
                av = bn - os.path.getmtime(path) <= bg
            except OSError:
                av = False
            (aq if av else bz).append(path if av else bl)
        if aq or bz:
            break
    return (aq, bz)

async def _live_bot_names(client) -> set[str] | None:
    """Bot names the API currently reports as active. None if it cannot be read."""
    try:
        bs = await client.bot_orchestration.get_active_bots_status()
        an = (bs.get('data') if isinstance(bs, dict) else bs) or {}
        return set(an.keys()) if isinstance(an, dict) else None
    except Exception as e:
        logger.warning('fill_guard: active-bot list unavailable (%s); falling back to mtime', e)
        return None

def _read_fills(ap: str, by: float) -> tuple[list[dict], str | None]:
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
    bx: list[dict] = []
    try:
        aj = sqlite3.connect(f'file:{ap}?mode=ro', uri=True, timeout=5.0)
        try:
            ag = aj.execute('SELECT config_file_path FROM TradeFill LIMIT 1').fetchone()
            if not ag:
                return ([], None)
            am = aj.execute('SELECT market, symbol, trade_type, price, amount, timestamp FROM TradeFill WHERE config_file_path = ? AND timestamp >= ?', (ag[0], int(by * 1000)))
            for bf, cd, cj, bq, aa, ci in am.fetchall():
                bx.append({'connector_name': bf, 'trading_pair': cd, 'trade_type': cj, 'price': float(bq), 'amount': float(aa), 'timestamp': ci})
        finally:
            aj.close()
    except Exception as e:
        return ([], f'  !! could not read {os.path.basename(ap)}: {e}')
    return (bx, None)

async def _check_once(client, config: Config) -> tuple[list[str], list[dict]]:
    """One pass. Returns (report_lines, controllers_to_kill)."""
    bn = time.time()
    cc = bn - config.window_sec
    az, ax = ([], [])
    ba = await _live_bot_names(client)
    aq, bz = _find_dbs(config.instances_dir, ba, config.max_stale_sec)
    if not aq:
        aw = 'no bot reported active' if ba is not None else 'no recently-written instance db'
        bm = f'  nothing to judge - {aw}'
        if bz:
            bm += f' ({len(bz)} inactive session(s) skipped)'
        return ([bm], [])
    ch, bt = ([], [])
    for ao in aq:
        bx, at = _read_fills(ao, cc)
        ch.extend(bx)
        if at:
            bt.append(at)
    if not ch:
        return ([f'  no fills in the last {config.window_sec}s across {len(aq)} instance(s)'] + bt, [])
    af = defaultdict(lambda: {'maker': [], 'taker': []})
    for t in ch:
        ak = t.get('connector_name')
        ab = (t.get('trading_pair') or '').split('-')[0].upper()
        if not ab:
            continue
        if ak == config.maker_connector:
            af[ab]['maker'].append(t)
        elif ak == config.taker_connector:
            af[ab]['taker'].append(t)
    az.extend(bt)
    if bz:
        az.append(f"  ({len(bz)} inactive session(s) skipped - dead sessions cannot be killed and their fills are not this controller's)")
    for ab, ay in sorted(af.items()):
        bi, cg = (ay['maker'], ay['taker'])
        n = len(bi)
        if not bi or not cg:
            az.append(f'  {ab:<9} one leg only in window (maker={len(bi)} taker={len(cg)}) - no verdict')
            continue
        if n < config.min_fills:
            az.append(f'  {ab:<9} {n} maker fill(s) < min_fills - no verdict')
            continue
        bd = sum((1 for t in bi if (t.get('trade_type') or '').upper() == 'BUY')) >= len(bi) / 2
        bk, bh = _vwap(bi)
        ck, _ = _vwap(cg)
        ca = _spread_bps(bk, ck, bd)
        cl = 'KILL' if ca < config.kill_threshold_bps else 'ok'
        az.append(f'  {ab:<9} {n:>3} fills  maker_vwap={bk:<14.8g} taker_vwap={ck:<14.8g} spread={ca:>7.2f}bp  {cl}')
        if cl == 'KILL':
            ax.append({'base': ab, 'spread': ca, 'fills': n})
    return (az, ax)

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
        au = dict(am)
        au['maker_connector'], au['taker_connector'] = (am.get('taker_connector'), am.get('maker_connector'))
        au['maker_trading_pair'], au['taker_trading_pair'] = (am.get('taker_trading_pair'), am.get('maker_trading_pair'))
        au['maker_side_str'] = 'SELL' if str(am.get('maker_side_str', 'BUY')).upper() == 'BUY' else 'BUY'
        await client.controllers.update_bot_controller_config(ad, ai, au)
        _FLIPS[ab] = _FLIPS.get(ab, 0) + 1
        return f'  FLIPPED {ab}: now rests on {au['maker_connector']} {au['maker_side_str']} (was {am.get('maker_connector')} {am.get('maker_side_str')})'
    except Exception as e:
        return f'  !! {ab}: flip failed: {e}'

async def _stop_controllers(client, ax, config: Config) -> list[str]:
    """Stop only the controllers whose base is in `kills`. Never stops the whole bot.

    There is no per-controller stop endpoint. The mechanism is the controller config's own
    `manual_kill_switch`: setting it true on ONE controller halts that controller and leaves
    every other controller in the same bot running. `bot_orchestration.stop_bot` would take
    all of them down, which is exactly the over-reaction this guard exists to avoid.
    """
    bo = []
    try:
        bs = await client.bot_orchestration.get_active_bots_status()
        ae = (bs.get('data') if isinstance(bs, dict) else bs) or {}
    except Exception as e:
        return [f'  !! cannot list bots to stop: {e}']
    cf = {k['base'].lower() for k in ax}
    for ad, info in ae.items() if isinstance(ae, dict) else []:
        bp = (info or {}).get('performance') or {} if isinstance(info, dict) else {}
        for al in bp:
            ai = str(al)
            bc = ai.lower()
            if not any((b in bc for b in cf)):
                continue
            ab = next((b for b in cf if b in bc), ai)
            if config.flip_on_bleed and _FLIPS.get(ab, 0) < config.max_flips_per_base:
                bo.append(await _flip_controller(client, ad, ai, ab))
                continue
            try:
                await client.controllers.update_bot_controller_config(ad, ai, {'manual_kill_switch': True})
                bu = 'bled again after a flip - the pair is wrong, not the venue' if _FLIPS.get(ab, 0) else 'flip disabled'
                bo.append(f'  KILL-SWITCHED {ad}/{ai} ({bu})')
            except Exception as e:
                bo.append(f'  !! failed to kill {ad}/{ai}: {e}')
    if not bo:
        bo.append('  (no running controller matched the killed bases)')
    return bo

async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    client = await get_client(context._chat_id, context=context)
    if not client:
        return 'No server available'
    bj = 'ARMED' if config.arm else 'DRY-RUN (reporting only)'
    logger.info('fill_guard starting: %s, every %ss', bj, config.interval_sec)
    while True:
        try:
            az, ax = await _check_once(client, config)
            cb = time.strftime('%H:%M:%S')
            ac = [f'[{cb}] fill_guard  window={config.window_sec}s  {bj}'] + az
            if ax:
                if config.arm:
                    ac += await _stop_controllers(client, ax, config)
                else:
                    ac.append('  WOULD STOP: ' + ', '.join((f'{k['base']} ({k['spread']:.1f}bp)' for k in ax)) + '   - set arm=True to act')
            logger.info('\n'.join(ac))
        except asyncio.CancelledError:
            logger.info('fill_guard cancelled')
            raise
        except Exception as e:
            logger.exception('fill_guard pass failed (continuing): %s', e)
        await asyncio.sleep(config.interval_sec)
