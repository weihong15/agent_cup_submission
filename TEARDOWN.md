# Teardown — after the Agent Builders Cup

What to delete once the race is over (Oct 2, 2026) and results are final (Oct 7).

**Not for Botcamp.** This is an operator note. It is in the submission folder because that
folder is the thing you will still have in October.

---

## Do this first: what must NOT be deleted

Two images on this machine are **locally built and cannot be re-pulled**. Deleting them is
unrecoverable — they are the production fork, not the published Hummingbot images that share
their name.

```bash
# Verify before deleting ANY hummingbot image. 0 = locally built = unrecoverable.
docker image inspect hummingbot/hummingbot:latest     --format '{{len .RepoDigests}}'
docker image inspect hummingbot/hummingbot-api:latest --format '{{len .RepoDigests}}'
```

| keep forever | why |
|---|---|
| `hummingbot/hummingbot:latest` | prod fork build, `RepoDigests` empty |
| `hummingbot/hummingbot-api:latest` | prod fork build, `RepoDigests` empty |
| `hummingbot/*:fork-local-backup` | second tag on those same images — the recovery path if `:latest` is ever overwritten by a pull. Costs no disk |
| `hummingbot-api_*`, `deploy_*` volumes | prod Postgres and EMQX state. `deploy_postgres-data` dates to Jul 2025 |
| `hummingbot-api`, `hummingbot-broker`, `hummingbot-postgres` containers | the prod stack |
| `cyclops-*`, `cerebro-*`, `sentinel_*` | other projects entirely |

**Never run `docker system prune -a`.** It would take the two fork images with it.

---

## Already done (2026-08-29)

Removed after the submission was pushed:

- 8 `fbc-livetest-*` containers
- the `agentcup` stack (`agentcup-api`, `-broker`, `-postgres`) and its 4 `agentcup_*` volumes
- images `agentcup/hummingbot`, `agentcup/hummingbot-api`,
  `hummingbot/hummingbot:version-2.16.0`, `hummingbot/hummingbot-api:1.0.1`
- the agent_env condor tmux session and the `basis_sampler` process

~12 GB reclaimed. Prod verified intact afterwards.

---

## Still to delete, after Oct 7

### 1. `~/gitFolder/agent_env/`

**It holds a real copy of live exchange API keys** (`hummingbot-api/.env` and
`bots/credentials/master_account/`), so it should not linger.

**Extract two things first — they exist nowhere else:**

```bash
# the UN-MANGLED routine sources. GitHub and the submission carry only the
# mangled copies, so this is the only readable version.
cp -R ~/gitFolder/agent_env/condor/agents/funding_builders_cup/_readable \
      ~/gitFolder/hb_eco/vendors/condor/docs/agent_cup_submission/_readable_sources

# ~111k cross-venue basis samples, 3 days, 166 bases
cp -R ~/gitFolder/agent_env/condor/data/basis_samples \
      ~/gitFolder/hb_eco/vendors/condor/docs/agent_builders_cup/basis_samples
```

Then:

```bash
rm -rf ~/gitFolder/agent_env
```

### 2. Any leftover containers or volumes

```bash
docker ps -a --format '{{.Names}}' | grep -E '^(fbc-|agentcup)' | xargs -r docker rm -f
docker volume ls --format '{{.Name}}' | grep '^agentcup_' | xargs -r docker volume rm
```

Both are no-ops today; they are here in case a later test run recreates them.

### 3. Positions opened during testing

Live tests left real hedged positions. Check and unwind by hand — they are delta-flat, so there
is no hurry, but they are not tracked by anything any more:

```
TRUMP   LONG hyperliquid / SHORT binance   ~45 base
ENA     SHORT hyperliquid / LONG binance   ~390 base
```

### 4. The `agentcup` server entry in the dev condor config

`~/gitFolder/agent_env/condor/config.yml` gets deleted with the folder. Nothing to do unless
you added an `agentcup` server to the **prod** condor — check with:

```bash
grep -A3 'servers:' ~/gitFolder/hb_eco/vendors/condor/config.yml
```

---

## Restoring the test environment, if Botcamp asks for a revision

Nothing is lost — all code is in git (this folder and
`github.com/weihong15/agent_cup_submission`). Rebuilding costs a ~6 GB pull plus a build:

```bash
docker pull hummingbot/hummingbot:version-2.16.0      # ~4.1 GB, slow
docker pull hummingbot/hummingbot-api:1.0.1           # ~2.0 GB
docker build -f docker/Dockerfile.bot -t agentcup/hummingbot:latest     executor/
docker build -f docker/Dockerfile.api -t agentcup/hummingbot-api:latest executor/
python verify_submission.py --api-url ... --bot-image agentcup/hummingbot:latest
```

⚠️ **Never build `FROM hummingbot/hummingbot:latest` on this machine** — that tag is the prod
fork, and the derived image would silently inherit private code. Both Dockerfiles pin released
version tags for exactly this reason.

---

## Order that avoids surprises

1. Confirm results are final (Oct 7) and Botcamp wants no more revisions
2. Extract `_readable/` and `basis_samples/` (above)
3. Unwind the test positions
4. `rm -rf ~/gitFolder/agent_env`
5. Leave every `hummingbot/*` image alone
