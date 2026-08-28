import logging
from decimal import Decimal
from typing import Dict, List, Optional, Union

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)
from hummingbot.core.rate_oracle.rate_oracle import RateOracle
from hummingbot.logger import HummingbotLogger
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.executors.perp_xemm_executor.data_types import PerpXEMMExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder


class PerpXEMMExecutor(ExecutorBase):
    _logger = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    @staticmethod
    @staticmethod
    def _normalize_token(token: str) -> str:
        # kPEPE (Hyperliquid kilo-prefix) and 1000PEPE (Binance/Aster) are the same contract
        if token.startswith("1000"):
            return token[4:]
        if len(token) > 1 and token[0] == "k" and token[1].isupper():
            return token[1:]
        return token

    @staticmethod
    def _are_tokens_interchangeable(first_token: str, second_token: str) -> bool:
        interchangeable_tokens = [
            {"WETH", "ETH"},
            {"WBTC", "BTC"},
            {"WBNB", "BNB"},
            {"WPOL", "POL"},
            {"WAVAX", "AVAX"},
            {"WONE", "ONE"},
            {"USDC", "USDC.E"},
            {"USOL", "SOL"},
            {"UETH", "ETH"},
            {"UBTC", "BTC"},
        ]
        if first_token == second_token:
            return True
        for pair in interchangeable_tokens:
            if {first_token, second_token} <= pair:
                return True
        if "USD" in first_token and "USD" in second_token:
            return True
        # Handle k<TOKEN> (Hyperliquid) == 1000<TOKEN> (Binance/Aster) convention
        norm1 = PerpXEMMExecutor._normalize_token(first_token)
        norm2 = PerpXEMMExecutor._normalize_token(second_token)
        return norm1 == norm2

    def is_arbitrage_valid(self, pair1: str, pair2: str) -> bool:
        base1, _ = split_hb_trading_pair(pair1)
        base2, _ = split_hb_trading_pair(pair2)
        return self._are_tokens_interchangeable(base1, base2)

    def __init__(self, strategy: StrategyV2Base, config: PerpXEMMExecutorConfig,
                 update_interval: float = 1.0, max_retries: int = 10):
        if not self.is_arbitrage_valid(config.buying_market.trading_pair, config.selling_market.trading_pair):
            raise Exception("PerpXEMM is not valid: trading pairs are not interchangeable.")
        if not (self.is_perpetual_connector(config.buying_market.connector_name)
                and self.is_perpetual_connector(config.selling_market.connector_name)):
            raise Exception("PerpXEMM requires both buying_market and selling_market to be perpetual connectors.")

        self.config = config
        if config.maker_side == TradeType.BUY:
            self.maker_connector = config.buying_market.connector_name
            self.maker_trading_pair = config.buying_market.trading_pair
            self.maker_order_side = TradeType.BUY
            self.taker_connector = config.selling_market.connector_name
            self.taker_trading_pair = config.selling_market.trading_pair
            self.taker_order_side = TradeType.SELL
        else:
            self.maker_connector = config.selling_market.connector_name
            self.maker_trading_pair = config.selling_market.trading_pair
            self.maker_order_side = TradeType.SELL
            self.taker_connector = config.buying_market.connector_name
            self.taker_trading_pair = config.buying_market.trading_pair
            self.taker_order_side = TradeType.BUY

        # Quote-currency normalization. taker_price (in taker_quote) is multiplied by
        # rate(taker_quote -> maker_quote) before edge computation so the bps edge
        # reflects same-currency comparison. Skipped (rate = 1) when quotes match, or
        # when RateOracle has no rate (warning is emitted in that case).
        _, self.maker_quote_asset = split_hb_trading_pair(self.maker_trading_pair)
        _, self.taker_quote_asset = split_hb_trading_pair(self.taker_trading_pair)
        self.quote_conversion_pair: Optional[str] = (
            f"{self.taker_quote_asset}-{self.maker_quote_asset}"
            if self.maker_quote_asset != self.taker_quote_asset
            else None
        )
        self.rate_oracle = RateOracle.get_instance()
        self._quote_conversion_rate: Decimal = Decimal("1")

        # Order tracking
        self.maker_order: Optional[TrackedOrder] = None
        self._completed_makers: List[TrackedOrder] = []
        self._cancelling_maker_orders: Dict[str, TrackedOrder] = {}  # orders sent to cancel, pending confirmation
        self._recently_completed_maker_ids: set = set()  # guard for late fills arriving after CompletedEvent
        self._pending_hedges: List[TrackedOrder] = []
        self._hedge_amounts: Dict[str, Decimal] = {}
        self.failed_orders: List[TrackedOrder] = []

        # Cumulative state
        self._cumulative_filled_base: Decimal = Decimal("0")
        self._unhedged_buffer_base: Decimal = Decimal("0")
        self._consecutive_hedge_failures: int = 0

        # Per-tick market state
        self._maker_px: Decimal = Decimal("0")  # quantized placement price (best ± limit_tick)
        self._maker_reference_price: Decimal = Decimal("0")  # best_bid (BUY) or best_ask (SELL) on maker venue
        self._taker_reference_price: Decimal = Decimal("0")  # best opposite-side price on taker venue
        self._taker_top_volume: Decimal = Decimal("0")

        # Per-maker order accounting (for partial-fill delta reasoning, if needed)
        self._maker_filled_for_current_order: Dict[str, Decimal] = {}
        self._cancel_reasons: Dict[str, str] = {}  # order_id → reason string, for confirmation logging

        # Throttled log timestamps
        self._last_gate_log_ts: float = 0.0
        self._last_size_log_ts: float = 0.0
        self._last_flush_skip_log_ts: float = 0.0
        self._last_quote_rate_log_ts: float = 0.0
        super().__init__(strategy=strategy,
                         connectors=[config.buying_market.connector_name, config.selling_market.connector_name],
                         config=config, update_interval=update_interval, max_retries=max_retries)

        self._maker_trading_rules = self.get_trading_rules(self.maker_connector, self.maker_trading_pair)
        self._taker_trading_rules = self.get_trading_rules(self.taker_connector, self.taker_trading_pair)

    # --------------------------------------------------------------------- balance

    async def validate_sufficient_balance(self):
        pass

    # --------------------------------------------------------------------- control loop

    async def control_task(self):
        if self.status == RunnableStatus.RUNNING:
            self._update_market_state()
            await self._control_maker_order()
        elif self.status == RunnableStatus.SHUTTING_DOWN:
            await self._control_shutdown_process()

    def _refresh_quote_conversion_rate(self) -> None:
        """Refresh self._quote_conversion_rate from RateOracle. Multiplies the taker
        reference price by this rate to bring it into the maker quote currency before
        edge computation. On missing/non-positive rate, falls back to 1 (treating the
        two stable quotes as 1:1 pegged) and emits a throttled warning."""
        if self.quote_conversion_pair is None:
            self._quote_conversion_rate = Decimal("1")
            return
        rate = self.rate_oracle.get_pair_rate(self.quote_conversion_pair)
        if rate is not None and rate > Decimal("0"):
            self._quote_conversion_rate = rate
            return
        self._quote_conversion_rate = Decimal("1")
        self._throttled_warn(
            "quote_rate",
            f"RateOracle returned no rate for {self.quote_conversion_pair}; "
            "defaulting to 1.0 (assumed 1:1 peg) for edge computation.",
        )

    def _compute_edge_bps(self) -> Decimal:
        """Cross-venue edge, anchored on raw best_bid/ask on the maker side (NOT _maker_px).
        This decouples the edge gate from limit_tick: the gate decides whether the cross-venue
        spread is wide enough to trade at all; limit_tick only controls queue position.

        Taker price is normalized into the maker quote currency via _quote_conversion_rate
        (== 1 when quotes match). This makes the bps edge currency-consistent across
        USDT/USDC/USD1-quoted venues.

        BUY maker: edge = (taker_norm - maker_best_bid) / taker_norm * 10000
        SELL maker: edge = (maker_best_ask - taker_norm) / taker_norm * 10000"""
        if self._taker_reference_price <= Decimal("0") or self._maker_reference_price <= Decimal("0"):
            return Decimal("0")
        taker_norm = self._taker_reference_price * self._quote_conversion_rate
        if taker_norm <= Decimal("0"):
            return Decimal("0")
        if self.maker_order_side == TradeType.BUY:
            return (taker_norm - self._maker_reference_price) / taker_norm * Decimal("10000")
        return (self._maker_reference_price - taker_norm) / taker_norm * Decimal("10000")

    def _update_market_state(self):
        # Pull a fresh stable-quote conversion rate first so _compute_edge_bps and the
        # gate see consistent state this tick.
        self._refresh_quote_conversion_rate()

        # Maker reference price: best bid (BUY) or best ask (SELL) on the maker venue.
        # This is the *raw* cross-venue spread anchor used by the edge gate — independent of
        # the depth we choose to anchor placement at.
        maker_tick = self._maker_trading_rules.min_price_increment
        if self.maker_order_side == TradeType.BUY:
            self._maker_reference_price = self.connectors[self.maker_connector].get_price(
                self.maker_trading_pair, False,
            )
        else:
            self._maker_reference_price = self.connectors[self.maker_connector].get_price(
                self.maker_trading_pair, True,
            )

        # Placement anchor: bid_{limit_depth} (BUY) or ask_{limit_depth} (SELL) on the maker book.
        # If the book is shallower than limit_depth, skip placement this tick.
        anchor = self._read_book_level(self.maker_connector, self.maker_trading_pair,
                                       self.maker_order_side, self.config.limit_depth)
        if anchor is None or anchor <= Decimal("0"):
            self._throttled_warn(
                "size",
                f"Maker book has fewer than limit_depth+1={self.config.limit_depth + 1} entries; "
                "skipping placement this tick.",
            )
            self._maker_px = Decimal("0")
        else:
            # `limit_tick` is signed: positive = step toward fill (aggressive), negative = away.
            # BUY:  maker_px = anchor + limit_tick * tick   (higher = more aggressive)
            # SELL: maker_px = anchor - limit_tick * tick   (lower  = more aggressive)
            if self.maker_order_side == TradeType.BUY:
                self._maker_px = anchor + Decimal(self.config.limit_tick) * maker_tick
            else:
                self._maker_px = anchor - Decimal(self.config.limit_tick) * maker_tick
            self._maker_px = self.connectors[self.maker_connector].quantize_order_price(
                self.maker_trading_pair, self._maker_px,
            )
        # Strip float-precision residue from connectors that build min_price_increment via from_float
        self._maker_px = self._clean_for_display(self._maker_px)
        self._maker_reference_price = self._clean_for_display(self._maker_reference_price)

        # Taker reference price: best price we'd hit when hedging (BUY taker → ask, SELL taker → bid)
        is_buy_taker = self.taker_order_side == TradeType.BUY
        self._taker_reference_price = self._clean_for_display(
            self.connectors[self.taker_connector].get_price(self.taker_trading_pair, is_buy_taker),
        )

        # Taker top-of-book volume on the side we'd hit
        try:
            order_book = self.connectors[self.taker_connector].get_order_book(self.taker_trading_pair)
            entries = order_book.ask_entries() if is_buy_taker else order_book.bid_entries()
            top = next(iter(entries), None)
            self._taker_top_volume = Decimal(str(top.amount)) if top is not None else Decimal("0")
        except Exception:
            self._taker_top_volume = Decimal("0")

    def _flush_done_cancelling_makers(self):
        """Drain _cancelling_maker_orders: once a cancel is confirmed (order terminal), move to
        _completed_makers so _maker_session_totals picks up their partial fills."""
        for oid in list(self._cancelling_maker_orders):
            tracked = self._cancelling_maker_orders[oid]
            if tracked.order is not None and tracked.is_done:
                reason = self._cancel_reasons.pop(oid, "unknown")
                self.logger().info(f"Cancel confirmed for maker order {oid} (reason: {reason}).")
                self._completed_makers.append(tracked)
                del self._cancelling_maker_orders[oid]

    async def _control_maker_order(self):
        self._flush_done_cancelling_makers()
        # Detect maker orders closed externally (e.g. exchange cancels the order 3ms after creation).
        # Without this guard, _control_update_maker_order sees is_open=False and early-returns every
        # tick, leaving maker_order set and blocking new placement indefinitely.
        if (self.maker_order is not None
                and self.maker_order.order is not None
                and self.maker_order.is_done):
            self.logger().warning(
                f"Maker order {self.maker_order.order_id} was closed externally "
                f"(not by executor refresh); clearing for re-placement.",
            )
            self._completed_makers.append(self.maker_order)
            self.maker_order = None
        remaining = self.config.total_amount - self._cumulative_filled_base - self._unhedged_buffer_base
        if remaining <= self._maker_trading_rules.min_order_size:
            if self.maker_order is None or not self.maker_order.is_open:
                self._status = RunnableStatus.SHUTTING_DOWN
            return

        if self.maker_order is None:
            self._create_maker_order()
        else:
            self._control_update_maker_order()

    def _create_maker_order(self):
        if self._maker_px <= Decimal("0"):
            # Set by _update_market_state when the maker book is shallower than limit_depth.
            return
        amount = self._compute_order_amount()
        if amount <= Decimal("0"):
            self._throttled_warn("size", "Computed maker order amount is below min_order_size; skipping placement.")
            return

        if not self._edge_gate_passes():
            self._throttled_warn(
                "gate",
                f"Edge gate failed: maker_px={self._maker_px} taker_ref={self._taker_reference_price} "
                f"edge={self._compute_edge_bps():.2f}bps required_bps={self.config.min_price_edge_bps}",
            )
            return

        # Use LIMIT_MAKER (post-only) — the venue rejects rather than executes as a taker
        # if maker_px ends up at or past the opposite book top (e.g. limit_tick=-1 in a
        # 1-tick spread). Protects realized edge from accidental adverse crossings.
        order_id = self.place_order(
            connector_name=self.maker_connector,
            trading_pair=self.maker_trading_pair,
            order_type=OrderType.LIMIT_MAKER,
            side=self.maker_order_side,
            amount=amount,
            price=self._maker_px,
            position_action=PositionAction.OPEN,
        )
        self.maker_order = TrackedOrder(order_id=order_id)
        self._maker_filled_for_current_order[order_id] = Decimal("0")
        self.logger().info(
            f"Created maker {self.maker_order_side.name} order {order_id} on {self.maker_connector} "
            f"at {self._maker_px} amount={amount}.",
        )

    def _compute_order_amount(self) -> Decimal:
        if self._maker_px <= Decimal("0"):
            return Decimal("0")
        impact_term = self.config.pct_impact * self._taker_top_volume
        max_term = self.config.max_notional / self._maker_px
        min_term = self.config.min_notional / self._maker_px
        desired = max(min(impact_term, max_term), min_term)
        remaining = self.config.total_amount - self._cumulative_filled_base - self._unhedged_buffer_base
        amount = min(desired, remaining)
        amount = self.connectors[self.maker_connector].quantize_order_amount(
            self.maker_trading_pair, amount,
        )
        if amount < self._maker_trading_rules.min_order_size:
            return Decimal("0")
        return amount

    def _edge_gate_passes(self) -> bool:
        if self._taker_reference_price <= Decimal("0") or self._maker_px <= Decimal("0"):
            return False
        return self._compute_edge_bps() >= self.config.min_price_edge_bps

    def _read_book_level(self, connector_name: str, trading_pair: str,
                         maker_side: TradeType, level: int) -> Optional[Decimal]:
        """Return the price at the given level (0-indexed: 0 = best bid/ask) of the relevant
        book side, or None if the book is shallower than `level + 1` entries.
        BUY anchors on bids; SELL anchors on asks."""
        try:
            order_book = self.connectors[connector_name].get_order_book(trading_pair)
            entries = list(order_book.bid_entries() if maker_side == TradeType.BUY
                           else order_book.ask_entries())
        except Exception:
            return None
        if len(entries) <= level:
            return None
        return Decimal(str(entries[level].price))

    @staticmethod
    def _clean_for_display(value: Decimal) -> Decimal:
        """Quantize to an 8-decimal grid to strip float-precision residue from connectors that
        construct ``min_price_increment`` via ``Decimal.from_float`` (e.g. hyperliquid). Sub-tick
        precision is well below any exchange's price increment, so this does not affect order
        placement (prices going to the connector are still passed through ``quantize_order_price``
        independently)."""
        if value is None:
            return value
        try:
            return value.quantize(Decimal("1E-8"))
        except Exception:
            return value

    def _control_update_maker_order(self):
        """Three independent triggers, evaluated in order:
          1. Depth refresh — live order pushed past the Nth book level on the maker side.
          2. Edge degradation — current cross-venue edge has decayed below
             refresh_edge_pct_threshold * min_price_edge_bps.
          3. Gap — too many empty ticks between live_price and best on the maker side
             (means we could reprice closer to best without giving up edge).
        On race (fill crossed with cancel in-flight): the order is moved to _cancelling_maker_orders
        so process_order_filled_event can still route late fills to the buffer and trigger a hedge.
        NOTE: any partial fills already happened are already in _unhedged_buffer_base via
        process_order_filled_event; cancel only releases the unfilled remainder.
        """
        if self.maker_order is None or self.maker_order.order is None or not self.maker_order.is_open:
            return
        reason = self._refresh_reason()
        if reason is None:
            return
        self.logger().info(
            f"Cancelling maker order {self.maker_order.order_id}: reason={reason} "
            f"live_price={self.maker_order.order.price}.",
        )
        self._cancel_reasons[self.maker_order.order_id] = reason
        self._strategy.cancel(self.maker_connector, self.maker_trading_pair, self.maker_order.order_id)
        # Keep reference in _cancelling_maker_orders so late fill events (fill crossed with cancel
        # in-flight) are still routed to the buffer and hedged. Moved to _completed_makers once
        # the cancel is confirmed or the order's in-flight state is terminal.
        self._cancelling_maker_orders[self.maker_order.order_id] = self.maker_order
        self.maker_order = None

    def _refresh_reason(self) -> Optional[str]:
        depth = self._depth_refresh_violation()
        if depth is not None:
            return depth
        edge = self._edge_refresh_violation()
        if edge is not None:
            return edge
        gap = self._gap_refresh_violation()
        if gap is not None:
            return gap
        return None

    def _depth_refresh_violation(self) -> Optional[str]:
        N = self.config.order_refresh_depth
        try:
            order_book = self.connectors[self.maker_connector].get_order_book(self.maker_trading_pair)
            entries = list(order_book.bid_entries() if self.maker_order_side == TradeType.BUY
                           else order_book.ask_entries())
        except Exception:
            return None
        if len(entries) < N:
            return None
        level_n_price = Decimal(str(entries[N - 1].price))
        live_price = self.maker_order.order.price
        # BUY: stale if live price is below the Nth bid level (queue past depth N).
        # SELL: stale if live price is above the Nth ask level.
        if (self.maker_order_side == TradeType.BUY and live_price < level_n_price) or \
                (self.maker_order_side == TradeType.SELL and live_price > level_n_price):
            return f"depth (live_price past level {N} = {level_n_price})"
        return None

    def _edge_refresh_violation(self) -> Optional[str]:
        """Cancel when the current cross-venue edge has degraded below the placement gate
        by more than (1 - pct) * |min_price_edge_bps|.

        Symmetric for positive and negative min_price_edge_bps:
          - min=+3, pct=0.8 → threshold = 3 - 0.6 = 2.4. Cancel zone: edge < 2.4.
          - min=-3, pct=0.8 → threshold = -3 - 0.6 = -3.6. Cancel zone: edge < -3.6.

        Using `pct * min` directly would put the cancel zone INSIDE the placement zone for
        negative min — every freshly placed order would be immediately cancelled."""
        decay_amount = (Decimal("1") - self.config.refresh_edge_pct_threshold) * abs(self.config.min_price_edge_bps)
        threshold = self.config.min_price_edge_bps - decay_amount
        current = self._compute_edge_bps()
        if current < threshold:
            return f"edge_decay (current={current:.2f}bps < {threshold:.2f}bps; gate={self.config.min_price_edge_bps} pct={self.config.refresh_edge_pct_threshold})"
        return None

    def _gap_refresh_violation(self) -> Optional[str]:
        """Detect a wasteful gap between our order price and the *next less-aggressive*
        level on the same side of the book. Concretely:
          - BUY: find the highest bid in the book strictly BELOW our live_price; that is the
            next bidder we'd be queued ahead of. If that bidder is `refresh_gap_threshold + 1`
            (or more) ticks below us, we are overpaying for queue priority — we could reprice
            to (next_level + 1 tick) and still maintain priority while saving edge.
          - SELL: mirror — find the lowest ask strictly ABOVE our live_price.
        Returns None (no violation) if there is no less-aggressive level in view (we are alone
        at the bottom of the book, or the book hasn't been read).
        """
        if self.config.refresh_gap_threshold <= 0:
            return None
        if self.maker_order is None or self.maker_order.order is None:
            return None
        tick = self._maker_trading_rules.min_price_increment
        if tick is None or tick <= 0:
            return None
        live_price = self.maker_order.order.price
        try:
            order_book = self.connectors[self.maker_connector].get_order_book(self.maker_trading_pair)
            entries = list(order_book.bid_entries() if self.maker_order_side == TradeType.BUY
                           else order_book.ask_entries())
        except Exception:
            return None
        if not entries:
            return None

        # Walk entries (bids descending / asks ascending) and find the first that is
        # *strictly* less aggressive than our live price (other entries at exactly our
        # price represent shared queue position and are ignored).
        next_level: Optional[Decimal] = None
        for entry in entries:
            entry_price = Decimal(str(entry.price))
            if self.maker_order_side == TradeType.BUY and entry_price < live_price:
                next_level = entry_price
                break
            if self.maker_order_side == TradeType.SELL and entry_price > live_price:
                next_level = entry_price
                break
        if next_level is None:
            return None

        # Number of empty ticks strictly between live_price and next_level. If they're
        # adjacent (1 tick apart), empty_ticks = 0; we ignore the off-by-one tolerance
        # introduced by float-precision residue in connector prices by rounding down.
        if self.maker_order_side == TradeType.BUY:
            distance_ticks = (live_price - next_level) / tick
        else:
            distance_ticks = (next_level - live_price) / tick
        empty_ticks = int(distance_ticks) - 1
        if empty_ticks >= self.config.refresh_gap_threshold:
            direction = "below" if self.maker_order_side == TradeType.BUY else "above"
            return (f"gap ({empty_ticks} empty ticks {direction} live_price; "
                    f"next less-aggressive level={next_level})")
        return None

    # --------------------------------------------------------------------- shutdown

    async def _control_shutdown_process(self):
        maker_done = self.maker_order is None or self.maker_order.is_done
        hedges_done = all(p.is_done for p in self._pending_hedges)
        if not (maker_done and hedges_done):
            return
        # CompletedEvent can arrive before FilledEvent on fast market fills (race condition).
        # Wait until every done hedge has its fill data populated on the InFlightOrder so
        # _taker_session_totals() is accurate before we stop.
        hedges_settled = all(
            p.executed_amount_base > Decimal("0")
            for p in self._pending_hedges
            if p.is_done
        )
        if not hedges_settled:
            self.logger().debug(
                "Shutdown deferred: waiting for fill events to settle on completed hedge orders.",
            )
            return
        # Final flush attempt — picks up any tail buffer that just became hedge-able.
        self._try_flush_buffer_to_hedge()
        # If we still have an un-hedgeable residue, surface it and complete.
        if self._unhedged_buffer_base > Decimal("0"):
            self.logger().warning(
                f"Stopping with unhedged residual base={self._unhedged_buffer_base} "
                f"(below taker min_order_size={self._taker_trading_rules.min_order_size}). "
                "Operator should flatten manually.",
            )
        if self.close_type is None:
            self.close_type = CloseType.COMPLETED
        self.stop()

    # --------------------------------------------------------------------- buffering

    def _try_flush_buffer_to_hedge(self):
        if self._unhedged_buffer_base <= Decimal("0"):
            return
        min_size = self._taker_trading_rules.min_order_size
        min_notional = self._taker_trading_rules.min_notional_size
        if self._unhedged_buffer_base < min_size:
            self._throttled_warn(
                "flush",
                f"Hedge skipped: buffer {self._unhedged_buffer_base} < taker min_order_size {min_size}.",
            )
            return
        ref_price = self._taker_reference_price if self._taker_reference_price > 0 else Decimal("1")
        if self._unhedged_buffer_base * ref_price < min_notional:
            self._throttled_warn(
                "flush",
                f"Hedge skipped: notional {self._unhedged_buffer_base * ref_price} "
                f"< taker min_notional {min_notional}.",
            )
            return
        hedge_amount = self.connectors[self.taker_connector].quantize_order_amount(
            self.taker_trading_pair, self._unhedged_buffer_base,
        )
        if hedge_amount <= Decimal("0") or hedge_amount < min_size:
            self._throttled_warn(
                "flush",
                f"Hedge skipped: quantized amount {hedge_amount} below taker min_order_size {min_size}.",
            )
            return
        self._place_taker_order(hedge_amount)
        self._unhedged_buffer_base -= hedge_amount
        self._cumulative_filled_base += hedge_amount
        self.logger().info(
            f"Hedge submitted: amount={hedge_amount}. "
            f"buffer_now={self._unhedged_buffer_base} cumulative={self._cumulative_filled_base}/{self.config.total_amount}",
        )

    def _place_taker_order(self, amount: Decimal):
        order_id = self.place_order(
            connector_name=self.taker_connector,
            trading_pair=self.taker_trading_pair,
            order_type=OrderType.MARKET,
            side=self.taker_order_side,
            amount=amount,
            position_action=PositionAction.OPEN,
        )
        tracked = TrackedOrder(order_id=order_id)
        self._pending_hedges.append(tracked)
        self._hedge_amounts[order_id] = amount
        self.logger().info(
            f"Hedging buffered amount={amount} via {self.taker_order_side.name} market on {self.taker_connector} "
            f"(order_id={order_id}).",
        )

    # --------------------------------------------------------------------- events

    def process_order_created_event(self, event_tag: int, market: ConnectorBase,
                                    event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent]):
        if self.maker_order and event.order_id == self.maker_order.order_id:
            self.maker_order.order = self.get_in_flight_order(self.maker_connector, event.order_id)
            return
        for hedge in self._pending_hedges:
            if hedge.order_id == event.order_id:
                hedge.order = self.get_in_flight_order(self.taker_connector, event.order_id)
                return

    def process_order_filled_event(self, event_tag: int, market: ConnectorBase, event: OrderFilledEvent):
        if self.maker_order and event.order_id == self.maker_order.order_id:
            fill_delta = Decimal(str(event.amount))
            self._unhedged_buffer_base += fill_delta
            self._maker_filled_for_current_order[event.order_id] = (
                self._maker_filled_for_current_order.get(event.order_id, Decimal("0")) + fill_delta
            )
            self.logger().info(
                f"Maker fill: order={event.order_id} delta={fill_delta} price={event.price} "
                f"buffer_now={self._unhedged_buffer_base} "
                f"cumulative={self._cumulative_filled_base}/{self.config.total_amount}",
            )
            overshoot = self._cumulative_filled_base + self._unhedged_buffer_base - self.config.total_amount
            if overshoot > Decimal("0"):
                self.logger().warning(
                    f"Maker fill exceeded total_amount by {overshoot}; will hedge anyway to keep delta neutral.",
                )
            self._try_flush_buffer_to_hedge()
        elif event.order_id in self._cancelling_maker_orders:
            # Fill crossed with cancel in-flight — the exchange matched the order before the cancel
            # landed. Buffer and hedge as normal to stay delta-neutral.
            fill_delta = Decimal(str(event.amount))
            self._unhedged_buffer_base += fill_delta
            self.logger().info(
                f"Late fill on cancelling maker order {event.order_id}: delta={fill_delta} "
                f"buffer_now={self._unhedged_buffer_base} "
                f"cumulative={self._cumulative_filled_base}/{self.config.total_amount}",
            )
            self._try_flush_buffer_to_hedge()
        elif event.order_id in self._recently_completed_maker_ids:
            # Fill arrived after the CompletedEvent (exchange sent completion before fill update).
            # Still buffer and hedge so we stay delta-neutral.
            fill_delta = Decimal(str(event.amount))
            if fill_delta > Decimal("0"):
                self._unhedged_buffer_base += fill_delta
                self.logger().info(
                    f"Late fill on completed maker order {event.order_id}: delta={fill_delta} "
                    f"buffer_now={self._unhedged_buffer_base} "
                    f"cumulative={self._cumulative_filled_base}/{self.config.total_amount}",
                )
                self._try_flush_buffer_to_hedge()

    def process_order_completed_event(self, event_tag: int, market: ConnectorBase,
                                      event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent]):
        if event.order_id in self._cancelling_maker_orders:
            tracked = self._cancelling_maker_orders.pop(event.order_id)
            self._completed_makers.append(tracked)
            self.logger().info(f"Cancelling maker order {event.order_id} completed; moved to completed_makers.")
            self._try_flush_buffer_to_hedge()
            return
        if self.maker_order and event.order_id == self.maker_order.order_id:
            completed_id = self.maker_order.order_id
            self._recently_completed_maker_ids.add(completed_id)
            self._completed_makers.append(self.maker_order)
            self.maker_order = None
            self._current_retries = 0  # successful fill resets the failure counter
            self.logger().info(
                f"Maker order {completed_id} completed. "
                f"cumulative={self._cumulative_filled_base}/{self.config.total_amount} "
                f"buffer={self._unhedged_buffer_base}",
            )
            # Flush the final tail of this maker cycle.
            self._try_flush_buffer_to_hedge()
            if self._cumulative_filled_base + self._unhedged_buffer_base >= self.config.total_amount:
                self.logger().info(
                    f"Total amount reached "
                    f"({self._cumulative_filled_base + self._unhedged_buffer_base} >= {self.config.total_amount}); "
                    "shutting down.",
                )
                self._status = RunnableStatus.SHUTTING_DOWN
            return
        for hedge in self._pending_hedges:
            if hedge.order_id == event.order_id:
                self._consecutive_hedge_failures = 0
                self.logger().info(
                    f"Hedge {event.order_id} completed. consec_failures reset → 0.",
                )
                return

    def process_order_failed_event(self, event_tag: int, market: ConnectorBase, event: MarketOrderFailureEvent):
        if self.maker_order and self.maker_order.order_id == event.order_id:
            self.failed_orders.append(self.maker_order)
            self.maker_order = None
            self._current_retries += 1
            self.logger().error(
                f"Maker order {event.order_id} failed. Retries {self._current_retries}/{self._max_retries}.",
            )
            return
        for hedge in self._pending_hedges:
            if hedge.order_id == event.order_id:
                refund = self._hedge_amounts.pop(event.order_id, Decimal("0"))
                self._unhedged_buffer_base += refund
                self._cumulative_filled_base -= refund
                self.failed_orders.append(hedge)
                self._pending_hedges.remove(hedge)
                self._consecutive_hedge_failures += 1
                self.logger().error(
                    f"Hedge order {event.order_id} failed. Refunded {refund} to buffer. "
                    f"Consecutive hedge failures: {self._consecutive_hedge_failures}/"
                    f"{self.config.max_consecutive_hedge_failures}.",
                )
                if self._consecutive_hedge_failures >= self.config.max_consecutive_hedge_failures:
                    self.logger().error("Max consecutive hedge failures reached; halting executor.")
                    self.close_type = CloseType.FAILED
                    self._status = RunnableStatus.SHUTTING_DOWN
                return

    # --------------------------------------------------------------------- pnl

    def get_cum_fees_quote(self) -> Decimal:
        total = Decimal("0")
        for tracked in self._completed_makers:
            total += tracked.cum_fees_quote
        if self.maker_order is not None:
            total += self.maker_order.cum_fees_quote
        for hedge in self._pending_hedges:
            total += hedge.cum_fees_quote
        return total

    def get_net_pnl_quote(self) -> Decimal:
        if not self.is_closed:
            return Decimal("0")
        maker_quote = Decimal("0")
        for tracked in self._completed_makers:
            maker_quote += tracked.executed_amount_base * tracked.average_executed_price
        taker_quote = Decimal("0")
        for hedge in self._pending_hedges:
            taker_quote += hedge.executed_amount_base * hedge.average_executed_price
        if self.maker_order_side == TradeType.BUY:
            # bought on maker, sold on taker: profit = taker_quote - maker_quote - fees
            return taker_quote - maker_quote - self.get_cum_fees_quote()
        # sold on maker, bought on taker: profit = maker_quote - taker_quote - fees
        return maker_quote - taker_quote - self.get_cum_fees_quote()

    def get_net_pnl_pct(self) -> Decimal:
        if self.config.total_amount == Decimal("0"):
            return Decimal("0")
        return self.get_net_pnl_quote() / self.config.total_amount

    # --------------------------------------------------------------------- early stop

    def early_stop(self, keep_position: bool = False):
        if self.maker_order and self.maker_order.is_open:
            self.logger().info(f"Early stop: cancelling maker order {self.maker_order.order_id}.")
            self._strategy.cancel(self.maker_connector, self.maker_trading_pair, self.maker_order.order_id)
        self.close_type = CloseType.POSITION_HOLD if keep_position else CloseType.EARLY_STOP
        self._status = RunnableStatus.SHUTTING_DOWN

    # --------------------------------------------------------------------- info

    def _maker_session_totals(self) -> tuple:
        """(filled_base, filled_quote) summed across completed maker orders + the live one."""
        base = Decimal("0")
        quote = Decimal("0")
        for tracked in self._completed_makers:
            base += tracked.executed_amount_base
            quote += tracked.executed_amount_base * tracked.average_executed_price
        if self.maker_order is not None and self.maker_order.executed_amount_base > 0:
            base += self.maker_order.executed_amount_base
            quote += self.maker_order.executed_amount_base * self.maker_order.average_executed_price
        return base, quote

    def _taker_session_totals(self) -> tuple:
        """(filled_base, filled_quote) summed across all hedges that have actual fills."""
        base = Decimal("0")
        quote = Decimal("0")
        for hedge in self._pending_hedges:
            if hedge.executed_amount_base > 0:
                base += hedge.executed_amount_base
                quote += hedge.executed_amount_base * hedge.average_executed_price
        return base, quote

    def get_custom_info(self) -> Dict:
        edge_bps = self._compute_edge_bps()
        maker_base, maker_quote = self._maker_session_totals()
        taker_base, taker_quote = self._taker_session_totals()
        avg_maker = (maker_quote / maker_base) if maker_base > 0 else Decimal("0")
        avg_taker = (taker_quote / taker_base) if taker_base > 0 else Decimal("0")
        if avg_maker > 0 and avg_taker > 0:
            if self.maker_order_side == TradeType.BUY:
                realized_spread_bps = (avg_taker - avg_maker) / avg_maker * Decimal("10000")
            else:
                realized_spread_bps = (avg_maker - avg_taker) / avg_maker * Decimal("10000")
        else:
            realized_spread_bps = Decimal("0")
        hedges_in_flight = sum(1 for h in self._pending_hedges if not h.is_done)
        hedges_completed = sum(1 for h in self._pending_hedges if h.is_done)
        return {
            "side": self.config.maker_side,
            "maker_connector": self.maker_connector,
            "maker_trading_pair": self.maker_trading_pair,
            "taker_connector": self.taker_connector,
            "taker_trading_pair": self.taker_trading_pair,
            "total_amount": self.config.total_amount,
            "cumulative_filled_base": self._cumulative_filled_base,
            "unhedged_buffer_base": self._unhedged_buffer_base,
            "unhedged_residual_base": self._unhedged_buffer_base if self.is_closed else Decimal("0"),
            "hedges_in_flight": hedges_in_flight,
            "hedges_completed": hedges_completed,
            "consecutive_hedge_failures": self._consecutive_hedge_failures,
            "maker_px": self._maker_px,
            "maker_reference_price": self._maker_reference_price,
            "taker_reference_price": self._taker_reference_price,
            "taker_top_volume": self._taker_top_volume,
            "edge_bps": edge_bps,
            "quote_conversion_pair": self.quote_conversion_pair,
            "quote_conversion_rate": self._quote_conversion_rate,
            "avg_maker_price": avg_maker,
            "avg_taker_price": avg_taker,
            "maker_filled_base": maker_base,
            "taker_filled_base": taker_base,
            "realized_spread_bps": realized_spread_bps,
        }

    def to_format_status(self) -> str:
        info = self.get_custom_info()
        return f"""
Maker Side: {self.maker_order_side}
-----------------------------------------------------------------------------------------------------------------------
    - Maker: {self.maker_connector} {self.maker_trading_pair} | Taker: {self.taker_connector} {self.taker_trading_pair}
    - Total: {info['total_amount']} | Filled (base): {info['cumulative_filled_base']} | Buffer: {info['unhedged_buffer_base']} | Hedges in-flight: {info['hedges_in_flight']} | Hedges completed: {info['hedges_completed']}
    - Maker filled: {info['maker_filled_base']} @ avg {info['avg_maker_price']} | Taker filled: {info['taker_filled_base']} @ avg {info['avg_taker_price']} | Realized spread: {info['realized_spread_bps']:.2f} bps
    - Maker ref: {info['maker_reference_price']} | Maker px: {info['maker_px']} | Taker ref: {info['taker_reference_price']} | Edge: {info['edge_bps']:.2f} bps (req {self.config.min_price_edge_bps} bps)
    - Quote conv: {info['quote_conversion_pair']} = {info['quote_conversion_rate']}
    - Hedge failures: {info['consecutive_hedge_failures']}/{self.config.max_consecutive_hedge_failures}
-----------------------------------------------------------------------------------------------------------------------
"""

    # --------------------------------------------------------------------- helpers

    def _throttled_warn(self, key: str, message: str, throttle_seconds: float = 5.0):
        ts_attr = f"_last_{key}_log_ts"
        now = self._strategy.current_timestamp if self._strategy else 0.0
        last = getattr(self, ts_attr, 0.0) or 0.0
        if now - last >= throttle_seconds:
            self.logger().warning(message)
            setattr(self, ts_attr, now)
