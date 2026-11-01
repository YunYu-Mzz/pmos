"""Liaoning: the legacy production manifest as PMOS specs, proven identity-equal.

Migration is safe only if nothing the legacy collector already consumed can be
sent again. The engine refuses a consumed identity, so every PMOS spec must
compute exactly the identity ``queue_compat`` derives from the legacy record of
the same task. This module

* turns legacy ``report`` sources (pdbln report components; the production
  manifest gives number, route, endpoint, period kind and verified
  selections; ``ReportAdapter`` gives the parameter names) into specs that send
  what the legacy browser code sends: ``queryParam[date_field]`` and
  ``queryParam[selection_field]`` on ``pdblnReport<number>``, then
  ``initData()``;
* checks parity: for every legacy record of those sources, the engine identity
  of the corresponding task equals the legacy identity. A source is eligible
  for cutover only when all its records map and match.

Read-only on the legacy root (declared files, SHA recorded); writes nothing
unless :func:`write_specs` is called with an explicit PMOS directory.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from ..contracts import canonical
from ..engine import task_identity
from ..faults import Fault
from ..queue_audit import _source_rule_refs
from ..queue_compat import QueueContext, decode_liaoning, read_json
from ..spec import DatasetSpec, SpecError, scope_key

MANIFEST = "state/server_verified_expansion_20261023/active_manifest.json"
QUEUES = ("state/server_verified_expansion_20261023/queue.json",
          "state/server_advanced_tasks_20261016.json", "state/server_sitewide_20261016_tasks.json")
APP = "/pxf-dif-settlement-outnetpub"
PERIOD_TOKEN = {"day": "{date}", "month": "{month}"}
PERIOD_START = {"day": "2026-01-01", "month": "2026-01"}


def _declared(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise FileNotFoundError(relative)
    return path


def read_manifest(root: Path) -> tuple[dict, str]:
    manifest, digest = read_json(_declared(root, MANIFEST))
    if not isinstance(manifest.get("sources"), dict):
        raise ValueError("liaoning_manifest_sources_required")
    return manifest, digest


def publish_lag(title: str) -> int:
    """Days a report's day is held back before it is asked for (PMOS saves an empty or partial answer for good).

    As Anhui (2026-11-01) and Liaoning's lesson 22: an actual report is published the next day (非市场机组总出力
    10-29/10-30 were empty the same day, 96 points later). What looks ahead (预测, 日前, a 计划 that is not its
    执行情况) waits 1 day, never the running one; everything else waits 2.
    """
    ahead = "预测" in title or "日前" in title or ("计划" in title and "执行" not in title)
    return 1 if ahead else 2


def report_spec(source: dict, rule: dict, manifest_sha256: str) -> dict:
    """One legacy report source as a PMOS spec (never enabled here)."""
    kind, number, dataset = source.get("period"), source.get("number"), source["dataset"]
    if source.get("kind") != "report" or kind not in PERIOD_TOKEN:
        raise ValueError("report_day_or_month_source_required")
    if not (isinstance(number, str) and number.isdigit() and len(number) == 3):
        raise ValueError("report_number_required")
    endpoint, route = source.get("endpoint"), source.get("route")
    if endpoint != f"/px-dif-settlement-infpubquery/pdblnReportController/pdblnReport{number}" \
            or route != f"{APP}/pdbln/report/PdblnReport{number}":
        raise ValueError("report_endpoint_or_route_unexpected")
    date_field, selection_field = rule["period_field"], rule.get("selection_field")
    set_values = {"tableData": [], f"queryParam.{date_field}": PERIOD_TOKEN[kind]}
    contract = {"request_data_path": ["set"], "date_fields": [f"queryParam.{date_field}"], "dimension_fields": {}}
    scopes = []
    if selection_field:
        selections = source.get("selections")
        if not isinstance(selections, list) or not selections or not all(isinstance(s, str) and s for s in selections):
            raise ValueError("verified_selections_required")
        set_values[f"queryParam.{selection_field}"] = "{scope[" + selection_field + "]}"
        contract["dimension_fields"] = {f"queryParam.{selection_field}": selection_field}
        scopes = [{selection_field: value} for value in selections]
    elif source.get("selections") not in (None, [""], []):
        raise ValueError("selections_without_selection_field")
    proofs = [p["sha256"] for p in source.get("proofs", []) if isinstance(p, dict) and isinstance(p.get("sha256"), str)]
    return {
        "id": dataset, "name": source.get("title") or dataset,
        "status": "verified" if proofs else "candidate",
        "period": {"kind": kind, "start": PERIOD_START[kind], "end": "today",
                   **({"lag_days": publish_lag(source.get("title") or dataset)} if kind == "day" else {})},
        "request": {"transport": "component", "method": source.get("method", "POST"), "path": endpoint,
                    "frame": f"/pdbln/report/PdblnReport{number}", "open_url": f"{APP}/#{route}",
                    **({"site_menu": {"source": source["source"], "route": route}} if source.get("source") else {}),
                    "component": f"pdblnReport{number}", "call": "initData", "set": set_values},
        "response": {"kind": "json"},
        **({"scopes": scopes} if scopes else {}),
        "source": {"legacy": "liaoning production manifest", "manifest": MANIFEST,
                   "manifest_sha256": manifest_sha256, "legacy_kind": "report", "number": number,
                   "legacy_source": source.get("source"), "legacy_scope": source.get("scope"),
                   "proof_sha256": proofs, "identity_contract": contract,
                   "note": "same component call as the legacy advanced/sitewide browser code"},
    }


def build_specs(root: Path) -> dict:
    """Specs for the uniform report sources; every other source with its reason."""
    root = Path(root).resolve()
    manifest, manifest_sha = read_manifest(root)
    rules, code_refs = _source_rule_refs(root)
    specs, deferred = {}, {}
    for name, source in manifest["sources"].items():
        kind = source.get("kind")
        if name in manifest.get("excluded", {}):
            deferred[name] = "excluded_by_legacy_manifest"
        elif kind != "report":
            deferred[name] = f"legacy_kind_{kind}_not_in_first_scope"
        elif name not in rules:
            deferred[name] = "no_legacy_parameter_rule"
        elif source.get("period") not in PERIOD_TOKEN:
            deferred[name] = f"period_{source.get('period')}_not_in_first_scope"
        else:
            try:
                value = report_spec(source, rules[name], manifest_sha)
                spec = DatasetSpec(**value)
                spec.check()
                specs[name] = value
            except (ValueError, KeyError, SpecError) as exc:
                deferred[name] = f"spec_rejected:{exc}"[:160]
    return {"manifest_sha256": manifest_sha, "code_refs": code_refs, "rules": rules,
            "specs": specs, "deferred": deferred}


def legacy_rows(root: Path, account_alias: str, built: dict) -> tuple[list, list]:
    """Decoded legacy records of the declared production queue files (read-only)."""
    root = Path(root).resolve()
    manifest, manifest_sha = read_manifest(root)
    kinds = {name: spec.get("period", "snapshot") for name, spec in manifest["sources"].items()}
    context = QueueContext("liaoning", account_alias)
    rows, refs = [], [{"path": MANIFEST, "sha256": manifest_sha}]
    for relative in QUEUES:
        try:
            value, digest = read_json(_declared(root, relative))
        except FileNotFoundError:
            refs.append({"path": relative, "missing": True})
            continue
        refs.append({"path": relative, "sha256": digest})
        rows += [(relative, row) for row in decode_liaoning(value, context, period_kinds=kinds,
                                                             source_contracts=built["rules"])]
    prior = {"sources": {name: {"tasks": {}, "outcomes": spec.get("prior_outcomes", {})}
                         for name, spec in manifest["sources"].items() if isinstance(spec.get("prior_outcomes"), dict)}}
    rows += [(MANIFEST + "#prior_outcomes", row) for row in decode_liaoning(prior, context, period_kinds=kinds,
                                                                            source_contracts=built["rules"])]
    return rows, refs


OPERATIONS = "state/server_verified_expansion_20261023/operations"


def issued_identities(root: Path, account_alias: str, built: dict) -> tuple[dict, dict]:
    """legacy_key -> identity from the legacy worker's own ``operations/<id>/issued.json`` (read-only).

    The verified-source queue keeps no query for a consumed task, but the
    worker wrote each issued job (key, dataset, params, period, selection,
    page) before sending it. The job decodes exactly like a pending task of
    that queue. A key issued with two different jobs is left unresolved.
    """
    root = Path(root).resolve()
    directory = root / OPERATIONS
    manifest, _ = read_manifest(root)
    kinds = {name: spec.get("period", "snapshot") for name, spec in manifest["sources"].items()}
    context = QueueContext("liaoning", account_alias)
    found, conflicting, files = {}, set(), []
    for path in sorted(directory.glob("*/issued.json")) if directory.is_dir() else []:
        raw = path.read_bytes()
        files.append(hashlib.sha256(raw).hexdigest())
        job = json.loads(raw).get("job")
        if not isinstance(job, dict) or not isinstance(job.get("key"), str) or not isinstance(job.get("dataset"), str):
            continue
        rows = decode_liaoning({"sources": {job["dataset"]: {"pending": [job], "outcomes": {}}}}, context,
                               period_kinds=kinds, source_contracts=built["rules"])
        identity = rows[0].identity if len(rows) == 1 and rows[0].legacy_key == job["key"] else None
        if identity is None:
            continue
        if job["key"] in found and found[job["key"]].key != identity.key:
            conflicting.add(job["key"])
        found.setdefault(job["key"], identity)
    for key in conflicting:
        del found[key]
    ref = {"path": OPERATIONS + "/*/issued.json", "files": len(files),
           "sha256": hashlib.sha256("".join(files).encode()).hexdigest(), "conflicting_keys": len(conflicting)}
    return found, ref


def bridged_identities(root: Path, account_alias: str) -> dict:
    """legacy_key -> identity recovered by the phase 5 exact identity bridges.

    The bridges rebuild the identity commitment of consumed legacy records
    whose queue entry no longer carries its query (5 empty, 6 saved). They are
    re-keyed to the cutover account alias; nothing else changes.
    """
    from dataclasses import replace
    from ..recovery.liaoning_bridges import resolve_queue_records
    return {task.legacy_key: replace(task.identity, account_alias=account_alias)
            for task in resolve_queue_records(root) if task.identity is not None}


def parity(root: Path, *, account_alias: str = "liaoning-production", bridges: bool = True) -> dict:
    """Engine identity == legacy identity for every legacy record of a generated spec."""
    built = build_specs(root)
    specs = {name: DatasetSpec(**value) for name, value in built["specs"].items()}
    rows, refs = legacy_rows(root, account_alias, built)
    issued, issued_ref = issued_identities(root, account_alias, built)
    refs = refs + [issued_ref]
    bridged, bridge_status = {}, "not_requested"
    if bridges:
        try:
            bridged, bridge_status = bridged_identities(root, account_alias), "applied"
        except ValueError as exc:
            # The bridges pin the legacy writer code and inputs; drift makes
            # them refuse, and those records then stay unresolved here too.
            bridge_status = f"refused:{exc}"
    # The worker's own issued jobs first; a phase 5 bridge only where none was written.
    bridged = {**bridged, **issued}
    per = {name: Counter() for name in specs}
    out_of_scope = Counter()
    examples = {}
    for source_ref, row in rows:
        spec = specs.get(row.dataset)
        if spec is None:
            out_of_scope["consumed" if row.consumed else "unconsumed"] += 1
            continue
        tally = per[row.dataset]
        tally["records"] += 1
        tally["consumed"] += int(row.consumed)
        if row.identity is None and row.legacy_key in bridged:
            from dataclasses import replace
            row = replace(row, identity=bridged[row.legacy_key])
            tally["bridged"] += 1
        if row.identity is None:
            tally["unmapped_consumed" if row.consumed else "unmapped"] += 1
            examples.setdefault(row.dataset, ("unmapped", source_ref, list(row.blockers)))
            continue
        legacy = row.identity
        scope = scope_key(legacy.dimensions) if spec.scopes else ("" if not legacy.dimensions else None)
        if scope is None or (spec.scopes and scope not in {key for key, _ in spec.scope_items()}):
            tally["scope_not_in_spec_consumed" if row.consumed else "scope_not_in_spec"] += 1
            examples.setdefault(row.dataset, ("scope_not_in_spec", source_ref, sorted(legacy.dimensions)))
            continue
        try:
            mine = task_identity("liaoning", account_alias, spec, legacy.period["label"], scope, page=legacy.page)
        except Fault as fault:
            tally["engine_identity_error"] += 1
            examples.setdefault(row.dataset, ("engine_identity_error", source_ref, fault.code))
            continue
        if mine.key == legacy.key:
            tally["matched"] += 1
        else:
            tally["mismatched_consumed" if row.consumed else "mismatched"] += 1
            examples.setdefault(row.dataset, ("mismatched", source_ref, _difference(mine.document(), legacy.document())))
    eligible = sorted(name for name, tally in per.items()
                      if tally["records"] and tally["matched"] == tally["records"])
    report = {
        "province": "liaoning", "account_alias": account_alias, "dispatch_allowed": False,
        "identity_bridges": bridge_status,
        "inputs": refs, "manifest_sha256": built["manifest_sha256"], "code_refs": built["code_refs"],
        "generated_specs": len(specs), "deferred_sources": len(built["deferred"]),
        "deferred_reasons": dict(Counter(reason.split(":")[0] for reason in built["deferred"].values())),
        "legacy_records_out_of_scope": dict(out_of_scope),
        "eligible": eligible,
        "not_eligible": {name: {k: v for k, v in per[name].items()} for name in sorted(per) if name not in eligible},
        "per_source": {name: dict(tally) for name, tally in sorted(per.items())},
        "first_problem": {name: list(example) for name, example in sorted(examples.items())},
    }
    report["summary"] = {
        "generated_specs": len(specs), "eligible_sources": len(eligible),
        "records": sum(t["records"] for t in per.values()), "matched": sum(t["matched"] for t in per.values()),
        "bridged": sum(t["bridged"] for t in per.values()),
        "consumed": sum(t["consumed"] for t in per.values()),
        "unresolved_consumed": sum(t[k] for t in per.values() for k in
                                   ("unmapped_consumed", "mismatched_consumed", "scope_not_in_spec_consumed")),
    }
    return report


def _difference(mine: dict, legacy: dict) -> list[str]:
    return sorted(key for key in set(mine) | set(legacy) if mine.get(key) != legacy.get(key))


def write_specs(built: dict, directory: Path, names: list[str] | None = None) -> list[str]:
    """Write generated specs into a PMOS datasets directory (never a legacy directory)."""
    directory = Path(directory).resolve()
    if directory.name != "datasets" or directory.parent.name != "liaoning" or directory.parent.parent.name != "provinces":
        raise ValueError("pmos_liaoning_datasets_directory_required")
    written = []
    for name in names or sorted(built["specs"]):
        value = built["specs"][name]
        spec = DatasetSpec(**value)
        spec.note("generated_from_legacy_manifest", manifest_sha256=built["manifest_sha256"])
        spec.save(directory / f"{name}.json")
        written.append(name)
    return written


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


# -- cutover --------------------------------------------------------------
GLOBAL_BLOCKERS = {"legacy_active_owner_unsettled", "orphan_active_preserved", "legacy_stage_requires_settlement"}
SOURCE_BLOCKERS = {"legacy_source_hold", "conflicting_legacy_outcomes"}


def plan_cutover(root: Path, *, account_alias: str = "liaoning-production", requested: list[str] | None = None,
                 bridges: bool = True) -> dict:
    """Which sources may move to PMOS from this (frozen) legacy root, and why not the others.

    Eligible: every legacy record maps to an engine-identical identity and the
    legacy owner holds nothing about the source. A legacy task still in
    flight, or a global legacy hold, blocks the whole cutover: freeze at a
    settled boundary first. Nothing is written.
    """
    report = parity(root, account_alias=account_alias, bridges=bridges)
    built = build_specs(root)
    rows, _ = legacy_rows(root, account_alias, built)
    blockers, source_blocked = Counter(), {}
    for _, row in rows:
        for blocker in row.blockers:
            if blocker in GLOBAL_BLOCKERS or blocker == "legacy_global_hold":
                blockers[blocker] += 1
            if blocker in SOURCE_BLOCKERS and row.dataset in report["per_source"]:
                source_blocked.setdefault(row.dataset, set()).add(blocker)
    eligible = [name for name in report["eligible"] if name not in source_blocked]
    if requested is not None:
        unknown = sorted(set(requested) - set(eligible))
        if unknown:
            raise ValueError("requested_sources_not_eligible:" + ",".join(unknown))
        eligible = [name for name in eligible if name in requested]
    plan = {
        "province": "liaoning", "account_alias": account_alias, "inputs": report["inputs"],
        "manifest_sha256": report["manifest_sha256"], "identity_bridges": report["identity_bridges"],
        "global_blockers": dict(blockers), "scope": eligible,
        "excluded": {**{name: "identity_parity_incomplete" for name in report["not_eligible"]},
                     **{name: sorted(reasons) for name, reasons in source_blocked.items()}},
        "deferred_sources": report["deferred_sources"], "deferred_reasons": report["deferred_reasons"],
        "per_source": {name: report["per_source"][name] for name in eligible},
        "ready": bool(eligible) and not blockers,
    }
    plan["plan_sha256"] = digest({k: v for k, v in plan.items()})
    return plan


def apply_cutover(root: Path, instance: Path, plan: dict, *, legacy_owner: dict, code_sha: str, approval: str,
                  request_budget: int, keepalive_dataset: str | None = None) -> dict:
    """Build a NEW PMOS ledger from the frozen legacy queue and grant the handoff.

    Every decoded legacy record of every declared queue file is imported as a
    non-dispatchable fact (all datasets, so later scope extensions stay
    protected); the handoff receipt then lets the engine send only the planned
    scope, at most ``request_budget`` site requests. Sends nothing.
    """
    from ..ledger import Ledger
    from ..queue_compat import import_shadow
    if not plan.get("ready"):
        raise ValueError("cutover_plan_not_ready")
    fresh = plan_cutover(root, account_alias=plan["account_alias"], requested=plan["scope"],
                         bridges=plan["identity_bridges"] == "applied")
    if fresh["plan_sha256"] != plan["plan_sha256"]:
        raise ValueError("legacy_root_changed_since_plan")
    instance = Path(instance)
    if instance.exists():
        raise ValueError("new_instance_directory_required")
    built = build_specs(root)
    rows, refs = legacy_rows(root, plan["account_alias"], built)
    bridged = bridged_identities(root, plan["account_alias"]) if plan["identity_bridges"] == "applied" else {}
    bridged = {**bridged, **issued_identities(root, plan["account_alias"], built)[0]}
    from dataclasses import replace
    by_source: dict[str, list] = {}
    for source_ref, row in rows:
        if row.identity is None and row.legacy_key in bridged:
            row = replace(row, identity=bridged[row.legacy_key])
        by_source.setdefault(source_ref, []).append(row)
    digests = {ref["path"]: ref["sha256"] for ref in refs if "sha256" in ref}
    instance.mkdir(parents=True, mode=0o700)
    ledger = Ledger(instance / "ledger.sqlite3")
    snapshots = []
    try:
        context = QueueContext("liaoning", plan["account_alias"])
        for source_ref, source_rows in sorted(by_source.items()):
            snapshot = digests.get(source_ref) or digest([row.summary() for row in source_rows])
            import_shadow(ledger, source_rows, context, source_id=source_ref, snapshot_sha256=snapshot)
            snapshots.append({"source_id": source_ref, "snapshot_sha256": snapshot})
        receipt = ledger.grant_handoff({
            "version": 1, "province": "liaoning", "account_alias": plan["account_alias"],
            "legacy_owner": legacy_owner, "snapshots": snapshots, "scope": plan["scope"],
            "request_budget": request_budget, "code_sha": code_sha, "seeding": "spec_periods",
            # Liaoning production renews with the mounted 022 report after 300 s idle (system_load).
            "session": {"keepalive_dataset": keepalive_dataset} if keepalive_dataset else {},
            "approval": approval + f" (plan {plan['plan_sha256']})"})
        summary = ledger.legacy_summary()
    finally:
        ledger.close()
    return {"receipt": receipt, "ledger": summary}
