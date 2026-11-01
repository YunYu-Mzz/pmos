"""Liaoning cutover, offline (no browser, no site request). Run on the Liaoning server from a release directory.

After the Liaoning session stopped liaoning-crawler.service (liaoning-browser-owner.service keeps the
browser and the login):

  PMOS_DATA=/home/ecs-user/pmos-data python tools/liaoning_cutover.py plan
  PMOS_DATA=/home/ecs-user/pmos-data python tools/liaoning_cutover.py apply --approval "<用户原话>" --budget N

``plan`` writes nothing. ``apply`` recomputes the plan from the frozen legacy root, builds the NEW instance
ledger (every legacy record a tombstone, early empties unconsumed, the receipt with keepalive system_load),
writes the instance's browser.json and the release's SHA256SUMS (its sha256 is the receipt's code_sha).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RELEASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RELEASE))

from pmos.migration import liaoning  # noqa: E402
from pmos.province import Province  # noqa: E402

LEGACY = Path("/home/ecs-user/sd_crawler/liaoning")
PROFILE = LEGACY / "state" / "server_browser_profile"
CRAWLER, OWNER = "liaoning-crawler.service", "liaoning-browser-owner.service"
KEEPALIVE = "system_load"


def unit_state(unit: str) -> str:
    return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()


def frozen() -> dict:
    crawler, owner = unit_state(CRAWLER), unit_state(OWNER)
    problems = ([] if crawler in ("inactive", "failed") else [f"legacy_crawler_{crawler or 'unknown'}"]) + \
               ([] if owner == "active" else [f"browser_owner_{owner or 'unknown'}"])
    return {"problems": problems,
            "evidence": {"stopped_evidence": f"systemctl is-active {CRAWLER}: {crawler}; {OWNER}: {owner}",
                         "host": os.uname().nodename, "service": CRAWLER, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}}


def checksums() -> str:
    files = sorted(p for p in [*(RELEASE / "pmos").rglob("*"), *(RELEASE / "provinces").rglob("*"),
                               *(RELEASE / "tools").glob("*.py")]
                   if p.is_file() and "__pycache__" not in p.parts)
    text = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(RELEASE)}\n" for p in files)
    (RELEASE / "SHA256SUMS").write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def summary(plan: dict) -> dict:
    return {"ready": plan["ready"], "global_blockers": plan["global_blockers"], "scope": len(plan["scope"]),
            "last_in_order": plan["scope"][-3:], "excluded": plan["excluded"], "plan_sha256": plan["plan_sha256"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    apply = commands.add_parser("apply")
    apply.add_argument("--approval", required=True)
    apply.add_argument("--budget", required=True, type=int)
    args = parser.parse_args()
    if not os.environ.get("PMOS_DATA"):
        raise SystemExit("PMOS_DATA must be set (e.g. /home/ecs-user/pmos-data)")
    state = frozen()
    plan = liaoning.plan_cutover(LEGACY)
    if args.command == "plan":
        print(json.dumps({**summary(plan), "legacy_frozen": not state["problems"], "problems": state["problems"]},
                         ensure_ascii=False, indent=2))
        return
    if state["problems"]:
        raise SystemExit("legacy not frozen: " + ", ".join(state["problems"]))
    if not plan["ready"]:
        raise SystemExit("plan not ready: " + json.dumps(plan["global_blockers"], ensure_ascii=False))
    code_sha = checksums()
    instance = Province.load("liaoning", RELEASE / "provinces").instance()
    result = liaoning.apply_cutover(LEGACY, instance, plan, legacy_owner=state["evidence"], code_sha=code_sha,
                                    approval=args.approval, request_budget=args.budget, keepalive_dataset=KEEPALIVE)
    (instance / "browser.json").write_text(json.dumps({"profile_dir": str(PROFILE), "profile_process_check": True}) + "\n",
                                           encoding="utf-8")
    print(json.dumps({**summary(plan), "instance": str(instance), "code_sha": code_sha,
                      "budget": result["receipt"]["request_budget"], "ledger": result["ledger"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
