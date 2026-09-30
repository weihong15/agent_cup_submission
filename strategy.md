# Stable Churn - a volume-churning algorithm for stablecoins

## The idea

A volume-churning algorithm has one job: trade as much as possible while losing as little as possible. Usually every
fill pays a fee, and often the spread, so volume costs P&L. Stable Churn trades the one book where that cost almost
disappears: **Binance charges 0% maker and 0% taker on its stablecoin pairs**. On **USD1/USDT** the tick is 0.1 bp and
the spread sits at one tick, so the worst a dollar of volume can cost is half a tick (0.05 bp), about $1 per $200,000
traded. There is no crypto price risk, only the peg. The whole problem becomes getting filled in a deep queue.

## What the backtests showed

A queue-aware backtest over recorded Binance order books (top of book, 20-level depth, every trade), at the live
1-second cadence with 150 ms order latency, under an optimistic and a pessimistic queue model. A change was kept only if
it helped under both models, on weekday and weekend data.

1. **Rest the whole balance.** Trades on this pair are large (~$4k), so once the queue ahead clears, one trade fills
   whatever is resting. Resting all available balance as one order doubled free volume versus splitting it.
2. **Chase the touch; don't hoard stale priority.** Keeping old orders off the touch for their queue place, or
   laddering behind it, collapsed volume: those orders tie up the balance.
3. **Step inside a wide spread.** When the spread opens to two ticks, quote one tick inside: the new best price with an
   empty queue. Weekday maker share went from 71-92% to 98-99%.
4. **Cross only a thin level.** When a market order is needed, send it only while the level it would hit holds at most
   30% of the size at the touch: it is about to be consumed and the price steps our way. Quiet-hour cost fell by more
   than half (pessimistic -0.023 -> -0.010 bp per $) at the same volume.

Also measured and dropped: skipping heavy queues (moved volume to paid takers), a BTC/ETH-implied signal (does not
lead the pair), grids and ladders, and a USD1/USDC triangle (the 1 bp tick stacks $200-370k at each price; a small
order at the back almost never fills).

| market | volume / h | maker share | cost, bp per $ |
|---|---|---|---|
| busy weekday | $75-92k | 90-99% | +0.002 to +0.009 |
| weekday, 25 h | $74-81k | 86-93% | -0.001 to +0.006 |
| quiet evening (held out) | $72-74k | 65-77% | -0.004 to -0.010 |

About $3.5-3.9M of volume per 48 hours on $800, P&L between about -$3.5 and +$2.

## How it trades

- **Once, at start:** funded in another stablecoin (e.g. USDC)? `bootstrap_pair: auto` sells it into USDT. Then one
  market order brings the account to 50/50 USD1/USDT.
- **Every second:** one post-only order at the best bid and one at the best ask, each sized to all the balance that
  side can fund, re-posted when the touch moves; one tick inside a 2+ tick spread (one side only at exactly two ticks,
  so our own orders never meet).
- **Only when behind** a straight-line volume schedule ($3.5M over 48 h, about what the makers carry for free): a
  market order, and only across a thin level. When one side has sold out, one order moves 90% of the account back into
  it. A same-side resting order is freed first, because it locks the balance.
- **No end state:** it runs until it is stopped; past 48 hours the schedule continues at the same pace.

## Built to run unattended

- **Fee kill-switch** (> 0.5 bp charged) and **drawdown kill-switch** ($20 below start) - both one-way.
- **Peg guard:** outside 0.9980-1.0020 it cancels everything and stands aside until the price returns.
- **Reject backoff:** if the exchange rejects orders twice in a row with nothing filled, it waits 30 s, doubling to
  10 min - a permanent rejection never becomes a request per second.
- **Venue truth:** balances from the exchange connector; volume and fees counted per executor, surviving pruning.
- **Plain Hummingbot:** one V2 controller using the standard `OrderExecutor` (`LIMIT_MAKER` and `MARKET`).

## The agent

The strategy runs inside Condor: the Condor agent `agent/stable_churn_operator` owns the controller, and its
`stable_churn_supervisor` loop runs in Condor's controller mode - it deploys the controller as its own bot on the first
tick and supervises it with no human in the loop. It never places orders. Every 5 minutes it reads the controller's STATUS lines (volume vs schedule, last-hour maker share,
fees split maker/taker, P&L, peg) and changes live settings. It is non-blocking: the controller decides every second
inside the bot and keeps trading on its last settings while the agent thinks.

- Fees on market orders only -> maker-only (`volume_target_usd: 0`). Fees on every fill -> USDC/USDT; charged there
  too -> shrink the target to what the fee allows; at standard spot fees -> sell to USDT and stop.
- A coin down 40 bp and still falling -> hold only the healthy coin; re-enter after an hour of calm.
- Maker share under 60% -> tighten taker timing, then trim the target; ahead late with free makers -> raise it slightly.
- A kill-switch trips or the bot dies -> redeploy a fresh controller with only the remaining target.

## Live test (Binance, 2026-09-29)

~$205 book, 55 minutes: $3,618 of volume in 27 fills, 97.0% maker, **fees $0.00** on maker and market orders,
P&L -$0.0009. Seven live setting changes (pause, resume, target up and down, exit to one coin, re-entry) were each
applied by the running bot within ~10 s.

## Parameters

| parameter | value | why |
|---|---|---|
| connector / pair | binance / USD1-USDT | 0% maker and taker; short queues for its flow |
| volume_target_usd | 3,500,000 | pace over 48 h, about what the makers carry for free; live-tunable |
| size_all / improve_inside | true / true | rest all balance; step inside a 2+ tick spread |
| taker_imbalance_max | 0.3 | cross only a level under 30% of top-of-book size |
| rebalance_to / behind_clips / clip_usd | 0.9 / 10 / 400 | refill an empty side; catch up only when far behind |
| max_fee_bps / max_drawdown_usd | 0.5 / 20 | kill-switches |
| peg_low / peg_high | 0.9980 / 1.0020 | stand aside outside (USD1 sits at 0.9996-0.9999) |
| bootstrap_pair / start_balanced | auto / true | any stablecoin in, 50/50 on the first ticks |
| close_at_end | false | no end state |

Every field but the pair, cadence and start-up options changes live; see the agent's `stable_churn_params` skill.

## Not wash trading

Every fill is against someone else's order at a public price. We cannot trade with ourselves even by accident:
Binance's self-trade prevention on this pair (`EXPIRE_MAKER`) cancels our own resting order before our market order
could meet it, and our two quotes never cross. The controller provides two-sided liquidity at the best prices of one of
Binance's most-traded books.
