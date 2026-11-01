#!/usr/bin/env python3
"""Liaoning's daily (--lane daily, 17), FineReport (--lane engine, 3), weekly (--lane weekly, 2) reports or the disclosure
columns and their files with the member lists (--lane lists, 63 + 3; --only a,b,c for a pilot) join the PMOS handoff.

Offline; sends nothing. plan: what the legacy worker completed of them (read-only).
apply --approval "...": their tombstones and the new collection order, with pmos-liaoning and the legacy
crawler stopped. Each report's new days then go before the node prices' backlog.

    PMOS_DATA=/home/ecs-user/pmos-data python tools/liaoning_daily_lane.py plan --lane engine
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
    plan = commands.add_parser("plan")
    apply = commands.add_parser("apply")
    apply.add_argument("--approval", required=True)
    for command in (plan, apply):
        command.add_argument("--lane", choices=("daily", "engine", "weekly", "lists"), default="daily")
        command.add_argument("--early-empty-consumed", action="store_true",
                             help="the empty answers asked before publication become tombstones too (user's decision); give it to plan and apply alike")
        command.add_argument("--only", help="lists: join just these columns now (comma separated); tombstones cover all")
        command.add_argument("--resend-empty", action="store_true",
                             help="lists: the files the site answered with 0 bytes may be asked again (user's decision); give it to plan and apply")
    args = parser.parse_args()
    if not os.environ.get("PMOS_DATA"):
        raise SystemExit("PMOS_DATA must be set (e.g. /home/ecs-user/pmos-data)")
    province = Province.load("liaoning", RELEASE / "provinces")
    specs = province.datasets()
    if args.lane == "lists":
        lane = liaoning.list_lane(LEGACY, specs, only=args.only.split(",") if args.only else None,
                                  resend_empty=args.resend_empty)
        names = lane["datasets"]
        extra = {key: lane[key] for key in ("columns", "datasets", "first_listing_pages", "files_left_to_download",
                                            "first_listing_pages_all_columns", "files_left_to_download_all_columns", "files_beyond_the_legacy_queue",
                                            "answered_without_a_file", "resend_empty", "withheld_year_conflict")}
    else:
        names = {"daily": liaoning.DAILY_LANE, "engine": liaoning.ENGINE_LANE, "weekly": liaoning.WEEKLY_LANE}[args.lane]
        lane = liaoning.daily_lane(LEGACY, specs, names=names, early_empty_consumed=args.early_empty_consumed)
        extra = {"early_empty_consumed": lane["early_empty_consumed"]}
    weak = [name for name in names if specs[name].status not in ("sampled", "verified", "enabled")]
    if weak:
        raise SystemExit("not verified: " + ",".join(weak))
    summary = {"tombstones": len(lane["rows"]), "will_be_asked": lane["will_be_asked"], **extra,
               "snapshot_sha256": lane["snapshot_sha256"], "per_dataset": lane["per_dataset"]}
    if args.command == "plan":
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    running = [unit for unit in ("pmos-liaoning", "liaoning-crawler") if active(unit) == "active"]
    if running:
        raise SystemExit("stop first: " + ",".join(running))
    ledger = Ledger(province.instance() / "ledger.sqlite3")
    try:
        result = liaoning.apply_daily_lane(ledger, lane, args.approval, source_id=f"liaoning-{args.lane}-lane")
    finally:
        ledger.close()
    print(json.dumps({**summary, "applied": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
