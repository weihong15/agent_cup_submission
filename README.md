# Funding Builders Cup — submission

**Read this file first.** It is the whole install in one page; `INSTALL.md` is the detail
behind step 1, and `verify_submission.py` proves the install worked.


Cross-venue funding arbitrage on Binance and Hyperliquid perpetuals. Delta-neutral perp_xemm:
post-only maker on one venue, market hedge on the other; the funding differential is the earner.

**Two drops.** Nothing else is needed.

### What is in this bundle

```
executor/          -> goes INTO the hummingbot image (drop 1)
docker/            -> the two Dockerfiles that do drop 1 for you
agent/             -> goes to condor/agents/funding_builders_cup/ (drop 2)
INSTALL.md         -> full build detail
verify_submission.py -> run it after installing
README.md          -> this file
```

`agent/` is the folder to copy, and it must land under the name
**`funding_builders_cup`** - `verify_submission.py` and the routine loader both key on it.

---

## Drop 1 — the executor (needs a local image build)

| file | goes to |
|---|---|
| `perp_xemm_executor/__init__.py` | `hummingbot/strategy_v2/executors/perp_xemm_executor/` |
| `perp_xemm_executor/data_types.py` | same |
| `perp_xemm_executor/perp_xemm_executor.py` | same |
| `register_executor.py` | run once at build time (see below) |
| `perp_xemm_controller.py` | `hummingbot-api/bots/controllers/generic/` |

`Dockerfile.bot` and `Dockerfile.api` are included and do all of this.

**The executor must go into TWO images.** The bot-runner image runs it; the `hummingbot-api`
image also needs it, because the API imports the controller to validate a config and the
controller imports `PerpXEMMExecutorConfig`. Miss the second and configs silently fail to save
while the bot image looks fine.

**Registration touches three files** (`executors/data_types.py`, `executor_orchestrator.py`,
`models/executors_info.py`). All three must agree, and all three fail at *deploy* time rather
than build time. `register_executor.py` does all three and is idempotent; both Dockerfiles run
it and then assert the result, so a mis-registration fails the build instead of the race.

```bash
docker build -f docker/Dockerfile.bot -t <YOUR_BOT_IMAGE>     executor/
docker build -f docker/Dockerfile.api -t <YOUR_API_IMAGE>     executor/
```

Full detail, including the two build gotchas (the conda interpreter, and the absent
`hummingbot` user), is in `INSTALL.md`.

---

## Drop 2 — the Condor agent (copy, no build)

Copy the whole folder to `condor/agents/funding_builders_cup/`:

```
AGENT.md                              role, venue facts, hard rules
strategies/perp_xemm_race/strategy.md the tick loop and every threshold
routines/slot_plan.py                 THE plan: ordered slot allocation
routines/basis_scanner.py             candidate table, 5 bulk API calls
routines/margin_book.py               per-venue free margin, hedge state
routines/fill_guard.py                continuous guard, kills bad sessions
skills/venue_rejects/SKILL.md         venue reject handling
```

---

## Two things you must set

`strategy.md` → `default_config.bot_image` currently reads `hummingbot/hummingbot:latest`.

**Set it to whatever you tagged the bot image**, or tag your build
`hummingbot/hummingbot:latest` and change nothing.

A mismatch is silent and fatal: the deploy *succeeds*, the container starts on an image with no
`perp_xemm_executor`, and exits code 1. The only symptom is a bot that is not running.

**2. Per-venue leverage.** Set it on both venues before the run - `leverage: 3` in
`strategy.md` is what the agent assumes, but the controller has no leverage field and cannot
set it. Cross-venue margin is not netted, so a large adverse move can liquidate the losing leg
on one venue while the offsetting gain sits unrealised on the other.

Also worth knowing:

- **`fill_guard` is continuous, and must be started with `arm=True` for the race.**

  ```
  manage_routines(action="run", name="fill_guard", agent="funding_builders_cup",
                  config={"arm": True})
  ```

  Start it alongside the agent and leave it running for the whole race. It is the only
  in-flight defence: between two 15-minute ticks it is the one thing that can stop a session
  filling at a negative spread. It ships `arm=False` so that a first run observes rather than
  acts - **that default is for install-time testing, not for the race.** Left at `False` it
  reports and never intervenes.
- **Leverage is a per-venue account setting**, not a controller field. Set it before the run.

---

## Verifying

```bash
python verify_submission.py --api-url http://localhost:8000 \
                            --user <u> --password <p> \
                            --bot-image <YOUR_BOT_IMAGE>
```

Six checks, in the order things actually break:

1. executor importable and registered in both images
2. controller discoverable through the API
3. a real controller config saves and reads back (incl. a negative exit edge)
4. all four routines discovered by Condor's loader
5. `basis_scanner` returns live candidates
6. the bot image exists and carries the executor

Everything is read-only except one controller config, which is deleted afterwards. It places
no orders.

---

## Venue facts the code depends on

| | Hyperliquid | Binance |
|---|---|---|
| pair suffix | **`-USD`** | **`-USDT`** |
| settles in | USDC | USDT |
| funding | hourly | 4h or 8h, per symbol |

`slot_plan` emits the exact connector/pair strings and the agent copies them verbatim, because
a Binance-spelled pair on Hyperliquid is accepted by the API and then kills the connector with
a `KeyError`.

---

## What it does each tick (900s)

```
slot_plan  ->  urgent unwinds, then enters/adds, then normal unwinds (6 slots, ranked by carry)
margin_book -> per-venue free margin = min(computed, venue-reported)
agent      ->  sizes each line, deploys one controller per line
fill_guard ->  between ticks: flips a bleeding session's resting venue, kills it if it bleeds again
```

Entry needs all three: `fundsig >= 0.3 bp/h`, `carry > 0`, `edge >= 5 bp`. Most pairs print the
same funding on both venues and net to zero carry — holding two positions rather than six on a
quiet day is correct behaviour, not a fault.
