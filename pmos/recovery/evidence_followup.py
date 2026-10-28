"""Index-led phase-five evidence research; no producer, database or replay.

Fixed phase-four cohorts remain the comparison unit. A new local artifact may
supply sample evidence, never historical consumption or dispatch permission.
Only metadata referenced by explicit IDs or binding commitments is inspected.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re

from ..adapters.anhui import project_bound, validate_retained_binding
from ..faults import Fault
from ..validate import Response, evaluate
from .liaoning_remaining import _same_request

PROVINCES = ("liaoning", "anhui", "shanxi")
BASELINES = {
    "liaoning": ("liaoning_phase4_remaining.json", "61473f8a198eba8fc87bf54268a3688c657ce97983ec8bac73680196bea61983"),
    "anhui": ("anhui_phase4_remaining.json", "4c77082676f5e69300229f9918a83ed81125428133ecca3c8a430191aedd3b2f"),
    "shanxi": ("shanxi_phase4_remaining.json", "58fc52552b4f54027331f977a10d397f3dad2e9f3fcdd513b84568d13b77d350"),
}
PRIVATE = re.compile(r"private|profile|auth|config|cookie|token|credential|password|secret|session|browser|login|keepalive|http_failure|runtime_control|proxy|\.har|__pycache__|node_modules|\.git(?:/|$)", re.I)
LIMIT = 4 * 1024 * 1024


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise ValueError("duplicate_evidence_json_key")
            value[key] = child
        return value
    def invalid(_):
        raise ValueError("nonfinite_evidence_json_value")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


class Reader:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.refs = {}
        self.changed_paths = set()

    def path(self, relative):
        path = Path(relative)
        if (path.is_absolute() or not path.parts or ".." in path.parts or PRIVATE.search(relative)
                or path.parts[0] not in {*PROVINCES, "pmos"}):
            raise ValueError("controlled_followup_path_required")
        if path.parts[0] == "pmos" and path.parts[:2] != ("pmos", "evidence"):
            raise ValueError("only_controlled_baseline_evidence_allowed")
        current = self.workspace
        for part in path.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("followup_symlink_rejected")
        return current

    def raw(self, relative, expected=None):
        if Path(relative).suffix not in {".json", ".jsonl"}:
            raise ValueError("followup_json_metadata_or_original_required")
        path = self.path(relative)
        try:
            before = path.stat()
            if not path.is_file() or before.st_size > LIMIT:
                raise ValueError("bounded_followup_evidence_required")
            raw = path.read_bytes()
            after = path.stat()
        except OSError:
            if relative in self.refs or expected is not None:
                self.changed_paths.add(relative)
            raise
        if len(raw) > LIMIT or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            self.changed_paths.add(relative)
            raise ValueError("followup_evidence_changed_during_read")
        digest = sha(raw)
        if expected is not None and digest != expected:
            self.changed_paths.add(relative)
            raise ValueError("followup_evidence_sha_changed")
        first = self.refs.get(relative)
        if first is not None and first["sha256"] != digest:
            self.changed_paths.add(relative)
            raise ValueError("followup_evidence_changed_during_scan")
        self.refs.setdefault(relative, {"path": relative, "sha256": digest, "bytes": len(raw)})
        return raw

    def json(self, relative, expected=None):
        return strict_json(self.raw(relative, expected))

    def ref(self, relative):
        return self.refs[relative]

    def finish(self):
        if self.changed_paths:
            raise ValueError("followup_prior_input_drift_detected")
        for path, reference in list(self.refs.items()):
            self.raw(path, reference["sha256"])


def _baseline(reader, province):
    name, expected = BASELINES[province]
    relative = "pmos/evidence/" + name
    return reader.json(relative, expected), reader.ref(relative)


def _files(reader, root, *, names=None, target_ids=None, name_pattern=None):
    base = reader.path(root)
    if not base.is_dir():
        return []
    results = []
    for directory, directories, files in os.walk(base, followlinks=False):
        directories[:] = sorted(n for n in directories if not PRIVATE.search(n) and not (Path(directory) / n).is_symlink()
                                and n not in {"render", "deps", "native_excel_deps"})
        for name in sorted(files):
            if PRIVATE.search(name):
                continue
            path = Path(directory) / name
            if path.is_symlink() or (names is not None and name not in names) or (name_pattern is not None and not name_pattern.fullmatch(name)):
                continue
            if target_ids is not None and not set(path.parts) & set(target_ids):
                continue
            results.append(path.relative_to(reader.workspace).as_posix())
            if len(results) > 10000:
                raise ValueError("followup_inventory_limit")
    return results


def _observe(reader, relative, expected):
    if not reader.path(relative).is_file():
        return {"path": relative, "expected_sha256": expected, "status": "missing"}
    raw = reader.raw(relative)
    return {**reader.ref(relative), "expected_sha256": expected,
            "status": "unchanged_from_phase4" if sha(raw) == expected else "changed_metadata_requires_review_no_upgrade"}


def _own_metadata(reader, paths, target_ids):
    """A directory/title/hash match alone is never a sample qualification."""
    observations = []
    for path in paths:
        value = reader.json(path)
        metadata = value.get("metadata", {}) if path.endswith("/provenance.json") else value
        dataset = metadata.get("dataset")
        if dataset not in target_ids:
            continue
        expected = metadata.get("body_sha256")
        body = str(Path(path).parent / "response.json")
        result = "metadata_only_original_or_context_missing"
        context = metadata.get("query") or metadata.get("query_identity")
        provenance_bound = (not path.endswith("/provenance.json") or
                            (value.get("dataset") == dataset and value.get("sha256") == expected
                             and value.get("expected_sha256") == expected))
        stages = {metadata.get(key) for key in ("stage", "kind", "operation") if isinstance(metadata.get(key), str)}
        non_business = bool(stages & {"dictionary", "filter", "filters", "period", "periods", "table_filters", "table_period", "engine_mapping"})
        non_business = non_business or ("data_report_permission_verified" in metadata and metadata["data_report_permission_verified"] is not True)
        if not provenance_bound:
            result = "provenance_envelope_mismatch_hold"
        elif non_business:
            result = "non_business_stage_or_data_permission_unverified"
        context_bound = (isinstance(context, dict) and bool(context) and context.get("dataset", dataset) == dataset
                         and dataset in Path(path).parts and metadata.get("body_file", "response.json") == "response.json"
                         and provenance_bound and not non_business)
        if isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected) and context_bound:
            if reader.path(body).is_file():
                raw = reader.raw(body, expected)
                if metadata.get("http_status") == 200:
                    try:
                        outcome = evaluate({"kind": "json"}, Response(200, "application/json", raw))
                        result = "new_own_empty_sample_only" if outcome.result == "empty" else "new_own_sample_chain_requires_semantic_review"
                    except Fault as fault:
                        result = "new_own_refusal_or_error_hold"
        observations.append({"dataset": dataset, "metadata_ref": reader.ref(path), "body_ref": reader.refs.get(body),
                             "result": result, "consumption_proven": False, "dispatch_allowed": False})
    return observations


def _liaoning(reader, baseline):
    rows = baseline["source_evidence"]["records"]
    ids = {row["dataset"] for row in rows}
    expected = {row["path"]: row["sha256"] for row in baseline["input_refs"]}
    paths = ["liaoning/state/sitewide_expansion_20261016/catalog_current.json", "liaoning/state/local_adaptation_20261020/native_coverage.json"]
    observations = [_observe(reader, path, expected[path]) for path in paths]
    catalog = reader.json(paths[0])
    coverage = reader.json(paths[1])
    jobs = {row["dataset"]: row for row in catalog["jobs"] if row.get("dataset") in ids}
    declarations = []
    for row in coverage["sources"]:
        dataset = row.get("dataset")
        if dataset not in jobs:
            continue
        relation = _same_request(row, jobs[dataset])
        if relation:
            declarations.append({"dataset": dataset, "target_technical_id": relation["target_dataset"],
                                 "binding_sha256": relation["native_binding_sha256"], "new_original_or_consumption_proven": False})
    roots = ["liaoning/data/2026/original", "liaoning/verification/20261027/disclosure_timing_full_rows/retained_remote/data/2026/original",
             "liaoning/state/quality_repair_20261021/recovered_originals", "liaoning/exports/local_adaptation_20261020", "liaoning/exports/field_audit_20261020"]
    files = [path for root in roots for path in _files(reader, root, names={"metadata.json", "provenance.json", "observation.json"}, target_ids=ids)]
    own = _own_metadata(reader, files, ids)
    return {"fixed_own_missing_sources": len(rows), "fixed_without_own_or_typed_alias_chain": baseline["summary"]["still_without_own_or_alias_chain"],
            "index_observations": observations, "typed_alias_metadata_declarations": declarations,
            "typed_alias_declarations_do_not_add_independent_sources": True,
            "original_roots_inventoried": roots, "target_metadata_files": len(files), "new_own_observations": own,
            "new_business_qualifications": 0, "new_consumed_tasks": 0,
            "qualification_changes": 0, "phase4_own_missing_qualifications_preserved": True}


def _candidate_ids(baseline, inventory, works, *, indexes_unchanged=True):
    indexed = {(row["surface"], row["source_index"]): row for row in inventory["entries"]}
    results = []
    for old in baseline["candidates"]["entries"]:
        surface, index = old["surface"], old["source_index"]
        if not indexes_unchanged:
            results.append({"cohort_entry_sha256": old["legacy_entry_sha256"], "status": "phase4_index_drift_hold", "technical_ids": []})
            continue
        entry, work = indexed.get((surface, index), {}), works[surface][index]
        if not entry or entry.get("menu_path") != work.get("menu_path"):
            results.append({"cohort_entry_sha256": old["legacy_entry_sha256"], "status": "scope_index_changed_hold", "technical_ids": []})
            continue
        ids = sorted({value for value in (entry.get("sampled_dataset"), work.get("publication_dataset")) if isinstance(value, str) and value})
        results.append({"cohort_entry_sha256": old["legacy_entry_sha256"], "status": "metadata_link_only_candidate_still_not_admitted", "technical_ids": ids})
    return results


def _binding_match(reader, old, binding, reference):
    """Only a committed object plus exact source/parsed projection can upgrade a copy."""
    relative = lambda path: path if path.startswith("anhui/") else "anhui/" + path
    parsed = reader.json(relative(old["parsed_ref"]), old["parsed_sha256"])
    raw = reader.raw(relative(old["source_ref"]), old["source_sha256"])
    if parsed.get("binding_sha256") != old["precommitted_binding_sha256"] or parsed.get("source_sha256") != old["source_sha256"]:
        raise ValueError("binding_precommitment_source_link_changed")
    receipt = {"dataset": old["dataset"], "source_path": old["source_ref"].removeprefix("anhui/"),
               "period": parsed["query_period"], "selectors": parsed["selectors"]}
    validate_retained_binding(receipt, parsed, binding)
    if project_bound(json.loads(raw, parse_float=Decimal), binding["model"], period=receipt["period"]) != parsed["rows"]:
        raise ValueError("binding_exact_projection_failed")
    return {"reference": reader.ref(reference), "source_ref": reader.ref(relative(old["source_ref"])),
            "parsed_ref": reader.ref(relative(old["parsed_ref"])), "old_capture_remains_originally_mutable": True,
            "consumption_proven": False}


def _anhui(reader, baseline):
    old_refs = {row["path"]: row["sha256"] for row in baseline["references"]}
    index_paths = ["anhui/state/server_metric_coverage_inventory.json", "anhui/state/server_all_site_metric_worklist.json", "anhui/state/server_portal_metric_worklist.json"]
    observations = [_observe(reader, path, old_refs[path]) for path in index_paths]
    inventory = reader.json(index_paths[0])
    works = {surface: reader.json(path)["entries"] for surface, path in zip(("disclosure", "portal"), index_paths[1:])}
    candidates = _candidate_ids(baseline, inventory, works, indexes_unchanged=all(row["status"] == "unchanged_from_phase4" for row in observations))
    ids = {dataset for row in candidates for dataset in row["technical_ids"]}
    new_indices = ["anhui/verification/20261028/pending33_initial_inventory.json", "anhui/verification/20261028/pending33_binding_audit.json", "anhui/verification/20261028/pending33_failed_source_audit.json"]
    index_results = []
    for path in new_indices:
        if not reader.path(path).is_file():
            index_results.append({"path": path, "status": "not_locally_retained"})
            continue
        rows = reader.json(path)
        linked = sorted({row["dataset"] for row in rows if row.get("dataset") in ids})
        index_results.append({"reference": reader.ref(path), "rows": len(rows), "exact_candidate_technical_ids_matched": linked,
                              "new_qualifications": 0})
    # Enumerate binding-named business metadata first; no arbitrary state JSON.
    binding_paths = []
    for path in _files(reader, "anhui/state", name_pattern=re.compile(r"server_native_table_binding_[a-f0-9]{24}\.json")):
        binding_paths.append(path)
    commitments = {old["precommitted_binding_sha256"] for old in baseline["bindings"]["entries"]}
    matches, target_metadata = {}, []
    binding_ids = {old["dataset"] for old in baseline["bindings"]["entries"]}
    for path in binding_paths:
        value = reader.json(path)
        digest = sha(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode())
        raw_digest = reader.ref(path)["sha256"]
        if digest in commitments or raw_digest in commitments:
            matches.setdefault(digest if digest in commitments else raw_digest, []).append((path, value))
        if value.get("dataset") in binding_ids:
            target_metadata.append({"dataset": value["dataset"], "reference": reader.ref(path), "canonical_sha256": digest,
                                    "matches_any_precommitment": digest in commitments or raw_digest in commitments})
    bindings = []
    for old in baseline["bindings"]["entries"]:
        proofs = []
        for path, value in matches.get(old["precommitted_binding_sha256"], []):
            try:
                proofs.append(_binding_match(reader, old, value, path))
            except (ValueError, KeyError, TypeError, Fault):
                continue
        bindings.append({"dataset": old["dataset"], "capture_sha256": old["source_sha256"],
                         "precommitted_binding_sha256": old["precommitted_binding_sha256"], "exact_copy_proofs": proofs,
                         "qualification": "new_exact_copy_evidence_only" if proofs else "exact_precommitted_copy_still_missing",
                         "original_capture_history_rewritten": False, "dispatch_allowed": False})
    return {"fixed_binding_captures": len(bindings), "binding_named_metadata_checked": len(binding_paths),
            "exact_binding_copies_newly_proven": sum(bool(row["exact_copy_proofs"]) for row in bindings), "bindings": bindings,
            "target_dataset_mutable_binding_metadata": target_metadata, "fixed_candidates": len(candidates),
            "candidate_index_observations": observations, "candidate_metadata": candidates,
            "candidates_with_explicit_technical_id": sum(bool(row["technical_ids"]) for row in candidates),
            "new_existing_pending_indexes": index_results, "candidate_admission_changes": 0,
            "new_business_qualifications": 0, "new_consumed_tasks": 0}


def _bound_receipt(reader, dataset, receipt):
    declared = receipt.get("dataset", receipt.get("dataset_candidate", receipt.get("id")))
    path = receipt.get("sample_path") or receipt.get("path")
    expected = receipt.get("sha256")
    if declared != dataset or not isinstance(path, str) or not path.startswith(("state/", "data/", "verification/")) or not re.fullmatch(r"[a-f0-9]{64}", expected or ""):
        return {"status": "metadata_only_no_exact_original_receipt"}
    reference = "shanxi/" + path
    if not reader.path(reference).is_file():
        return {"path": reference, "expected_sha256": expected, "status": "bound_original_path_still_missing"}
    raw = reader.raw(reference, expected)
    if type(receipt.get("size")) is not int or len(raw) != receipt["size"] or receipt.get("http_status") != 200:
        return {"reference": reader.ref(reference), "status": "receipt_size_or_http_unverified_hold"}
    context = {key: receipt.get(key) for key in ("selected_period", "period", "kind", "request", "step") if key in receipt}
    if not context:
        return {"reference": reader.ref(reference), "status": "receipt_context_missing_hold"}
    try:
        result = evaluate({"kind": "json"}, Response(200, receipt.get("mime", "application/json"), raw))
        status = "existing_empty_response_only" if result.result == "empty" else "existing_nonempty_sample_pending_semantics"
    except Fault:
        status = "existing_refusal_or_error_hold"
    return {"reference": reader.ref(reference), "context_sha256": sha(canonical(context)), "status": status,
            "consumption_proven": False, "dispatch_allowed": False}


def _shanxi(reader, baseline):
    old = {row["path"]: row["sha256"] for row in baseline["input_refs"]}
    names = ["dataset_catalog.json", "verification/20261027/disclosure_timing/findings.json", "verification/20261019/channel_forecast_empty_selector_evidence.json",
             "verification/20261019/discovered_special_form_review.json"]
    observations = [_observe(reader, "shanxi/" + path, old[path]) for path in names]
    catalog = reader.json("shanxi/dataset_catalog.json")
    targets = {row["id"]: row for row in catalog["targets"]}
    remaining = []
    for row in baseline["remaining_sources"]:
        dataset = row["dataset"]
        entry = targets.get(dataset, {})
        receipts = [_bound_receipt(reader, dataset, entry[key]) for key in ("query_evidence", "special_query_evidence", "selector_evidence") if isinstance(entry.get(key), dict)]
        remaining.append({"dataset": dataset, "phase4_group": row["phase4_evidence_group"], "receipt_observations": receipts,
                          "phase4_qualification_changed": False, "dispatch_allowed": False})
    boundaries, seen = [], {}
    for row in baseline["phase3_boundary_rechecks"]:
        refs = []
        for item in row["rechecked_receipts"]:
            for name in ("receipt", "original"):
                ref = item[name]
                key = "shanxi/" + ref["path"]
                if key not in seen:
                    seen[key] = _observe(reader, key, ref["sha256"])
                refs.append(seen[key])
        alias = row.get("alias_evidence")
        if alias:
            ref = alias["reference"]
            key = "shanxi/" + ref["path"]
            if key not in seen:
                seen[key] = _observe(reader, key, ref["sha256"])
            refs.append(seen[key])
        boundaries.append({"dataset": row["dataset"], "phase4_class": row["phase3_class"],
                           "all_receipt_original_links_unchanged": bool(refs) and all(r["status"] == "unchanged_from_phase4" for r in refs),
                           "link_check": "checked_references" if refs else "not_applicable_no_bound_reference",
                           "link_set_sha256": sha(canonical(refs)), "dispatch_allowed": False})
    return {"fixed_missing_business_originals": len(remaining), "remaining_sources": remaining, "index_observations": observations,
            "boundary_sources": boundaries, "boundary_count": len(boundaries), "refusal_sources_preserved": baseline["phase3_refusal_evidence_sources_retained"],
            "historical_refusals_preserved": baseline["phase3_historical_denials_retained"],
            "unknown_set_preserved": baseline["historical_identity_search"]["phase3_unknown_set_preserved"],
            "embedded_empty_selector_does_not_restore_missing_path": True,
            "new_business_qualifications": 0, "new_consumed_tasks": 0, "qualification_changes": 0}


def recover(workspace, *, provinces=None):
    selected = PROVINCES if provinces is None else tuple(provinces) if not isinstance(provinces, str) else ()
    if not selected or len(selected) != len(set(selected)) or any(p not in PROVINCES for p in selected):
        raise ValueError("explicit_unique_followup_province_subset_required")
    reader = Reader(workspace)
    results, baselines = {}, []
    for province in selected:
        baseline, reference = _baseline(reader, province)
        baselines.append(reference)
        results[province] = {"liaoning": _liaoning, "anhui": _anhui, "shanxi": _shanxi}[province](reader, baseline)
    reader.finish()
    return {"mode": "phase5_index_led_read_only_evidence_followup", "provinces": list(selected), "baseline_refs": baselines,
            "summary": {"provinces_checked": len(selected), "new_business_qualifications": 0,
                        "new_consumed_tasks": 0, "production_writes": 0, "website_requests": 0,
                        **({"liaoning_own_missing_cohort": results["liaoning"]["fixed_own_missing_sources"],
                            "liaoning_without_own_or_alias_chain_cohort": results["liaoning"]["fixed_without_own_or_typed_alias_chain"]} if "liaoning" in results else {}),
                        **({"anhui_binding_capture_cohort": results["anhui"]["fixed_binding_captures"],
                            "anhui_exact_binding_copies_newly_proven": results["anhui"]["exact_binding_copies_newly_proven"],
                            "anhui_unadmitted_candidate_cohort": results["anhui"]["fixed_candidates"]} if "anhui" in results else {}),
                        **({"shanxi_missing_business_original_cohort": results["shanxi"]["fixed_missing_business_originals"],
                            "shanxi_boundary_sources_checked": results["shanxi"]["boundary_count"]} if "shanxi" in results else {})},
            "results": results, "input_refs": list(reader.refs.values()), "active_sqlite_connections": 0,
            "production_writes": 0, "website_requests": 0, "dispatch_allowed": False,
            "limitations": ["Fixed phase-four cohorts are comparison units, not current production totals.",
                "Original, typed alias, empty response, refusal, metadata and missing-path evidence remain separate.",
                "Newly retained local evidence never establishes historical consumption or clears any refusal/unknown.",
                "Same titles or same empty bytes never establish source identity.",
                "No full sample replay, archive extraction, database connection, browser or network operation is performed."]}
