# Stable Churn

A volume-churning algorithm for stablecoins: two-sided post-only market making at the touch of Binance spot
**USD1/USDT** (0% maker and taker fees), supervised by an autonomous Condor agent. Strategy and evidence:
[`strategy.md`](strategy.md).

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

## Run with hummingbot-api

1. Copy `controllers/generic/stable_churn.py` to `bots/controllers/generic/` and
   `conf/controllers/stable_churn_usd1usdt.yml` to `bots/conf/controllers/`.
2. `POST /bot-orchestration/deploy-v2-controllers`
   ```json
   {"instance_name": "stable-churn-usd1", "credentials_profile": "<binance account>",
    "controllers_config": ["stable_churn_usd1usdt.yml"], "headless": true}
   ```

## Run with plain Hummingbot

Mount `controllers/` as the instance's `controllers/`, `conf/controllers/` and `conf/scripts/` into its `conf/`, then:
```
start --script v2_with_controllers.py --conf conf_v2_stable_churn_race.yml
```
or headless: `-e SCRIPT_CONFIG=conf_v2_stable_churn_race.yml -e HEADLESS_MODE=true` on the
`hummingbot/hummingbot:version-2.17.0` image.

## Run with the Condor agent

Copy `agent/stable_churn_operator/` into condor's `agents/`. The agent syncs its controller
(`manage_agent_controllers sync`), uploads the `race_usd1usdt` sample, deploys it with `manage_bots`, then runs the
`stable_churn_supervisor` loop. See `agent/stable_churn_operator/AGENT.md`.

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
