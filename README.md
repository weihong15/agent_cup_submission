# Stable Churn

A volume-churning algorithm for stablecoins: two-sided post-only market making at the touch of Binance spot
**USD1/USDT** (0% maker and taker fees). It runs **inside Condor, as the Condor agent `stable_churn_operator`**: the
agent's loop runs in Condor's controller mode, deploys the Hummingbot V2 controller it owns, and supervises it with no
human in the loop. Strategy and evidence: [`strategy.md`](strategy.md).

## The entry is one folder: `agent/stable_churn_operator/`

Copy it into condor's `agents/`. Everything lives there, once:

| path (inside the agent) | what |
|---|---|
| `AGENT.md` | the agent: levers, hard rules, deploy |
| `loops/stable_churn_supervisor/loop.md` | the supervisor loop (controller mode, every 300 s, restart on boot) |
| `controllers/stable_churn/stable_churn.py` | the Hummingbot V2 controller the agent owns (stock Hummingbot imports only) |
| `controllers/stable_churn/CONTROLLER.md` | how the controller trades, live parameters, gotchas |
| `controllers/stable_churn/sample_configs/` | `race_usd1usdt` (the race), `fallback_usdcusdt`, `maker_only_usd1usdt`, `exit_usd1usdt`, `livetest_40usd` |
| `skills/stable_churn_params/` | every parameter: live or restart-only, safe ranges, how to change it |
| `skills/stable_churn_knowledge/` | backtest results, what normal looks like, worked examples |
| `shutdown.md` | what to do on shutdown |

Outside the agent: `strategy.md` (the write-up) and `tests/smoke_stable_churn.py` (offline test of the controller).

## Run inside Condor

1. Copy `agent/stable_churn_operator/` into condor's `agents/`.
2. Start the `stable_churn_supervisor` loop on a server with a funded Binance spot account (connector `binance`).
   Its `default_config`: `execution_mode: loop`, every 300 s, `bot_mode: bot` (controller mode), `restart_on_boot: true`,
   `total_amount_quote: 800`, `risk_limits.max_position_size_quote: 800`.
3. On its first tick the loop syncs the controller to the server (`manage_agent_controllers sync`), uploads the
   `race_usd1usdt` sample and deploys it as its own bot (`manage_bots deploy`, `max_global_drawdown_quote: 20`).
   Every later tick it reads the bot's four STATUS lines and tunes live settings (`manage_bots update_config`). Extra
   bots it may need (fallback pair, redeploy, exit) are named `<its bot>-<tag>`, inside Condor's ownership namespace.

Funding in any stablecoin works: the race config sells USDC / FDUSD into USDT once at start (`bootstrap_pair: auto`),
then goes 50/50 with one order. There is no end state: it runs until stopped.

Verified: a full local rehearsal on upstream condor + hummingbot-api (the loop synced, uploaded and deployed the
controller on its first tick and supervised it; 0 ownership violations, zero fees), plus condor's own loop store,
config model, ownership check and risk engine; live-tested on Binance.

## Test

The controller's offline smoke test, in the official Hummingbot image:
```
docker run --rm --entrypoint bash -e PYTHONPATH=/home/hummingbot -v "$PWD":/repo \
  hummingbot/hummingbot:version-2.17.0 -lc 'conda activate hummingbot; cd /repo && python -B tests/smoke_stable_churn.py'
# -> ALL SMOKE CHECKS PASSED
```
