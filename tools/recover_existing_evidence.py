"""Run province-specific retained-evidence discovery with no online entrypoint.

Reports contain references/hashes and qualifications, never originals or live
configuration. Optional persistent output is limited to a NEW pmos/var folder.
"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def validate_report(value, path=""):
    forbidden = {"password", "cookie", "cookies", "token", "access_token", "authorization", "secret", "headers",
                 "raw", "raw_body", "request_body", "selectors", "params", "model"}
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in forbidden:
                raise ValueError("non_metadata_field_in_recovery_report")
            validate_report(child, path + "/" + key)
    elif isinstance(value, list):
        for child in value:
            validate_report(child, path)


def output_directory(project, requested):
    """Validate before scanning; a symlink cannot redirect output to old state."""
    project = Path(project).resolve()
    directory = Path(requested).absolute()
    base = project / "var"
    try:
        relative = directory.relative_to(project)
    except ValueError:
        raise ValueError("new_output_directory_under_pmos_var_required") from None
    if (not relative.parts or relative.parts[0] != "var" or ".." in relative.parts
            or any(project.joinpath(*relative.parts[:i]).is_symlink()
                   for i in range(1, len(relative.parts) + 1))
            or directory.exists() or not directory.resolve().is_relative_to(base)):
        raise ValueError("new_output_directory_under_pmos_var_required")
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(description="Only read existing province evidence; no browser/network/worker")
    parser.add_argument("province", choices=["liaoning", "anhui", "shanxi", "all"])
    parser.add_argument("--stage", choices=["retained", "remaining"], default="retained",
                        help="remaining checks the saved unresolved cohort; Liaoning verifies Monday identities first")
    parser.add_argument("--workspace", type=Path, default=PROJECT.parent)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    directory = None
    if args.output_dir:
        directory = output_directory(PROJECT, args.output_dir)
    provinces = ["liaoning", "anhui", "shanxi"] if args.province == "all" else [args.province]
    reports = {}
    for province in provinces:
        if args.stage == "remaining" and province == "liaoning":
            monday = importlib.import_module("pmos.recovery.monday").recover(args.workspace / province)
            validate_report(monday)
            reports["liaoning_monday"] = monday
        module = importlib.import_module("pmos.recovery." + province + ("_remaining" if args.stage == "remaining" else ""))
        result = module.recover(args.workspace / province)
        validate_report(result)
        reports[province] = result
    if directory:
        directory.mkdir(parents=True)
        for province, report in reports.items():
            with (directory / (province + ".json")).open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
    summaries = {province: report.get("summary", report.get("counts", {"report_ready": True}))
                 for province, report in reports.items()}
    print(json.dumps({"mode": "read_only_existing_evidence", "stage": args.stage, "summaries": summaries}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
