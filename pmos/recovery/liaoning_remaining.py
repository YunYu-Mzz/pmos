"""Further local Liaoning research against the immutable phase3 remainder.

Only declared business receipts, original metadata and historical JSON queues
are read. No crawler import, SQLite connection, extraction, request or source
write occurs. Same-request source aliases are distinct from own originals and
from old queue identity recovery. Native parameters never enter the report.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
import io
import re
import tarfile

from ..contracts import canonical
from .liaoning import (_Reader, _business_files, _expanded_key, _identity,
                       _rules, _sample, _sample_jobs, _sha, baseline_queue_records,
                       EXPORTED, FAMILIES, EXTRA_SNAPSHOTS, MANIFEST)

PHASE3 = "pmos/evidence/liaoning_phase3_recovery.json"
PHASE3_SHA256 = "433ec2090a1741bda88534b12212db0f89d79f065831b1485aa0c61bef0c5eaf"
CATALOG = "state/sitewide_expansion_20261016/catalog_current.json"
COVERAGE = "state/local_adaptation_20261020/native_coverage.json"
CHECKPOINT = ("reporting/diagnostics_20261028/resource_wait/checkpoints_before/"
              "state/server_verified_expansion_20261023/queue.json")
SNAPSHOTS = tuple(dict.fromkeys((*EXTRA_SNAPSHOTS, CHECKPOINT)))
RECEIPTS = ("state/quality_supplement_20261021/queue.json",
            "state/dimension_coverage_20261024/reused_sample_outcomes.json")
ARCHIVES = ("exports/local_adaptation_20261020/verified_adapters.tar.gz",
            "state/aliyun_deployment_20261023/release.tar.gz",
            "verification/20261027/disclosure_timing_full_rows/retained_export.tar.gz")
NEGATIVE_RESULTS = (
    "state/local_adaptation_20261020/responses/sitewide_6c6bd17628fc29f7_engine_mapping/result.json",
    "state/local_adaptation_20261020/responses/sitewide_a64ef9a87d10919c_table_data/result.json",
    "state/local_adaptation_20261020/responses/sitewide_a64ef9a87d10919c_table_filters/result.json",
    "state/local_adaptation_20261020/responses/sitewide_a64ef9a87d10919c_table_period/result.json",
)
DICTIONARY = ("state/continuous_expansion_20261019/samples/sitewide_7a300d985f7a8cfc/"
              "ea24e6b71120c0e66f25bd3bd6645a3383ed089a8a198e26a8d4d99517ab0755/observation.json")
PRIVATE_MEMBER = re.compile(r"(?i)private|profile|config|auth|cookie|token|credential|session|browser|\.har$|__pycache__")
DATASET = re.compile(r"[A-Za-z0-9_]{1,120}\Z")


def _phase3(reader):
    value, digest = reader.json(reader.workspace / PHASE3)
    if digest != PHASE3_SHA256:
        raise ValueError("immutable_phase3_cohort_sha256_changed")
    sources = {row["dataset"]: row for row in value["source_evidence"]["records"]
               if row["status"] == "evidence_still_missing"}
    records = {}
    for view in value["queue_evidence"]["source_views"]:
        for row in view["records"]:
            kind = value["queue_evidence"]["record_kinds"][row["record_kind"]]
            if kind["status"] == "evidence_still_missing":
                records[row["legacy_key_sha256"]] = {**row, "source_ref": view["source_ref"],
                    "source_sha256": view["sha256"]}
    if len(sources) != 51 or len(records) != 11:
        raise ValueError("immutable_phase3_remaining_cohort_shape_changed")
    return value, sources, records


def _same_request(row, catalog):
    """A recorded alias, source/route and native number must all agree."""
    dataset, target, number = row.get("dataset"), row.get("prior_dataset"), row.get("number")
    if (not isinstance(dataset, str) or not isinstance(target, str) or not DATASET.fullmatch(target)
            or not isinstance(number, str) or not re.fullmatch(r"\d{3}", number)):
        return None
    if (catalog.get("dataset") != dataset or catalog.get("number") != number
            or catalog.get("phase") not in {"existing_workflow", "alias_of_same_request"}
            or row.get("state") not in {"prior_verified_report_sample", "prior_verified_request_alias"}
            or (row.get("source") != catalog.get("source_id") and row.get("source") not in catalog.get("aliases", []))
            or row.get("route") not in catalog.get("routes", [])):
        return None
    if row["state"] == "prior_verified_report_sample" and row.get("reuse_basis") != "existing_workflow_same_native_report_number":
        return None
    return {"dataset": dataset, "target_dataset": target, "native_report_number": number,
            "native_endpoint_sha256": _sha(("/px-dif-settlement-infpubquery/pdblnReportController/pdblnReport" + number).encode()),
            "native_binding_sha256": _sha(canonical({"source_id": row["source"],
                "route": row["route"], "number": number, "target_dataset": target}))}


def _alias_samples(binding, samples):
    endpoint = "/px-dif-settlement-infpubquery/pdblnReportController/pdblnReport" + binding["native_report_number"]
    return [sample for sample in samples if sample.dataset == binding["target_dataset"]
            and sample.metadata.get("endpoint_path") == endpoint
            and sample.replay_status == "sample_only_passed"]


def _cohort_tasks(reader, expected):
    """Re-identify old keys in the unchanged baseline, never a new checkpoint."""
    result = {}
    for source_ref, rows in baseline_queue_records(reader.root).items():
        selected = []
        for task in rows:
            digest = _sha(task.legacy_key.encode())
            row = expected.get(digest)
            if row is None or source_ref != str(reader.workspace / row["source_ref"]):
                continue
            if (row["dataset"], row["state"], row["consumed"], row["facts_sha256"]) != (
                    task.dataset, task.state, task.consumed, task.facts_sha256):
                raise ValueError("phase3_remaining_old_key_or_facts_changed")
            source_path = reader.workspace / row["source_ref"].split("#", 1)[0]
            if _sha(reader.raw(source_path)) != row["source_sha256"]:
                raise ValueError("phase3_remaining_source_snapshot_sha256_changed")
            selected.append(task)
        if selected:
            result[source_ref] = selected
    if sum(map(len, result.values())) != len(expected):
        raise ValueError("phase3_remaining_old_key_not_retained")
    return result


def _queue_candidates(value):
    """Only business job/identity containers; no recursive arbitrary JSON walk."""
    groups = value.get("sources", value.get("datasets", {}))
    if not isinstance(groups, dict):
        raise ValueError("declared_business_queue_groups_required")
    active = value.get("active") or {}
    active = active.get("job", active)
    candidates = []
    for dataset, group in groups.items():
        jobs = group.get("tasks")
        if jobs is None:
            jobs = {job["key"]: job for job in group.get("pending", [])}
        if not isinstance(jobs, dict) or not isinstance(group.get("outcomes", {}), dict):
            raise ValueError("declared_business_queue_shape_changed")
        if active.get("dataset") == dataset and active.get("key"):
            jobs = {**jobs, active["key"]: active}
        for key, job in jobs.items():
            outcome = group.get("outcomes", {}).get(key, {})
            candidates.append((dataset, key, job, outcome if isinstance(outcome, dict) else {}))
    return candidates, groups


def _confirmed_job(dataset, key, job, rule):
    if job.get("dataset") not in (None, dataset) or job.get("key", key) != key:
        return None
    params = job.get("params")
    if "snapshot_date" in job:
        anchor, page = job["snapshot_date"], job.get("page")
        if (not isinstance(anchor, str) or not anchor or type(page) is not int or page < 1
                or not isinstance(params, dict) or type(params.get("page")) is not int
                or params != {"page": page, "snapshot_date": anchor}
                or job.get("period", anchor) != anchor
                or key != dataset + ":snapshot:" + _sha(canonical(params))):
            return None
        identity, _ = _identity(dataset, job, None, "snapshot")
        return identity
    if not rule:
        return None
    field, selection = rule["period_field"], rule.get("selection_field")
    if (not isinstance(params, dict) or set(params) != {field} | ({selection} if selection else set())
            or job.get("period") != params[field]):
        return None
    native = {"period": params[field]}
    if selection:
        native["selection"] = params[selection]
    if _expanded_key(dataset, native) != key:
        return None
    identity, _ = _identity(dataset, job, rule, rule["period_kind"])
    return identity


def _exact_bridge(dataset, key, job, body_sha256, expected_body_sha256, rule):
    if (not isinstance(expected_body_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_body_sha256)
            or body_sha256 != expected_body_sha256):
        return None
    return _confirmed_job(dataset, key, job, rule)


def _archive_inventory(reader, relative, targets):
    path = reader.root / relative
    raw = reader.raw(relative)
    count = private = total = 0
    hits = []
    # List bounded members first; never extract or read archive member payloads.
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
        for member in archive:
            count += 1
            if count > 4096 or member.size < 0 or member.size > 64 * 1024 * 1024:
                raise ValueError("bounded_archive_members_required")
            total += member.size
            if total > 128 * 1024 * 1024:
                raise ValueError("bounded_archive_declared_size_required")
            name = member.name
            if (name.startswith("/") or ".." in name.split("/") or "\\" in name
                    or member.issym() or member.islnk() or PRIVATE_MEMBER.search(name)):
                private += 1
                continue
            if member.isfile():
                matches = [dataset for dataset in targets if dataset in name.split("/")
                           or any(part == dataset or part.startswith(dataset + "_") for part in name.split("/"))]
                if matches:
                    hits.append({"member_path": name, "datasets": sorted(matches), "declared_bytes": member.size})
    return {"archive_ref": reader.ref(path), "member_count": count,
            "excluded_unsafe_or_private_members": private, "target_member_matches": hits,
            "archive_members_extracted": 0, "archive_member_payloads_read": 0}


def _negative_evidence(reader, sources):
    result = defaultdict(list)
    for relative in NEGATIVE_RESULTS:
        if not (reader.root / relative).is_file():
            continue
        value, _ = reader.json(relative)
        dataset = next((dataset for dataset in sources if any(
            part == dataset or part.startswith(dataset + "_") for part in relative.split("/"))), None)
        if not dataset:
            continue
        response_file = value.get("response_file")
        response_ref = None
        response_sha_matches = False
        if isinstance(response_file, str) and (reader.root / response_file).is_file():
            raw = reader.raw(response_file)
            response_sha_matches = _sha(raw) == value.get("sha256")
            response_ref = reader.ref(response_file)
        stage = "data_denied" if value.get("outcome") == "denied" else "non_data_filter_or_period_stage_only"
        result[dataset].append({"receipt_ref": reader.ref(relative), "response_ref": response_ref,
                                "response_sha_matches_declared_body": response_sha_matches,
                                "declared_raw_body_persisted": value.get("raw_body_persisted"),
                                "finding": stage, "qualifies_as_business_original": False})
    if (reader.root / DICTIONARY).is_file():
        value, _ = reader.json(DICTIONARY)
        dataset = value.get("dataset")
        if dataset in sources:
            path = (reader.root / DICTIONARY).with_name("response.json")
            raw = reader.raw(path)
            if _sha(raw) != value.get("body_sha256"):
                raise ValueError("dictionary_original_sha256_changed")
            if value.get("stage") == "dictionary" and value.get("data_report_permission_verified") is False:
                result[dataset].append({"receipt_ref": reader.ref(DICTIONARY), "response_ref": reader.ref(path),
                    "finding": "dictionary_only_data_permission_explicitly_unverified",
                    "qualifies_as_business_original": False})
    return result


def _run(root):
    reader = _Reader(root)
    _, sources, old_records = _phase3(reader)
    tasks = _cohort_tasks(reader, old_records)
    rules = _rules(reader)
    catalog_doc, _ = reader.json(CATALOG)
    catalog = {row["dataset"]: row for row in catalog_doc["jobs"]}
    coverage_doc, _ = reader.json(COVERAGE)
    coverage = {row["dataset"]: row for row in coverage_doc["sources"] if row.get("dataset") in sources}
    bindings = {dataset: binding for dataset, row in coverage.items()
                if (binding := _same_request(row, catalog.get(dataset, {}))) is not None}
    targets = set(sources) | {binding["target_dataset"] for binding in bindings.values()} | {
        row["dataset"] for row in old_records.values()}
    samples, original_inventory, rejected = [], [], []
    for prefix in (EXPORTED, "data/2026/original"):
        for family in FAMILIES:
            total = selected = 0
            base = reader.root / prefix / family
            for path in _business_files(base, "metadata.json"):
                total += 1
                if not (targets & set(path.parts)):
                    continue
                selected += 1
                try:
                    samples.append(_sample(reader, path))
                except (ValueError, TypeError, KeyError) as exc:
                    rejected.append({"metadata_ref": reader.ref(path), "reason": str(exc)})
            original_inventory.append({"path": "liaoning/" + prefix + "/" + family,
                                       "metadata_files_listed": total, "target_metadata_files_read": selected})
    negative = _negative_evidence(reader, sources)
    source_rows = []
    for dataset in sorted(sources):
        own = [sample for sample in samples if sample.dataset == dataset and sample.replay_status == "sample_only_passed"]
        binding = bindings.get(dataset)
        aliases = _alias_samples(binding, samples) if binding else []
        row = {"dataset": dataset, "source_own_business_original_found": bool(own),
               "exact_same_request_target_chain_found": bool(aliases),
               "business_acceptance": "pending", "full_scope_acceptance": "pending",
               "online_eligibility_inherited": False, "source_terminal_state_changed": False,
               "recorded_state": coverage.get(dataset, {}).get("state", "not_in_native_coverage_snapshot"),
               "source_binding_refs": [reader.ref(CATALOG), reader.ref(COVERAGE)],
               "negative_evidence": negative.get(dataset, []),
               "own_original_evidence": [sample.public() for sample in own],
               "alias_target_evidence": [sample.public() for sample in aliases]}
        if binding:
            row["same_request_binding"] = binding
        row["remaining_reason"] = (None if own else "source_directory_has_no_own_sha_bound_business_original")
        if aliases:
            row["qualification"] = "same_native_request_alias_with_target_original_only"
        elif own:
            row["qualification"] = "own_original_sample_only"
        else:
            row["qualification"] = "exact_original_chain_still_missing"
        source_rows.append(row)

    # Every queue lookup begins with the frozen old key and body SHA. A source
    # alias or a later checkpoint outcome is never enough to construct a job.
    by_old_key = {task.legacy_key: task for rows in tasks.values() for task in rows}
    old_outcomes = {}
    for source_ref in sorted({row["source_ref"] for row in old_records.values()}):
        path = reader.workspace / source_ref
        value, _ = reader.json(path)
        _, groups = _queue_candidates(value)
        for key, task in by_old_key.items():
            if task.dataset in groups:
                outcome = groups[task.dataset].get("outcomes", {}).get(key)
                if isinstance(outcome, dict):
                    old_outcomes[key] = outcome
    if len(old_outcomes) != len(old_records):
        raise ValueError("phase3_remaining_old_outcome_not_retained")
    bridges, searched = defaultdict(list), []
    originals = _sample_jobs(samples, rules)
    old_datasets = {task.dataset for task in by_old_key.values()}
    key_protocols = Counter("expanded" if key.startswith("liaoning:2026:expanded:" + task.dataset + ":")
                            else "native_list_snapshot" if key.startswith(task.dataset + ":snapshot:") else "other"
                            for key, task in by_old_key.items())
    original_protocols = Counter("native_list_snapshot" if key.startswith(dataset + ":snapshot:") else "expanded"
                                for dataset, key, _ in originals if dataset in old_datasets)
    for key, task in by_old_key.items():
        digest = old_outcomes[key].get("sha256", old_outcomes[key].get("raw_sha256"))
        for job, sample in originals.get((task.dataset, key, digest), []):
            identity = _confirmed_job(task.dataset, key, job, rules.get(task.dataset))
            if identity:
                bridges[key].append((identity, [sample.metadata_ref, sample.response_ref]))
    for relative in SNAPSHOTS:
        path = reader.root / relative
        if not path.is_file():
            searched.append({"path": "liaoning/" + relative, "locally_retained": False})
            continue
        value, _ = reader.json(relative)
        candidates, groups = _queue_candidates(value)
        stats = Counter()
        for dataset, key, job, outcome in candidates:
            task = by_old_key.get(key)
            if task is None or task.dataset != dataset:
                continue
            stats["exact_old_key_jobs"] += 1
            expected = old_outcomes[key].get("sha256", old_outcomes[key].get("raw_sha256"))
            actual = outcome.get("sha256", outcome.get("raw_sha256"))
            identity = _exact_bridge(dataset, key, job, actual, expected, rules.get(dataset))
            if identity:
                bridges[key].append((identity, [reader.ref(relative)]))
                stats["exact_key_query_and_body_sha_bridges"] += 1
        stats["exact_old_key_outcomes_only"] = sum(key in groups.get(task.dataset, {}).get("outcomes", {})
                                                       for key, task in by_old_key.items())
        searched.append({"snapshot_ref": reader.ref(relative), "locally_retained": True,
                         "dataset_groups": len(groups), "counts": dict(stats),
                         "new_snapshot_records_added_to_cohort": 0})
    receipt_stats = []
    for relative in RECEIPTS:
        value, _ = reader.json(relative)
        candidates = []
        if relative.endswith("reused_sample_outcomes.json"):
            for key, receipt in value["outcomes"].items():
                candidates.append((key, receipt.get("dataset"), receipt, receipt.get("sha256")))
        else:
            for job in value["tasks"]:
                dataset, rule = job.get("dataset"), rules.get(job.get("dataset"))
                params = job.get("params", {})
                if not rule or rule["period_field"] not in params:
                    continue
                native = {"period": params[rule["period_field"]]}
                if rule.get("selection_field"):
                    native["selection"] = params.get(rule["selection_field"])
                key = _expanded_key(dataset, native)
                job = {**job, "period": native["period"]}
                candidates.append((key, dataset, job, (job.get("result") or {}).get("sha256")))
        stats = Counter()
        for key, dataset, job, digest in candidates:
            task = by_old_key.get(key)
            if task is None or task.dataset != dataset:
                continue
            stats["exact_old_key_jobs"] += 1
            expected = old_outcomes[key].get("sha256", old_outcomes[key].get("raw_sha256"))
            identity = _exact_bridge(dataset, key, job, digest, expected, rules.get(dataset))
            if identity:
                bridges[key].append((identity, [reader.ref(relative)]))
                stats["exact_key_query_and_body_sha_bridges"] += 1
        receipt_stats.append({"receipt_ref": reader.ref(relative), "business_records_examined": len(candidates),
                              "counts": dict(stats)})
    queue_rows = []
    resolved = {}
    for source_ref, rows in tasks.items():
        resolved[source_ref] = []
        for task in rows:
            candidates = bridges.get(task.legacy_key, [])
            identities = {identity.key for identity, _ in candidates}
            after = task
            if len(identities) == 1:
                after = replace(task, identity=candidates[0][0], blockers=tuple(blocker for blocker in task.blockers
                    if blocker not in {"identity_mapping_required", "source_parameter_identity_mapping_required"}))
            resolved[source_ref].append(after)
            expected = old_records[_sha(task.legacy_key.encode())]
            queue_rows.append({"legacy_key_sha256": _sha(task.legacy_key.encode()), "dataset": task.dataset,
                "state": task.state, "consumed": task.consumed, "facts_sha256": task.facts_sha256,
                "old_outcome_body_sha256": old_outcomes[task.legacy_key].get("sha256", old_outcomes[task.legacy_key].get("raw_sha256")),
                "identity_key_sha256": after.identity.key if after.identity else None,
                "status": "exact_bridge_recovered" if after.identity else "exact_bridge_still_missing",
                "reason": None if after.identity else "no_retained_exact_old_key_query_and_body_sha256_bridge",
                "source_snapshot_ref": {"path": expected["source_ref"], "sha256": expected["source_sha256"]},
                "evidence_refs": [ref for _, refs in candidates for ref in refs],
                "remaining_blockers": list(after.blockers), "dispatch_allowed": False})
    archives = [_archive_inventory(reader, relative, set(sources)) for relative in ARCHIVES]
    changed = reader.changed()
    if changed:
        raise ValueError("business_inputs_changed_during_remaining_recovery")
    report = {"schema": "pmos-liaoning-phase4-remaining-v1", "province": "liaoning",
        "mode": "read_only_frozen_phase3_remainder", "phase3_ref": reader.ref(reader.workspace / PHASE3),
        "website_requests": 0, "production_writes": 0, "active_sqlite_connections": 0,
        "archive_members_extracted": 0, "dispatch_allowed": False,
        "source_evidence": {"phase3_remaining_cohort": len(sources),
            "source_own_originals_recovered": sum(row["source_own_business_original_found"] for row in source_rows),
            "exact_same_request_alias_target_chains": sum(row["exact_same_request_target_chain_found"] for row in source_rows),
            "still_without_exact_own_or_alias_chain": sum(not row["source_own_business_original_found"] and not row["exact_same_request_target_chain_found"] for row in source_rows),
            "source_directories_still_without_own_originals": sum(not row["source_own_business_original_found"] for row in source_rows),
            "not_new_independent_originals_or_metrics": True, "records": source_rows},
        "queue_evidence": {"phase3_remaining_cohort": len(old_records),
            "cohort_old_key_protocols": dict(key_protocols),
            "original_metadata_jobs_for_cohort_datasets_by_protocol": dict(original_protocols),
            "supported_bridge_protocols": ["expanded", "native_list_snapshot"],
            "exact_bridges_recovered": sum(row["identity_key_sha256"] is not None for row in queue_rows),
            "exact_bridges_still_missing": sum(row["identity_key_sha256"] is None for row in queue_rows),
            "records": queue_rows, "snapshots_examined": searched, "receipts_examined": receipt_stats,
            "source_aliases_do_not_upgrade_old_queue_records": True},
        "original_metadata_inventory": original_inventory, "archive_inventory": archives,
        "rejected_original_chains": rejected,
        "input_refs": [{"path": path, "sha256": digest} for path, digest in sorted(reader.refs.items())],
        "inputs_changed_during_scan": changed, "rolling_read_not_frozen": True,
        "limitations": ["same-request aliases reuse an existing target original and do not grant source permission or acceptance",
            "dictionary/filter/period responses and refused data endpoints remain negative evidence",
            "new checkpoints and original captures cannot replace an old key/query/body-SHA bridge",
            "old states, consumed facts, holds and source files are unchanged"]}
    report["summary"] = {"source_cohort": len(sources),
        "source_own_originals_recovered": report["source_evidence"]["source_own_originals_recovered"],
        "same_request_alias_target_chains": report["source_evidence"]["exact_same_request_alias_target_chains"],
        "still_without_own_or_alias_chain": report["source_evidence"]["still_without_exact_own_or_alias_chain"],
        "source_own_originals_still_missing": report["source_evidence"]["source_directories_still_without_own_originals"],
        "queue_cohort": len(old_records),
        "queue_bridges_recovered": report["queue_evidence"]["exact_bridges_recovered"],
        "queue_bridges_still_missing": report["queue_evidence"]["exact_bridges_still_missing"]}
    return report, resolved


def recover(root):
    """Return only redacted paths, technical IDs, hashes, states and counts."""
    return _run(root)[0]


def resolve_queue_records(root):
    """RAM-only frozen remainder; original state/consumed/facts/holds survive."""
    report, resolved = _run(root)
    if report["inputs_changed_during_scan"]:
        raise ValueError("business_inputs_changed_during_remaining_recovery")
    return resolved
