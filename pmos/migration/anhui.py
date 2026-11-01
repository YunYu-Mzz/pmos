"""Anhui cutover: the legacy native lane as PMOS fetch specs, identity parity, plan, handoff.

The Anhui legacy worker replays each report's own page request (the
``_bindings`` proof: endpoint, method, body shape, the date's parameter path)
with only the date changed, and writes every send to
``state/server_native_attempts_<dataset>.json`` before it goes out (keyed by
period, with the UI selectors). A first batch takes the reports whose page
request varies only by the date: one endpoint, one body shape, selectors
exactly ``{"日期": period}``. PMOS replays the page's body the same way
(``template_body_set``) and opens the report through its UI spec (menu, date,
查询) when no page request has been observed yet.

Read-only on the legacy root; sends nothing.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from ..contracts import canonical
from ..engine import task_identity
from ..faults import Fault
from ..queue_compat import QueueContext, decode_jobs, read_json
from ..spec import DatasetSpec, SpecError

REGISTRY = "state/server_native_rollout_registry.json"
BINDINGS = "data/2026/native_tables/_bindings"
DATE_SELECTOR = "日期"
# A legacy send that has not settled: freeze at a settled boundary first.
IN_FLIGHT = {"attempting", "requesting"}
# Days a report's day is held back before it is asked for (PMOS saves an empty
# or partial answer for good). What happened during a day (actual, real-time,
# execution, operating-day reports) waits 2 days: the legacy worker's 27
# empties of 省间联络线输电情况 all came at 1 day and were there at 3
# (2026-11-01). Everything else 1 day: the legacy worker never asked for the
# running day (its first batch: at least 1 day, from its 10-23/24 backfill).
PUBLISH_LAG_DAYS = {
    "actual_load": 2, "generation_output_actual": 2, "hydro_storage_output_actual": 2,
    "non_market_output_actual": 2, "system_reserve": 2,
    "publication_1ade3e9512c04404": 2,   # 日前发电设备检修计划执行情况
    "publication_54367a8737ed44c9": 2,   # 开停机不满最小约束时间机组名单
    "publication_77a790459318559b": 2,   # 实际运行输电断面约束情况
    "publication_d25c63c96933e7cf": 2,   # 省内关键输电断面可用容量日
    "publication_f4a61b9f9aa8886c": 2,   # 日前输变电设备检修计划执行情况
    "important_channel_transmission_actual": 2, "intertie_actual_huan_i": 2, "renewables_output_actual": 2,
    "real_time_transmission_constraints": 2, "real_time_node_marginal_price": 2, "generation_types_energy_count": 2,
    "publication_1404bce682303715": 2,   # 每个交易时段实际运行输电断面约束
    "publication_22c779b5bdc2edc8": 2,   # 辅助服务市场申报出清信息
    "publication_6d80c3d4514ccfc2": 2,   # 运行日各时段临时调用机组情况
    "publication_9d35ad5e2d74c67c": 2,   # 重要线路与变压器平均潮流
    "publication_9dda25255779e435": 2,   # 运行日各时段分类电源装机
}


def publish_lag(name: str) -> int:
    return PUBLISH_LAG_DAYS.get(name, 1)


def _declared(root: Path, relative: str) -> Path:
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
        raise FileNotFoundError(relative)
    return path


def attempts_path(dataset: str) -> str:
    return f"state/server_native_attempts_{dataset.lower()}.json"


def _bindings(root: Path) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for path in sorted((Path(root) / BINDINGS).glob("*.json")):
        value = json.loads(path.read_bytes())
        if isinstance(value.get("dataset"), str):
            found.setdefault(value["dataset"], []).append({**value, "_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return found


def first_batch(root: Path) -> tuple[dict, dict]:
    """name -> (registry entry, binding) of the date-only native reports; name -> reason for the others."""
    registry, _ = read_json(_declared(root, REGISTRY))
    bindings = _bindings(root)
    chosen, deferred = {}, {}
    for name, entry in sorted(registry.items()):
        if entry.get("phase") != "native_verified":
            deferred[name] = f"legacy_phase_{entry.get('phase')}"
            continue
        proofs = bindings.get(name, [])
        shapes = {canonical(p.get("body_shape")) for p in proofs}
        routes = {(p.get("method"), p.get("endpoint_path"), tuple(p.get("parameter_path") or ())) for p in proofs}
        if not proofs or len(shapes) != 1 or len(routes) != 1:
            deferred[name] = "binding_not_single_route" if proofs else "binding_missing"
            continue
        method, endpoint, parameter = next(iter(routes))
        shape = proofs[0].get("body_shape")
        if (method != "POST" or not isinstance(endpoint, str) or not endpoint.startswith("/px-") or len(parameter) != 1
                or not isinstance(shape, dict) or parameter[0] not in shape):
            deferred[name] = "binding_route_unsupported"
            continue
        if any(set(p.get("selectors") or {}) != {DATE_SELECTOR} for p in proofs):
            deferred[name] = "selectors_beyond_the_date"
            continue
        chosen[name] = (entry, proofs[-1])
    return chosen, deferred


def build_specs(root: Path, ui_specs: dict[str, DatasetSpec]) -> dict:
    """PMOS fetch specs for the first batch (never enabled here); the UI spec opens the report once."""
    chosen, deferred = first_batch(root)
    specs = {}
    for name, (entry, proof) in chosen.items():
        ui = ui_specs.get(name)
        # The UI spec itself, or (once generated) the fetch spec's own UI bootstrap.
        page = None if ui is None else (ui.request if ui.request.get("transport") == "ui" else ui.request.get("bootstrap"))
        if not isinstance(page, dict) or page.get("transport") != "ui" or ui.period.get("kind") != "day":
            deferred[name] = "ui_bootstrap_spec_missing"
            continue
        field = proof["parameter_path"][0]
        bootstrap = {**{k: v for k, v in page.items() if k not in ("action", "path", "method")},
                     "action": "query", "path": proof["endpoint_path"], "method": "POST"}
        value = {
            "id": name, "name": entry.get("title") or ui.name, "status": "verified",
            "period": {"kind": "day", "start": ui.period.get("start", "2026-01-01"), "end": "today",
                       "lag_days": publish_lag(name)},
            "request": {"transport": "fetch", "method": "POST", "path": proof["endpoint_path"],
                        "template_body_set": {field: "{date}"}, "bootstrap": bootstrap},
            "response": {"kind": "json"},
            "source": {"legacy": "anhui native_rollout_registry", "binding_sha256": proof["_sha256"],
                       "success_code": proof.get("success_code"),
                       "identity_contract": {"request_data_path": ["template_body_set"], "date_fields": [field],
                                             "dimension_fields": {field: DATE_SELECTOR}}},
        }
        try:
            DatasetSpec(**value).check()
        except (SpecError, ValueError) as exc:
            deferred[name] = f"spec_rejected:{exc}"[:160]
            continue
        specs[name] = value
    return {"specs": specs, "deferred": deferred}


def legacy_rows(root: Path, account_alias: str, names) -> tuple[list, list]:
    """Decoded legacy native attempts of these datasets (every send, any phase), read-only."""
    rows, refs = [], []
    for name in sorted(names):
        relative = attempts_path(name)
        try:
            attempts, digest = read_json(_declared(root, relative))
        except FileNotFoundError:
            refs.append({"path": relative, "missing": True})
            continue
        refs.append({"path": relative, "sha256": digest})
        context = QueueContext("anhui", account_alias, name, "day")
        rows += [(relative, row) for row in decode_jobs({"jobs": {}}, context, attempts=_with_receipt_query(name, attempts))]
    return rows, refs


def _with_receipt_query(dataset: str, attempts: dict) -> dict:
    """A captured attempt that kept only its receipt: the worker's own receipt names its period and selectors.

    Used only when the receipt is of this dataset and its period is the attempt's key.
    """
    out = {}
    for key, attempt in attempts.items():
        receipt = attempt.get("receipt") if isinstance(attempt, dict) else None
        if (isinstance(receipt, dict) and "period" not in attempt and "selectors" not in attempt
                and receipt.get("dataset") == dataset and receipt.get("period") == key
                and isinstance(receipt.get("selectors"), dict)):
            attempt = {**attempt, "period": receipt["period"], "selectors": receipt["selectors"]}
        out[key] = attempt
    return out


def in_flight(root: Path) -> list[str]:
    """Every legacy native send still open, in any dataset (not only the batch)."""
    found = []
    for path in sorted((Path(root) / "state").glob("server_native_attempts_*.json")):
        for key, attempt in json.loads(path.read_bytes()).items():
            if isinstance(attempt, dict) and attempt.get("phase") in IN_FLIGHT:
                found.append(f"{path.name}#{key}")
    return found


def parity(root: Path, ui_specs: dict[str, DatasetSpec], *, account_alias: str = "anhui-production") -> dict:
    """Engine identity == legacy identity for every legacy attempt of a generated spec."""
    built = build_specs(root, ui_specs)
    specs = {name: DatasetSpec(**value) for name, value in built["specs"].items()}
    rows, refs = legacy_rows(root, account_alias, specs)
    per = {name: Counter() for name in specs}
    examples = {}
    for source_ref, row in rows:
        tally = per[row.dataset]
        tally["records"] += 1
        tally["consumed"] += int(row.consumed)
        if row.identity is None:
            tally["unmapped"] += 1
            examples.setdefault(row.dataset, ("unmapped", source_ref, list(row.blockers)))
            continue
        try:
            mine = task_identity("anhui", account_alias, specs[row.dataset], row.identity.period["label"], "")
        except Fault as fault:
            tally["engine_identity_error"] += 1
            examples.setdefault(row.dataset, ("engine_identity_error", source_ref, fault.code))
            continue
        if mine.key == row.identity.key:
            tally["matched"] += 1
        else:
            tally["mismatched"] += 1
            examples.setdefault(row.dataset, ("mismatched", source_ref, sorted(
                k for k in set(mine.document()) | set(row.identity.document())
                if mine.document().get(k) != row.identity.document().get(k))))
    eligible = sorted(name for name, tally in per.items() if tally["records"] and tally["matched"] == tally["records"])
    return {"province": "anhui", "account_alias": account_alias, "inputs": refs, "generated_specs": len(specs),
            "deferred": built["deferred"], "eligible": eligible,
            "not_eligible": {name: dict(per[name]) for name in sorted(per) if name not in eligible},
            "per_source": {name: dict(tally) for name, tally in sorted(per.items())},
            "first_problem": {name: list(example) for name, example in sorted(examples.items())}}


def plan_cutover(root: Path, ui_specs: dict[str, DatasetSpec], *, account_alias: str = "anhui-production",
                 requested: list[str] | None = None) -> dict:
    """Which first-batch reports may move to PMOS from this (frozen) legacy root. Nothing is written."""
    report = parity(root, ui_specs, account_alias=account_alias)
    eligible = report["eligible"]
    if requested is not None:
        unknown = sorted(set(requested) - set(eligible))
        if unknown:
            raise ValueError("requested_sources_not_eligible:" + ",".join(unknown))
        eligible = [name for name in eligible if name in requested]
    open_sends = in_flight(root)
    plan = {"province": "anhui", "account_alias": account_alias, "inputs": report["inputs"],
            "global_blockers": {"legacy_attempt_in_flight": len(open_sends)} if open_sends else {},
            "scope": eligible,
            "excluded": {**{name: "identity_parity_incomplete" for name in report["not_eligible"]},
                         **report["deferred"]},
            "per_source": {name: report["per_source"][name] for name in eligible},
            "ready": bool(eligible) and not open_sends}
    plan["plan_sha256"] = hashlib.sha256(canonical(plan)).hexdigest()
    return plan


def apply_cutover(root: Path, instance: Path, plan: dict, ui_specs: dict[str, DatasetSpec], *, legacy_owner: dict,
                  code_sha: str, approval: str, request_budget: int, keepalive_dataset: str | None = None) -> dict:
    """A NEW PMOS ledger with every legacy native send of the scope as a tombstone, and the handoff receipt."""
    from ..ledger import Ledger
    from ..queue_compat import import_shadow
    if not plan.get("ready"):
        raise ValueError("cutover_plan_not_ready")
    fresh = plan_cutover(root, ui_specs, account_alias=plan["account_alias"], requested=plan["scope"])
    if fresh["plan_sha256"] != plan["plan_sha256"]:
        raise ValueError("legacy_root_changed_since_plan")
    instance = Path(instance)
    if instance.exists():
        raise ValueError("new_instance_directory_required")
    rows, refs = legacy_rows(root, plan["account_alias"], plan["scope"])
    digests = {ref["path"]: ref["sha256"] for ref in refs if "sha256" in ref}
    by_source: dict[str, list] = {}
    for source_ref, row in rows:
        by_source.setdefault(source_ref, []).append(row)
    instance.mkdir(parents=True, mode=0o700)
    ledger = Ledger(instance / "ledger.sqlite3")
    try:
        context = QueueContext("anhui", plan["account_alias"])
        snapshots = []
        for source_ref, source_rows in sorted(by_source.items()):
            import_shadow(ledger, source_rows, context, source_id=source_ref, snapshot_sha256=digests[source_ref])
            snapshots.append({"source_id": source_ref, "snapshot_sha256": digests[source_ref]})
        receipt = ledger.grant_handoff({
            "version": 1, "province": "anhui", "account_alias": plan["account_alias"], "legacy_owner": legacy_owner,
            "snapshots": snapshots, "scope": plan["scope"], "request_budget": request_budget, "code_sha": code_sha,
            "seeding": "spec_periods",
            "session": {"keepalive_dataset": keepalive_dataset} if keepalive_dataset else {},
            "approval": approval + f" (plan {plan['plan_sha256']})"})
        summary = ledger.legacy_summary()
    finally:
        ledger.close()
    return {"receipt": receipt, "ledger": summary}


def write_specs(built: dict, directory: Path, names: list[str]) -> list[str]:
    """Write generated specs into the PMOS Anhui datasets directory (never a legacy directory)."""
    directory = Path(directory).resolve()
    if directory.name != "datasets" or directory.parent.name != "anhui" or directory.parent.parent.name != "provinces":
        raise ValueError("pmos_anhui_datasets_directory_required")
    written = []
    for name in names:
        spec = DatasetSpec(**built["specs"][name])
        source = built["specs"][name]["source"]
        spec.note("generated_from_legacy_native_binding" if "binding_sha256" in source else "generated_from_legacy_queue",
                  **({"binding_sha256": source["binding_sha256"]} if "binding_sha256" in source else
                     {"per_option": source.get("per_option")}))
        spec.save(directory / f"{name}.json")
        written.append(name)
    return written
