# Stable Churn

A volume-churning algorithm for stablecoins: two-sided post-only market making at the touch of Binance spot
**USD1/USDT** (0% maker and taker fees). It runs **inside Condor, via the Condor agent `stable_churn_operator`**: the
agent's loop runs in Condor's controller mode, deploys the Hummingbot V2 controller it owns, and supervises it with no
human in the loop. Strategy and evidence: [`strategy.md`](strategy.md).

Verified against the official **hummingbot v2.17.0** image, **hummingbot-api** main and **condor** main (2026-09-29):
the real controller loader, the smoke test, hummingbot-api's config validation, and condor's agent checks.

## What's here

| path | what |
|---|---|
| `controllers/generic/stable_churn.py` | the Hummingbot V2 controller (one file, stock Hummingbot imports only) |
| `conf/controllers/stable_churn_usd1usdt.yml` | the race config |
| `conf/controllers/stable_churn_usdcusdt.yml` | the same strategy on USDC/USDT (fallback) |
| `conf/controllers/stable_churn_livetest.yml` | a 1-hour test on ~$40 that exercises every path |
| `conf/scripts/conf_v2_stable_churn_*.yml` | `v2_with_controllers.py` script configs for a plain Hummingbot run |
| `agent/stable_churn_operator/` | the Condor agent: `AGENT.md`, supervisor loop, skills, and the controller it owns |
| `docs/PARAMS.md` | every parameter: live or restart-only, safe ranges, how to change it on a running bot |
| `tools/set_param.py` | change a running bot's parameters and wait for its confirmation |
| `tests/smoke_stable_churn.py` | offline smoke test (no network, no orders) |

## Account

- Binance **spot**, funded in any stablecoin. With `bootstrap_pair: auto` (race config) the bot sells USDC / FDUSD into
  USDT once at start, then one market order brings it to 50/50 USD1/USDT.
- Recommended: turn off "Use BNB to pay fees", so any fee would be charged in the pair's coins where the kill-switches
  see it.
- No end time is needed: the bot has no end state and runs until it is stopped.

## Run inside Condor (the entry)

1. Copy `agent/stable_churn_operator/` into condor's `agents/` (the agent ships the controller in
   `controllers/stable_churn/` with its sample configs).
2. Start the agent's `stable_churn_supervisor` loop on a server with a funded Binance spot account. Its
   `default_config`: `execution_mode: loop`, every 300 s, `bot_mode: bot` (controller mode), `restart_on_boot: true`,
   `total_amount_quote: 800`, `risk_limits.max_position_size_quote: 800`.
3. On its first tick the loop syncs the controller to the server (`manage_agent_controllers sync`), uploads the
   `race_usd1usdt` config and deploys it as its own bot (`manage_bots deploy`, with `max_global_drawdown_quote: 20`).
   Every later tick it reads the bot's STATUS lines and tunes live settings (`manage_bots update_config`). Any extra bot
   it needs (fallback pair, redeploy, exit) is named `<its bot>-<tag>`, inside Condor's ownership namespace.

Verified against upstream condor: the loop loads through Condor's `StrategyStore`, its config validates as
`AgentConfig`, every bot name it uses passes Condor's ownership check, and its deploy passes Condor's risk engine.

## Run the controller alone (testing, without Condor)

### With hummingbot-api

1. Copy `controllers/generic/stable_churn.py` to `bots/controllers/generic/` and
   `conf/controllers/stable_churn_usd1usdt.yml` to `bots/conf/controllers/`.
2. `POST /bot-orchestration/deploy-v2-controllers`
   ```json
   {"instance_name": "stable-churn-usd1", "credentials_profile": "<binance account>",
    "controllers_config": ["stable_churn_usd1usdt.yml"], "headless": true}
   ```

### With plain Hummingbot

Mount `controllers/` as the instance's `controllers/`, `conf/controllers/` and `conf/scripts/` into its `conf/`, then:
```
start --script v2_with_controllers.py --conf conf_v2_stable_churn_race.yml
```
or headless: `-e SCRIPT_CONFIG=conf_v2_stable_churn_race.yml -e HEADLESS_MODE=true` on the
`hummingbot/hummingbot:version-2.17.0` image.

## Changing settings while it runs

Hummingbot re-reads the controller yml about every 10 s; the controller logs `CONFIG UPDATE applied: field: old -> new`
and a `STATUS` line every minute. From hummingbot-api:
`POST /controllers/bots/{bot_name}/{controller_config_name}/config` with `{"field": value}`. On a plain install:
```
python tools/set_param.py --yml conf/controllers/stable_churn_usd1usdt.yml \
    --log logs/logs_conf_v2_stable_churn_race.log pause=true
```

## Test

Inside the hummingbot environment. With the official image:
```
docker run --rm --entrypoint bash -e PYTHONPATH=/home/hummingbot -v "$PWD":/repo \
  hummingbot/hummingbot:version-2.17.0 -lc 'conda activate hummingbot; cd /repo && python -B tests/smoke_stable_churn.py'
# -> ALL SMOKE CHECKS PASSED
```
