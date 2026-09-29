---
type: generic
description: "GENERIC (ControllerBase) spot market maker for a 0%-fee stablecoin pair: two-sided post-only quotes at the touch sized to all balance, one tick inside a 2+ tick spread, takers timed to a thin level, volume schedule, peg guard, fee + drawdown kill-switches, live pause/close switches, 50/50 finish"
---

# stable_churn

## Category: generic
Extends plain `ControllerBase`: the server imports it from `controllers/generic/stable_churn.py`, and every config
must carry `controller_type: generic`, `controller_name: stable_churn`.

## What it does
Every `tick_interval_s` (1 s) on Binance spot USD1-USDT (0% maker and taker):
1. **Guards first**, in order: optional bootstrap conversion; DRAWDOWN kill-switch; FEE kill-switch (both one-way);
   supervisor `pause`; depeg band -> cancel everything and wait.
2. **Start**, once: `bootstrap_pair: auto` sells any other stablecoin (USDC / FDUSD / USD1 not in the pair) into
   USDT; then `start_balanced` checks balances, one market order to 50/50, then churn. An all-USDT deposit
   buys half in USD1. Skipped while outside the peg band.
3. **No end state** by default (`close_at_end: false`): it churns until the organisers stop the bot; past the
   planned end the schedule continues at the same pace. **Close phase** (`close_now`, or with `close_at_end: true`
   `stop_lead_s` before the end): cancel resting makers first, then one market order
   to `close_base_share` of the account in base (0.5 = 50/50), then idle. `close_now` runs even outside the peg band -
   it is how a supervisor exits a depegging coin (`close_base_share` 0 or 1).
4. **Makers**: one LIMIT_MAKER per side at the current touch, sized to all available balance of its coin
   (`size_all`); re-posted when the touch moves. Spread 3+ ticks: both one tick inside; exactly 2: one side inside.
5. **Takers**, only while behind a linear volume schedule to `volume_target_usd`:
   - paced rebalance - a side just sold out: move `rebalance_to` of the account back with one MARKET order;
   - schedule - behind by `behind_clips` x `clip_usd`: one clip toward 50/50;
   - both wait until the level they would hit is <= `taker_imbalance_max` of top-of-book size (it is about to be
     consumed and tick our way), unless behind by more than `imbalance_valve` x behind_clips x clip.
   A same-side maker is stopped first (it locks the balance).
6. **Accounting**: volume and fees per executor id (survive pruning); four short STATUS lines every 60 s (Condor's log tool cuts messages at 80 chars) with rolling 1 h
   volume and maker share, and fees split into `fee_bp_maker` / `fee_bp_taker`.

## Non-blocking
All decisions are made here, every second, inside the bot. A supervisor (the Condor agent) only reads the logs and
writes the yml; the bot keeps trading on the last config while the supervisor is thinking or absent.

## Live parameters
Every field except `connector_name`, `trading_pair`, `bootstrap_pair`, `tick_interval_s`, `race_duration_s` changes
while running (Hummingbot re-reads the yml ~every 10 s; the controller logs `CONFIG UPDATE applied: ...`).
Changing `volume_target_usd` or `race_end_ts` re-anchors the schedule - no jump. `pause` and `close_now` are the
supervisor's switches. Full table, ranges and playbook: the `stable_churn_params` skill.

## Gotchas
- Kill-switches are one-way: raising a threshold after it tripped does not restart it.
- Binance "pay fees with BNB" must be OFF: fees charged in BNB can read as 0 in `cum_fees_quote`; the drawdown
  switch is the backstop for exactly that.
- Quantities are whole base units (stepSize 1); min notional $5, so a side under ~$6 is not quoted.
- The account must hold only this pair's two coins for `value`/`pnl` in STATUS to mean the account.

## Sample configs
- `race_usd1usdt` - the race: $3.5M over 48 h. Set `race_end_ts` before uploading.
- `fallback_usdcusdt` - same on USDC-USDT (longer queues, lower maker share).
- `livetest_40usd` - 1 hour on a $40 book; every path fires.
- `maker_only_usd1usdt` - no takers (volume_target_usd 0): the mode when only taker fills are charged.
- `exit_usd1usdt` - one market order to `close_base_share`, then idle; deploy it when a controller is killed.
