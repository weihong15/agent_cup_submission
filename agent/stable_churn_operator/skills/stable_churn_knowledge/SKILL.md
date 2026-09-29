---
name: stable_churn_knowledge
description: What the research proved about stable_churn - the scoring math, expected numbers per market regime,
  measured effect of every parameter, ideas already rejected, and worked STATUS -> decision examples. The supervisor's
  judgment comes from here.
when_to_use: Every supervisor tick before deciding anything that the loop's rule table does not settle on its own, and
  before ANY change to volume_target_usd, taker_imbalance_max, rebalance_to, behind_clips or clip_usd.
created: '2026-09-29T00:00:00Z'
source: agent:stable_churn_operator
---

# stable_churn - what we know, and how to use it

All numbers are backtest results: a queue-aware simulation over recorded Binance order books (top of book, 20-level
depth, every trade) at the live cadence: 1 s decisions, 150 ms order latency. Every result was run under TWO queue
models - *optimistic* (cancels ahead of us advance us) and *pessimistic* (they don't) - and the truth sits between.
A change was adopted only if it helped under both models AND on weekday AND Sunday data.

## 1. What we are optimising

Score = 0.4 x volume rank + 0.4 x P&L rank + 0.2 x vote rank, over 14 entries. **Ranks, not dollars.**
Fitting the field (44 public entries) gave an exchange rate: **$1 of P&L ~ $200,000 of volume** (lambda ~ 2e5,
range 1.7-4.4e5). So:
- extra volume is worth buying up to **~0.05 bp per $** (= $1 per $200k), and that is exactly what one taker clip costs
  here (half a 0.1 bp tick). This is why the target sits just past the free maker volume and not far beyond it.
- one P&L dollar moves the expected prize ~$50; one vote rank ~$250-300. Do not trade dollars of P&L for small volume.
- the $3.5M target had the lowest max-regret across field scenarios (P(top 3) ~0.82). $4M lost in hot and cold fields.

Worked value check: lowering the target by $500k saves at most $500k x 0.05 bp = **$2.50**, and gives up $500k of
volume, which is worth ~$2.50 at lambda 2e5. **A target cut only pays when takers cost MORE than 0.05 bp**, i.e. when
the maker share collapses and the takers are also adversely timed. That is rare; prefer taker-timing changes first.

## 2. What normal looks like (final config, USD1/USDT, $3.5M, 1 s)

| regime | vol_1h | maker_share_1h | cost, bp per $ | notes |
|---|---|---|---|---|
| busy weekday (Asia/EU hours) | $75-92k | 0.90-0.99 | +0.002..+0.009 | makers overshoot the schedule on their own |
| weekday average (25 h) | $74-81k | 0.86-0.93 | -0.001..+0.006 | |
| quiet US evening (Mon 21-00 UTC) | $72-74k | 0.65-0.77 | -0.004..-0.010 | 2-tick spreads rare (1.2-1.8% of time vs 3.4-4.6%) |
| worst single hours seen | ~$57k | 0.36-0.54 (before taker timing) | -0.03 | recovered within 1-3 h every time |

Totals: ~$3.5-3.9M per 48 h, P&L between about -$3.5 and +$2. `pnl` in STATUS includes mark noise of ~$0.13 (the
peg wiggles a fraction of a bp); ignore moves under $0.50. `fees` must be ~0: Binance charges 0% maker and taker here.

## 3. What each parameter really does (measured)

| field | effect in backtests | when to touch |
|---|---|---|
| `taker_imbalance_max` 0.3 | takers wait for a thin level. vs off (1.0), pessimistic cost: quiet -0.023 -> -0.010, weekday -0.005 -> -0.001, Sunday -0.015 -> -0.004 bp; volume unchanged. 0.15 was similar on average but worse on Sunday (-0.012) | 0.2 if maker_share_1h < 0.60 for 2 h; never below 0.15; back to 0.3 when > 0.80 |
| `volume_target_usd` 3.5M | 3M vs 3.5M in quiet regimes: saves ~$2 per 48 h, loses $0.5M volume (a wash at lambda 2e5). 4M: -$0.9 more on weekdays pessimistic, and worse everywhere | see section 1; steps of 10-15%, at most one per 2 h |
| `rebalance_to` 0.9 | grid 0.5/0.75/0.9: 0.9 best at 1 s (the emptied side waits a whole second; a bigger refill = bigger next maker). 0.5 worst (P&L sum +0.33 vs +0.80) | leave |
| `behind_clips` 10 | 2/5/10/20: 10 cheapest near target (busy 0.015 vs 0.019 bp at 5); 20 ~= 10 | leave |
| `clip_usd` 400 | 200 ~= 400; 800 cannot fire (needs $800 free, never there at 50/50) | leave |
| `size_all` true | resting all balance doubles free maker volume vs fixed clips | never set false |
| `improve_inside` true | maker share 71-92% -> 98-99.7% on a weekday; the single biggest win | never set false |
| `imbalance_valve` 3 | lets takers ignore timing only when >$12k behind; keeps the target reachable | leave |
| peg band 0.9980-1.0020 | USD1 sits 0.9996-0.9999; a false halt would end the volume run | never widen; exits and pauses happen in the loop before the band |

Things that do NOT matter much: reaction speed (50 ms-1 s, 10-300 ms latency moves results ~10-15%).

## 4. Already tested and rejected - do not re-try live

Laddering / keeping old orders for queue priority (volume collapsed); splitting the balance into smaller orders (half
the volume); skipping heavy queue levels (moved volume to paid takers); a grid; volatility-spaced quotes; a BTC/ETH
cross-asset signal (no lead); taking on the thicker side; topping up orders; a USD1/USDC triangle (0-6 fills/h: the
1 bp-tick level holds $200-370k of queue); FDUSD (no flow); perps (fees); USDC/USDT as the main pair (longer queues,
USD1 is worth ~+$250-400 expected prize).

## 5. Worked examples (STATUS -> decision)

Names below: BOT is the bot `[CONTROLLER MODE]` names (new bots must be BOT-<tag>); `config_name` is stable_churn_usd1usdt.
Every change is sent as
`manage_bots(action="update_config", bot_name=BOT, config_name="stable_churn_usd1usdt",
config_data={"controller_type": "generic", "controller_name": "stable_churn", <field>: <value>}, confirm_override=true)`
and verified with `manage_bots(action="logs", bot_name=BOT, search_term="CONFIG UPDATE", limit=2)`.

**A. Healthy - HOLD.**
`state=CHURNING volume=1204311 schedule=1150220 vol_1h=83412 maker_share_1h=0.941 target=3500000 maker_share=0.902
fees=0.0000 value=800.61 pnl=+0.61 mid=0.99974 base_share=0.51 hours_left=31.20`
Ahead of schedule, maker share in the weekday band, fees 0, P&L inside noise. Journal: "HOLD - weekday regime, 4.7%
ahead, maker 0.94." No call.

**B. Quiet regime - HOLD, it is expected.**
`vol_1h=72950 maker_share_1h=0.702 ... pnl=-0.41 ... hours_left=22.0` at 22:00 UTC.
Matches the quiet-evening row. Taker timing is already doing its job. HOLD. (Changing things here would chase noise.)

**C. Maker share collapsed for 2 ticks - tighten taker timing.**
Two consecutive ticks: `maker_share_1h=0.52` then `0.55`, `taker_imbalance_max` is 0.3.
Below 0.60 and below the quiet band -> send `{"taker_imbalance_max": 0.2}`. Verify the log shows
`taker_imbalance_max: 0.3 -> 0.2`. Journal the before-value. Re-check after 2 ticks; when maker_share_1h > 0.80 for
an hour, set it back to 0.3.

**D. Still collapsed an hour after C - small target cut.**
12 ticks later, maker_share_1h still 0.50-0.58 and `pnl` fell from -0.8 to -2.1 in that hour (takers costing > 0.05 bp).
Cut 15%: `{"volume_target_usd": 2975000}`. The controller re-anchors: STATUS `schedule` must NOT jump down; it bends.
Journal the numbers. At most one cut per 2 h; restore the target if the weekday regime returns before the last
12 h.

**E. Maker fills charged - leave the pair, then stand down if the fallback is charged too.** (loop row 4)
`state=KILLED_FEE ... fees=1.9200 fee_bp_maker=7.500 fee_bp_taker=10.000 volume=2350 base_share=0.46 mid=0.99972`
Maker fills pay 7.5 bp here; we earn at most 0.1 bp per round trip. Churning can only lose. Steps, one per tick:
1. The killed controller ignores `close_now`, so deploy `exit_usd1usdt` as BOT-exit (close_base_share 0: sell the USD1, keep USDT;
   one taker, ~$0.35 of fee). Mode EXITED.
2. Deploy `fallback_usdcusdt` as bot BOT-usdc (starts from all USDT; its first maker buy gets USDC). It has
   its own fee kill-switch after $2k. Mode FALLBACK.
3. If STATUS of the fallback also shows `fee_bp_maker` > 0.02 (or KILLED_FEE): upsert a copy of `exit_usd1usdt` with
   `trading_pair: USDC-USDT, id: stable_churn_usdc_exit` via `manage_controllers(action="upsert", target="config")`,
   deploy it (sells the USDC), then apply the loop's FEE_SIZED table: at 0.5-2 bp keep churning the cheaper pair with a
   small target (0.5 bp -> $400k, 1 bp -> $200k, 2 bp -> $100k): the field model puts stopping at a 0% chance of the
   top 3, a small target still at 2-17%. At >= 5 bp nothing wins a prize, so exit to USDT and stop (STOOD_DOWN) to keep
   the capital.

**E2. Only takers charged - go maker-only on the same pair.** (loop row 5)
`fees=0.4000 fee_bp_maker=0.000 fee_bp_taker=10.000 maker_share_1h=0.88 state=CHURNING`
Makers are still free, so keep them and drop every taker: `{"volume_target_usd": 0}` (no schedule -> no paced
rebalance, no catch-up). Backtest maker-only weekday volume with improve_inside: ~$72-83k/h - most of the volume, none of
the fee. If the controller was already KILLED_FEE, deploy `maker_only_usd1usdt` instead as BOT-maker (fresh fee counters; its own
fills are all maker). Mode MAKER_ONLY.

**F. Peg moving - stand aside, exit if it runs, re-enter when it settles.** (loop rows 2, 3, 7, 8)
`mid=0.99842` (normal 0.9996-0.9999): row 7 -> `{"pause": true}`.
`mid=0.99570`, lower than last tick: row 2 -> `{"close_base_share": 0, "close_now": true}` in ONE update. It works in
DEPEG_HALT (close_now bypasses the peg guard): cancels makers, sells all USD1. Verify `base_share` ~0 next STATUS.
Mode EXITED. USDT falling instead (`mid` > 1.0040 and rising): same with `close_base_share: 1`.
Re-entry (row 8): 12 ticks inside 0.9990-1.0010 -> `{"close_now": false, "close_base_share": 0.5, "pause": false}`.
The schedule re-anchored while you stood aside, so there is no catch-up burst.

**G. Drawdown.** (loop rows 6, 9)
`pnl=-10.40` and falling: `{"pause": true}`, and next tick find the cause: fees (E/E2), peg (F), otherwise mark noise
of a thin moment. `state=KILLED_DRAWDOWN`: if `fee_bp_maker` and `fee_bp_taker` are ~0 and `mid` is inside
0.9990-1.0010, the loss is a one-off - redeploy `race_usd1usdt` as a new bot BOT-r2 with
`max_drawdown_usd: 12` (upsert the config with that field), at most once per race. Otherwise deploy `exit_usd1usdt`
toward the healthy coin and stay EXITED until row 8 holds.

**H. Ahead late in the race - optional small raise.**
`hours_left=10.5 volume=3310000 schedule=3120000 maker_share_1h=0.95` on a busy weekday morning.
Makers are overshooting for free. Raise 5%: `{"volume_target_usd": 3675000}` (it re-anchors; the extra is spread over
the remaining hours). At most twice per race. Never raise when maker_share_1h < 0.85.

**I. Killed.** Never raise the threshold (it would not restart). KILLED_FEE -> example E / E2 by the fee split;
KILLED_DRAWDOWN -> example G.

**J. The organisers announce the end time.**
`{"race_end_ts": <unix s>}` (compute it; do not guess). It only re-paces the schedule (no jump); there is no end
state - the bot keeps churning until the organisers stop it.

**Every redeploy mid-race** (E, E2, G, K): a new controller counts volume from zero. Upsert its config with
`volume_target_usd` = old target - `volume` from the old controller's last STATUS (what is left), and the same
`race_end_ts`, so it does not chase the full target again with paid takers.

**K. Bot gone.** No STATUS for 3 minutes and `manage_bots(action="status")` does not list it running: deploy the live
config again as BOT-r<n> with the remaining target (above). At most once per hour.
