# Installing `perp_xemm_executor` — for the Hummingbot / Botcamp team

Everything needed to run the submitted strategy. Three files, two images, one bind-mount.

**Time: ~5 minutes** on a machine that already has the official images.

---

## TL;DR

```bash
docker build -f docker/Dockerfile.bot -t agentcup/hummingbot:latest     executor/
docker build -f docker/Dockerfile.api -t agentcup/hummingbot-api:latest executor/
cp executor/perp_xemm_controller.py <hummingbot-api>/bots/controllers/generic/
```

Then run the API from `agentcup/hummingbot-api:latest`, and deploy bots with
`image: agentcup/hummingbot:latest`.

**⚠️ Tag the bot image whatever you like - but tell us, or tag it
`hummingbot/hummingbot:latest`.** The strategy passes an image name on every deploy, read from
`bot_image` in `strategy.md`. If that name does not match your build, the deploy SUCCEEDS, the
container starts on an image with no `perp_xemm_executor`, and exits immediately. The only
symptom is a bot that is not running. See §4a.

Both builds **self-verify** and fail loudly if the registration did not take.

---

## 1. What the files are, and where each one goes

| File | Destination | Needs an image rebuild? |
|---|---|---|
| `perp_xemm_executor/data_types.py` | `hummingbot/strategy_v2/executors/perp_xemm_executor/` | **Yes — in two images** |
| `perp_xemm_executor/perp_xemm_executor.py` | same | **Yes — in two images** |
| `perp_xemm_controller.py` | `hummingbot-api/bots/controllers/generic/` | **No** — bind-mounted |

### Why the executor has to go into *two* images

This is the part that is easy to get wrong, because missing the second one produces a
**working build and a broken deploy**.

1. **The bot-runner image** (`hummingbot/hummingbot`) — the container `hummingbot-api` spawns per
   bot. It imports and runs the executor. Obvious.

2. **The `hummingbot-api` image** — less obvious. The API imports the *controller* to read and
   validate its config schema, and the controller's first import is:

   ```python
   from hummingbot.strategy_v2.executors.perp_xemm_executor.data_types import PerpXEMMExecutorConfig
   ```

   The API's own image pip-installs `hummingbot`, so if the executor is missing there, saving a
   controller config fails — even though the bot image is perfect.

The controller needs no rebuild: `docker_service.py` bind-mounts `bots/controllers` to
`/home/hummingbot/controllers` in the bot container, and the API mounts `./bots` into itself.

---

## 2. Registration: three files that must agree

Dropping the executor directory in is not enough. Upstream keeps its executor registry in three
places and **all three must be updated together**:

| # | File | Edit |
|---|---|---|
| 1 | `strategy_v2/executors/data_types.py` | add `"perp_xemm_executor"` to the `type` Literal |
| 2 | `strategy_v2/executors/executor_orchestrator.py` | import `PerpXEMMExecutor`, add it to `_executor_mapping` |
| 3 | `strategy_v2/models/executors_info.py` | import `PerpXEMMExecutorConfig`, add it to the `AnyExecutorConfig` union |

Miss #1 and the config fails validation. Miss #2 and the executor is never constructed. Miss #3 and
the executor's state cannot be serialised back. **All three fail at deploy time, not at build
time**, which is why `register_executor.py` does all three and the Dockerfiles assert the result.

`register_executor.py` is idempotent — re-running it, or rebuilding on a base that already has the
executor, is a no-op rather than a double edit.

---

## 3. The builds

Build context is `executor/` for both.

```bash
docker build -f docker/Dockerfile.bot -t agentcup/hummingbot:latest     executor/
docker build -f docker/Dockerfile.api -t agentcup/hummingbot-api:latest executor/
```

Each Dockerfile:

1. starts `FROM` a **pinned official released tag**;
2. `COPY`s the executor package into that image's `hummingbot` install path;
3. runs `register_executor.py` against it;
4. **asserts** the executor is in `_executor_mapping` and in `AnyExecutorConfig`, failing the build
   otherwise.

Two details that cost time if you rediscover them:

- **The install path differs between the images.** Bot: `/home/hummingbot/hummingbot`.
  API: `/opt/conda/envs/hummingbot-api/lib/python3.12/site-packages/hummingbot`.
- **`python` at build time is the wrong interpreter.** Both images run inside a conda env that is
  activated by the entrypoint, so a `RUN python ...` layer gets the *base* env and dies with
  `ModuleNotFoundError: No module named 'pandas'`. Both Dockerfiles pin the env interpreter via
  `ARG PY`.

### If you prefer to build hummingbot from source

Not required, and not recommended for this. But if you do, copy `executor/perp_xemm_executor/`
into `hummingbot/strategy_v2/executors/`, apply the same three registration edits, and build both
images from that tree — the `hummingbot-api` Dockerfile expects `hummingbot/` as a sibling of
`hummingbot-api/` because it does `COPY hummingbot/ ./hummingbot-src/`.

---

## 3a. Three things that cost time if you rediscover them

All three were hit while building this; each produces a confusing failure.

**The `hummingbot` user does not exist.** The official image runs as root and has no `hummingbot`
entry in `/etc/passwd`. A `USER hummingbot` line builds fine and then fails at *run* time with
`unable to find user hummingbot: no matching entries in passwd file`. Neither Dockerfile sets
`USER` — the base image's own setting is correct.

**Fork-only connectors will break API startup.** If your credentials directory carries a connector
that official hummingbot does not ship, the API crashes on boot while decrypting:

```
KeyError: 'coinbase_perpetual'
...
ERROR:    Application startup failed. Exiting.
```

It is fatal, not a warning, and the message does not say "unknown connector". Keep only connectors
present in the official build — for this strategy, `binance_perpetual` and `hyperliquid_perpetual`.

**`POST .../config/validate` requires an explicit `id`.** The save endpoint
(`POST /controllers/configs/{name}`) does not. If you only want to confirm the executor is wired,
save a config and read it back — that exercises the same imports without the quirk.

## 4. Running it

```yaml
# docker-compose.override.yml, next to hummingbot-api's docker-compose.yml
services:
  hummingbot-api:
    image: agentcup/hummingbot-api:latest
```

Compose merges an override file automatically, so the upstream compose file stays untouched.

Deploy bots with `image: agentcup/hummingbot:latest` — it is a field on the deploy request
(`models/bot_orchestration.py`), defaulting to `hummingbot/hummingbot:latest`.

---

## 4b. The image name is the one thing we cannot guess

This bit us in testing, so it is worth being explicit.

`manage_bots(action="deploy", ...)` takes an `image` field which **defaults to
`hummingbot/hummingbot:latest`** (`models/bot_orchestration.py`). Our strategy sets it from
`bot_image` in `agents/funding_builders_cup/strategies/perp_xemm_race/strategy.md`:

```yaml
default_config:
  bot_image: hummingbot/hummingbot:latest
```

**Two ways to make this correct, pick either:**

1. **Tag your build `hummingbot/hummingbot:latest`** - then the shipped default is already right
   and nothing needs editing. Simplest if this host only runs competition bots.
2. **Tag it anything else** (e.g. `botcamp/hummingbot-perpxemm:1.0`) and change the one line
   above to match.

Either is fine. What is fatal is a mismatch: the API pulls or finds the named image, starts a
container that cannot construct `perp_xemm_executor`, and the container exits with code 1 while
the deploy call reports success. No error surfaces to the agent.

**Verify before the race** - after the first deploy, the container should still be up:

```bash
docker ps --filter name=<bot_name> --format '{{.Names}} {{.Status}}'
```

`Exited (1)` within seconds means the image is wrong.

## 5. Verifying

```bash
./verify.sh
```

Or by hand, per image:

```bash
docker run --rm --entrypoint /opt/conda/envs/hummingbot/bin/python agentcup/hummingbot:latest -c "
from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator as O
print('perp_xemm_executor' in O._executor_mapping)"
```

Expected: `True`. Use `/opt/conda/envs/hummingbot-api/bin/python` for the API image.

**The strongest end-to-end check** — save a controller config and read it back. This only succeeds
if the API image imported `PerpXEMMExecutorConfig`:

```bash
curl -u $USER:$PASS -X POST -H 'Content-Type: application/json' \
  -d '{"controller_name":"perp_xemm_controller","controller_type":"generic",
       "maker_connector":"hyperliquid_perpetual","maker_trading_pair":"XMR-USD",
       "taker_connector":"binance_perpetual","taker_trading_pair":"XMR-USDT",
       "maker_side_str":"BUY","total_amount":0.05,"min_notional":11,"max_notional":60,
       "pct_impact":0.5,"min_price_edge_bps":8,"order_refresh_depth":5}' \
  http://localhost:8000/controllers/configs/smoke_test
```

Expected: `{"message":"Configuration 'smoke_test' saved successfully"}`. Delete it afterwards.

---

## 6. Two warnings worth reading

**`:latest` may not be what you think.** On a machine that has ever built the strategy from source,
`hummingbot/hummingbot:latest` is a *local build*, not the published image — `docker image inspect`
shows empty `RepoDigests`. Building `FROM ...:latest` there silently inherits whatever that build
contained. Both Dockerfiles therefore pin **released version tags**. Please keep them pinned.

**Do not `docker pull hummingbot/hummingbot:latest` on such a machine** without tagging the local
build first — the pull overwrites the tag and the local image is unrecoverable:

```bash
docker tag hummingbot/hummingbot:latest hummingbot/hummingbot:fork-local-backup
```

---

## 7. Files

```
executor/
  perp_xemm_executor/
    __init__.py
    data_types.py            # PerpXEMMExecutorConfig
    perp_xemm_executor.py    # PerpXEMMExecutor
  perp_xemm_controller.py    # -> bots/controllers/generic/
  register_executor.py       # idempotent 3-file registration, runs at build time
docker/
  Dockerfile.bot             # FROM hummingbot/hummingbot:version-2.16.0
  Dockerfile.api             # FROM hummingbot/hummingbot-api:1.0.1
verify.sh
```
