"""Bounded, read-only recovery of retained Liaoning evidence.

This module never imports the legacy crawler, opens a browser, writes a source
file, or makes a request. Queue identities are returned in RAM; reports contain
only hashes and public evidence paths, never native selectors or response rows.
"""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re

from ..adapters.liaoning import contract_for_sample, decode
from ..contracts import TaskIdentity, canonical, reject_secrets
from ..faults import Fault
from ..queue_audit import _source_rule_refs
from ..queue_compat import QueueContext, decode_frontier, decode_liaoning

LIMIT = 64 * 1024 * 1024
PRIVATE = re.compile(r"(?i)private|profile|browser|cookie|credential|password|authorization|token|session|\.har$|(?:^|/)(?:auth|config|configs)(?:/|$)")
EXPORTED = "verification/20261027/disclosure_timing_full_rows/retained_remote/data/2026/original"
FAMILIES = ("advanced_reports", "power_reports", "native_lists", "report_engine")
QUEUE = "state/server_verified_expansion_20261023/queue.json"
MANIFEST = "state/server_verified_expansion_20261023/active_manifest.json"
ENGINE = "state/server_engine_collection_20261023"
RULE_FILES = ("lncrawler/expanded_adapters.py", "lncrawler/sitewide_specs.py",
              "lncrawler/goal92_candidate_specs.py")
EXTRA_SNAPSHOTS = ("state/server_verified_expansion_20261023/server_advanced_tasks_20261016.json",
                   "state/server_verified_expansion_20261023/server_sitewide_20261016_tasks.json",
                   "state/dimension_coverage_20261024/advanced_before.json",
                   "state/dimension_coverage_20261024/sitewide_before.json",
                   "reporting/diagnostics_20261024/retained_for_attachment/ordinary_queue.json")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class _Reader:
    def __init__(self, root):
        root = Path(root).absolute()
        if root.name != "liaoning" and (root / "liaoning").is_dir():
            root = root / "liaoning"
        if root.is_symlink() or root.name != "liaoning":
            raise ValueError("explicit_liaoning_root_required")
        self.root, self.workspace = root.resolve(), root.resolve().parent
        self.refs = {}
        self.changed_paths = set()

    def raw(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        if (not path.is_relative_to(self.workspace) or not path.resolve().is_relative_to(self.workspace)
                or ".." in path.parts or PRIVATE.search(str(path.relative_to(self.workspace)))):
            raise ValueError("controlled_business_reference_required")
        relative = path.relative_to(self.workspace)
        current = self.workspace
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("symlink_business_reference_forbidden")
        if not path.is_file() or path.stat().st_size > LIMIT:
            raise ValueError("bounded_business_file_required")
        raw = path.read_bytes()
        if len(raw) > LIMIT:
            raise ValueError("bounded_business_file_required")
        digest = _sha(raw)
        if str(relative) in self.refs and self.refs[str(relative)] != digest:
            self.changed_paths.add(str(relative))
            raise ValueError("business_reference_changed_during_scan")
        self.refs.setdefault(str(relative), digest)
        return raw

    def json(self, path):
        raw = self.raw(path)
        value = json.loads(raw)
        reject_secrets(value)
        return value, _sha(raw)

    def ref(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        relative = str(path.relative_to(self.workspace))
        return {"path": relative, "sha256": self.refs[relative]}

    def changed(self):
        for relative in sorted(self.refs):
            try:
                self.raw(self.workspace / relative)
            except ValueError:
                self.changed_paths.add(relative)
        return sorted(self.changed_paths)


def _business_files(base, filename):
    """Traverse only a declared original directory, pruning before descent."""
    if not base.is_dir() or base.is_symlink():
        return
    count = 0
    for directory, directories, files in os.walk(base, followlinks=False):
        directories[:] = sorted(name for name in directories
                                if not PRIVATE.search(name) and not (Path(directory) / name).is_symlink())
        if filename in files:
            count += 1
            if count > 10000:
                raise ValueError("declared_original_inventory_limit")
            yield Path(directory) / filename


def _rules(reader):
    rules = {}
    for relative in RULE_FILES:
        if not (reader.root / relative).is_file():
            continue
        raw = reader.raw(relative)
        for node in ast.walk(ast.parse(raw)):
            if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name)
                    or node.func.id not in {"ReportAdapter", "MondayWeekAdapter"} or len(node.args) < 4):
                continue
            try:
                dataset, field = ast.literal_eval(node.args[0]), ast.literal_eval(node.args[3])
                keywords = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
            except (TypeError, ValueError):
                continue
            if not isinstance(dataset, str) or not isinstance(field, str) or keywords.get("kind", "report") != "report":
                continue
            rules[dataset] = {"period_field": field, "selection_field": keywords.get("selection_field"),
                "period_kind": keywords.get("period", "day"), "source_ref": reader.ref(relative)}
    return rules


@dataclass
class _Sample:
    dataset: str
    metadata: dict
    response_path: Path
    metadata_ref: dict
    response_ref: dict
    query: dict
    contract_sha256: str | None
    replay_status: str
    row_count: int | None
    empty: bool | None

    def public(self):
        return {"metadata_ref": self.metadata_ref, "response_ref": self.response_ref,
                "query_identity_sha256": _sha(canonical(self.query)),
                "contract_sha256": self.contract_sha256, "replay_status": self.replay_status,
                "row_count": self.row_count, "empty": self.empty,
                "semantic_acceptance": "pending", "full_scope_acceptance": "pending"}


def _sample(reader, metadata_path, *, wrapper=None):
    outer, _ = reader.json(metadata_path)
    metadata = outer.get("metadata", {}) if wrapper == "provenance" else outer
    dataset = metadata.get("dataset")
    if not isinstance(dataset, str) or dataset not in metadata_path.parts:
        raise ValueError("metadata_dataset_path_mismatch")
    expected = metadata.get("body_sha256")
    if wrapper == "provenance":
        if (outer.get("dataset") != dataset or outer.get("sha256") != expected
                or outer.get("expected_sha256") != expected):
            raise ValueError("provenance_identity_or_sha256_mismatch")
    if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
        raise ValueError("metadata_body_sha256_required")
    if metadata.get("body_file", "response.json") != "response.json":
        raise ValueError("declared_response_filename_required")
    response_path = metadata_path.parent / "response.json"
    raw = reader.raw(response_path)
    if _sha(raw) != expected or metadata.get("body_bytes", len(raw)) != len(raw):
        raise ValueError("original_sha256_or_size_mismatch")
    status = metadata.get("http_status", metadata.get("response", {}).get("http_status"))
    if status != 200:
        raise ValueError("business_success_receipt_required")
    query = metadata.get("query", metadata.get("query_identity", metadata.get("params", {})))
    if not query and metadata.get("date"):
        if metadata.get("native_date_confirmed") != metadata["date"]:
            raise ValueError("engine_native_date_confirmation_required")
        query = {"period": metadata["date"]}
    if not isinstance(query, dict) or not query:
        raise ValueError("native_query_identity_required")
    contract_digest = rows = empty = None
    replay = "shape_review_required"
    try:
        page = query.get("page", query.get("pageInfo", {}).get("pageNum"))
        # Do not invent a request page size from a response total/page count.
        size = query.get("page_size", query.get("pageInfo", {}).get("pageSize"))
        contract = contract_for_sample(raw, request_page=page, page_size=size)
        parsed = decode(raw, contract, http_status=status)
        contract_digest, replay, rows, empty = _sha(canonical(contract)), "sample_only_passed", len(parsed.rows), parsed.empty
    except (Fault, ValueError, TypeError, KeyError, AttributeError) as exc:
        replay = getattr(exc, "code", type(exc).__name__)
    return _Sample(dataset, metadata, response_path, reader.ref(metadata_path), reader.ref(response_path),
                   query, contract_digest, replay, rows, empty)


def _samples(reader):
    samples, failures = [], []
    for prefix in (EXPORTED, "data/2026/original"):
        for family in FAMILIES:
            for path in _business_files(reader.root / prefix / family, "metadata.json"):
                try:
                    samples.append(_sample(reader, path))
                except (ValueError, TypeError, KeyError) as exc:
                    failures.append({**reader.ref(path), "reason": str(exc)})
    for relative, filename, wrapper in (
        ("state/quality_repair_20261021/recovered_originals", "provenance.json", "provenance"),
        ("state/continuous_expansion_20261019/samples", "observation.json", None),
        ("state/sitewide_expansion_20261016/after_document_20261019/samples", "observation.json", None),
    ):
        for path in _business_files(reader.root / relative, filename):
            try:
                samples.append(_sample(reader, path, wrapper=wrapper))
            except (ValueError, TypeError, KeyError) as exc:
                failures.append({**reader.ref(path), "reason": str(exc)})
    return samples, failures


def _expanded_key(dataset, identity):
    return "liaoning:2026:expanded:" + dataset + ":" + _sha(canonical(identity))


def _sample_jobs(samples, rules):
    """Exact old-key bridges; equal response hashes alone never join records."""
    result = defaultdict(list)
    for sample in samples:
        metadata, query, dataset = sample.metadata, sample.query, sample.dataset
        job = None
        if isinstance(metadata.get("query"), dict):
            query = metadata["query"]
            params, period, page = query.get("params"), query.get("snapshot_date"), query.get("page")
            if (query.get("dataset") == dataset and isinstance(params, dict)
                    and params == {"page": page, "snapshot_date": period}
                    and query.get("key") == dataset + ":snapshot:" + _sha(canonical(params))):
                job = dict(query)
        elif isinstance(metadata.get("query_identity"), dict):
            native = metadata["query_identity"]
            rule = rules.get(dataset)
            if "period" in native:
                identity = native
                if rule and set(native) <= {"period", "selection"}:
                    params = {rule["period_field"]: native["period"]}
                    selection = rule.get("selection_field")
                    if selection:
                        if "selection" not in native:
                            continue
                        params[selection] = native["selection"]
                    job = {"key": _expanded_key(dataset, identity), "dataset": dataset,
                           "period": native["period"], "params": params}
            elif rule:
                field, selection = rule["period_field"], rule.get("selection_field")
                if set(native) == {field} | ({selection} if selection else set()):
                    identity = {"period": native[field]}
                    if selection:
                        identity["selection"] = native[selection]
                    job = {"key": _expanded_key(dataset, identity), "dataset": dataset,
                           "period": native[field], "params": dict(native)}
        if job:
            result[(dataset, job["key"], sample.response_ref["sha256"])].append((job, sample))
    return result


def _receipt_jobs(reader, rules):
    """Recorded native parameters can prove identity even if bytes are remote.

    These receipts do not count as found originals or as sample replay passes.
    An exact recomputed old key AND the old outcome's body SHA are required.
    """
    result = defaultdict(list)
    if not (reader.root / MANIFEST).is_file():
        return result
    manifest, _ = reader.json(MANIFEST)
    for dataset, source in manifest.get("sources", {}).items():
        rule = rules.get(dataset)
        if not rule:
            continue
        field, selection = rule["period_field"], rule.get("selection_field")
        for proof in source.get("proofs", []):
            params, digest = proof.get("params"), proof.get("sha256")
            if (proof.get("http_status") != 200 or proof.get("outcome") not in {"saved", "empty", "nonempty"}
                    or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)
                    or not isinstance(params, dict) or set(params) != {field} | ({selection} if selection else set())):
                continue
            identity = {"period": params[field]}
            if selection:
                identity["selection"] = params[selection]
            key = _expanded_key(dataset, identity)
            job = {"key": key, "dataset": dataset, "period": params[field], "params": params}
            result[(dataset, key, digest)].append((job, reader.ref(MANIFEST)))
    return result


def _baseline_views(reader):
    baseline, _ = reader.json(reader.workspace / "pmos/evidence/liaoning_queue_phase2.json")
    manifest, manifest_sha = reader.json(MANIFEST)
    kinds = {dataset: spec.get("period", "snapshot") for dataset, spec in manifest.get("sources", {}).items()}
    for relative in RULE_FILES[:2]:
        if (reader.root / relative).is_file():
            reader.raw(relative)
    old_rules, _ = _source_rule_refs(reader.root)
    context = QueueContext("liaoning", "offline-shadow-review")
    views = []
    for ref in baseline["source_views"]:
        source_ref = ref["source_ref"]
        # Baseline files have absolute refs; permit only this known suffix.
        if "/liaoning/" in source_ref:
            relative = source_ref.split("/liaoning/", 1)[1]
        elif source_ref.startswith("liaoning/"):
            relative = source_ref[len("liaoning/"):]
        else:
            raise ValueError("known_liaoning_baseline_reference_required")
        if relative == MANIFEST + "#prior_outcomes":
            value = {"sources": {dataset: {"tasks": {}, "outcomes": spec.get("prior_outcomes", {})}
                                  for dataset, spec in manifest.get("sources", {}).items()}}
            digest = _sha(canonical(value))
            tasks = decode_liaoning(value, context, period_kinds=kinds, source_contracts=old_rules)
        elif relative == QUEUE:
            value, digest = reader.json(relative)
            tasks = decode_liaoning(value, context, period_kinds=kinds, source_contracts=old_rules)
        elif re.fullmatch(ENGINE + r"/[A-Za-z0-9_]+/queue\.json", relative):
            value, digest = reader.json(relative)
            tasks = decode_frontier(value, QueueContext("liaoning", context.account_alias, value["dataset"], "day"))
        else:
            raise ValueError("undeclared_baseline_queue_reference")
        observed = {"records": len(tasks), "states": dict(Counter(task.state for task in tasks)),
                    "mapped": sum(task.identity is not None for task in tasks),
                    "unmapped": sum(task.identity is None for task in tasks),
                    "consumed": sum(task.consumed for task in tasks)}
        baseline_counts_match = all(observed[key] == ref[key] for key in observed)
        if digest == ref["sha256"] and not baseline_counts_match:
            raise ValueError("phase2_baseline_decoder_count_or_state_changed")
        views.append({"source_ref": str(reader.root / relative), "relative": relative, "value": value,
                      "sha256": digest, "expected_sha256": ref["sha256"], "tasks": tasks,
                      "phase2_unmapped": ref["unmapped"], "kinds": kinds, "manifest_sha256": manifest_sha,
                      "phase2_counts_and_states_match": baseline_counts_match})
    if all(view["sha256"] == view["expected_sha256"] for view in views):
        all_tasks = [task for view in views for task in view["tasks"]]
        if (len(all_tasks) != baseline["normalized_records"]
                or dict(Counter(task.state for task in all_tasks)) != baseline["states"]
                or sum(task.identity is None for task in all_tasks) != baseline["unmapped_records"]):
            raise ValueError("phase2_baseline_total_count_or_state_changed")
    return baseline, views


def _groups(view):
    value = view["value"]
    return value.get("sources", value.get("datasets", {value.get("dataset"): value}))


def _job_and_outcome(view, task):
    group = _groups(view).get(task.dataset, {})
    jobs = group.get("tasks")
    if jobs is None:
        jobs = {job["key"]: job for job in group.get("pending", [])}
    job = jobs.get(task.legacy_key, {})
    active = view["value"].get("active") or {}
    active = active.get("job", active)
    if not job and active.get("key") == task.legacy_key:
        job = active
    outcome = group.get("outcomes", {}).get(task.legacy_key, {})
    return job, outcome if isinstance(outcome, dict) else {}


def _identity(dataset, job, rule, kind):
    if kind == "week_start_monday":
        try:
            if date.fromisoformat(job.get("period")).weekday() != 0:
                return None, "native_monday_query_invalid"
        except (ValueError, TypeError):
            return None, "native_monday_query_invalid"
        return None, "native_monday_anchor_not_expressible_by_current_protocol"
    period = job.get("snapshot_date", job.get("period"))
    params, page = job.get("params", {}), job.get("page")
    if "snapshot_date" in job:
        if params != {"page": page, "snapshot_date": period}:
            return None, "snapshot_parameters_do_not_match"
        dimensions, kind = {}, "snapshot"
    elif job.get("_explicit_engine_outcome_date") is True and not params:
        dimensions, page = {}, None
    elif rule:
        field, selection = rule["period_field"], rule.get("selection_field")
        if set(params) != {field} | ({selection} if selection else set()) or params.get(field) != period:
            return None, "native_parameters_do_not_match_reviewed_rule"
        dimensions, page = ({selection: params[selection]} if selection else {}), None
    elif not params and period:
        dimensions = {}
    else:
        return None, "native_parameter_rule_required"
    try:
        identity = TaskIdentity("liaoning", "offline-shadow-review", dataset,
                                {"kind": kind, "label": str(period)}, dimensions, page=page)
        identity.document()
    except (ValueError, TypeError, KeyError):
        return None, "source_period_not_expressible_by_current_protocol"
    return identity, None


def _resolve(reader, samples, rules):
    baseline, views = _baseline_views(reader)
    bridges = _sample_jobs(samples, rules)
    receipts = _receipt_jobs(reader, rules)
    old_jobs = defaultdict(list)
    for relative in EXTRA_SNAPSHOTS:
        if not (reader.root / relative).is_file():
            continue
        value, _ = reader.json(relative)
        for dataset, group in value.get("datasets", value.get("sources", {})).items():
            for key, job in (group.get("tasks") or {}).items():
                outcome = group.get("outcomes", {}).get(key, {})
                digest = outcome.get("sha256", outcome.get("raw_sha256")) if isinstance(outcome, dict) else None
                if digest:
                    old_jobs[(dataset, key, digest)].append((job, reader.ref(relative)))
    results, reports = {}, []
    for view in views:
        stable = view["sha256"] == view["expected_sha256"]
        rows, entries = [], []
        for task in view["tasks"]:
            result = task
            if task.identity is not None:
                rows.append(task)
                continue
            entry = {"legacy_key_sha256": _sha(task.legacy_key.encode()), "dataset": task.dataset,
                     "state": task.state, "consumed": task.consumed, "facts_sha256": task.facts_sha256,
                     "identity_key_sha256": None, "evidence_refs": [], "dispatch_allowed": False}
            if not stable:
                entry.update(status="source_changed_not_baseline_recovery", reason="phase2_source_sha256_changed")
            else:
                job, outcome = _job_and_outcome(view, task)
                rule = rules.get(task.dataset)
                kind = "day" if view["relative"].startswith(ENGINE + "/") else view["kinds"].get(task.dataset)
                found = False
                if job:
                    found = True
                    entry["evidence_refs"] = ([rule["source_ref"]] if rule else [])
                    entry["identity_evidence"] = "exact_old_key_native_queue_job_and_source_rule"
                else:
                    outcome_sha = outcome.get("sha256", outcome.get("raw_sha256"))
                    candidates = bridges.get((task.dataset, task.legacy_key, outcome_sha), [])
                    identities = {_sha(canonical(candidate)) for candidate, _ in candidates}
                    if len(identities) == 1:
                        job = candidates[0][0]
                        found = True
                        entry["evidence_refs"] = [ref for _, sample in candidates
                                                  for ref in (sample.metadata_ref, sample.response_ref)]
                        entry["identity_evidence"] = "exact_old_key_query_identity_and_outcome_body_sha256"
                    elif len(identities) > 1:
                        entry["reason"] = "conflicting_native_identity_bridges"
                    elif receipts.get((task.dataset, task.legacy_key, outcome_sha)):
                        candidates = receipts[(task.dataset, task.legacy_key, outcome_sha)]
                        if len({_sha(canonical(candidate)) for candidate, _ in candidates}) == 1:
                            job, _ = candidates[0]
                            found = True
                            entry["evidence_refs"] = [ref for _, ref in candidates]
                            if rule:
                                entry["evidence_refs"].append(rule["source_ref"])
                            entry["identity_evidence"] = "exact_old_key_native_receipt_parameters_and_outcome_body_sha256"
                    elif old_jobs.get((task.dataset, task.legacy_key, outcome_sha)):
                        candidates = old_jobs[(task.dataset, task.legacy_key, outcome_sha)]
                        if len({_sha(canonical(candidate)) for candidate, _ in candidates}) == 1:
                            job, _ = candidates[0]
                            found = True
                            entry["evidence_refs"] = [ref for _, ref in candidates]
                            entry["identity_evidence"] = "exact_old_key_and_outcome_sha256_in_retained_old_snapshot"
                    elif view["relative"].startswith(ENGINE + "/") and outcome.get("date"):
                        # The old business outcome explicitly records its query date.
                        # No date is extracted from directory or body-hash filenames.
                        try:
                            period = outcome["date"]
                            if date.fromisoformat(period).isoformat() != period:
                                raise ValueError()
                            job = {"period": period, "_explicit_engine_outcome_date": True}
                            found = True
                            entry["identity_evidence"] = "exact_old_key_explicit_native_engine_outcome_date"
                            reader.raw("lncrawler/server_engine_collection.py")
                            entry["evidence_refs"] = [reader.ref("lncrawler/server_engine_collection.py")]
                        except (ValueError, TypeError):
                            pass
                if found:
                    if kind == "week_start_monday":
                        entry["native_period_kind"] = kind
                        entry["native_query_period_sha256"] = _sha(canonical(job.get("period")))
                        try:
                            entry["native_monday_confirmed"] = date.fromisoformat(job.get("period")).weekday() == 0
                        except (ValueError, TypeError):
                            entry["native_monday_confirmed"] = False
                    identity, reason = _identity(task.dataset, job, rule, kind)
                    entry.update(status="mapped_from_retained_evidence" if identity else "evidence_found_protocol_pending",
                                 reason=reason)
                    if identity:
                        # All original state, consumption knowledge, holds and hashes
                        # survive. Only identity-specific blockers are discharged.
                        result = replace(task, identity=identity, blockers=tuple(x for x in task.blockers
                            if x not in {"identity_mapping_required", "source_parameter_identity_mapping_required"}))
                        entry["identity_key_sha256"] = identity.key
                else:
                    entry.update(status="evidence_still_missing", reason=entry.get("reason", "exact_old_key_native_query_bridge_not_retained"))
            entry["remaining_blockers"] = list(result.blockers)
            rows.append(result)
            entries.append(entry)
        results[view["source_ref"]] = rows
        counts = Counter(entry["status"] for entry in entries)
        reports.append({"source_ref": "liaoning/" + view["relative"], "sha256": view["sha256"],
                        "phase2_sha256": view["expected_sha256"], "phase2_sha256_matches": stable,
                        "phase2_counts_and_states_match": view.get("phase2_counts_and_states_match", True),
                        "phase2_unmapped": view["phase2_unmapped"], "current_unmapped_cohort": len(entries),
                        "counts": dict(counts), "records": entries})
    return baseline, results, reports


def resolve_queue_records(root):
    """RAM-only LegacyTask views, preserving state/consumed/facts and holds."""
    reader = _Reader(root)
    samples, _ = _samples(reader)
    _, results, _ = _resolve(reader, samples, _rules(reader))
    if reader.changed():
        raise ValueError("business_inputs_changed_during_recovery")
    return results


def baseline_queue_records(root):
    """RAM-only phase2 decoder views for independent before/after checks."""
    reader = _Reader(root)
    _, views = _baseline_views(reader)
    if reader.changed():
        raise ValueError("business_inputs_changed_during_recovery")
    return {view["source_ref"]: view["tasks"] for view in views}


def recover(root):
    """Return a redacted discovery report without writing anything anywhere."""
    reader = _Reader(root)
    phase2, _ = reader.json(reader.workspace / "pmos/evidence/liaoning_phase2.json")
    samples, failures = _samples(reader)
    rules = _rules(reader)
    baseline, _, views = _resolve(reader, samples, rules)
    by_dataset = defaultdict(list)
    for sample in samples:
        by_dataset[sample.dataset].append(sample)
    sources = []
    for item in phase2["entries"]:
        if item["offline_status"] == "sample_only_passed":
            continue
        evidence = by_dataset[item["dataset"]]
        sources.append({"dataset": item["dataset"], "phase2_status": item["offline_status"],
            "status": "sha_bound_original_found" if evidence else "evidence_still_missing",
            "reason": None if evidence else "declared_retained_business_roots_have_no_sha_bound_original",
            "sample_only_replay_passed": any(s.replay_status == "sample_only_passed" for s in evidence),
            "evidence": [sample.public() for sample in evidence],
            "online_status": item.get("online_status"), "full_dataset_coverage_verified": False})
    source_counts = Counter(item["status"] for item in sources)
    queue_counts = Counter()
    for view in views:
        queue_counts.update(view["counts"])
    extra = []
    for relative in EXTRA_SNAPSHOTS:
        if (reader.root / relative).is_file():
            value, _ = reader.json(relative)
            groups = value.get("datasets", value.get("sources", {}))
            extra.append({**reader.ref(relative), "dataset_groups": len(groups),
                          "phase2_cohort_member": False, "added_to_baseline_recovery_count": False})
    drift = reader.changed()
    report = {"schema": "pmos-liaoning-retained-recovery-v1", "province": "liaoning",
            "mode": "read_only_retained_originals_and_queue_snapshots", "website_requests": 0,
            "production_writes": 0, "dispatch_allowed": False, "live_handoff_completed": False,
            "source_evidence": {"baseline_missing_or_review": len(sources), **dict(source_counts),
                                "sample_only_replay_passed": sum(i["sample_only_replay_passed"] for i in sources),
                                "records": sources},
            "queue_evidence": {"baseline_unmapped_record_views": baseline["unmapped_records"],
                               "counts": dict(queue_counts), "source_views": views,
                               "overlapping_snapshots_are_not_unique_tasks": True},
            "additional_old_snapshots": extra, "rejected_original_chains": failures,
            "input_refs": [{"path": key, "sha256": value} for key, value in sorted(reader.refs.items())],
            "inputs_changed_during_scan": drift, "rolling_read_not_frozen": True,
            "limitations": ["found originals are technical-source evidence, not full history or semantic acceptance",
                "newer captures never establish an old queue identity without an exact old-key/query/body-SHA bridge",
                "week_start_monday remains unmapped until the identity protocol can preserve its native semantics",
                "existing unknown/failed/denied, stages and holds remain consumed and non-dispatchable",
                "unretained remote originals and credentials were not accessed"]}
    # Intern public path/SHA references once. Large pending cohorts share the
    # same declaration; no selector or source response is copied into Git.
    references = report.pop("input_refs")
    ids = {(ref["path"], ref["sha256"]): f"e{index:04d}" for index, ref in enumerate(references)}
    report["evidence_refs"] = {ids[(ref["path"], ref["sha256"])]: ref for ref in references}
    for item in report["source_evidence"]["records"]:
        for sample in item["evidence"]:
            for key in ("metadata_ref", "response_ref"):
                ref = sample[key]
                sample[key] = ids[(ref["path"], ref["sha256"])]
    record_kinds, kind_ids = {}, {}
    for view in report["queue_evidence"]["source_views"]:
        for item in view["records"]:
            item["evidence_refs"] = list(dict.fromkeys(ids[(ref["path"], ref["sha256"])]
                                                       for ref in item["evidence_refs"]))
            shared = {key: item.pop(key) for key in
                      ("status", "reason", "identity_evidence", "remaining_blockers", "dispatch_allowed") if key in item}
            shared_key = canonical(shared)
            if shared_key not in kind_ids:
                kind_id = f"k{len(kind_ids):03d}"
                kind_ids[shared_key] = kind_id
                record_kinds[kind_id] = shared
            item["record_kind"] = kind_ids[shared_key]
    report["queue_evidence"]["record_kinds"] = record_kinds
    return report
