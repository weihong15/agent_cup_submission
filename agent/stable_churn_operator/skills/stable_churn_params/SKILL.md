---
name: stable_churn_params
description: Every stable_churn config field - which change live, safe ranges, how to change and verify - plus the
  supervisor playbook.
when_to_use: Before changing ANY parameter of a running stable_churn controller, or when asked what a field does.
created: '2026-09-29T00:00:00Z'
source: agent:stable_churn_operator
---

Hummingbot re-reads the controller's yml about **every 10 s** and applies every field marked updatable. The controller
then logs one line per change, which is the only proof that a change took:

```
[stable_churn_usd1] CONFIG UPDATE applied: taker_imbalance_max: 0.3 -> 0.5; pause: False -> True
```

Every 60 s it also logs four short, machine-readable status lines (each <= 77 characters, because Condor's
`manage_bots(action="logs")` cuts every message at 80; the same fields show in Hummingbot's `status`):

```
STATUS state=CHURNING volume=812345 schedule=820000
STATUS2 vol_1h=78210 maker_share_1h=0.934 target=2500000
STATUS3 fees=0.0000 fee_bp_maker=0.000 fee_bp_taker=0.000 pnl=+0.1234
STATUS4 mid=0.99971 base_share=0.498 value=800.12 hours_left=36.5
```
`state` is one of CHURNING, PAUSED, DEPEG_HALT, CLOSING, KILLED_FEE, KILLED_DRAWDOWN.

## Three ways to change a parameter (all write the same yml)

| Where the bot runs | How |
|---|---|
| hummingbot-api | `POST /controllers/bots/{bot_name}/{controller_config_name}/config` with body `{"field": value}` |
| Condor agent | `manage_bots(action="update_config", bot_name=..., config_name=..., config_data={...}, confirm_override=true)` |

Then wait ~10-15 s and look for `CONFIG UPDATE applied` in the bot log. No line = nothing changed (the value was
already set, the field is not updatable, or the bot is not running). Writing the yml drops its comments; that is harmless.

## Every parameter

**Live** = changes while running. **Restart** = only read at start: stop the bot, edit, start again.

| field | live? | race value | what it does | safe range / when to change |
|---|---|---|---|---|
| `volume_target_usd` | live | 2,500,000 | volume to reach by the planned end (it keeps going after). Changing it **re-anchors** the schedule: no jump, the difference is spread over the time left | 2.5M-4M. Lower if maker share stays < 60% for hours; raise only if we are ahead with maker share > 90% |
| `taker_imbalance_max` | live | 0.3 | a taker only crosses when the level it hits is <= this share of top-of-book size | 0.15-0.5; 1.0 = off (old behaviour) |
| `imbalance_valve` | live | 3 | ...unless behind schedule by more than this x behind_clips x clip ($12k) | 2-5 |
| `behind_clips` | live | 10 | clips behind schedule before a catch-up taker | 5-20 |
| `rebalance_to` | live | 0.9 | share of the account moved into an emptied side by one taker | 0.5-0.9 |
| `clip_usd` | live | 400 | size of a catch-up taker clip | 200-800 |
| `band_usd` | live | 400 | max tilt from 50/50 (only used when size_all is false) | leave |
| `size_all` | live | true | rest the whole available balance on each side | leave true |
| `improve_inside` | live | true | step one tick inside a 2+ tick spread | leave true |
| `pause` | live | false | **true: cancel everything, stand aside, keep inventory.** false: resume. No catch-up burst on resume: the rest of the target is spread over the time left | use on anything unexplained |
| `close_base_share` | live | 0.5 | what a close rebalances to: base coin's share of the account. 0 = all quote (exit USD1), 1 = all base | 0.5 for a planned close; 0 or 1 to exit a depegging coin |
| `close_now` | live | false | **true: close now (cancel makers, one market order to `close_base_share`), then idle - works even outside the peg band.** false: back to churning | use for a planned early finish |
| `peg_low` / `peg_high` | live | 0.9980 / 1.0020 | outside this band: cancel everything and wait | widen only with a clear reason |
| `max_fee_bps` | live | 0.5 | FEE kill-switch threshold | **one-way:** once tripped it never restarts, even if you raise it |
| `max_drawdown_usd` | live | 20 | DRAWDOWN kill-switch threshold | **one-way**, same |
| `race_end_ts` | live | 0 | planned end (unix s) - only paces the schedule; 0 = first tick + 48 h; moving it re-anchors | set to the organiser's time if known |
| `close_at_end` | live | false | false: NO end state - keeps churning until the organisers stop the bot. true: close to `close_base_share` `stop_lead_s` before the end, then idle | race: false |
| `stop_lead_s` | live | 900 | the schedule reaches the target this long before the planned end (and, with close_at_end, the close starts) | 600-1800 |
| `start_balanced` | restart | true | at start: check balances, one market order to 50/50 before quoting | leave true |
| `bootstrap_pair` | restart | auto | at start, sell any USDC / FDUSD / USD1 that is not a pair coin into USDT (funding in any stablecoin); "" = off | race: auto; a personal account: off |
| `connector_name`, `trading_pair` | restart | binance, USD1-USDT, "" | what it trades | - |
| `tick_interval_s` | restart | 1.0 | decision interval | fixed at 1 s |
| `race_duration_s` | restart | 172800 | only used when race_end_ts is 0 | - |

## Playbook (autonomous - no human in the loop)

The full decision table is the supervisor loop (`loops/stable_churn_supervisor/loop.md`) with worked examples in the
`stable_churn_knowledge` skill. In short:

| Seen in STATUS | Action |
|---|---|
| `fee_bp_maker` > 0.02 or KILLED_FEE with maker fees | exit the pair (`exit_usd1usdt`), move to `fallback_usdcusdt`; charged there too -> exit to USDT and stand down |
| `fee_bp_taker` > 0.02, maker free | `volume_target_usd=0` (maker-only); if already killed, deploy `maker_only_usd1usdt` |
| `mid` < 0.9960 falling / > 1.0040 rising | `close_base_share=0` (or 1) + `close_now=true`: hold only the healthy coin |
| `mid` outside 0.9985-1.0015 | `pause=true`; re-enter after 1 h inside 0.9990-1.0010 |
| KILLED_DRAWDOWN, fees and peg clean | redeploy fresh with `max_drawdown_usd: 12`, at most twice |
| `maker_share_1h` < 0.60 for 2 ticks | `taker_imbalance_max=0.2`; still low an hour later with P&L falling -> target -15% |
| ahead, maker_share_1h > 0.90, < 12 h left | target +5% (max twice) |
| newest STATUS time unchanged between two ticks (bot dead; `status` may still say running) | `stop_bot`, redeploy with the remaining target |
