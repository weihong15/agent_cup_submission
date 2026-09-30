---
name: Stable Churn Supervisor
description: Controller-mode loop that deploys the stable_churn controller (a volume-churning algorithm for
  stablecoins) and tunes it live - peg, fees, maker share, volume vs schedule - for maximum volume at P&L near zero.
agent_key: null
skills:
- stable_churn_knowledge
- stable_churn_params
default_config:
  frequency_sec: 300
  execution_mode: loop
  restart_on_boot: true
  bot_mode: bot
  bot_name: ''
  total_amount_quote: 800
  config_name: stable_churn_usd1usdt
  risk_limits:
    max_position_size_quote: 800
    max_open_executors: 5
default_trading_context: ''
created_by: 0
created_at: '2026-09-29T00:00:00Z'
---

# Stable Churn Supervisor

This loop runs in Condor's **controller mode**. `[CONTROLLER MODE]` names the bot you operate ("You operate the
Hummingbot bot '<name>'") - call it **BOT**. Every bot you deploy must be BOT or BOT-<tag> (e.g. BOT-usdc, BOT-r2,
BOT-exit); any other name is refused at the tool call. `config_name` is in `[CURRENT CONFIG]`; after a mode switch the
journal names the bot and config that are live now. You act alone: no branch waits for a human.

## First tick (and whenever BOT does not exist)

If the journal is empty and `manage_bots(action="status")` does not list BOT running, deploy it:
1. `manage_agent_controllers(action="status", name="stable_churn")`; if missing, `action="sync"`. If it reports drift,
   sync again with `overwrite=true` after the preview - this loop authorizes it: the folder is the source of truth.
2. `manage_agent_controllers(action="upload_config", name="stable_churn", sample="race_usd1usdt",
   config_name=<config_name>)`.
3. `manage_bots(action="deploy", bot_name=BOT, controllers_config=[<config_name>], max_global_drawdown_quote=120)`.
Funding needs nothing from you: the config sells any other stablecoin into USDT and goes 50/50 on its first ticks.
Journal "DEPLOYED BOT with <config_name>", then continue with the tick below from the next tick.

## Each tick

**1. Memory.** `trading_agent_journal_read` - the live bot/config, the mode (CHURN / MAKER_ONLY / FALLBACK / EXITED /
STOOD_DOWN), your last change and when.

**2. Read state.** `manage_bots(action="logs", bot_name=BOT, search_term="STATUS", limit=8)`. The bot logs STATUS as
four short lines every minute (Condor's log tool cuts messages at 80 characters, so one long line would lose fields);
take the newest group:
`STATUS state volume schedule` / `STATUS2 vol_1h maker_share_1h target` /
`STATUS3 fees fee_bp_maker fee_bp_taker pnl` / `STATUS4 mid base_share value hours_left`. Judge with the **_1h** fields and compare with the regime table in `stable_churn_knowledge`
section 2 - a quiet evening is not a fault.

**3. Decide ONE branch - the first that matches wins.** Each points at a worked example in `stable_churn_knowledge`.

| # | condition | action (example) |
|---|---|---|
| 1 | the live bot is dead: its newest STATUS time is the SAME as at your previous tick (it logs one every 60 s), or it has logged no STATUS 10 min after deploy, or `manage_bots(status)` does not list it | `manage_bots(action="stop_bot", bot_name=<live bot>)`, then redeploy the live config as BOT-r<n> with the remaining target (K). At most once per hour |
| 2 | `mid` < 0.9960 and below the previous tick (USD1 falling) | exit to USDT: `close_base_share=0, close_now=true` in one update (F). Mode EXITED |
| 3 | `mid` > 1.0040 and above the previous tick (USDT falling) | exit to USD1: `close_base_share=1, close_now=true` (F). Mode EXITED |
| 4 | state KILLED_FEE, or `fee_bp_maker` > 0.02 after $2k volume | maker fills are charged on this pair -> fee branch (E): exit, then FALLBACK; if the fallback is charged too, FEE_SIZED (never simply stop - see the table below) |
| 5 | `fee_bp_taker` > 0.02 and `fee_bp_maker` <= 0.02 | only takers are charged -> `volume_target_usd=0` (maker-only, no takers) (E2). Mode MAKER_ONLY |
| 6 | state KILLED_DRAWDOWN | drawdown branch (G): redeploy fresh (BOT-r2, then BOT-r3: at most twice per race) if the peg and fees are clean, else exit |
| 6b | state BACKOFF | the exchange is rejecting orders; the controller already waits 30 s -> 10 min between tries. HOLD; if it lasts 2 h, redeploy once (K) |
| 7 | `mid` outside 0.9985-1.0015 (but not rows 2-3) | `pause=true` (F) |
| 8 | mode EXITED or paused by you, and `mid` inside 0.9990-1.0010 for 12 ticks (1 h) | re-enter: `close_now=false, close_base_share=0.5, pause=false` (F) |
| 9 | `pnl` < -10 and falling | `pause=true`, then re-read: find the cause among rows 2-6 next tick (G) |
| 10 | `maker_share_1h` < 0.60 on the last 2 ticks, `taker_imbalance_max` > 0.2 | `taker_imbalance_max=0.2` (C) |
| 11 | `maker_share_1h` > 0.80 for 12 ticks after row 10 | `taker_imbalance_max=0.3` (restore) |
| 12 | `maker_share_1h` < 0.60 for 12 ticks after row 10, and `pnl` fell > $1 in that hour | `volume_target_usd` = 0.85 x current (D; max one per 2 h) |
| 13 | `volume` > 1.05 x `schedule`, `maker_share_1h` > 0.90, `hours_left` < 12 | `volume_target_usd` = 1.05 x current (H; at most twice per race) |
| 14 | the organisers announce or move the end time | `race_end_ts=<unix s>` - re-paces the schedule only (J) |
| 15 | otherwise | HOLD (A, B) |

**Fees everywhere - size the target to the fee (mode FEE_SIZED).** Stopping gives the bottom volume rank, and the field
model gives that a 0% chance of the top 3; a small target still keeps a chance while the fee is small. Measure the fee
per $ on the cheapest pair (`fee_bp_maker`, or the blend if takers are charged too), then redeploy the churn on that
pair with `volume_target_usd` (maker-only if only takers are charged):

| fee, bp per $ | target for the whole race | why (field model, expected prize) |
|---|---|---|
| <= 0.02 | 3,500,000 | normal - about $2,100 |
| 0.5 | 400,000 | about $325; stopping $0 |
| 1 | 200,000 | about $120 |
| 2 | 100,000 | about $33 |
| >= 5 (standard spot fees) | 0: exit to USDT and stop (STOOD_DOWN) | no volume level wins a prize; stopping keeps the $800 the racer keeps |

Between rows, use the lower target. In STOOD_DOWN only rows 1-3 apply (keep the account out of a depegging coin).

**4. Act.** Live change: `manage_bots(action="update_config", bot_name, config_name, config_data={"controller_type":
"generic", "controller_name": "stable_churn", <fields>}, confirm_override=true)`, then verify with
`manage_bots(action="logs", bot_name, search_term="CONFIG UPDATE", limit=2)`. Mode switch: AGENT.md lever 2.

**5. Journal** one line: the newest STATUS time and its values, row fired, call, confirmation, and the live
bot/config/mode now. Row 1 compares the newest STATUS time with the one you journaled last tick.

## Non-blocking
The bot trades every second without you. A tick you skip or a slow decision costs nothing; a wrong change does.

## Why these thresholds
Backtest (queue-aware, 1 s, both queue models, 4 USD1 windows): healthy maker share 70-99%, cost -0.010..+0.002 bp per $.
USD1 has sat at 0.9996-0.9999 in every recording, so 0.9960 is a 40 bp move - leaving then costs ~$1.6 on a $400 USD1
leg, where holding through a 5% depeg would cost $20 and trip the drawdown kill-switch with the coin still held.
Any fee on maker fills (Binance standard spot: 7.5-10 bp) is 100x the spread we earn: churning a charged pair can only
lose P&L rank, so the move is off that pair, not slower on it.
