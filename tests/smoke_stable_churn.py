"""Smoke test for stable_churn (two-sided, chase-the-touch) under a real hummingbot import. No network, no orders.

Run inside the hummingbot environment: python tests/smoke_stable_churn.py
"""
import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "controllers" / "generic"))
import stable_churn as mod  # noqa: E402
from hummingbot.core.data_type.common import PriceType, TradeType  # noqa: E402
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy  # noqa: E402
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction  # noqa: E402

BID, ASK = Decimal("1.00012"), Decimal("1.00013")


class Conn:
    def __init__(self):
        self.bal = {"USDC": Decimal("800"), "USDT": Decimal("0")}
        self.locked = {}

    def get_balance(self, a):
        return self.bal.get(a, Decimal("0"))                  # a real connector reports 0 for a coin never held

    def get_available_balance(self, a):
        return self.bal.get(a, Decimal("0")) - self.locked.get(a, Decimal("0"))


class MDP:
    def __init__(self):
        self.t = 1_000_000.0
        self.bid, self.ask = BID, ASK
        self.conn = Conn()

    def time(self):
        return self.t

    def get_connector(self, n):
        return self.conn

    def get_trading_rules(self, c, p):
        return SimpleNamespace(min_price_increment=Decimal("0.00001"))

    def get_price_by_type(self, c, p, pt):
        return {PriceType.BestBid: self.bid, PriceType.BestAsk: self.ask}.get(pt, (self.bid + self.ask) / 2)


def ex(id_, side, maker, price=None, filled=0, active=True, fees=0):
    return SimpleNamespace(id=id_, is_active=active, timestamp=0, filled_amount_quote=Decimal(str(filled)),
                           cum_fees_quote=Decimal(str(fees)),
                           config=SimpleNamespace(side=side, price=price,
                                                  execution_strategy=ExecutionStrategy.LIMIT_MAKER if maker
                                                  else ExecutionStrategy.MARKET))


def creates(actions):
    return [a.executor_config for a in actions if isinstance(a, CreateExecutorAction)]


def stops(actions):
    return [a for a in actions if isinstance(a, StopExecutorAction)]


async def step(ctl):
    await ctl.update_processed_data()
    return ctl.determine_executor_actions()


async def main():
    cfg = mod.StableChurnConfig(start_balanced=False, id="sc", size_all=False)     # fixed-clip mode first
    mdp = MDP()
    ctl = mod.StableChurnController(cfg, mdp, asyncio.Queue())
    print("0. defaults: target", cfg.volume_target_usd, "clip", cfg.clip_usd, "band", cfg.band_usd)

    # 1. start all USDC: only the SELL side may quote (a buy would push the tilt past the band)
    c = creates(await step(ctl))
    assert len(c) == 1 and c[0].side == TradeType.SELL and c[0].execution_strategy == ExecutionStrategy.LIMIT_MAKER
    assert c[0].price == ASK and c[0].amount == Decimal("399"), (c[0].price, c[0].amount)
    print("1. all-USDC start -> one maker SELL", c[0].amount, "@ ask")

    # 2. at 50/50 both sides quote at the touch
    mdp.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("400")}
    c = creates(await step(ctl))
    sides = sorted((x.side.name, x.price) for x in c if x.execution_strategy == ExecutionStrategy.LIMIT_MAKER)
    assert sides == [("BUY", BID), ("SELL", ASK)], sides
    print("2. 50/50 -> maker BUY @ bid and SELL @ ask")

    # 3. resting makers stay while at the touch; get stopped when the touch moves (chase, don't hoard priority)
    ctl.executors_info = [ex("b1", TradeType.BUY, True, BID), ex("s1", TradeType.SELL, True, ASK)]
    a = await step(ctl)
    assert not stops(a) and not creates(a), a
    mdp.bid, mdp.ask = BID + Decimal("0.00001"), ASK + Decimal("0.00001")
    a = await step(ctl)
    assert {x.executor_id for x in stops(a)} == {"b1", "s1"}, a
    print("3. makers kept at the touch, both stopped when the touch moved")
    mdp.bid, mdp.ask = BID, ASK

    # 4. behind schedule, tilted to USDT -> taker BUY; but a resting BUY maker locks the USDT, so it is stopped
    #    first and the taker only goes out once the maker is gone (Binance would reject for balance otherwise)
    ctl.executors_info = [ex("b1", TradeType.BUY, True, BID)]
    mdp.conn.bal = {"USDC": Decimal("300"), "USDT": Decimal("500")}
    mdp.t += 3600
    a = await step(ctl)
    assert [x.executor_id for x in stops(a)] == ["b1"], a
    assert not [x for x in creates(a) if x.execution_strategy == ExecutionStrategy.MARKET], "taker sent while balance locked"
    assert not [x for x in creates(a) if x.side == TradeType.BUY], "re-rested the maker it is freeing"
    ctl.executors_info = []
    c = creates(await step(ctl))
    tk = [x for x in c if x.execution_strategy == ExecutionStrategy.MARKET]
    assert len(tk) == 1 and tk[0].side == TradeType.BUY, c
    print("4. behind schedule -> same-side maker freed first, then taker BUY", tk[0].amount,
          f"(schedule ${ctl.processed_data['target_now']:,.0f})")

    # 4b. no second taker while one is in flight
    ctl.executors_info = [ex("t1", TradeType.BUY, False)]
    c = creates(await step(ctl))
    assert not [x for x in c if x.execution_strategy == ExecutionStrategy.MARKET]
    print("4b. no stacked takers")

    # 5. volume accounting survives pruning, never double counts
    ctl.executors_info = [ex("t1", TradeType.BUY, False, filled=400, active=False)]
    await step(ctl)
    ctl.executors_info = []
    await step(ctl)
    assert ctl.processed_data["volume"] == Decimal("400")
    print("5. volume kept after executor pruned:", ctl.processed_data["volume"])

    # 6. depeg guard
    mdp.bid, mdp.ask = Decimal("0.9980"), Decimal("0.9981")
    ctl.executors_info = [ex("b2", TradeType.BUY, True, Decimal("0.9980"))]
    a = await step(ctl)
    assert stops(a) and not creates(a)
    print("6. depeg -> all orders stopped, none placed")
    mdp.bid, mdp.ask = BID, ASK

    # 7. close phase (close_at_end=true, the live-test setting): rebalance to 50/50 from either side, then idle
    ctl.config = ctl.config.model_copy(update={"close_at_end": True})
    mdp.t = 1_000_000.0 + cfg.race_duration_s - cfg.stop_lead_s + 1
    ctl.executors_info = []
    mdp.conn.bal = {"USDC": Decimal("0"), "USDT": Decimal("800")}
    c = creates(await step(ctl))
    assert c[0].side == TradeType.BUY and Decimal("395") <= c[0].amount <= Decimal("400")
    mdp.conn.bal = {"USDC": Decimal("800"), "USDT": Decimal("0")}
    c = creates(await step(ctl))
    assert c[0].side == TradeType.SELL and Decimal("395") <= c[0].amount <= Decimal("400")
    mdp.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("399.5")}
    assert creates(await step(ctl)) == []
    print("7. close phase -> 50/50 from either side, then idle")
    # 7n. NO END STATE (race default, close_at_end=false): past the planned end it keeps quoting both sides and the
    #     volume schedule keeps growing at the same pace
    cfgn = mod.StableChurnConfig(start_balanced=False, id="ne")
    assert cfgn.close_at_end is False
    mdpn = MDP()
    mdpn.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("400")}
    ctln = mod.StableChurnController(cfgn, mdpn, asyncio.Queue())
    await step(ctln)
    mdpn.t = 1_000_000.0 + cfgn.race_duration_s + 3600              # an hour past the planned end
    ctln.executors_info = [ex("hist", TradeType.BUY, True, price=BID, filled=3_700_000, active=False)]  # on pace
    c = creates(await step(ctln))
    assert {x.side for x in c} == {TradeType.BUY, TradeType.SELL} and all(
        x.execution_strategy == ExecutionStrategy.LIMIT_MAKER for x in c), c
    assert ctln.processed_data["target_now"] > cfgn.volume_target_usd
    assert "state=CHURNING" in " ".join(ctln.status_fields())
    print("7n. no end state: 1 h past the planned end still quoting both sides; schedule",
          f"{ctln.processed_data['target_now']:,.0f} > target {cfgn.volume_target_usd:,.0f}")

    # 7h. close with makers RESTING (the realistic bell): stop them first, then the taker, never stuck
    cfgh = mod.StableChurnConfig(start_balanced=False, id="ch", size_all=True, close_at_end=True)
    mdph = MDP()
    mdph.conn.bal = {"USDC": Decimal("700"), "USDT": Decimal("100")}
    ctlh = mod.StableChurnController(cfgh, mdph, asyncio.Queue())
    await step(ctlh)
    mdph.t = 1_000_000.0 + cfgh.race_duration_s - cfgh.stop_lead_s + 1
    ctlh.executors_info = [ex("mb", TradeType.BUY, True, price=BID), ex("ms", TradeType.SELL, True, price=ASK)]
    a = await step(ctlh)
    assert len(stops(a)) == 2 and not creates(a), a
    ctlh.executors_info = []
    c = creates(await step(ctlh))
    assert len(c) == 1 and c[0].side == TradeType.SELL and c[0].execution_strategy == ExecutionStrategy.MARKET, c
    print("7h. close with makers resting: makers stopped first, then one taker SELL", c[0].amount, "toward 50/50")

    # 1s. START: check balances, one market order to 50/50, then churn. All-USDT deposit -> BUY half; then makers
    cfgs = mod.StableChurnConfig(id="st")                             # start_balanced defaults to True
    mdps = MDP()
    mdps.conn.bal = {"USDC": Decimal("0"), "USDT": Decimal("800")}
    ctls = mod.StableChurnController(cfgs, mdps, asyncio.Queue())
    c = creates(await step(ctls))
    assert len(c) == 1 and c[0].side == TradeType.BUY and c[0].execution_strategy == ExecutionStrategy.MARKET, c
    assert Decimal("395") <= c[0].amount <= Decimal("400"), c
    ctls.executors_info = [ex("sb", TradeType.BUY, False)]            # rebalance order working: wait
    assert not creates(await step(ctls))
    ctls.executors_info = []
    mdps.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("400")}   # filled
    c = creates(await step(ctls))
    assert {x.side for x in c} == {TradeType.BUY, TradeType.SELL} and all(
        x.execution_strategy == ExecutionStrategy.LIMIT_MAKER for x in c), c
    mdps2 = MDP()
    mdps2.conn.bal = {"USDC": Decimal("402"), "USDT": Decimal("398")}  # already ~50/50: no market order at all
    c = creates(await step(mod.StableChurnController(mod.StableChurnConfig(id="st2"), mdps2, asyncio.Queue())))
    assert not [x for x in c if x.execution_strategy == ExecutionStrategy.MARKET], c
    print("1s. start: all-USDT -> one market BUY", "~400", "-> waits -> two makers; already 50/50 -> no market order")

    # 1r. REJECT BACKOFF: 2 executors in a row end FAILED with nothing filled -> no new orders for 30 s, then retry;
    #     another failure doubles it; a fill resets
    mdpr = MDP()
    mdpr.conn.bal = {"USDC": Decimal("0"), "USDT": Decimal("800")}
    ctlr = mod.StableChurnController(mod.StableChurnConfig(id="rj"), mdpr, asyncio.Queue())
    assert creates(await step(ctlr))                                   # start rebalance order
    failed = lambda i: SimpleNamespace(**{**vars(ex(i, TradeType.BUY, False, active=False)), "close_type": mod.CloseType.FAILED})
    ctlr.executors_info = [failed("f1")]
    assert creates(await step(ctlr))                                   # one failure: retry at once
    ctlr.executors_info = [failed("f1"), failed("f2")]
    assert not creates(await step(ctlr)) and "state=BACKOFF" in " ".join(ctlr.status_fields())
    mdpr.t += 31
    assert creates(await step(ctlr))                                   # window over: try again
    ctlr.executors_info = [failed("f1"), failed("f2"), failed("f3")]
    await step(ctlr)
    assert ctlr._backoff_until - mdpr.t == 60                          # doubled
    ctlr.executors_info = [ex("ok", TradeType.BUY, False, filled=97, active=False)]
    mdpr.t += 61
    await step(ctlr)
    assert ctlr._fail_streak == 0
    print("1r. reject backoff: 2 failures -> 30 s pause (state=BACKOFF), 3 -> 60 s, a fill resets")

    # 7b. paced rebalance: all-USDT and behind schedule (by any amount) -> taker BUY half now; on schedule -> no taker
    cfg3 = mod.StableChurnConfig(start_balanced=False, id="pr", size_all=False)
    mdp3 = MDP()
    mdp3.conn.bal = {"USDC": Decimal("0"), "USDT": Decimal("800")}
    ctl3 = mod.StableChurnController(cfg3, mdp3, asyncio.Queue())
    await step(ctl3)                                   # t0 = now: schedule 0, not behind
    assert not [x for x in creates(ctl3.determine_executor_actions()) if x.execution_strategy == ExecutionStrategy.MARKET]
    mdp3.t += 60                                       # $1k behind: < 5 clips, so only the PACED path can fire
    c = creates(await step(ctl3))
    tk = [x for x in c if x.execution_strategy == ExecutionStrategy.MARKET]
    assert len(tk) == 1 and tk[0].side == TradeType.BUY and Decimal("715") <= tk[0].amount <= Decimal("720"), c
    print("7b. paced rebalance: all-USDT + slightly behind -> taker BUY", tk[0].amount, "(rebalance_to 0.9) ; on schedule -> none")

    # 7g. queue-imbalance taker timing: all-USDT, slightly behind -> the paced BUY waits while the ask is thick
    #     (the maker bid keeps resting), fires once the ask is thin, and fires regardless when far behind (valve)
    class OB:
        def __init__(self, bq, aq):
            self.bq, self.aq = bq, aq

        def bid_entries(self):
            return iter([SimpleNamespace(price=float(BID), amount=self.bq)])

        def ask_entries(self):
            return iter([SimpleNamespace(price=float(ASK), amount=self.aq)])

    cfg7 = mod.StableChurnConfig(start_balanced=False, id="im", size_all=False)
    mdp7 = MDP()
    mdp7.conn.bal = {"USDC": Decimal("0"), "USDT": Decimal("800")}
    mdp7.ob = OB(1_000_000.0, 1_000_000.0)                  # 50% of the touch on the ask -> too thick to hit
    mdp7.get_order_book = lambda c, p: mdp7.ob
    ctl7 = mod.StableChurnController(cfg7, mdp7, asyncio.Queue())
    await step(ctl7)
    mdp7.t += 60                                            # $1k behind: within the valve (3 x 10 x $400)
    c = creates(await step(ctl7))
    assert not [x for x in c if x.execution_strategy == ExecutionStrategy.MARKET], c
    assert [x for x in c if x.side == TradeType.BUY and x.execution_strategy == ExecutionStrategy.LIMIT_MAKER], c
    mdp7.ob = OB(1_000_000.0, 200_000.0)                    # ask now 17% of the touch -> about to tick up: take it
    ctl7b = mod.StableChurnController(cfg7, mdp7, asyncio.Queue())
    mdp7.t -= 60; await step(ctl7b); mdp7.t += 60
    tk = [x for x in creates(await step(ctl7b)) if x.execution_strategy == ExecutionStrategy.MARKET]
    assert len(tk) == 1 and tk[0].side == TradeType.BUY, tk
    mdp7.ob = OB(1_000_000.0, 1_000_000.0)                  # thick again, but $14k+ behind (> valve $12k) -> fires
    ctl7c = mod.StableChurnController(cfg7, mdp7, asyncio.Queue())
    mdp7.t -= 60; await step(ctl7c); mdp7.t += 900
    tk = [x for x in creates(await step(ctl7c)) if x.execution_strategy == ExecutionStrategy.MARKET]
    assert len(tk) == 1 and tk[0].side == TradeType.BUY, tk
    mdp7.t -= 840
    print("7g. imbalance timing: thick ask -> taker held, maker rests; thin ask -> taker BUY; far behind -> valve fires")

    # 7i. fee split in STATUS: taker fills charged 10 bp, maker fills free -> fee_bp_taker 10, fee_bp_maker 0
    cfgi = mod.StableChurnConfig(start_balanced=False, id="fs", max_fee_bps=1000)
    mdpi = MDP()
    ctli = mod.StableChurnController(cfgi, mdpi, asyncio.Queue())
    ctli.executors_info = [ex("mk", TradeType.BUY, True, price=BID, filled=3000, active=False),
                           ex("tk", TradeType.SELL, False, filled=1000, active=False, fees=1)]
    await step(ctli)
    st = dict(kv.split("=", 1) for kv in ctli.status_fields())
    assert st["fee_bp_maker"] == "0.000" and st["fee_bp_taker"] == "10.000", st
    print("7i. fee split: maker", st["fee_bp_maker"], "bp, taker", st["fee_bp_taker"], "bp")

    # 7c. FEE KILL-SWITCH: zero fees keep trading; 10 bp charged on $3k -> stop everything, permanently
    cfg4 = mod.StableChurnConfig(start_balanced=False, id="fk")
    mdp4 = MDP(); mdp4.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("400")}
    ctl4 = mod.StableChurnController(cfg4, mdp4, asyncio.Queue())
    ctl4.executors_info = [ex("f1", TradeType.SELL, False, filled=3000, active=False, fees=0)]
    assert creates(await step(ctl4)), "zero-fee fills must not trip the kill-switch"
    ctl4.executors_info = [ex("f2", TradeType.SELL, False, filled=3000, active=False, fees=3.0),
                           ex("m9", TradeType.BUY, True, BID)]
    a = await step(ctl4)
    assert not creates(a) and [x.executor_id for x in stops(a)] == ["m9"], a
    ctl4.executors_info = []
    assert creates(await step(ctl4)) == [], "must never restart after the kill-switch"
    print("7c. fee kill-switch: 0 fees trade on; 10 bp charged -> all stopped, never restarts")

    # 7d. improve_inside: 1-tick spread -> at the touch; 2 ticks -> ONE side steps in (no self-cross); 3 -> both
    cfg5 = mod.StableChurnConfig(start_balanced=False, id="im", size_all=False)
    mdp5 = MDP(); mdp5.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("400")}
    ctl5 = mod.StableChurnController(cfg5, mdp5, asyncio.Queue())
    px = lambda acts: sorted((x.side.name, x.price) for x in creates(acts) if x.execution_strategy == ExecutionStrategy.LIMIT_MAKER)
    assert px(await step(ctl5)) == [("BUY", BID), ("SELL", ASK)]
    mdp5.bid, mdp5.ask = Decimal("1.00012"), Decimal("1.00014")            # 2-tick spread, 50/50 -> sell side steps in
    got = px(await step(ctl5))
    assert got == [("BUY", Decimal("1.00012")), ("SELL", Decimal("1.00013"))], got
    assert len({p for _, p in got}) == 2, "self-cross"
    mdp5.bid, mdp5.ask = Decimal("1.00012"), Decimal("1.00015")            # 3 ticks -> both step in
    got = px(await step(ctl5))
    assert got == [("BUY", Decimal("1.00013")), ("SELL", Decimal("1.00014"))], got
    print("7d. improve_inside: 1 tick -> touch; 2 ticks -> one side inside (no self-cross); 3 ticks -> both inside")

    # 7e. DRAWDOWN KILL-SWITCH: value $800 -> $785 keeps trading; -> $779 (> $20 down) stops forever
    cfg6 = mod.StableChurnConfig(start_balanced=False, id="dd")
    mdp6 = MDP(); mdp6.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("400")}
    ctl6 = mod.StableChurnController(cfg6, mdp6, asyncio.Queue())
    assert creates(await step(ctl6))                                         # baseline taken (~800.05)
    mdp6.conn.bal = {"USDC": Decimal("390"), "USDT": Decimal("395")}         # ~785: -15
    assert creates(await step(ctl6)), "must keep trading at -$15"
    mdp6.conn.bal = {"USDC": Decimal("380"), "USDT": Decimal("399")}         # ~779: -21
    ctl6.executors_info = [ex("m1", TradeType.BUY, True, BID)]
    a = await step(ctl6)
    assert not creates(a) and [x.executor_id for x in stops(a)] == ["m1"], a
    mdp6.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("400")}; ctl6.executors_info = []
    assert creates(await step(ctl6)) == [], "must never restart after the drawdown kill"
    print("7e. drawdown kill-switch: -$15 trades on; -$21 stops everything, never restarts")

    # 7f. BOOTSTRAP: USDC-only account with bootstrap_pair=USDC-USDT -> one market SELL of all USDC, then churn USD1
    cfg7 = mod.StableChurnConfig(start_balanced=False, id="bs", trading_pair="USD1-USDT", bootstrap_pair="USDC-USDT")
    assert cfg7.update_markets({}) == {"binance": {"USD1-USDT", "USDC-USDT"}}
    mdp7 = MDP(); mdp7.conn.bal = {"USD1": Decimal("0"), "USDT": Decimal("0"), "USDC": Decimal("800")}
    ctl7 = mod.StableChurnController(cfg7, mdp7, asyncio.Queue())
    c = creates(await step(ctl7))
    assert len(c) == 1 and c[0].trading_pair == "USDC-USDT" and c[0].side == TradeType.SELL and c[0].amount == Decimal("800"), c
    ctl7.executors_info = [ex("b0", TradeType.SELL, False)]                  # conversion in flight -> wait
    assert creates(await step(ctl7)) == []
    ctl7.executors_info = [ex("b0", TradeType.SELL, False, filled=800, active=False)]
    mdp7.conn.bal = {"USD1": Decimal("0"), "USDT": Decimal("800"), "USDC": Decimal("0")}
    assert creates(await step(ctl7)) == []                                    # this tick: sees no USDC left, marks done
    c = creates(await step(ctl7))                                             # next tick: churning starts
    assert c and all(x.trading_pair == "USD1-USDT" for x in c) and c[0].side == TradeType.BUY, c
    print("7f. bootstrap: sells all USDC once on USDC-USDT, waits, then quotes USD1-USDT (first maker BUY)")

    # 7j. bootstrap_pair=auto: funded in ANY stablecoin. USDC + FDUSD -> sold into USDT one by one -> 50/50 start buys
    #     half USD1. On the USDC-USDT fallback, auto never sells the pair's own USDC.
    cfgj = mod.StableChurnConfig(id="au", trading_pair="USD1-USDT", bootstrap_pair="auto")      # start_balanced on
    assert cfgj.bootstrap_pairs() == ["USDC-USDT", "FDUSD-USDT"], cfgj.bootstrap_pairs()
    assert cfgj.update_markets({}) == {"binance": {"USD1-USDT", "USDC-USDT", "FDUSD-USDT"}}
    assert mod.StableChurnConfig(id="a2", trading_pair="USDC-USDT", bootstrap_pair="auto").bootstrap_pairs() == [
        "FDUSD-USDT", "USD1-USDT"]
    mdpj = MDP(); mdpj.conn.bal = {"USDC": Decimal("300"), "FDUSD": Decimal("500"), "USDT": Decimal("0")}
    ctlj = mod.StableChurnController(cfgj, mdpj, asyncio.Queue())
    c = creates(await step(ctlj))
    assert len(c) == 1 and c[0].trading_pair == "USDC-USDT" and c[0].amount == Decimal("300"), c
    ctlj.executors_info = []; mdpj.conn.bal = {"USDC": Decimal("0"), "FDUSD": Decimal("500"), "USDT": Decimal("300")}
    c = creates(await step(ctlj))
    assert len(c) == 1 and c[0].trading_pair == "FDUSD-USDT" and c[0].amount == Decimal("500"), c
    ctlj.executors_info = []; mdpj.conn.bal = {"USDC": Decimal("0"), "FDUSD": Decimal("0"), "USDT": Decimal("800")}
    assert creates(await step(ctlj)) == []                                    # nothing left: bootstrap done
    c = creates(await step(ctlj))                                             # 50/50 start: market BUY ~half USD1
    assert len(c) == 1 and c[0].trading_pair == "USD1-USDT" and c[0].side == TradeType.BUY and \
        c[0].execution_strategy == ExecutionStrategy.MARKET and Decimal("395") <= c[0].amount <= Decimal("400"), c
    mdpk = MDP(); mdpk.conn.bal = {"USD1": Decimal("400"), "USDT": Decimal("400")}              # pair coins only
    ctlk = mod.StableChurnController(mod.StableChurnConfig(id="a3", trading_pair="USD1-USDT", bootstrap_pair="auto"),
                                     mdpk, asyncio.Queue())
    assert creates(await step(ctlk)) == [] and ctlk._boot_done                # nothing to convert: no order
    print("7j. bootstrap auto: USDC then FDUSD sold into USDT, then one market BUY to 50/50; pair coins never sold")

    # 8. size_all (default): new makers take ALL available balance of their funding coin; existing ones not resized
    cfg2 = mod.StableChurnConfig(start_balanced=False, id="sa")
    assert cfg2.size_all
    mdp2 = MDP()
    mdp2.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("400")}
    ctl2 = mod.StableChurnController(cfg2, mdp2, asyncio.Queue())
    c = creates(await step(ctl2))
    got = {x.side: x.amount for x in c if x.execution_strategy == ExecutionStrategy.LIMIT_MAKER}
    assert got[TradeType.SELL] == Decimal("399") and got[TradeType.BUY] == Decimal("399"), got
    # sell filled: now 0 USDC / 800 USDT; the resting buy ($400) is kept, no new sell possible, nothing resized
    mdp2.conn.bal = {"USDC": Decimal("0"), "USDT": Decimal("800")}
    mdp2.conn.locked = {"USDT": Decimal("400")}
    ctl2.executors_info = [ex("b1", TradeType.BUY, True, BID)]
    a = await step(ctl2)
    assert not stops(a) and not [x for x in creates(a) if x.execution_strategy == ExecutionStrategy.LIMIT_MAKER], a
    # the buy fills: 800 USDC / 0 USDT -> one new maker SELL sized to ALL 800 USDC
    mdp2.conn.bal = {"USDC": Decimal("800"), "USDT": Decimal("0")}
    mdp2.conn.locked = {}
    ctl2.executors_info = []
    c = creates(await step(ctl2))
    sells = [x for x in c if x.side == TradeType.SELL and x.execution_strategy == ExecutionStrategy.LIMIT_MAKER]
    assert len(sells) == 1 and sells[0].amount >= Decimal("799"), c
    print("8. size_all: makers take all available balance (399/399, then 799 after a full flip); kept orders not resized")
    # 9. LIVE CONFIG UPDATES - exactly hummingbot's path: re-read yml -> Config(**yml) -> controller.update_config()
    base_yml = dict(start_balanced=False, id="lu", size_all=True, trading_pair="USDC-USDT", volume_target_usd=3500000)
    mdp9 = MDP()
    mdp9.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("400")}
    ctl9 = mod.StableChurnController(mod.StableChurnConfig(**base_yml), mdp9, asyncio.Queue())
    await step(ctl9)
    mdp9.t += 3600
    await ctl9.update_processed_data()
    s1 = ctl9.processed_data["target_now"]
    # 9a. updatable fields apply, non-updatable are ignored
    ctl9.update_config(mod.StableChurnConfig(**{**base_yml, "taker_imbalance_max": "1.0", "band_usd": "300",
                                                "tick_interval_s": 0.25, "trading_pair": "USD1-USDT"}))
    assert ctl9.config.taker_imbalance_max == Decimal("1.0") and ctl9.config.band_usd == Decimal("300")
    assert ctl9.config.tick_interval_s == 1.0 and ctl9.config.trading_pair == "USDC-USDT"
    # 9b. raising the target re-anchors: no jump now, the extra is spread over the remaining time
    ctl9.update_config(mod.StableChurnConfig(**{**base_yml, "volume_target_usd": 7000000}))
    await ctl9.update_processed_data()
    s2 = ctl9.processed_data["target_now"]
    assert abs(s2 - s1) < 1, (s1, s2)
    mdp9.t += 3600
    await ctl9.update_processed_data()
    s3 = ctl9.processed_data["target_now"]
    assert s3 - s2 > 2 * s1 * Decimal("0.99"), (s1, s2, s3)          # new pace ~ (7M - s1) / 46.75 h > 2x old
    # 9c. pause: cancels everything, places nothing; schedule does not build a debt while paused
    ctl9.executors_info = [ex("m1", TradeType.BUY, True, price=BID), ex("m2", TradeType.SELL, True, price=ASK)]
    ctl9.update_config(mod.StableChurnConfig(**{**base_yml, "volume_target_usd": 7000000, "pause": True}))
    a = await step(ctl9)
    assert len(stops(a)) == 2 and not creates(a), a
    ctl9.executors_info = []
    mdp9.t += 3 * 3600
    a = await step(ctl9)
    assert not creates(a), a
    debt = ctl9.processed_data["target_now"] - ctl9.processed_data["volume"]
    assert debt <= 1, debt
    ctl9.update_config(mod.StableChurnConfig(**{**base_yml, "volume_target_usd": 7000000, "pause": False}))
    a = await step(ctl9)
    assert not [x for x in creates(a) if x.execution_strategy == ExecutionStrategy.MARKET], a   # no catch-up burst
    assert len(creates(a)) == 2, a                                                              # both makers back
    # 9d. close_now: straight to the 50/50 close (tilted account -> one taker toward 50/50), then back
    mdp9.conn.bal = {"USDC": Decimal("700"), "USDT": Decimal("100")}
    ctl9.update_config(mod.StableChurnConfig(**{**base_yml, "volume_target_usd": 7000000, "close_now": True}))
    a = await step(ctl9)
    c9 = creates(a)
    assert len(c9) == 1 and c9[0].side == TradeType.SELL and c9[0].execution_strategy == ExecutionStrategy.MARKET, c9
    assert "state=CLOSING" in " ".join(ctl9.status_fields())
    ctl9.update_config(mod.StableChurnConfig(**{**base_yml, "volume_target_usd": 7000000, "close_now": False}))
    assert "state=CHURNING" in " ".join(ctl9.status_fields())
    # 9f. EXIT a depegging base coin: close_base_share 0 + close_now works OUTSIDE the peg band (sells all base)
    mdp9.bid, mdp9.ask = Decimal("0.99500"), Decimal("0.99501")
    mdp9.conn.bal = {"USDC": Decimal("400"), "USDT": Decimal("400")}
    ctl9.executors_info = []
    a = await step(ctl9)
    assert not creates(a), a                                          # plain depeg: stand aside
    ctl9.update_config(mod.StableChurnConfig(**{**base_yml, "volume_target_usd": 7000000, "close_now": True,
                                                "close_base_share": "0"}))
    c9 = creates(await step(ctl9))
    assert len(c9) == 1 and c9[0].side == TradeType.SELL and c9[0].amount >= Decimal("399"), c9
    ctl9.update_config(mod.StableChurnConfig(**{**base_yml, "volume_target_usd": 7000000}))
    mdp9.bid, mdp9.ask = BID, ASK
    print("9f. depeg exit: close_base_share=0 + close_now sells ALL base even outside the peg band:", c9[0].amount)

    # 9e. status line is machine-readable
    st = dict(kv.split("=", 1) for kv in ctl9.status_fields())
    assert {"state", "volume", "schedule", "vol_1h", "maker_share_1h", "target", "maker_share", "fees", "pnl", "mid",
            "hours_left"} <= set(st), st
    # 9g. STATUS survives Condor's 80-char log cut: four lines, each <= 77 chars even at worst-case values
    worst = mod.StableChurnController(mod.StableChurnConfig(start_balanced=False, id="a_long_controller_id_xyz"),
                                      MDP(), asyncio.Queue())
    worst.status_fields = lambda: ["state=KILLED_DRAWDOWN", "volume=99999999", "schedule=99999999",
                                   "vol_1h=9999999", "maker_share_1h=0.000", "target=999999999", "maker_share=0.000",
                                   "fees=1234.5678", "fee_bp_maker=12.345", "fee_bp_taker=12.345", "value=12345.678",
                                   "pnl=-1234.5678", "mid=0.99977500", "base_share=1.000", "hours_left=999.99"]
    lines = worst.status_lines()
    assert len(lines) == 4 and all(len(x) <= 77 for x in lines), [(len(x), x) for x in lines]
    assert all("fee_bp_maker=" in x for x in lines[2:3]) and "pnl=" in lines[2] and "mid=" in lines[3]
    print("9g. STATUS as 4 lines, longest", max(len(x) for x in lines), "chars (Condor cuts at 80)")

    print("9. live updates: updatable applied / tick+pair ignored; target raise re-anchors (no jump);"
          " pause cancels + no debt + no burst on resume; close_now -> 50/50; STATUS line", st["state"])

    print("ALL SMOKE CHECKS PASSED")


asyncio.run(main())
