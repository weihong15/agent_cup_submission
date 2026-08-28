from decimal import Decimal
from typing import Literal

from pydantic import model_validator

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.executors.data_types import ConnectorPair, ExecutorConfigBase


class PerpXEMMExecutorConfig(ExecutorConfigBase):
    type: Literal["perp_xemm_executor"] = "perp_xemm_executor"
    buying_market: ConnectorPair
    selling_market: ConnectorPair
    maker_side: TradeType
    total_amount: Decimal
    # Defaults are 0/0 (top of book, no offset). Keeping these defaulted so older stored
    # executor records that predate the limit_depth/limit_tick split still validate.
    limit_depth: int = 0
    limit_tick: int = 0
    min_notional: Decimal
    max_notional: Decimal
    pct_impact: Decimal
    min_price_edge_bps: Decimal
    order_refresh_depth: int
    refresh_edge_pct_threshold: Decimal = Decimal("0.8")
    refresh_gap_threshold: int = 2
    max_consecutive_hedge_failures: int = 2

    @model_validator(mode="after")
    def _validate_fields(self):
        if self.total_amount <= 0:
            raise ValueError("total_amount must be > 0")
        # Pricing convention (limit_depth is 0-indexed — 0 = best bid/ask):
        #   BUY:  maker_px = bid_{limit_depth} + limit_tick * min_price_increment
        #   SELL: maker_px = ask_{limit_depth} - limit_tick * min_price_increment
        # `limit_depth >= 0` selects which book level to anchor on (0 = best, 1 = second-best,
        # etc.). `limit_tick` is the offset in ticks from that anchor toward fill-direction;
        # positive = more aggressive, negative = more passive. There is no hard cap on
        # `limit_tick` because placement uses OrderType.LIMIT_MAKER (post-only): if the
        # resulting maker_px would cross the opposite top, the venue rejects rather than
        # executes as a taker.
        # min_price_edge_bps can be negative — used in exit/unwind scenarios where we are
        # willing to lose up to that many bps to flatten a position quickly.
        if self.limit_depth < 0:
            raise ValueError("limit_depth must be >= 0 (0 = best bid/ask)")
        if self.pct_impact <= 0:
            raise ValueError("pct_impact must be > 0")
        if self.min_notional <= 0 or self.max_notional <= 0:
            raise ValueError("min_notional and max_notional must be > 0")
        if self.min_notional > self.max_notional:
            raise ValueError("min_notional must be <= max_notional")
        if self.order_refresh_depth < 1:
            raise ValueError("order_refresh_depth must be >= 1")
        if not (Decimal("0") <= self.refresh_edge_pct_threshold <= Decimal("1")):
            raise ValueError("refresh_edge_pct_threshold must be in [0, 1]")
        if self.refresh_gap_threshold < 0:
            raise ValueError("refresh_gap_threshold must be >= 0 (use 0 to disable)")
        if self.max_consecutive_hedge_failures < 1:
            raise ValueError("max_consecutive_hedge_failures must be >= 1")
        return self
