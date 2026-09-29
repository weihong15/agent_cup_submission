#!/usr/bin/env python3
"""Change stable_churn parameters on a RUNNING Hummingbot bot, and confirm the bot applied them.

Hummingbot re-reads every controller yml ~every 10 s and applies the fields marked is_updatable; stable_churn logs
`CONFIG UPDATE applied: field: old -> new` when it does. This tool does exactly what hummingbot-api's
POST /controllers/bots/{bot}/{controller}/config does (read yml, update keys, write back), but atomically, and then
waits for that log line.

  set_param.py --yml conf/controllers/stable_churn_livetest.yml --log logs/logs_conf_v2_stable_churn_livetest.log \
      taker_imbalance_max=0.5 pause=true

Values are parsed as YAML (true/false, numbers). Refuses non-updatable fields (they would be silently ignored).
Exit 0 = applied and confirmed; 2 = written but not confirmed within --timeout.
"""
import argparse
import os
import sys
import time

import yaml

UPDATABLE = {"volume_target_usd", "clip_usd", "size_all", "improve_inside", "rebalance_to", "band_usd", "behind_clips",
             "taker_imbalance_max", "imbalance_valve", "max_fee_bps", "max_drawdown_usd", "peg_low", "peg_high",
             "pause", "close_now", "close_base_share", "close_at_end", "race_end_ts", "stop_lead_s", "manual_kill_switch", "total_amount_quote"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yml", required=True)
    ap.add_argument("--log", help="bot log to watch for the CONFIG UPDATE confirmation")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("pairs", nargs="+", help="field=value")
    a = ap.parse_args()
    upd = {}
    for kv in a.pairs:
        k, _, v = kv.partition("=")
        if k not in UPDATABLE:
            sys.exit(f"{k} is not updatable while running (would be ignored). Updatable: {sorted(UPDATABLE)}")
        upd[k] = yaml.safe_load(v)
    cfg = yaml.safe_load(open(a.yml))
    old = {k: cfg.get(k) for k in upd}
    cfg.update(upd)
    start = os.path.getsize(a.log) if a.log and os.path.exists(a.log) else 0
    tmp = a.yml + ".tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    os.replace(tmp, a.yml)                       # atomic: the bot never reads a half-written file
    print(f"written {a.yml}: " + ", ".join(f"{k} {old[k]} -> {upd[k]}" for k in upd))
    if not a.log:
        return
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        with open(a.log) as f:
            f.seek(start)
            for line in f:
                if "CONFIG UPDATE applied" in line:
                    print("CONFIRMED:", line.split("CONFIG UPDATE applied:", 1)[1].strip())
                    return
        time.sleep(1)
    print(f"NOT CONFIRMED within {a.timeout:.0f}s (unchanged value, or bot not running)")
    sys.exit(2)


if __name__ == "__main__":
    main()
