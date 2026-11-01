"""Anhui one-time cutover, offline (no browser, no site request). User 2026-11-01: 现在就切吧.

Run from a release directory, after the Anhui session froze the legacy worker
(rollout control enabled=false, worker/keeper/page-recovery booted out):

  PMOS_DATA=~/pmos-anhui/data python tools/anhui_cutover.py plan
  PMOS_DATA=~/pmos-anhui/data python tools/anhui_cutover.py apply --approval "<用户原话>" --budget 5

``plan`` writes nothing. ``apply`` regenerates the option and whole-day specs
from the frozen candidates (the legacy found new options and 6d80 units up to
the freeze), recomputes the plan, builds the NEW instance ledger (every legacy
send a tombstone, the receipt, the open candidates as tasks), writes the
instance's browser.json and the release's SHA256SUMS.
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

from pmos.migration import anhui_multidim as multidim  # noqa: E402
from pmos.province import Province  # noqa: E402

LEGACY = Path("/Users/yunyu/Desktop/sd_crawler/anhui")
PROFILE = LEGACY / "state" / "server_browser_profile"
LEGACY_UNITS = ("com.yunyu.anhui.worker.", "com.yunyu.anhui.keeper.", "com.yunyu.anhui.page-recovery.")
CONTROL = LEGACY / "state" / "server_native_rollout_control.json"
KEEPALIVE = "queryRqxtfhyc"


def frozen() -> dict:
    """The legacy owner's stop evidence, or the reasons it is not stopped."""
    listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=True).stdout
    loaded = sorted({line.split()[-1] for line in listed.splitlines()
                     if line.split() and line.split()[-1].startswith(LEGACY_UNITS)})
    control = json.loads(CONTROL.read_text(encoding="utf-8"))
    problems = ([f"legacy_unit_loaded:{name}" for name in loaded]
                + ([] if control.get("enabled") is False else ["legacy_rollout_control_enabled"]))
    return {"problems": problems,
            "evidence": {"stopped_evidence": "launchctl list: no com.yunyu.anhui worker/keeper/page-recovery unit "
                                             "loaded; server_native_rollout_control enabled=false",
                         "launchctl_legacy_units_loaded": loaded, "rollout_control_enabled": control.get("enabled"),
                         "rollout_control_sha256": hashlib.sha256(CONTROL.read_bytes()).hexdigest(),
                         "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}}


def regenerate() -> list[str]:
    directory = RELEASE / "provinces" / "anhui" / "datasets"
    specs = Province.load("anhui", RELEASE / "provinces").datasets()
    options = multidim.build_specs(LEGACY, specs)
    written = multidim.write_specs(options, directory, sorted(options["specs"]))
    whole = multidim.build_whole_day_specs(LEGACY, options["specs"])
    return written + multidim.write_specs(whole, directory, sorted(whole["specs"]))


def checksums() -> str:
    """SHA256SUMS of the code and province config this cutover runs; its own SHA is the receipt's code_sha."""
    files = sorted(p for p in [*(RELEASE / "pmos").rglob("*"), *(RELEASE / "provinces").rglob("*"),
                               *(RELEASE / "tools").glob("*.py")]
                   if p.is_file() and "__pycache__" not in p.parts)
    text = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(RELEASE)}\n" for p in files)
    (RELEASE / "SHA256SUMS").write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def summary(plan: dict) -> dict:
    return {"ready": plan["ready"], "global_blockers": plan["global_blockers"], "scope": len(plan["scope"]),
            "first_batch": len(plan["first_batch"]), "multidimensional": len(plan["multidimensional"]),
            "whole_day": len(plan["whole_day"]), "plan_sha256": plan["plan_sha256"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    apply = commands.add_parser("apply")
    apply.add_argument("--approval", required=True)
    apply.add_argument("--budget", required=True, type=int)
    args = parser.parse_args()
    data = os.environ.get("PMOS_DATA", "")
    if not data or Path(data).expanduser().resolve().is_relative_to(Path.home() / "Desktop"):
        raise SystemExit("PMOS_DATA must be set, outside ~/Desktop (launchd EPERM there, Anhui 11-01)")
    state = frozen()
    if args.command == "plan":
        plan = multidim.plan_cutover(LEGACY, Province.load("anhui", RELEASE / "provinces").datasets())
        print(json.dumps({**summary(plan), "legacy_frozen": not state["problems"], "problems": state["problems"]},
                         ensure_ascii=False, indent=2))
        return
    if state["problems"]:
        raise SystemExit("legacy not frozen: " + ", ".join(state["problems"]))
    written = regenerate()
    province = Province.load("anhui", RELEASE / "provinces")
    specs = province.datasets()
    plan = multidim.plan_cutover(LEGACY, specs)
    if not plan["ready"]:
        raise SystemExit("plan not ready: " + json.dumps(plan["global_blockers"], ensure_ascii=False))
    code_sha = checksums()
    instance = province.instance()
    result = multidim.apply_cutover(LEGACY, instance, plan, specs, legacy_owner=state["evidence"], code_sha=code_sha,
                                    approval=args.approval, request_budget=args.budget, keepalive_dataset=KEEPALIVE)
    (instance / "browser.json").write_text(json.dumps({"profile_dir": str(PROFILE)}) + "\n", encoding="utf-8")
    print(json.dumps({**summary(plan), "instance": str(instance), "regenerated_specs": len(written),
                      "code_sha": code_sha, "budget": result["receipt"]["request_budget"],
                      "seeded": sum(n for k, n in result["seeded"].items() if not k.endswith(":option_unresolved")),
                      "seeded_by_dataset": result["seeded"],
                      "ledger": result["ledger"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
