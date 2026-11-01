#!/usr/bin/env python3
"""The 18 daily Liaoning reports join the PMOS handoff (offline; sends nothing).

plan: what the legacy worker completed of them (read-only).
apply --approval "...": their tombstones and the new collection order, with pmos-liaoning and the legacy
crawler stopped. Each report's new days then go before the node prices' backlog.

    PMOS_DATA=/home/ecs-user/pmos-data python tools/liaoning_daily_lane.py plan
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

RELEASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RELEASE))

from pmos.ledger import Ledger  # noqa: E402
from pmos.migration import liaoning  # noqa: E402
from pmos.province import Province  # noqa: E402

LEGACY = Path("/home/ecs-user/sd_crawler/liaoning")


def active(unit: str) -> str:
    return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    apply = commands.add_parser("apply")
    apply.add_argument("--approval", required=True)
    args = parser.parse_args()
    if not os.environ.get("PMOS_DATA"):
        raise SystemExit("PMOS_DATA must be set (e.g. /home/ecs-user/pmos-data)")
    province = Province.load("liaoning", RELEASE / "provinces")
    specs = province.datasets()
    weak = [name for name in liaoning.DAILY_LANE if specs[name].status not in ("sampled", "verified", "enabled")]
    if weak:
        raise SystemExit("not verified: " + ",".join(weak))
    lane = liaoning.daily_lane(LEGACY, specs)
    summary = {"tombstones": len(lane["rows"]), "snapshot_sha256": lane["snapshot_sha256"], "per_dataset": lane["per_dataset"],
               "will_be_asked": sum(len(v["open"]) + len(v["early_empty"]) for v in lane["per_dataset"].values())}
    if args.command == "plan":
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    running = [unit for unit in ("pmos-liaoning", "liaoning-crawler") if active(unit) == "active"]
    if running:
        raise SystemExit("stop first: " + ",".join(running))
    ledger = Ledger(province.instance() / "ledger.sqlite3")
    try:
        result = liaoning.apply_daily_lane(ledger, lane, args.approval)
    finally:
        ledger.close()
    print(json.dumps({**summary, "applied": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
