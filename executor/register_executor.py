#!/usr/bin/env python3
"""Register perp_xemm_executor inside an installed hummingbot package.

Runs INSIDE the image at build time. Idempotent: re-running is a no-op, so a
rebuilt layer or a re-pulled base image cannot double-apply it.

Upstream ships the executor registry split across three files, and all three
must agree or the failure is silent-until-deploy:

  1. executors/data_types.py        - the `type` Literal that validates a config
  2. executors/executor_orchestrator.py - the name -> class dispatch table
  3. models/executors_info.py       - the AnyExecutorConfig union

Miss #2 or #3 and the image builds, the API starts, and the deploy fails.
"""
import re
import sys
from pathlib import Path

NAME = "perp_xemm_executor"
CLS = "PerpXEMMExecutor"
CFG = "PerpXEMMExecutorConfig"


def patch(path: Path, checks) -> bool:
    src = original = path.read_text()
    for probe, pattern, repl in checks:
        if probe in src:
            continue                      # already registered
        src, n = re.subn(pattern, repl, src, count=1)
        if n == 0:
            sys.exit(f"FAILED: no match for {pattern!r} in {path}\n"
                     f"Upstream layout changed - re-derive the patch before shipping.")
    if src != original:
        path.write_text(src)
        return True
    return False


def main(pkg_root: str) -> None:
    pkg = Path(pkg_root)
    ex = pkg / "strategy_v2" / "executors"
    if not (ex / NAME / "perp_xemm_executor.py").is_file():
        sys.exit(f"FAILED: {ex / NAME} not populated - COPY the executor before registering.")

    changed = []

    # 1/3 - the type Literal
    if patch(ex / "data_types.py", [(
        f'"{NAME}"',
        r'("lp_executor")(\])',
        r'\1, "' + NAME + r'"\2',
    )]):
        changed.append("data_types.py")

    # 2/3 - import + dispatch table
    if patch(ex / "executor_orchestrator.py", [
        (f"import {CLS}",
         r'(from hummingbot\.strategy_v2\.executors\.position_executor\.position_executor import PositionExecutor)',
         f'from hummingbot.strategy_v2.executors.{NAME}.{NAME} import {CLS}\n' + r'\1'),
        (f'"{NAME}": {CLS}',
         r'("lp_executor":\s*LPExecutor,)',
         r'\1\n        "' + NAME + f'": {CLS},'),
    ]):
        changed.append("executor_orchestrator.py")

    # 3/3 - import + AnyExecutorConfig union
    if patch(pkg / "strategy_v2" / "models" / "executors_info.py", [
        (f"import {CFG}",
         r'(from hummingbot\.strategy_v2\.executors\.position_executor\.data_types import PositionExecutorConfig)',
         f'from hummingbot.strategy_v2.executors.{NAME}.data_types import {CFG}\n' + r'\1'),
        (f"{CFG}]",
         r'(AnyExecutorConfig = Union\[)(.*?)(\])',
         r'\1\2, ' + CFG + r'\3'),
    ]):
        changed.append("executors_info.py")

    print(f"register_executor: {'patched ' + ', '.join(changed) if changed else 'already registered (no-op)'}")


if __name__ == "__main__":
    main(sys.argv[1])
