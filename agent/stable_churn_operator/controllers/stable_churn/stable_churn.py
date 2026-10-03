"""stable_churn - a volume-churning algorithm for stablecoins (Hummingbot V2 controller).

Binance spot stablecoin pairs such as USD1/USDT charge 0% maker AND 0% taker, and the spread sits at one 0.1 bp tick.
So a dollar of volume costs at most half a tick (0.05 bp) and carries no crypto price risk, only the peg:

  * Start: optionally sell another funding stablecoin into the quote (bootstrap_pair), then one market order to 50/50.
  * Maker, two-sided, every tick: one post-only order at the CURRENT touch per side, sized to all the balance that side
    can fund (size_all), re-posted when the touch moves; one tick inside a 2+ tick spread (improve_inside).
  * Taker only when behind a linear volume schedule to volume_target_usd: a paced rebalance when a side has sold out,
    or one clip when far behind - both only across a thin level (taker_imbalance_max).
  * Guards: fee and drawdown kill-switches, peg band, reject backoff, supervisor pause / close_now switches. No end state
    by default (close_at_end false): it runs until stopped.

Almost every field is live-updatable; the controller logs "CONFIG UPDATE applied" per change and four short STATUS
lines every minute (each <= 77 chars: Condor's log tool cuts messages at 80). Volume and fees are counted from executor fills, keyed by executor id, so pruned executors are never lost.
Live-tested on Binance 2026-09-29 (official hummingbot 2.17.0): 97% maker, zero fees.
"""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional

from pydantic import Field

from hummingbot.core.data_type.common import MarketDict, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction


class StableChurnConfig(ControllerConfigBase):
    controller_type: str = "generic"
    controller_name: str = "stable_churn"

    connector_name: str = Field("binance", description="Spot connector with a zero-fee stable pair.")
    trading_pair: str = Field("USD1-USDT", description="Zero-fee stablecoin pair. Base is the starting asset.")

    volume_target_usd: Decimal = Field(
        Decimal("2500000"), json_schema_extra={"is_updatable": True},
        description="Volume to reach by the end of the race. Worst-case cost = 0.05 bp x this.")
    clip_usd: Decimal = Field(
        Decimal("400"), json_schema_extra={"is_updatable": True},
        description="Size of each resting maker order and each taker clip, in quote. With a 50/50 base on $800 this is "
                    "one order per side.")
    size_all: bool = Field(
        True, json_schema_extra={"is_updatable": True},
        description="Size each NEW maker order to ALL available balance of its funding coin (clip_usd then only caps "
                    "taker clips). Backtest: doubles free maker volume ($27-29k/h vs $14-15k/h) and halves the cost of a "
                    "$2M schedule (0.018-0.026 vs 0.036-0.039 bp), because USDC/USDT trades are large and one trade "
                    "fills whatever we rest once the queue ahead clears. Existing orders are not resized (keeps queue).")
    improve_inside: bool = Field(
        True, json_schema_extra={"is_updatable": True},
        description="When the spread is 2+ ticks, quote one tick INSIDE it: we become the new best price with an empty "
                    "queue. At exactly 2 ticks only one side steps in (both would share one price and self-cross). "
                    "Backtest: maker share on USD1 @ $3M 71-92% -> 98-99.7% (weekday), and better on every window.")
    rebalance_to: Decimal = Field(
        Decimal("0.9"), json_schema_extra={"is_updatable": True},
        description="Paced rebalance size: when one side has sold out while behind schedule, move this fraction of the "
                    "account back into the empty side with one market order. Backtest (1 s tick, 3 datasets x 2 "
                    "queue policies, grid 0.5/0.75/0.9 x behind_clips 5/10/20): 0.9 with behind_clips 10 best overall "
                    "(the next maker on that side is bigger); 0.5 worst.")
    band_usd: Decimal = Field(
        Decimal("400"), json_schema_extra={"is_updatable": True},
        description="Max imbalance vs 50/50 (base value minus half the account). The side that would breach it is not "
                    "quoted. Also caps peg exposure: at most half the account is ever tilted to one coin.")
    behind_clips: Decimal = Field(
        Decimal("10"), json_schema_extra={"is_updatable": True}, description="How many clips behind schedule before a SCHEDULE taker fires. The cheaper paced "
                                   "rebalance (a side just emptied while behind at all) fires regardless. Swept "
                                   "2/5/10/20 x clip 200/400/800 on two USD1 windows: 10 x 400 cheapest at ~target.")
    taker_imbalance_max: Decimal = Field(
        Decimal("0.3"), json_schema_extra={"is_updatable": True},
        description="Queue-imbalance timing for takers: cross only while the level we would hit holds at most this "
                    "share of the top-of-book size (bid+ask). A thin ask is about to be consumed and tick up, so "
                    "buying it then gets the half-tick back. 1.0 = off. Backtest (1 s, 4 USD1 windows, both queue "
                    "policies): 0.3 cut pessimistic cost on the quiet out-of-sample window -0.023 -> -0.010 bp/$ and "
                    "never hurt elsewhere, at the same volume.")
    imbalance_valve: Decimal = Field(
        Decimal("3"), json_schema_extra={"is_updatable": True}, description="Ignore taker_imbalance_max once behind schedule by more than this many times "
                                  "behind_clips x clip_usd, so the volume target can never stall.")
    max_fee_bps: Decimal = Field(
        Decimal("0.5"), json_schema_extra={"is_updatable": True}, description="FEE KILL-SWITCH. The whole strategy assumes the pair is 0% maker AND taker. If the "
                                   "fees actually charged on our fills exceed this (bps of our volume, after the first "
                                   "$2k), the promo has ended or does not apply: stop everything and never restart. "
                                   "At standard spot fees (10 bp) a $3M target would cost ~$3,000.")
    max_drawdown_usd: Decimal = Field(
        Decimal("20"), json_schema_extra={"is_updatable": True}, description="INDEPENDENT second kill-switch: stop everything, permanently, if the account's value "
                                   "in this pair (both coins at mid) falls this far below its value at the first tick. "
                                   "Normal 48 h P&L is within +/-$5 and price noise ~$0.13, so $20 never false-trips; it "
                                   "catches fees the fee field misses (e.g. a failed BNB conversion reads as 0).")
    bootstrap_pair: str = Field(
        "auto", description="Funding in another stablecoin: 'auto' = at start, sell ALL of any USDC / FDUSD / USD1 that is not "
                        "one of this pair's coins into USDT (one market order each, on <COIN>-USDT), then the 50/50 "
                        "start. Or name one pair, e.g. 'USDC-USDT'. Empty = off (use off on an account that holds other "
                        "coins you want kept). The quote must be this controller's quote asset.")
    start_balanced: bool = Field(
        True, description="At start (after any bootstrap), check the balances and bring the account to 50/50 in this "
                          "pair's two coins with one market order before quoting - e.g. an all-USDT deposit buys half "
                          "in USD1. Costs half a tick on half the account (~$0.002 on $800). Skipped during a depeg.")
    peg_low: Decimal = Field(Decimal("0.998"), json_schema_extra={"is_updatable": True},
                             description="Halt trading if mid < this (depeg guard).")
    peg_high: Decimal = Field(Decimal("1.002"), json_schema_extra={"is_updatable": True},
                              description="Halt trading if mid > this.")
    pause: bool = Field(
        False, json_schema_extra={"is_updatable": True},
        description="Supervisor switch: true = cancel every order and stand aside (no close, inventory kept). false = "
                    "resume. The volume schedule does not build up a debt while paused: on resume the remaining "
                    "target is spread over the remaining time.")
    close_base_share: Decimal = Field(
        Decimal("0.5"), json_schema_extra={"is_updatable": True},
        description="What the close rebalances to, as the base coin's share of the account: 0.5 = 50/50 (default, the "
                    "race finish), 0 = sell ALL base (exit a falling base coin), 1 = buy all base (exit a falling quote "
                    "coin). Used by close_now and by the end-of-race close.")
    close_now: bool = Field(
        False, json_schema_extra={"is_updatable": True},
        description="Supervisor switch: true = enter the end-of-race close NOW (cancel makers, rebalance to 50/50, then "
                    "idle). false = back to churning.")

    tick_interval_s: float = Field(
        1.0, description="Controller decision interval. Hummingbot's default is 1.0 s; the backtest put ~10% more maker "
                         "volume at 0.25-0.5 s (reacting to a moved touch sooner). Safe from duplicate orders: the base "
                         "controller waits for executors_info to refresh after every batch of actions.")
    race_end_ts: int = Field(0, json_schema_extra={"is_updatable": True},
                             description="Unix seconds of the bell; 0 = first tick + race_duration_s.")
    race_duration_s: int = Field(172800, description="48h.")
    stop_lead_s: int = Field(900, json_schema_extra={"is_updatable": True},
                             description="With close_at_end: stop churning this long before the bell, then rebalance "
                                         "to 50/50. Also where the volume schedule reaches volume_target_usd.")
    close_at_end: bool = Field(
        False, json_schema_extra={"is_updatable": True},
        description="false (race default): NO end state - keep churning until the organisers stop the bot; past the "
                    "planned end the volume schedule simply continues at the same pace. true: stop_lead_s before the "
                    "bell, cancel makers, rebalance to close_base_share and idle (used by the 1 h live test).")

    def bootstrap_pairs(self) -> List[str]:
        """The conversion pairs the start-up bootstrap may sell on (see bootstrap_pair)."""
        if self.bootstrap_pair.strip().lower() == "auto":
            base, quote = self.trading_pair.split("-")
            if quote != "USDT":
                return []
            return [f"{coin}-USDT" for coin in ("USDC", "FDUSD", "USD1") if coin not in (base, quote)]
        return [self.bootstrap_pair] if self.bootstrap_pair else []

    def update_markets(self, markets: MarketDict) -> MarketDict:
        pairs = {self.trading_pair} | set(self.bootstrap_pairs())
        markets[self.connector_name] = markets.get(self.connector_name, set()) | pairs
        return markets


class StableChurnController(ControllerBase):

    def __init__(self, config: StableChurnConfig, *args, **kwargs):
        self.config = config
        kwargs.setdefault("update_interval", float(config.tick_interval_s))
        super().__init__(config, *args, **kwargs)
        self._t0: Optional[float] = None
        self._filled_quote: Dict[str, Decimal] = {}     # executor id -> max filled quote seen
        self._halt_logged = False
        self._fees: Dict[str, Decimal] = {}               # executor id -> max cum fees seen (quote)
        self._fee_killed = False
        self._value0: Optional[Decimal] = None
        self._dd_killed = False
        self._boot_done = not config.bootstrap_pairs()
        self._start_done = not config.start_balanced
        self._maker_ids: set = set()                       # executor ids that were LIMIT_MAKER (maker-share metric)
        self._anchor = None                               # (t, scheduled volume at t, target, end) - see _schedule
        self._last_status = 0.0
        self._failed_ids: set = set()                     # executors that ended FAILED with nothing filled
        self._fail_streak = 0
        self._backoff_until = 0.0
        self._hist: List[tuple] = []                      # (t, volume, maker volume) at each STATUS, for 1 h rates

    # ---- live config updates (hummingbot re-reads the yml every ~10 s; only is_updatable fields apply) ----

    def update_config(self, new_config):
        keys = [k for k, f in type(self.config).model_fields.items() if (f.json_schema_extra or {}).get("is_updatable")]
        before = {k: getattr(self.config, k) for k in keys}          # snapshot: some versions update in place
        super().update_config(new_config)
        changed = [f"{k}: {before[k]} -> {getattr(self.config, k)}" for k in keys if before[k] != getattr(self.config, k)]
        if changed:
            self.logger().info(f"[{self.config.id}] CONFIG UPDATE applied: " + "; ".join(changed))

    # ---- venue truth -----------------------------------------------------

    def _balances(self):
        base, quote = self.config.trading_pair.split("-")
        c = self.market_data_provider.get_connector(self.config.connector_name)
        return Decimal(str(c.get_balance(base))), Decimal(str(c.get_balance(quote)))

    def _available(self):
        """Balances NOT locked in our own open orders (what a new order can actually use)."""
        base, quote = self.config.trading_pair.split("-")
        c = self.market_data_provider.get_connector(self.config.connector_name)
        get = getattr(c, "get_available_balance", None) or c.get_balance
        return Decimal(str(get(base))), Decimal(str(get(quote)))

    def _tick(self) -> Decimal:
        """Price increment of the pair from the connector's trading rules (0 if unavailable -> no improvement)."""
        try:
            rules = self.market_data_provider.get_trading_rules(self.config.connector_name, self.config.trading_pair)
            return Decimal(str(rules.min_price_increment))
        except Exception:
            return Decimal("0")

    def _price(self, price_type) -> Decimal:
        return Decimal(str(self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, price_type)))

    def _touch_sizes(self):
        """(best bid size, best ask size) in base, or None if the book cannot be read (then takers are not gated)."""
        try:
            ob = self.market_data_provider.get_order_book(self.config.connector_name, self.config.trading_pair)
            b, a = next(ob.bid_entries(), None), next(ob.ask_entries(), None)
            if b is None or a is None or b.amount + a.amount <= 0:
                return None
            return Decimal(str(b.amount)), Decimal(str(a.amount))
        except Exception:
            return None

    def volume_usd(self) -> Decimal:
        for ex in self.executors_info:
            q = Decimal(str(ex.filled_amount_quote or 0))
            if q > self._filled_quote.get(ex.id, Decimal("0")):
                self._filled_quote[ex.id] = q
            f = Decimal(str(getattr(ex, "cum_fees_quote", 0) or 0))
            if f > self._fees.get(ex.id, Decimal("0")):
                self._fees[ex.id] = f
        return sum(self._filled_quote.values(), Decimal("0"))

    def maker_volume_usd(self) -> Decimal:
        for ex in self.executors_info:
            if getattr(ex.config, "execution_strategy", None) == ExecutionStrategy.LIMIT_MAKER:
                self._maker_ids.add(ex.id)
        return sum((v for k, v in self._filled_quote.items() if k in self._maker_ids), Decimal("0"))

    def _fee_bp(self, maker: bool) -> Decimal:
        """Fees charged, in bp of volume, on maker (or taker) fills only - tells a supervisor which side is charged."""
        ids = [k for k in self._filled_quote if (k in self._maker_ids) == maker]
        vol = sum((self._filled_quote[k] for k in ids), Decimal("0"))
        fee = sum((self._fees.get(k, Decimal("0")) for k in ids), Decimal("0"))
        return fee / vol * Decimal("10000") if vol else Decimal("0")

    def _schedule(self, now: float, end: float, volume: Decimal, standing_aside: bool) -> Decimal:
        """Scheduled volume by `now`: linear from an anchor to volume_target_usd at (end - stop_lead_s).

        Re-anchored (no jump) whenever the target or the end changes mid-run, and while standing aside (pause, depeg)
        so no catch-up debt builds up: the remaining target is then spread over the remaining time.
        """
        c = self.config
        stop = end - c.stop_lead_s
        target = c.volume_target_usd

        def at(anchor, t):
            t_a, s_a, tgt, stp = anchor
            frac = max(0.0, (t - t_a) / max(1.0, stp - t_a))
            if c.close_at_end:
                frac = min(1.0, frac)                         # no end state: the pace simply continues past the plan
            return s_a + (tgt - s_a) * Decimal(str(frac))

        if self._anchor is None:
            self._anchor = (self._t0, Decimal("0"), target, stop)
        elif (self._anchor[2], self._anchor[3]) != (target, stop) or standing_aside:
            s_now = at(self._anchor, now)
            self._anchor = (now, min(s_now, volume) if standing_aside else s_now, target, stop)
        return at(self._anchor, now)

    def fees_usd(self) -> Decimal:
        return sum(self._fees.values(), Decimal("0"))

    async def update_processed_data(self):
        now = self.market_data_provider.time()
        if self._t0 is None:
            self._t0 = now
        end = self.config.race_end_ts or (self._t0 + self.config.race_duration_s)
        mid = self._price(PriceType.MidPrice)
        base_bal, quote_bal = self._balances()
        volume = self.volume_usd()
        c = self.config
        aside = c.pause or c.close_now or not (c.peg_low <= mid <= c.peg_high)
        self.processed_data = {
            "now": now, "end": end, "mid": mid, "base": base_bal, "quote": quote_bal,
            "volume": volume, "target_now": self._schedule(now, end, volume, aside),
            "maker_volume": self.maker_volume_usd(),
        }
        if now - self._last_status >= 60:
            self._last_status = now
            self._hist = [h for h in self._hist if now - h[0] <= 3660] + [(now, volume, self.processed_data["maker_volume"])]
            for line in self.status_lines():
                self.logger().info(line)

    def status_lines(self) -> List[str]:
        """STATUS as four short lines. Condor's manage_bots(action="logs") cuts every log message to 80 characters
        (77 + "..."), and the supervisor reads STATUS through it - one long line lost fees, P&L and the peg. Each line
        stays <= 77 characters even at worst-case values; read the newest STATUS..STATUS4 group."""
        f = dict(kv.split("=", 1) for kv in self.status_fields() if "=" in kv)
        if not f:
            return ["STATUS state=STARTING"]
        return [f"STATUS state={f['state']} volume={f['volume']} schedule={f['schedule']}",
                f"STATUS2 vol_1h={f['vol_1h']} maker_share_1h={f['maker_share_1h']} target={f['target']}",
                f"STATUS3 fees={f['fees']} fee_bp_maker={f['fee_bp_maker']} fee_bp_taker={f['fee_bp_taker']} "
                f"pnl={f['pnl']}",
                f"STATUS4 mid={f['mid']} base_share={f['base_share']} value={float(f['value']):.2f} "
                f"hours_left={float(f['hours_left']):.1f}"]

    def status_fields(self) -> List[str]:
        """One machine-readable line for the supervisor agent (also in the Hummingbot `status` command)."""
        pd = self.processed_data or {}
        if not pd:
            return ["starting"]
        c = self.config
        mid, vol = pd["mid"], pd["volume"]
        value = pd["base"] * mid + pd["quote"]
        state = ("KILLED_FEE" if self._fee_killed else "KILLED_DRAWDOWN" if self._dd_killed else "PAUSED" if c.pause
                 else "DEPEG_HALT" if not (c.peg_low <= mid <= c.peg_high)
                 else "BACKOFF" if pd["now"] < self._backoff_until
                 else "CLOSING" if (c.close_now or (c.close_at_end and pd["now"] >= pd["end"] - c.stop_lead_s))
                 else "CHURNING")
        t_old, v_old, m_old = self._hist[0] if self._hist else (pd["now"], vol, pd["maker_volume"])
        dv, dm, dt = vol - v_old, pd["maker_volume"] - m_old, max(1.0, pd["now"] - t_old)
        return [f"state={state}", f"volume={vol:.0f}", f"schedule={pd['target_now']:.0f}",
                f"vol_1h={dv * Decimal(str(3600 / dt)):.0f}", f"maker_share_1h={(dm / dv if dv else 0):.3f}",
                f"target={c.volume_target_usd:.0f}",
                f"maker_share={(pd['maker_volume'] / vol if vol else 0):.3f}",
                f"fees={self.fees_usd():.4f}", f"fee_bp_maker={self._fee_bp(True):.3f}",
                f"fee_bp_taker={self._fee_bp(False):.3f}", f"value={value:.4f}",
                f"pnl={(value - self._value0) if self._value0 is not None else 0:+.4f}",
                f"mid={mid}", f"base_share={(pd['base'] * mid / value if value else 0):.3f}",
                f"hours_left={max(0.0, (pd['end'] - pd['now']) / 3600):.2f}"]

    # ---- actions ---------------------------------------------------------

    @staticmethod
    def _qty(quote_usd: Decimal, px: Decimal) -> Decimal:
        """Whole base units (USDC stepSize 1), rounded DOWN so an order never exceeds the balance."""
        return (quote_usd / px).quantize(Decimal("1"), rounding=ROUND_DOWN)

    def _bootstrap(self, active) -> List[ExecutorAction]:
        """Sell every other stablecoin the account was funded in for our quote (one market order each), then churn."""
        c = self.config
        if active:
            return []                                              # wait for the conversion to finish
        conn = self.market_data_provider.get_connector(c.connector_name)
        get = getattr(conn, "get_available_balance", None) or conn.get_balance
        for pair in c.bootstrap_pairs():
            b_base = pair.split("-")[0]
            amt = Decimal(str(get(b_base))).quantize(Decimal("1"), rounding=ROUND_DOWN)
            if amt * Decimal("0.99") < Decimal("6"):
                continue
            self.logger().info(f"[{c.id}] bootstrap: selling {amt} {b_base} on {pair}")
            cfg = OrderExecutorConfig(timestamp=self.market_data_provider.time(), connector_name=c.connector_name,
                                      trading_pair=pair, side=TradeType.SELL, amount=amt,
                                      execution_strategy=ExecutionStrategy.MARKET, controller_id=c.id)
            return [CreateExecutorAction(executor_config=cfg, controller_id=c.id)]
        self._boot_done = True
        self.logger().info(f"[{c.id}] bootstrap: nothing (left) to convert; starting")
        return []

    def _order(self, side: TradeType, amount_base: Decimal, price: Optional[Decimal], maker: bool) -> CreateExecutorAction:
        cfg = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=side,
            amount=amount_base,
            price=price,
            execution_strategy=ExecutionStrategy.LIMIT_MAKER if maker else ExecutionStrategy.MARKET,
            controller_id=self.config.id,
        )
        return CreateExecutorAction(executor_config=cfg, controller_id=self.config.id)

    def _close(self, active, pd, share: Optional[Decimal] = None) -> List[ExecutorAction]:
        """Cancel resting makers, then one market order to `close_base_share` of the account in base, then idle.

        At 0.5 (the race finish) a peg move in either coin at the bell touches at most half the account.
        """
        c = self.config
        makers = [ex for ex in active if ex.config.execution_strategy == ExecutionStrategy.LIMIT_MAKER]
        if makers:
            return [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in makers]
        if active:
            return []                                                 # a taker is still working: wait for it
        mid = pd["mid"]
        base_val, quote_val = pd["base"] * mid, pd["quote"]
        share = c.close_base_share if share is None else share
        gap = base_val - (base_val + quote_val) * share                # >0: too much base -> sell the gap
        if abs(gap) < Decimal("6"):
            return []
        if gap > 0:
            return [self._order(TradeType.SELL, self._qty(gap, self._price(PriceType.BestBid)), None, maker=False)]
        return [self._order(TradeType.BUY, self._qty(-gap, self._price(PriceType.BestAsk)), None, maker=False)]

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """Decide, then apply the reject BACKOFF: the exchange rejecting every order (API permissions, balance, a
        filter) must not become a request per second for 48 h - that risks an IP ban for every bot on the host."""
        now = self.market_data_provider.time()
        for ex in self.executors_info:
            if ex.is_active or ex.id in self._failed_ids:
                continue
            if getattr(ex, "close_type", None) == CloseType.FAILED and not (ex.filled_amount_quote or 0):
                self._failed_ids.add(ex.id)
                self._fail_streak += 1
                if self._fail_streak >= 2:
                    wait = min(600.0, 30.0 * 2 ** (self._fail_streak - 2))
                    self._backoff_until = now + wait
                    self.logger().warning(f"[{self.config.id}] BACKOFF: {self._fail_streak} orders in a row rejected by "
                                          f"the exchange with nothing filled; no new orders for {wait:.0f} s")
            elif (ex.filled_amount_quote or 0) > 0:
                self._fail_streak = 0
        actions = self._decide()
        if now < self._backoff_until:
            actions = [a for a in actions if not isinstance(a, CreateExecutorAction)]
        return actions

    def _decide(self) -> List[ExecutorAction]:
        pd = self.processed_data
        if not pd:
            return []
        c = self.config
        mid, base_bal, quote_bal, now = pd["mid"], pd["base"], pd["quote"], pd["now"]
        active = [ex for ex in self.executors_info if ex.is_active]

        # BOOTSTRAP (optional): convert a funding coin (e.g. USDC) into our quote asset once, before anything else.
        if not self._boot_done:
            return self._bootstrap(active)

        # DRAWDOWN KILL-SWITCH (independent of fee reporting). One-way.
        value = pd["base"] * mid + pd["quote"]
        if self._value0 is None:
            self._value0 = value
        if self._dd_killed or self._value0 - value > c.max_drawdown_usd:
            if not self._dd_killed:
                self.logger().error(f"[{c.id}] DRAWDOWN KILL-SWITCH: value {value:.2f} vs start {self._value0:.2f} "
                                    f"(> ${c.max_drawdown_usd} down). Stopping everything, permanently.")
                self._dd_killed = True
            return [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in active]

        # FEE KILL-SWITCH: the 0% promo is the whole premise. One-way: once tripped, cancel and never trade again.
        vol, fees = pd["volume"], self.fees_usd()
        if self._fee_killed or (vol > Decimal("2000") and fees / vol * Decimal("10000") > c.max_fee_bps):
            if not self._fee_killed:
                self.logger().error(f"[{c.id}] FEE KILL-SWITCH: paid ${fees:.4f} on ${vol:,.0f} "
                                    f"({fees / vol * 10000:.2f} bp > {c.max_fee_bps}); the zero-fee promo is not "
                                    f"applying. Stopping everything, permanently.")
                self._fee_killed = True
            return [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in active]

        # supervisor pause: cancel and stand aside (inventory kept)
        if c.pause:
            return [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in active]

        # supervisor close: runs even outside the peg band - it is how the supervisor EXITS a depegging coin
        if c.close_now:
            return self._close(active, pd)

        # depeg guard: cancel and stand aside
        if not (c.peg_low <= mid <= c.peg_high):
            if not self._halt_logged:
                self.logger().warning(f"[{c.id}] mid {mid} outside peg band [{c.peg_low}, {c.peg_high}]: halted")
                self._halt_logged = True
            return [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in active]
        self._halt_logged = False

        base_val, quote_val = base_bal * mid, quote_bal
        if c.close_at_end and now >= pd["end"] - c.stop_lead_s:
            return self._close(active, pd)

        # START: check balances and go 50/50 with one market order, then hand over to churning
        if not self._start_done:
            if active and not any(ex.config.execution_strategy == ExecutionStrategy.LIMIT_MAKER for ex in active):
                return []                                             # the rebalance order is still working
            acts = self._close(active, pd, share=Decimal("0.5"))
            if acts:
                if any(isinstance(x, CreateExecutorAction) for x in acts):
                    self.logger().info(f"[{c.id}] start: base {base_val:.2f} / quote {quote_val:.2f} -> rebalancing to 50/50")
                return acts
            self._start_done = True
            self.logger().info(f"[{c.id}] start: balanced (base {base_val:.2f} / quote {quote_val:.2f}); churning")

        # ---- two-sided, chase-the-touch maker (queue-optimised; backtest results) ----
        # Backtest on 2 h of USDC/USDT order-book data: re-posting at the CURRENT touch on both sides fills ~$16k/h at ~0 cost;
        # keeping stale orders for queue priority, or laddering behind the touch, collapsed volume to $1-5k/h and
        # pinned inventory at the band, because this pair drifts enough that left-behind orders never come back.
        total = base_val + quote_val
        imb = base_val - total / 2                       # >0: tilted to USDC
        clip = c.clip_usd
        bid, ask = self._price(PriceType.BestBid), self._price(PriceType.BestAsk)
        makers = {TradeType.BUY: [], TradeType.SELL: []}
        takers = []
        for ex in active:
            strat = getattr(ex.config, "execution_strategy", None)
            if strat == ExecutionStrategy.LIMIT_MAKER:
                makers[ex.config.side].append(ex)
            else:
                takers.append(ex)
        actions: List[ExecutorAction] = []
        # tolerance: a stablecoin priced 1.0001 makes an exact 50/50 split read a few cents off, which must not
        # lock a side out when clip == band (the default: one order per side, oscillating 0 <-> +/-band)
        tol = clip * Decimal("0.02")
        allowed = {TradeType.BUY: imb + clip <= c.band_usd + tol, TradeType.SELL: imb - clip >= -c.band_usd - tol}
        touch = {TradeType.BUY: bid, TradeType.SELL: ask}
        if c.improve_inside:
            tick = self._tick()
            if tick > 0:
                n = ((ask - bid) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                if n >= 3:
                    touch = {TradeType.BUY: bid + tick, TradeType.SELL: ask - tick}
                elif n == 2:
                    # both sides inside would sit on the same price and self-cross: step in on the side that rests
                    # more size (more volume per fill)
                    if base_val >= quote_val:
                        touch = {TradeType.BUY: bid, TradeType.SELL: ask - tick}
                    else:
                        touch = {TradeType.BUY: bid + tick, TradeType.SELL: ask}

        # taker top-up when behind the volume schedule, on the side that pulls inventory back toward 50/50.
        # A resting maker LOCKS its balance on the venue, so a taker on the same side would be rejected for
        # insufficient balance: stop that maker first and send the taker on a later tick once it is gone.
        # Two taker paths (backtest: "paced rebalance + far-behind schedule" was the cheapest way to hit a target):
        #  a) PACED REBALANCE - behind schedule at all AND one side cannot fund an order (its coin was all sold):
        #     rebalance to 50/50 now, so BOTH sides quote again. It fires exactly when a maker leg just emptied a side,
        #     which keeps two-sided presence ~90%+ and made USD1/USDT volume cost 0.002-0.012 bp.
        #  b) SCHEDULE - far behind (behind_clips clips): one clip toward 50/50, as before.
        behind_any = pd["target_now"] > pd["volume"]
        behind_far = pd["target_now"] - pd["volume"] > c.behind_clips * clip
        tside, tsize = None, clip
        total_val = base_val + quote_val
        # never leave the side we move FROM under 12 (it would read "empty" (< 6) next tick and the rebalance would
        # ping-pong; live 2026-10-03 on a $50 book). At race size this is exactly rebalance_to (800 x 0.9 < 800 - 12).
        keep = Decimal("12")
        if behind_any and base_val < Decimal("6") and quote_val >= Decimal("12") + keep:
            tside, tsize = TradeType.BUY, min(quote_val - keep, total_val * c.rebalance_to)   # all quote: buy back
        elif behind_any and quote_val < Decimal("6") and base_val >= Decimal("12") + keep:
            tside, tsize = TradeType.SELL, min(base_val - keep, total_val * c.rebalance_to)   # all base: sell back
        elif behind_far:
            tside = TradeType.SELL if imb >= 0 else TradeType.BUY
        # Queue-imbalance timing: hold the taker (and keep the maker resting) until the level it would hit is thin,
        # unless we are so far behind that the target itself is at risk.
        valve = c.imbalance_valve * c.behind_clips * clip
        if tside is not None and c.taker_imbalance_max < 1 and pd["target_now"] - pd["volume"] <= valve:
            sizes = self._touch_sizes()
            if sizes is not None:
                hit = sizes[1] if tside == TradeType.BUY else sizes[0]
                if hit / (sizes[0] + sizes[1]) > c.taker_imbalance_max:
                    tside = None

        for side in (TradeType.BUY, TradeType.SELL):
            freeing = side == tside
            for ex in makers[side]:
                if freeing or (not c.size_all and not allowed[side]) or Decimal(str(ex.config.price)) != touch[side]:
                    actions.append(StopExecutorAction(controller_id=c.id, executor_id=ex.id))
            # Always rest at the touch, whatever its queue: the backtest showed skipping heavy levels (> $0.5-2M queue)
            # only moved volume to the taker leg and raised cost.
            if (c.size_all or allowed[side]) and not makers[side] and not freeing:
                if c.size_all:
                    free_base, free_quote = self._available()
                    size = free_quote if side == TradeType.BUY else free_base * mid
                    if size >= Decimal("6"):
                        actions.append(self._order(side, self._qty(size, touch[side]), touch[side], maker=True))
                else:
                    avail = quote_val if side == TradeType.BUY else base_val
                    if avail >= clip:
                        actions.append(self._order(side, self._qty(clip, touch[side]), touch[side], maker=True))

        if tside is not None and not takers and not makers[tside]:
            avail = base_val if tside == TradeType.SELL else quote_val
            size = tsize
            if size >= Decimal("6") and avail >= size:
                px = bid if tside == TradeType.SELL else ask
                actions.append(self._order(tside, self._qty(size, px), None, maker=False))
        return actions

    def to_format_status(self) -> List[str]:
        pd = self.processed_data or {}
        return [f"=== stable_churn [{self.config.id}] {self.config.connector_name} {self.config.trading_pair} ===",
                f"  mid {pd.get('mid')}  base {pd.get('base')}  quote {pd.get('quote')}",
                f"  volume ${pd.get('volume', 0):,.0f} / schedule ${pd.get('target_now', 0):,.0f} "
                f"/ target ${self.config.volume_target_usd:,.0f}",
                "  " + " ".join(self.status_fields())]
