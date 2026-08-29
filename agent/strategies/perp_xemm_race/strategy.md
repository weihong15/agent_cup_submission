---
name: Cross-Venue Funding Arb
description: 'Delta-neutral funding arbitrage on Binance and Hyperliquid perpetuals. Holds the
  leg that receives funding and hedges the other through perp_xemm sessions - a post-only maker
  order on one venue, a market hedge on the other. Funding is the earner: positions are
  force-closed at the end of the race and that close is scored, so a price basis is handed back
  if it has not moved, while funding is banked at every settlement. Carry is computed as discrete
  settlements over a hold horizon H = max(the two legs'' funding intervals). Candidates must clear
  a funding bar AND have positive carry AND clear an execution floor; they are ranked by carry.
  Six slots fill in fixed priority: urgent unwinds, then enters and adds, then normal unwinds.
  The exit price scales with margin health and is then carry-adjusted. A continuous fill_guard
  flips or kills controllers that fill badly between ticks.'
agent_key: null
skills: []
default_config:
  frequency_sec: 900
  execution_mode: loop
  slots: 6
  maker_venue_hl: hyperliquid_perpetual
  taker_venue_bin: binance_perpetual
  min_fundsig_bph: 0.3
  min_edge_bps: 5.0
  min_volume_usd: 1500000
  min_max_leverage: 3
  urgent_fundsig_bph: -0.1
  margin_floor_pct: 0.10
  exit_bps_at_full_margin: -6.0
  exit_bps_at_no_margin: -16.0
  size_usd: 90              # baseline notional per entry, in QUOTE currency (6 x min_notional)
  size_scaled_usd: 120      # after a pair proves itself, see step 3 (8 x min_notional)
  scale_up_realized_bps: 20.0
  leverage: 3
  min_notional: 15
  max_notional: 30
  pct_impact: 0.5
  limit_depth: 0
  limit_tick: 0
  order_refresh_depth: 5
  refresh_edge_pct_threshold: 0.8
  refresh_gap_threshold: 2
  max_consecutive_hedge_failures: 2
  bot_name: fbc-race
  bot_image: hummingbot/hummingbot:latest
  max_global_drawdown_quote: 40
  risk_limits:
    max_position_size_quote: 120
    max_open_executors: 8
default_trading_context: ''
---

# Cross-Venue Funding Arb

Long one venue, short the other, same base, same size. Market risk cancels; the funding
differential remains.

**Funding is the earner. The price edge is an execution floor, not profit** - positions are
force-closed at the end of the race and that close is scored.

## Venue facts

`BASE-USD` on Hyperliquid (settles USDC). `BASE-USDT` on Binance (settles USDT).
Hyperliquid funding settles hourly. Binance settles **1h, 4h or 8h depending on the symbol** -
never assume 8h. The interval decides how many settlements a hold actually collects, so
`basis_scanner` reads it per symbol and reports it; carry is computed over
`H = max(the two intervals)`.

## Each tick

### 1. Get the plan

```
manage_routines(action="run", name="slot_plan", agent="funding_builders_cup",
  config={"slots": <slots>, "min_fundsig_bph": <min_fundsig_bph>,
          "min_edge_bps": <min_edge_bps>, "min_volume_usd": <min_volume_usd>,
          "min_max_leverage": <min_max_leverage>,
          "urgent_fundsig_bph": <urgent_fundsig_bph>,
          "margin_floor_pct": <margin_floor_pct>,
          "size_usd": <size_usd>, "size_scaled_usd": <size_scaled_usd>,
          "min_notional": <min_notional>,
          "exit_bps_at_full_margin": <exit_bps_at_full_margin>,
          "exit_bps_at_no_margin": <exit_bps_at_no_margin>})
```

It returns the ordered allocation: **urgent unwinds -> enters/adds -> normal unwinds**, ranked by
carry. Execute it top-down. Do not re-rank, re-gate or re-size it.

**`MARGIN x% < floor` -> the plan contains no entries.** Free margin is the binding constraint,
not cost, and unwinds are the only way to get it back. Deploy the unwinds and wait.

**`SLOT PLAN REFUSED` -> stop.** Positions could not be read; journal it and do nothing else.

**`UNHEDGED` warning -> re-hedge or close that base before opening anything new.** One leg is
naked directional risk.

### 2. Check margin

```
manage_routines(action="run", name="margin_book", agent="funding_builders_cup",
  config={"margin_floor_pct": <margin_floor_pct>})
```

Read free margin per venue and whatever `fill_guard` did since the last tick.
**A venue marked UNREADABLE -> place nothing on it.**

### 3. Size

**The plan hands you both amounts. Copy one; do not compute it.** Each entry line prints:

```
-> total_amount 289.482 base ($45) | scaled 578.964 ($90) @ 0.15545
```

Those are in **base units** (the coin), already converted from the quote-currency size and
already rounded to a whole number of `min_notional` slices.

- **Default: use `total_amount`** (the `$45` figure).
- **Use `scaled` only after the pair has actually realized it**: the previous window's realized
  spread on that base, from `race_book`/`fill_guard`, was above `scale_up_realized_bps`. Carry
  and edge are forecasts; realized spread is the only number that has already happened.
- Drop straight back to the base amount on the first window that realizes below the bar.
- **Leverage is capped by the venue, per coin.** The scanner already drops anything below
  `min_max_leverage`. Do not assume a coin supports more than 3x: on Hyperliquid most alts cap
  at 3-5x, and the high-leverage majors are exactly the ones whose funding does not diverge.
- **A base that keeps qualifying is where the next dollar goes.** There is no per-base cap:
  adding to a winner beats forcing capital into a worse candidate. Exposure is bounded by
  margin, which `slot_plan` enforces by planning no entries below `margin_floor_pct`.
- Still never exceed the venue's free margin on a single line.

`total_amount` is also the executor's **lifetime** - it stops when that amount has filled.

### 4. Deploy

```
manage_controllers(action="upsert", target="config", config_name="fbc_{base}",
                   config_data={...})
manage_bots(action="deploy", bot_name=<bot_name>, controllers_config=[...],
            image=<bot_image>,
            max_global_drawdown_quote=<max_global_drawdown_quote>)
```

**Copy `maker_connector`, `maker_trading_pair`, `taker_connector`, `taker_trading_pair` and
`maker_side_str` from the plan line verbatim.** Each line prints them.

**`image` is required.** Omitted, the API defaults to a stock image with no `perp_xemm_executor`:
the deploy succeeds, the container starts, and exits. The only symptom is a bot that is not running.

`min_price_edge_bps` is the controller's rest floor:
- **ENTER** -> set it to `min_edge_bps`. Never 0; a 0 floor fills at any edge, including negative.
- **UNWIND** -> set it to the plan's `exit_bps` (negative - what we will pay to leave).

Stop the previous tick's controllers before deploying this tick's plan.

**Unwind timing:** an unwind just **before** a funding print forfeits it; just **after** banks it.
If an unwind is not urgent and a print is minutes away, wait for the next tick.

### 5. Journal - one line

```
trading_agent_journal_write(entry_type="action", text="...")
```

Fixed shape, every tick - the last three are the next tick's only memory:

```
T<n> | <ACTION> <base> <dir> $<size> carry<c> -> <filled/none> @<realized>bp | guard:<what> | HL <x>% BIN <y>%
```

A `learning` only when genuinely new. A `canvas` revision only when a section is now wrong.

## fill_guard, running without you

Every 5 minutes it reads fills from each live bot's SQLite. On a bleeding pair it **flips** the
resting venue first, and kills only if it bleeds again after the flip.

A flip is legal mid-tick because it does not change the trade - direction, position and funding
are identical, only the resting venue moves.

A base it **killed** is telling you the pair is wrong, not the venue. Do not re-enter it
immediately; let it clear the gate again.

## Guardrails

- Leverage <= `leverage`, set per venue before the run. Cross-venue margin is not netted: a large
  adverse move can liquidate one leg while the offsetting gain sits unrealised on the other.
- Below `margin_floor_pct` on a venue: no new entries there.
- Never trade a base the scanner rejected (non-ASCII symbol, or a size-multiplier mismatch such
  as `1000PEPE` against `kPEPE`).
- Never enter on price edge alone.
- Halt a pair, not the run.

## Intended conditions

Liquid perps on both venues whose funding rates diverge enough to pay for a round trip. Most
pairs print the same rate and net to zero carry - sitting out is correct. On a quiet day this
holds two positions, not six.
