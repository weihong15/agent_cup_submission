#!/usr/bin/env python
"""Verify a Funding Builders Cup submission is correctly installed.

Read-only apart from one controller config, which is created and then deleted. Places no
orders and starts no bots.

    python verify_submission.py --api-url http://localhost:8000 \
                                --user <u> --password <p> \
                                --bot-image botcamp/hummingbot-perpxemm:1.0

Checks run in the order things actually break in practice. Each prints PASS/FAIL with the
reason, and the exit code is the number of failures.
"""
import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"\n         {detail}" if detail else ""))
    return ok


def api(url, user, pw, path, method="GET", body=None):
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def docker_py(image, py, code):
    """Run a snippet inside an image with that image's own interpreter."""
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", py, image, "-c", code],
        capture_output=True, text=True, timeout=180,
    )


EXECUTOR_PROBE = """
from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator as O
from hummingbot.strategy_v2.models.executors_info import AnyExecutorConfig
from hummingbot.strategy_v2.executors.perp_xemm_executor.data_types import PerpXEMMExecutorConfig as C
import typing
assert 'perp_xemm_executor' in O._executor_mapping, 'not in orchestrator dispatch'
assert C in typing.get_args(AnyExecutorConfig), 'not in AnyExecutorConfig union'
print('ok')
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--api-url", default="http://localhost:8000")
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--bot-image", required=True,
                   help="the bot-runner image you built, and the value of bot_image in strategy.md")
    p.add_argument("--api-image", default=None, help="optional: the API image, checked the same way")
    p.add_argument("--condor-dir", default=".",
                   help="condor checkout, for the routine-discovery check")
    p.add_argument("--python", default=None,
                   help="interpreter with condor's deps (default: <condor-dir>/.venv/bin/python "
                        "if present, else this one). Steps 4-5 import aiohttp and pydantic.")
    a = p.parse_args()

    # Condor runs under its own venv (uv sync). Using the ambient interpreter here just
    # reports ModuleNotFoundError for aiohttp and looks like a broken submission.
    import os
    py = a.python or (
        os.path.join(a.condor_dir, ".venv", "bin", "python")
        if os.path.isfile(os.path.join(a.condor_dir, ".venv", "bin", "python"))
        else sys.executable
    )

    print("\nFunding Builders Cup - submission check")
    print(f"  interpreter for condor checks: {py}\n")

    # 1. the bot image carries a registered executor -------------------------
    print("1. executor in the bot image")
    r = docker_py(a.bot_image, "/opt/conda/envs/hummingbot/bin/python", EXECUTOR_PROBE)
    check(f"{a.bot_image} has perp_xemm_executor registered",
          r.returncode == 0 and "ok" in r.stdout,
          (r.stderr or r.stdout).strip().splitlines()[-1] if r.returncode else "")

    if a.api_image:
        r = docker_py(a.api_image, "/opt/conda/envs/hummingbot-api/bin/python", EXECUTOR_PROBE)
        check(f"{a.api_image} has perp_xemm_executor registered",
              r.returncode == 0 and "ok" in r.stdout,
              (r.stderr or r.stdout).strip().splitlines()[-1] if r.returncode else "")

    # 2. the API can see the controller --------------------------------------
    print("\n2. controller visible to the API")
    try:
        ctrls = api(a.api_url, a.user, a.password, "/controllers/")
        gen = (ctrls or {}).get("generic", []) if isinstance(ctrls, dict) else []
        check("perp_xemm_controller listed", "perp_xemm_controller" in gen,
              "" if "perp_xemm_controller" in gen else f"generic controllers: {gen}")
    except Exception as e:
        check("controller list reachable", False, str(e))

    # 3. the API resolves its schema -> it imported the executor --------------
    print("\n3. controller config validates")
    try:
        tpl = api(a.api_url, a.user, a.password,
                  "/controllers/generic/perp_xemm_controller/config/template")
        need = {"maker_connector", "taker_connector", "min_price_edge_bps", "total_amount"}
        check("config template resolves (proves the API imported the executor)",
              need.issubset(set(tpl or {})),
              "" if need.issubset(set(tpl or {})) else f"missing: {need - set(tpl or {})}")
    except Exception as e:
        check("config template", False, str(e))

    # 4. a real config saves, including the NEGATIVE exit edge ----------------
    name = "fbc_verify_tmp"
    cfg = {
        "controller_name": "perp_xemm_controller", "controller_type": "generic",
        "maker_connector": "hyperliquid_perpetual", "maker_trading_pair": "XMR-USD",
        "taker_connector": "binance_perpetual", "taker_trading_pair": "XMR-USDT",
        "maker_side_str": "BUY", "total_amount": 0.07,
        "min_notional": 11, "max_notional": 40, "pct_impact": 0.5,
        "min_price_edge_bps": -12.5,          # unwinds use a negative floor
        "limit_depth": 0, "limit_tick": 0, "order_refresh_depth": 5,
        "refresh_edge_pct_threshold": 0.8, "refresh_gap_threshold": 2,
        "max_consecutive_hedge_failures": 2, "total_amount_quote": 33,
    }
    try:
        api(a.api_url, a.user, a.password, f"/controllers/configs/{name}", "POST", cfg)
        back = api(a.api_url, a.user, a.password, f"/controllers/configs/{name}")
        ok = str(back.get("min_price_edge_bps")) == "-12.5"
        check("config saves and reads back, negative exit edge preserved", ok,
              "" if ok else f"got min_price_edge_bps={back.get('min_price_edge_bps')}")
        api(a.api_url, a.user, a.password, f"/controllers/configs/{name}", "DELETE")
    except Exception as e:
        check("config save/read", False, str(e))

    # 5. Condor discovers the routines ---------------------------------------
    print("\n4. condor routines")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from routines.base import discover_routines_from_path\n"
        "from pathlib import Path\n"
        "f = discover_routines_from_path("
        "Path(%r)/'agents/funding_builders_cup/routines', 'funding_builders_cup')\n"
        "print(','.join(sorted(f)))\n" % (a.condor_dir, a.condor_dir)
    )
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=120)
        found = set(r.stdout.strip().split(",")) if r.stdout.strip() else set()
        want = {"slot_plan", "basis_scanner", "margin_book", "fill_guard"}
        check("all four routines discovered", want.issubset(found),
              f"found: {sorted(found)}" if not want.issubset(found) else "")
    except Exception as e:
        check("routine discovery", False, str(e))

    # 6. the scanner reaches both venues -------------------------------------
    print("\n5. live market data")
    code = (
        "import sys, asyncio, importlib.util; sys.path.insert(0, %r)\n"
        "s = importlib.util.spec_from_file_location('bs', %r + "
        "'/agents/funding_builders_cup/routines/basis_scanner.py')\n"
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
        "print(asyncio.run(m.run(m.Config(), None)))\n" % (a.condor_dir, a.condor_dir)
    )
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=180)
        ok = "universe" in r.stdout and "bulk calls" in r.stdout
        line = next((l.strip() for l in r.stdout.splitlines() if "universe" in l), "")
        check("basis_scanner reaches Hyperliquid + Binance", ok, line if ok else r.stderr[-200:])
    except Exception as e:
        check("basis_scanner", False, str(e))

    # ------------------------------------------------------------------------
    bad = results.count(False)
    print(f"\n  {results.count(True)} passed, {bad} failed\n")
    if bad:
        print("  A failure at step 1 means the image build did not take.")
        print("  A failure at step 3 means the API image is missing the executor.")
        print("  Nothing was left behind: no orders, no bots, no config.\n")
    return bad


if __name__ == "__main__":
    sys.exit(main())
