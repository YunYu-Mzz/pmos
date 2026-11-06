"""Anhui second batch: the legacy multidimensional candidates (date × option) as PMOS tasks.

Legacy facts (read-only survey 2026-10-31, docs/ANHUI_MIGRATION.md):
``state/server_multidimensional_candidates_<ds>.json`` keys every candidate by
``sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
separators=(',',':')))`` of ``{dataset, period, selectors, identity_bindings}``.
A replay changes only the date path and the option path(s) of a sibling's own
request (the ``_bindings`` replay proof: ``option_parameter_paths``,
``option_echo_fields``); the option's request value is its display label for
the reports handled here (``option_value_sources.kind == display_label``).

This module turns the reports with such a proof into fetch specs (one scope
per option combination the legacy discovery found), imports every candidate
(consumed ones as tombstones, never re-sent) and seeds the open ones. Read-only
on the legacy root; sends nothing.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from ..contracts import TaskIdentity, canonical
from ..engine import task_identity
from ..faults import Fault
from ..queue_compat import LegacyTask, QueueContext, read_json
from ..spec import DatasetSpec, SpecError, scope_key
from .anhui import BINDINGS, _declared, publish_lag

PERIOD_LABELS = {"日期": "day", "月份": "month"}
PERIOD_TOKEN = {"day": "{date}", "month": "{month}"}
OPEN = {"requesting", "attempting"}
# Whole-day reports (legacy node_price_whole_day: one blank-option request per
# day and type): rolling their thousands of options per day would replace 3
# requests by ~1,000; they wait for the whole-day lane instead.
WHOLE_DAY = {"day_ahead_node_marginal_price", "real_time_node_marginal_price", "publication_1404bce682303715",
             "intertie_forecast_huan_i"}
# Legacy candidate phase -> (PMOS legacy state, consumed). Only an observed
# candidate is still to be collected; every other phase was sent, settled or
# reviewed and is never sent again.
PHASES = {"observed_candidate": ("pending", False),
          "captured": ("saved", True), "captured_whole_day": ("saved", True), "historically_captured": ("saved", True),
          "source_empty": ("empty", True), "source_empty_object": ("empty", True), "source_null_only": ("empty", True),
          "whole_day_not_returned": ("skipped", True), "whole_day_option_review": ("held", True),
          "pending_review": ("held", True)}


def candidates_path(dataset: str) -> str:
    return f"state/server_multidimensional_candidates_{dataset.lower()}.json"


def candidate_key(identity: dict) -> str:
    """The legacy job key (multidimensional_discovery.py): compact, sorted, non-ASCII kept."""
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def replay_routes(root: Path) -> tuple[dict, dict]:
    """dataset -> the one replay route of its display-label proofs; dataset -> reason for the others."""
    proofs: dict[str, list] = {}
    for path in sorted((Path(root) / BINDINGS).glob("*.json")):
        value = json.loads(path.read_bytes())
        if isinstance(value.get("dataset"), str) and isinstance(value.get("option_parameter_paths"), dict):
            proofs.setdefault(value["dataset"], []).append({**value, "_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    routes, deferred = {}, {}
    for name, found in sorted(proofs.items()):
        kinds = lambda p: {v.get("kind") for v in p["option_value_sources"].values() if isinstance(v, dict)}
        usable = [p for p in found if isinstance(p.get("option_value_sources"), dict) and p["option_value_sources"]
                  and kinds(p) in ({"display_label"}, {"archived_response_value"})
                  and len(p["option_value_sources"]) == len(p["option_parameter_paths"])]
        if not usable:
            deferred[name] = "option_values_not_display_labels"
            continue
        shapes = {(p.get("method"), p.get("endpoint_path"), canonical(p.get("parameter_path")),
                   canonical(p.get("option_parameter_paths"))) for p in usable}
        if len(shapes) != 1:
            deferred[name] = "replay_route_not_unique"
            continue
        proof = usable[-1]
        date, options = proof.get("parameter_path") or [], proof["option_parameter_paths"]
        if (proof.get("method") != "POST" or not str(proof.get("endpoint_path", "")).startswith("/px-") or len(date) != 1
                or not options or any(not isinstance(p, list) or len(p) != 1 or p[0] == date[0] for p in options.values())):
            deferred[name] = "replay_route_unsupported"
            continue
        echoes = [p.get("option_echo_fields") for p in usable if p.get("option_echo_fields")]
        internal = kinds(proof) == {"archived_response_value"}
        if internal and len(options) != 1:
            deferred[name] = "internal_option_route_unsupported"
            continue
        routes[name] = {"endpoint": proof["endpoint_path"], "date_path": date[0],
                        "options": {label: path[0] for label, path in options.items()},
                        "echo": echoes[-1] if echoes else {}, "binding_sha256": proof["_sha256"], "internal": internal}
    return routes, deferred


def option_identity(value) -> str | None:
    """The legacy option identity: SHA-256(JSON.stringify([typeof value, value])) for a string or an integer."""
    kind = "string" if isinstance(value, str) else "number" if isinstance(value, int) and not isinstance(value, bool) else None
    if kind is None:
        return None
    return hashlib.sha256(json.dumps([kind, value], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def internal_values(root: Path, dataset: str, queue: dict, route: dict, *, name_field: str = "NAME",
                    value_field: str = "unit_id") -> dict[str, str]:
    """display label -> internal request value, from the legacy worker's own verified originals (archive_resolver).

    A label is resolved only if exactly one value found beside it in an
    original (content SHA checked) has the candidate's own option identity.
    """
    label = next(iter(route["options"]))
    wanted = {}
    for job in queue["jobs"].values():
        binding = ((job.get("identity") or {}).get("identity_bindings") or {}).get(label) or {}
        if isinstance(binding.get("display_label"), str) and isinstance(binding.get("identity_sha256"), str):
            wanted.setdefault(binding["display_label"], set()).add(binding["identity_sha256"])
    receipts = [job.get("receipt") for job in queue["jobs"].values() if job.get("phase") == "captured"]
    try:
        attempts, _ = read_json(_declared(root, f"state/server_native_attempts_{dataset.lower()}.json"))
        receipts += [a.get("receipt") for a in attempts.values() if isinstance(a, dict) and a.get("phase") == "captured"]
    except FileNotFoundError:
        pass
    seen: dict[str, set] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or not isinstance(receipt.get("source_path"), str):
            continue
        try:
            raw = _declared(root, receipt["source_path"]).read_bytes()
        except FileNotFoundError:
            continue
        if hashlib.sha256(raw).hexdigest() != receipt.get("source_sha256"):
            continue
        rows = json.loads(raw).get("data")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get(name_field), str) and row.get(value_field) is not None:
                seen.setdefault(row[name_field], set()).add(row[value_field])
    resolved = {}
    for display, hashes in wanted.items():
        matches = {value for value in seen.get(display, ()) if option_identity(value) in hashes}
        if len(hashes) == 1 and len(matches) == 1:
            resolved[display] = matches.pop()
    return resolved


def _candidates(root: Path, dataset: str) -> tuple[dict, str]:
    value, digest = read_json(_declared(root, candidates_path(dataset)))
    if value.get("dataset") != dataset or not isinstance(value.get("jobs"), dict):
        raise ValueError("candidate_queue_shape_changed")
    return value, digest


def _period_label(identity: dict) -> tuple[str, str] | None:
    found = [(label, kind) for label, kind in PERIOD_LABELS.items() if (identity.get("selectors") or {}).get(label) == identity.get("period")]
    return found[0] if len(found) == 1 else None


def build_specs(root: Path, ui_specs: dict[str, DatasetSpec]) -> dict:
    """Fetch specs for the replayable reports; a UI query with the task's own option opens each once."""
    routes, deferred = replay_routes(root)
    specs = {}
    for name, route in routes.items():
        ui = ui_specs.get(name)
        # The UI spec itself, or (once generated) the original UI request kept in the fetch spec's source.
        page = None if ui is None else (ui.request if ui.request.get("transport") == "ui" else ui.source.get("ui_request"))
        if not isinstance(page, dict) or page.get("transport") != "ui":
            deferred[name] = "ui_bootstrap_spec_missing"
            continue
        try:
            value, _ = _candidates(root, name)
        except FileNotFoundError:
            deferred[name] = "candidate_queue_missing"
            continue
        kinds, scopes = set(), {}
        resolved = internal_values(root, name, value, route) if route["internal"] else None
        for job in value["jobs"].values():
            identity = job.get("identity") or {}
            found = _period_label(identity)
            if found is None:
                continue
            kinds.add(found[1])
            options = {label: (identity.get("selectors") or {}).get(label) for label in route["options"]}
            if not all(isinstance(v, str) and v for v in options.values()):
                continue
            if resolved is not None:
                label, path = next(iter(route["options"].items()))
                if options[label] not in resolved:
                    continue                  # its internal value is not in any verified original yet
                options = {label: options[label], path: resolved[options[label]]}
            scopes[scope_key(options)] = options
        if len(kinds) != 1 or not scopes:
            deferred[name] = "candidate_periods_or_options_unclear"
            continue
        kind = kinds.pop()
        token = PERIOD_TOKEN[kind]
        dates = [f for f in page.get("fields", []) if f.get("kind") == "date"]
        if len(dates) != 1:
            deferred[name] = "ui_date_field_not_unique"
            continue
        fields = [dates[0]] + [{"kind": "select", "label": label, "value": "{scope[" + label + "]}"} for label in route["options"]]
        bootstrap = {**{k: v for k, v in page.items() if k not in ("action", "path", "method", "fields")},
                     "fields": fields, "action": "query", "path": route["endpoint"], "method": "POST"}
        body = {route["date_path"]: token, **{path: "{scope[" + (path if route["internal"] else label) + "]}"
                                              for label, path in route["options"].items()}}
        spec = {
            "id": name, "name": (ui.name if ui else name), "status": "verified",
            # The legacy discovery went up to yesterday; a report of what happened that day waits 2.
            "period": {"kind": kind, "start": "2026-01-01" if kind == "day" else "2026-01", "end": "today",
                       **({"lag_days": publish_lag(name)} if kind == "day" else {})},
            "request": {"transport": "fetch", "method": "POST", "path": route["endpoint"], "template_body_set": body,
                        "bootstrap": bootstrap},
            "response": {"kind": "json", **({"echo": {route["echo"][label][0]: label for label in route["echo"]}}
                                            if route["echo"] else {}),
                         # An internal option's answers carry the ID that was asked for.
                         **({"echo": {"unit_id": next(iter(route["options"].values()))}} if route["internal"] else {})},
            "scopes": [scopes[key] for key in sorted(scopes)],
            "source": {"legacy": "anhui multidimensional candidates", "binding_sha256": route["binding_sha256"],
                       "ui_request": page, "roll_forward_options": name not in WHOLE_DAY,
                       # One-dropdown reports with text-valued options: new days' options are read from the page.
                       **({"discover_options": {"label": next(iter(route["options"]))}}
                          if name not in WHOLE_DAY and not route["internal"] and len(route["options"]) == 1 else {}),
                       **({"partial_options": True,
                           "discover_options": {"label": next(iter(route["options"])),
                                                "value_field": next(iter(route["options"].values()))}}
                          if route["internal"] else {}),
                       "identity_contract": {"request_data_path": ["template_body_set"], "date_fields": [route["date_path"]],
                                             "dimension_fields": {route["date_path"]: "日期" if kind == "day" else "月份",
                                                                  **({} if route["internal"] else
                                                                     {path: label for label, path in route["options"].items()})},
                                             **({"scope_dimensions": list(route["options"])} if route["internal"] else {})}},
        }
        try:
            DatasetSpec(**spec).check()
        except (SpecError, ValueError) as exc:
            deferred[name] = f"spec_rejected:{exc}"[:160]
            continue
        specs[name] = spec
    return {"specs": specs, "deferred": deferred}


def legacy_rows(root: Path, account_alias: str, names) -> tuple[list, list, list]:
    """(rows, refs, open sends) of these datasets' candidate queues, read once each."""
    rows, refs, open_sends = [], [], []
    for name in sorted(names):
        value, digest = _candidates(root, name)
        refs.append({"path": candidates_path(name), "sha256": digest, "revision": value.get("revision")})
        for key, job in value["jobs"].items():
            identity = job.get("identity") if isinstance(job, dict) else None
            phase = job.get("phase") if isinstance(job, dict) else None
            if phase in OPEN:
                open_sends.append(f"{name}#{key}")
            blockers, task = [], None
            found = _period_label(identity or {})
            if not isinstance(identity, dict) or candidate_key(identity) != key:
                blockers.append("candidate_key_mismatch")
            elif found is None:
                blockers.append("candidate_period_unclear")
            else:
                task = TaskIdentity("anhui", account_alias, name, {"kind": found[1], "label": identity["period"]},
                                    dict(identity["selectors"]), operation="query")
            state, consumed = PHASES.get(phase, ("unknown", True))
            if phase not in PHASES:
                blockers.append(f"candidate_phase_{phase}")
            facts = {"phase": phase, "identity": identity, "receipt_sha256": (job.get("receipt") or {}).get("source_sha256")}
            rows.append((candidates_path(name), LegacyTask(key, name, str(phase), state, consumed, task, tuple(blockers),
                                                           hashlib.sha256(canonical(facts)).hexdigest())))
    return rows, refs, open_sends


def _identity_scopes(spec: DatasetSpec) -> dict:
    """scope key of the identity labels -> the spec's scope key (an internal value is in the scope only)."""
    labels = spec.source.get("identity_contract", {}).get("scope_dimensions")
    if not labels:
        return {key: key for key in spec.scope_index()}
    cached = spec.__dict__.get("_identity_scopes")
    if cached is None or cached[0] != id(spec.scopes):
        cached = (id(spec.scopes), {scope_key({l: values[l] for l in labels}): key for key, values in spec.scope_index().items()})
        spec.__dict__["_identity_scopes"] = cached
    return cached[1]


def task_of(row: LegacyTask, spec: DatasetSpec) -> tuple[str, str, str] | None:
    """The PMOS task (dataset, period, scope key) of a legacy candidate, if the spec has its option."""
    if row.identity is None:
        return None
    label = next((k for k in PERIOD_LABELS if k in row.identity.dimensions), None)
    options = {k: v for k, v in row.identity.dimensions.items() if k != label}
    key = _identity_scopes(spec).get(scope_key(options))
    return (spec.id, row.identity.period["label"], key) if key is not None else None


def parity(root: Path, ui_specs: dict[str, DatasetSpec], *, account_alias: str = "anhui-production") -> dict:
    built = build_specs(root, ui_specs)
    specs = {name: DatasetSpec(**value) for name, value in built["specs"].items()}
    rows, refs, open_sends = legacy_rows(root, account_alias, specs)
    per = {name: Counter() for name in specs}
    examples = {}
    for source_ref, row in rows:
        tally = per[row.dataset]
        tally["records"] += 1
        tally["consumed"] += int(row.consumed)
        task = task_of(row, specs[row.dataset])
        if task is None and row.identity is not None and specs[row.dataset].source.get("partial_options"):
            tally["option_unresolved"] += 1     # never sent by PMOS until its internal value is verified
            continue
        if task is None:
            tally["unmapped"] += 1
            examples.setdefault(row.dataset, ("unmapped", source_ref, list(row.blockers)))
            continue
        try:
            mine = task_identity("anhui", account_alias, specs[row.dataset], task[1], task[2])
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
    eligible = sorted(n for n, t in per.items() if t["matched"] and t["matched"] + t["option_unresolved"] == t["records"])
    return {"province": "anhui", "inputs": refs, "open_sends": len(open_sends), "generated_specs": len(specs),
            "deferred": built["deferred"], "eligible": eligible,
            "not_eligible": {n: dict(per[n]) for n in sorted(per) if n not in eligible},
            "per_source": {n: dict(t) for n, t in sorted(per.items())},
            "first_problem": {n: list(e) for n, e in sorted(examples.items())}}


def plan_cutover(root: Path, specs: dict[str, DatasetSpec], *, account_alias: str = "anhui-production") -> dict:
    """Both batches in one cutover (user 2026-10-31: 一次切换): the date-only reports and the candidates."""
    from . import anhui as first
    one = first.plan_cutover(root, specs, account_alias=account_alias)
    two = parity(root, specs, account_alias=account_alias)
    three = whole_day_parity(root, specs, account_alias=account_alias)
    scope = one["scope"] + [name for name in two["eligible"] + three["eligible"] if name not in one["scope"]]
    blockers = dict(one["global_blockers"])
    if two["open_sends"]:
        blockers["legacy_candidate_in_flight"] = two["open_sends"]
    if three["open_sends"]:
        blockers["legacy_whole_day_in_flight"] = three["open_sends"]
    excluded = {**one["excluded"], **two["deferred"], **three["deferred"],
                **{n: "identity_parity_incomplete" for n in list(two["not_eligible"]) + list(three["not_eligible"])}}
    plan = {"province": "anhui", "account_alias": account_alias,
            "inputs": one["inputs"] + two["inputs"] + three["inputs"],
            "first_batch": one["scope"], "multidimensional": two["eligible"], "whole_day": three["eligible"],
            "scope": scope, "global_blockers": blockers,
            "excluded": {k: v for k, v in excluded.items() if k not in scope},
            "per_source": {**one["per_source"], **{n: two["per_source"][n] for n in two["eligible"]},
                           **{n: three["per_source"][n] for n in three["eligible"]}},
            "ready": bool(one["scope"]) and bool(two["eligible"]) and not blockers}
    plan["plan_sha256"] = hashlib.sha256(canonical(plan)).hexdigest()
    return plan


def apply_cutover(root: Path, instance: Path, plan: dict, specs: dict[str, DatasetSpec], *, legacy_owner: dict,
                  code_sha: str, approval: str, request_budget: int, keepalive_dataset: str | None = None) -> dict:
    """A NEW ledger: every legacy send of both batches as a tombstone, the receipt, daily growth of the
    date-only reports, and the open candidates as tasks (``legacy_pending_only``)."""
    from ..ledger import Ledger
    from ..queue_compat import import_shadow
    from . import anhui as first
    if not plan.get("ready"):
        raise ValueError("cutover_plan_not_ready")
    if plan_cutover(root, specs, account_alias=plan["account_alias"])["plan_sha256"] != plan["plan_sha256"]:
        raise ValueError("legacy_root_changed_since_plan")
    instance = Path(instance)
    if instance.exists():
        raise ValueError("new_instance_directory_required")
    alias = plan["account_alias"]
    rows, refs = first.legacy_rows(root, alias, plan["first_batch"])
    multi, multi_refs, _ = legacy_rows(root, alias, plan["multidimensional"])
    whole, whole_refs, _ = whole_day_rows(root, alias, plan.get("whole_day", []))
    rows += multi + whole
    digests = {ref["path"]: ref["sha256"] for ref in refs + multi_refs + whole_refs if "sha256" in ref}
    by_source: dict[str, list] = {}
    for source_ref, row in rows:
        by_source.setdefault(source_ref, []).append(row)
    instance.mkdir(parents=True, mode=0o700)
    ledger = Ledger(instance / "ledger.sqlite3")
    seeded = Counter()
    try:
        context = QueueContext("anhui", alias)
        snapshots = []
        for source_ref, source_rows in sorted(by_source.items()):
            import_shadow(ledger, source_rows, context, source_id=source_ref, snapshot_sha256=digests[source_ref])
            snapshots.append({"source_id": source_ref, "snapshot_sha256": digests[source_ref]})
        receipt = ledger.grant_handoff({
            "version": 1, "province": "anhui", "account_alias": alias, "legacy_owner": legacy_owner,
            "snapshots": snapshots, "scope": plan["scope"], "request_budget": request_budget, "code_sha": code_sha,
            "seeding": "legacy_pending_only",
            "session": {"keepalive_dataset": keepalive_dataset} if keepalive_dataset else {},
            "approval": approval + f" (plan {plan['plan_sha256']})"})
        # The date-only and whole-day reports grow by day (legacy-complete days skipped); candidates only as discovered.
        ledger.extend_scope(plan["scope"], plan["first_batch"] + plan.get("whole_day", []), approval)
        for _, row in whole:
            if row.state != "pending" or row.consumed:
                continue
            task = whole_day_task(row, specs[row.dataset])
            if task is None:
                raise ValueError("legacy_whole_day_scope_missing_from_spec:" + row.dataset)
            seeded[row.dataset] += ledger.seed(task[0], [task[1]], task[2])
        for _, row in multi:
            if row.state != "pending" or row.consumed:
                continue
            task = task_of(row, specs[row.dataset])
            if task is None and specs[row.dataset].source.get("partial_options"):
                seeded[row.dataset + ":option_unresolved"] += 1
                continue
            if task is None:
                raise ValueError("legacy_pending_scope_missing_from_spec:" + row.dataset)
            seeded[row.dataset] += ledger.seed(task[0], [task[1]], task[2])
        summary = ledger.legacy_summary()
    finally:
        ledger.close()
    return {"receipt": receipt, "seeded": dict(seeded), "ledger": summary}


def write_specs(built: dict, directory: Path, names: list[str]) -> list[str]:
    """Write generated specs into the PMOS Anhui datasets directory (never a legacy directory)."""
    from . import anhui as first
    return first.write_specs(built, directory, names)


# -- whole-day lane (legacy node_price_whole_day.SPECS) ---------------------
WHOLE_DAY_SPECS = {
    "day_ahead_node_marginal_price": {"types": ["节点价格", "能量价格", "阻塞价格"], "option_field": "node_name",
                                      "type_field": "price_type", "repeat": "single"},
    "real_time_node_marginal_price": {"types": ["节点价格", "能量价格", "阻塞价格"], "option_field": "node_name",
                                      "type_field": "price_type", "repeat": "single"},
    "publication_1404bce682303715": {"types": None, "option_field": "sec_name", "repeat": "uniform"},
    "intertie_forecast_huan_i": {"types": None, "option_field": "name", "repeat": "uniform"},
}
TYPE_LABEL = "数据类型"


def whole_day_path(dataset: str) -> str:
    return f"state/server_node_price_whole_day_queue_{dataset.lower()}.json"


def whole_day_id(dataset: str) -> str:
    return f"{dataset}_whole_day"


def build_whole_day_specs(root: Path, option_specs: dict) -> dict:
    """One blank-option spec per whole-day report: the page body with the date, ``genName=''`` and the type.

    Its page request is primed once by a record the legacy worker already
    collected (the newest captured candidate), queried through the report's UI.
    """
    specs, deferred = {}, {}
    for name, rule in WHOLE_DAY_SPECS.items():
        option = option_specs.get(name)
        if option is None:
            deferred[whole_day_id(name)] = "option_spec_missing"
            continue
        try:
            queue, _ = _candidates(root, name)
        except FileNotFoundError:
            deferred[whole_day_id(name)] = "candidate_queue_missing"
            continue
        captured = sorted((job["identity"]["period"], key) for key, job in queue["jobs"].items()
                          if job.get("phase") == "captured" and _period_label(job.get("identity") or {}))
        if not captured:
            deferred[whole_day_id(name)] = "no_collected_record_to_prime"
            continue
        period, key = captured[-1]
        bootstrap = option["request"]["bootstrap"]
        # Only the date: the whole-day request leaves the option blank anyway, and a node is chosen in
        # a tree, not a dropdown (Anhui 2026-11-01 13:14: select_option_missing). The page's own query
        # with its defaults shows the request to replay.
        fields = [{**f, "value": period} for f in bootstrap["fields"] if f.get("kind") == "date"]
        body = {"businessTime": "{date}", "genName": "", **({"type": "{scope[" + TYPE_LABEL + "]}"} if rule["types"] else {})}
        spec = {
            "id": whole_day_id(name), "name": f"{option.get('name', name)}（整日）", "status": "verified",
            "period": {"kind": "day", "start": "2026-01-01", "end": "today", "lag_days": 2},
            "request": {"transport": "fetch", "method": "POST", "path": option["request"]["path"],
                        "template_body_set": body, "timeout": 120,
                        "prime": {"period": period, "request": {**bootstrap, "fields": fields}}},
            "response": {"kind": "json", "whole_day": {"option_field": rule["option_field"], "time_field": "dataTime",
                                                       "times": 96, "repeat": rule["repeat"],
                                                       **({"type_field": rule["type_field"], "type_label": TYPE_LABEL}
                                                          if rule["types"] else {})}},
            **({"scopes": [{TYPE_LABEL: t} for t in rule["types"]]} if rule["types"] else {}),
            "source": {"legacy": "anhui node_price_whole_day", "per_option": name,
                       "identity_contract": {"request_data_path": ["template_body_set"], "date_fields": ["businessTime"],
                                             "dimension_fields": {"businessTime": "日期",
                                                                  **({"type": TYPE_LABEL} if rule["types"] else {})}}},
        }
        try:
            DatasetSpec(**spec).check()
        except (SpecError, ValueError) as exc:
            deferred[whole_day_id(name)] = f"spec_rejected:{exc}"[:160]
            continue
        specs[spec["id"]] = spec
    return {"specs": specs, "deferred": deferred}


WHOLE_DAY_PHASES = {"pending": ("pending", False), "captured": ("saved", True), "source_empty": ("empty", True),
                    "pending_review": ("held", True)}


def whole_day_rows(root: Path, account_alias: str, names) -> tuple[list, list, list]:
    """The legacy whole-day queues (``<date>|<type>`` / ``<date>|全部``) as rows of the whole-day specs."""
    rows, refs, open_sends = [], [], []
    for spec_id in sorted(names):
        dataset = spec_id.removesuffix("_whole_day")
        value, digest = read_json(_declared(root, whole_day_path(dataset)))
        refs.append({"path": whole_day_path(dataset), "sha256": digest})
        for key, job in value.get("jobs", {}).items():
            phase = job.get("phase") if isinstance(job, dict) else None
            if phase in OPEN:
                open_sends.append(f"{spec_id}#{key}")
            period, kind = job.get("period"), job.get("type")
            blockers = [] if key == f"{period}|{kind or '全部'}" else ["whole_day_key_mismatch"]
            identity = TaskIdentity("anhui", account_alias, spec_id, {"kind": "day", "label": period},
                                    {"日期": period, **({TYPE_LABEL: kind} if kind else {})}, operation="query")
            state, consumed = WHOLE_DAY_PHASES.get(phase, ("unknown", True))
            facts = hashlib.sha256(canonical({"key": key, "job": job})).hexdigest()
            rows.append((whole_day_path(dataset), LegacyTask(key, spec_id, str(phase), state, consumed,
                                                             None if blockers else identity, tuple(blockers), facts)))
    return rows, refs, open_sends


def whole_day_task(row: LegacyTask, spec: DatasetSpec) -> tuple[str, str, str] | None:
    if row.identity is None:
        return None
    options = {k: v for k, v in row.identity.dimensions.items() if k != "日期"}
    key = scope_key(options) if spec.scopes else ""
    return (spec.id, row.identity.period["label"], key) if (not spec.scopes or key in spec.scope_index()) else None


def whole_day_parity(root: Path, specs: dict[str, DatasetSpec], *, account_alias: str = "anhui-production") -> dict:
    """Written whole-day specs vs the legacy whole-day queues, identity by identity."""
    built = build_whole_day_specs(root, build_specs(root, specs)["specs"])
    names = [n for n in built["specs"] if n in specs]
    deferred = {**built["deferred"], **{n: "whole_day_spec_not_written" for n in built["specs"] if n not in specs}}
    rows, refs, open_sends = whole_day_rows(root, account_alias, names)
    per = {name: Counter() for name in names}
    for _, row in rows:
        tally = per[row.dataset]
        tally["records"] += 1
        tally["consumed"] += int(row.consumed)
        task = whole_day_task(row, specs[row.dataset])
        if task is None:
            tally["unmapped"] += 1
            continue
        try:
            mine = task_identity("anhui", account_alias, specs[row.dataset], task[1], task[2])
        except Fault:
            tally["engine_identity_error"] += 1
            continue
        tally["matched" if mine.key == row.identity.key else "mismatched"] += 1
    eligible = sorted(n for n, t in per.items() if t["records"] and t["matched"] == t["records"])
    return {"inputs": refs, "open_sends": len(open_sends), "deferred": deferred, "eligible": eligible,
            "not_eligible": {n: dict(per[n]) for n in sorted(per) if n not in eligible},
            "per_source": {n: dict(t) for n, t in sorted(per.items())}}
