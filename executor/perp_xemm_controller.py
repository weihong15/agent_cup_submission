from decimal import Decimal
from typing import List

from pydantic import Field

from hummingbot.core.data_type.common import MarketDict, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.perp_xemm_executor.data_types import PerpXEMMExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction


class PerpXEMMControllerConfig(ControllerConfigBase):
    controller_type: str = "generic"
    controller_name: str = "perp_xemm_controller"

    maker_connector: str = Field(
        ..., json_schema_extra={"prompt": "Maker connector (limit leg)", "prompt_on_new": True}
    )
    maker_trading_pair: str = Field(
        ..., json_schema_extra={"prompt": "Maker trading pair", "prompt_on_new": True}
    )
    taker_connector: str = Field(
        ..., json_schema_extra={"prompt": "Taker connector (market hedge leg)", "prompt_on_new": True}
    )
    taker_trading_pair: str = Field(
        ..., json_schema_extra={"prompt": "Taker trading pair", "prompt_on_new": True}
    )
    maker_side_str: str = Field(
        "BUY",
        json_schema_extra={"prompt": "Maker side: BUY or SELL", "prompt_on_new": True},
    )

    total_amount: Decimal = Field(
        Decimal("0.01"),
        json_schema_extra={"prompt": "Total base amount to fill this session", "prompt_on_new": True},
    )
    min_notional: Decimal = Field(
        Decimal("11"),
        json_schema_extra={"prompt": "Floor on per-cycle order size in quote", "prompt_on_new": True},
    )
    max_notional: Decimal = Field(
        Decimal("100"),
        json_schema_extra={"prompt": "Cap on per-cycle order size in quote", "prompt_on_new": True},
    )
    pct_impact: Decimal = Field(
        Decimal("0.5"),
        json_schema_extra={"prompt": "Fraction of top-of-taker-book volume per cycle", "prompt_on_new": True},
    )
    limit_depth: int = Field(
        0,
        json_schema_extra={"prompt": "Book level to anchor placement on (0 = best bid/ask)", "prompt_on_new": True},
    )
    limit_tick: int = Field(
        0,
        json_schema_extra={"prompt": "Tick offset from limit_depth anchor", "prompt_on_new": True},
    )
    min_price_edge_bps: Decimal = Field(
        Decimal("3"),
        json_schema_extra={"prompt": "Minimum required edge in bps", "prompt_on_new": True},
    )
    order_refresh_depth: int = Field(
        5,
        json_schema_extra={"prompt": "Cancel-replace if live maker price falls past the Nth book level", "prompt_on_new": True},
    )
    refresh_edge_pct_threshold: Decimal = Field(
        Decimal("0.8"),
        json_schema_extra={"prompt": "Cancel if edge falls below this fraction of min_price_edge_bps", "prompt_on_new": True},
    )
    refresh_gap_threshold: int = Field(
        2,
        json_schema_extra={"prompt": "Cancel if this many empty ticks exist between order and best (0 disables)", "prompt_on_new": True},
    )
    max_consecutive_hedge_failures: int = Field(
        2,
        json_schema_extra={"prompt": "Halt after this many consecutive hedge failures", "prompt_on_new": True},
    )

    def update_markets(self, markets: MarketDict) -> MarketDict:
        markets[self.maker_connector] = markets.get(self.maker_connector, set()) | {self.maker_trading_pair}
        markets[self.taker_connector] = markets.get(self.taker_connector, set()) | {self.taker_trading_pair}
        return markets


class PerpXEMMController(ControllerBase):
    """
    Single-session perp XEMM controller. Spawns one PerpXEMMExecutor on first tick; does not
    restart after completion. Run multiple instances (via separate controller configs) to trade
    multiple pairs simultaneously under a single script/MarketDataProvider.
    """

    def __init__(self, config: PerpXEMMControllerConfig, *args, **kwargs):
        self.config = config
        super().__init__(config, *args, **kwargs)

    def _maker_side(self) -> TradeType:
        return TradeType.BUY if self.config.maker_side_str.upper() == "BUY" else TradeType.SELL

    async def update_processed_data(self):
        pass

    def determine_executor_actions(self) -> List[ExecutorAction]:
        if self.executors_info:
            return []

        maker_side = self._maker_side()
        if maker_side == TradeType.BUY:
            buying_market = ConnectorPair(
                connector_name=self.config.maker_connector, trading_pair=self.config.maker_trading_pair
            )
            selling_market = ConnectorPair(
                connector_name=self.config.taker_connector, trading_pair=self.config.taker_trading_pair
            )
        else:
            buying_market = ConnectorPair(
                connector_name=self.config.taker_connector, trading_pair=self.config.taker_trading_pair
            )
            selling_market = ConnectorPair(
                connector_name=self.config.maker_connector, trading_pair=self.config.maker_trading_pair
            )

        cfg = PerpXEMMExecutorConfig(
            controller_id=self.config.id,
            timestamp=self.market_data_provider.time(),
            buying_market=buying_market,
            selling_market=selling_market,
            maker_side=maker_side,
            total_amount=self.config.total_amount,
            limit_depth=self.config.limit_depth,
            limit_tick=self.config.limit_tick,
            min_notional=self.config.min_notional,
            max_notional=self.config.max_notional,
            pct_impact=self.config.pct_impact,
            min_price_edge_bps=self.config.min_price_edge_bps,
            order_refresh_depth=self.config.order_refresh_depth,
            refresh_edge_pct_threshold=self.config.refresh_edge_pct_threshold,
            refresh_gap_threshold=self.config.refresh_gap_threshold,
            max_consecutive_hedge_failures=self.config.max_consecutive_hedge_failures,
        )
        self.logger().info(
            f"[{self.config.id}] Spawning PerpXEMMExecutor {cfg.id[:12]} | "
            f"maker={self.config.maker_connector} {self.config.maker_trading_pair} | "
            f"taker={self.config.taker_connector} {self.config.taker_trading_pair} | "
            f"side={maker_side.name} | total={cfg.total_amount}",
        )
        return [CreateExecutorAction(executor_config=cfg, controller_id=self.config.id)]

    def to_format_status(self) -> List[str]:
        lines = [
            f"=== PerpXEMM [{self.config.id}] "
            f"{self.config.maker_connector} {self.config.maker_trading_pair} ↔ "
            f"{self.config.taker_connector} {self.config.taker_trading_pair} "
            f"side={self.config.maker_side_str.upper()} ==="
        ]
        if not self.executors_info:
            lines.append("  (waiting for first tick)")
            return lines
        for executor in self.executors_info:
            ci = executor.custom_info or {}
            realized_spread = ci.get("realized_spread_bps", Decimal("0")) or Decimal("0")
            edge_bps = ci.get("edge_bps", Decimal("0")) or Decimal("0")
            lines.append(
                f"  Executor {executor.id[:12]} | status={executor.status.name} | close={executor.close_type}"
            )
            lines.append(
                f"    Filled: {ci.get('cumulative_filled_base')} / {ci.get('total_amount')} | "
                f"Buffer: {ci.get('unhedged_buffer_base')} | "
                f"Hedges in-flight: {ci.get('hedges_in_flight')} | "
                f"Hedges done: {ci.get('hedges_completed')}"
            )
            lines.append(
                f"    Maker px: {ci.get('maker_px')} | Taker ref: {ci.get('taker_reference_price')} | "
                f"Edge: {edge_bps:.2f} bps | Spread: {realized_spread:.2f} bps"
            )
            lines.append(
                f"    Hedge failures: {ci.get('consecutive_hedge_failures')}/{self.config.max_consecutive_hedge_failures}"
            )
        return lines
