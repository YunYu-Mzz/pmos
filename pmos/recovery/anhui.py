"""Read-only recovery of the *phase-two* Anhui evidence cohort.

This is a local evidence resolver, not a producer, importer or queue repairer.
Only declared business documents are opened. Original selectors and article
IDs remain in RAM; ``recover`` emits hashes and references only.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from ..adapters.anhui import DATES, project_bound, validate_retained_binding
from ..contracts import TaskIdentity, canonical
from ..faults import Fault
from ..queue_compat import LegacyTask, QueueContext, decode_jobs


MAX_BYTES = 64 * 1024 * 1024
FORBIDDEN = re.compile(r"(?i)private|profile|credential|password|cookie|token|authorization|browser|session|(?:^|_)config|(?:^|_)auth")
# These exact source revisions were inspected, never imported or executed.
REVIEWED_RULES = {
    "powercrawler/production_plan.py": "d67090fa9835c2fe0035f26c2f688d507e796c51f61a62594a69a430e7ec552c",
    "powercrawler/native_rollout.py": "f7d71474d9e4359e3af123c75012f6f536863e918a66e475bf8b2a1b13acf8b8",
    "powercrawler/period_reports.py": "c9faa8b8cb54708262399d59cedb203d03b0c47225ad7827ea4347acc6b2486b",
    "powercrawler/publications.py": "8dfd9c51054b72d2d02020b08878fd6ddb267e26c0fd2cc76c245557be96e420",
    "powercrawler/native_publications.py": "6b0bf17b966d94f188bed1e1d000b28becb4e68841f2120a4ddb70dd9bfb0e14",
}
CARRIERS = {
    "publication_e0693cbeead18de2", "publication_c1add81ba5c2f3e5",
    "publication_32101a70fcd45761", "portal_contract_summary",
    "interregional_exchange_monthly", "market_disclosure_monthly",
    "portal_historical_contracts",
}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _article_key(title, published):
    # Exact historical PublicationRepository.enqueue encoding, not canonical().
    return _sha(json.dumps([title, published], ensure_ascii=False).encode())[:24]


class _Reader:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.refs = {}
        self.drifts = []
        self.changed_paths = set()

    def raw(self, relative, expected=None):
        rel = Path(relative)
        if (rel.is_absolute() or ".." in rel.parts or not rel.parts
                or any(FORBIDDEN.search(part) for part in rel.parts)):
            raise ValueError("declared_public_business_path_required")
        path = self.root / rel
        if (not path.resolve().is_relative_to(self.root)
                or any((self.root.joinpath(*rel.parts[:i])).is_symlink() for i in range(1, len(rel.parts) + 1))
                or not path.is_file() or path.stat().st_size > MAX_BYTES):
            raise ValueError("bounded_regular_business_file_required")
        raw = path.read_bytes()
        digest = _sha(raw)
        reference = str(Path(self.root.name) / rel)
        first = self.refs.get(reference)
        if first is not None and first["sha256"] != digest:
            self.changed_paths.add(reference)
            self.drifts.append({"path": reference, "expected_sha256": first["sha256"],
                                "observed_sha256": digest, "reason": "changed_during_read"})
            raise ValueError("recovery_inputs_changed_during_read")
        self.refs.setdefault(reference, {"path": reference, "sha256": digest})
        if expected is not None and digest != expected:
            self.drifts.append({"path": reference, "expected_sha256": expected, "observed_sha256": digest})
            raise ValueError("baseline_or_reference_sha256_changed")
        return raw

    def json(self, relative, expected=None):
        return json.loads(self.raw(relative, expected))

    def ref(self, relative):
        return self.refs[str(Path(self.root.name) / relative)]

    def recheck(self):
        changed = set(self.changed_paths)
        for reference, item in list(self.refs.items()):
            relative = str(Path(reference).relative_to(self.root.name))
            try:
                self.raw(relative, item["sha256"])
            except (ValueError, OSError):
                changed.add(reference)
        return sorted(changed)


def _baseline(root, name, supplied=None):
    if supplied is not None:
        return supplied, None
    path = root.parent / "pmos" / "evidence" / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("phase_two_baseline_required")
    raw = path.read_bytes()
    return json.loads(raw), {"path": str(Path("pmos/evidence") / name), "sha256": _sha(raw)}


def _relative(root, source):
    # Phase two used absolute queue paths, and repository-relative artifact refs.
    path = Path(source)
    if path.is_absolute():
        return str(path.relative_to(root))
    if path.parts and path.parts[0] == root.name:
        return str(path.relative_to(root.name))
    return str(path)


def _identity(dataset, period, kind, dimensions=None, *, page=None, operation="query"):
    task = TaskIdentity("anhui", "offline-shadow-review", dataset,
                        {"kind": kind, "label": str(period)}, dimensions or {}, page=page, operation=operation)
    task.document()
    return task


def _receipt(reader, receipt):
    raw = reader.raw(receipt["source_path"], receipt["source_sha256"])
    parsed = reader.json(receipt["parsed_path"], receipt["parsed_sha256"])
    if (parsed.get("source_path") != receipt["source_path"]
            or parsed.get("source_sha256") != receipt["source_sha256"]
            or parsed.get("dataset") != receipt.get("dataset")
            or parsed.get("query_period") != receipt.get("period")
            or parsed.get("selectors") != receipt.get("selectors")):
        raise ValueError("exact_parsed_receipt_link_required")
    return raw, parsed, [reader.ref(receipt["source_path"]), reader.ref(receipt["parsed_path"])]


def _listing_index(reader, dataset):
    relative = f"state/server_native_listing_sources_{dataset.lower()}.json"
    if not (reader.root / relative).is_file():
        return {}
    markers = reader.json(relative)
    index = {}
    for marker in markers.values():
        raw = reader.raw(marker["source_path"], marker["source_sha256"])
        parsed = reader.json(marker["parsed_path"], marker["parsed_sha256"])
        payload = json.loads(raw)
        data = payload.get("data", {})
        rows = data.get("list")
        if (parsed.get("dataset") != dataset or parsed.get("source_path") != marker["source_path"]
                or parsed.get("source_sha256") != marker["source_sha256"]
                or not parsed.get("all_ui_listing_rows_matched")
                or not parsed.get("publication_date_is_not_statistical_period")
                or not isinstance(rows, list) or len(rows) != marker["source_rows"]
                or str(data.get("pageNum")) != str(marker["page"])):
            raise ValueError("exact_listing_source_link_required")
        field = parsed.get("publication_date_field")
        if field not in {"validDate", "operateDate"}:
            raise ValueError("reviewed_listing_date_field_required")
        shown = {(article["title"], article["publication_date"]) for article in parsed["articles"]}
        for row in rows:
            value = row.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError("listing_date_field_invalid")
            published = datetime.fromtimestamp(value / 1000, ZoneInfo("Asia/Shanghai")).date().isoformat()
            title, guid, information = row.get("title"), row.get("guid"), row.get("informationId")
            if (not isinstance(title, str) or (title, published) not in shown
                    or not isinstance(guid, str) or not guid or not isinstance(information, str) or not information):
                continue
            key = _article_key(title, published)
            index.setdefault(key, []).append({"title": title, "publication_date": published,
                "guid": guid, "information_id": information,
                "refs": [reader.ref(relative), reader.ref(marker["source_path"]), reader.ref(marker["parsed_path"])]})
    return index


def _monthly_context(reader, queue):
    proofs = []
    for job in queue["jobs"].values():
        original = job.get("original", {})
        if not isinstance(original, dict):
            continue
        provenance = original.get("provenance", {})
        period = job.get("period")
        if (provenance.get("selectors") == {"月份": period}
                and provenance.get("statistical_period") == period
                and provenance.get("menu_path") == queue.get("menu_path")
                and original.get("dataset") == queue.get("dataset") and original.get("period") == period):
            reader.raw(original["path"], original["sha256"])
            proofs.append(reader.ref(original["path"]))
    return proofs


def _portal(reader, queue, job, attempt, dataset):
    version = queue.get("version")
    if not isinstance(version, str) or not version or not isinstance(job.get("page"), int):
        raise ValueError("portal_version_and_page_required")
    dimensions = {**queue.get("selectors", {}), **job.get("selectors", {}), "source_version": version}
    receipt = attempt.get("receipt", job.get("native_receipt"))
    if isinstance(receipt, dict):
        if receipt.get("dataset") != dataset or str(receipt.get("period")) != str(job["page"]):
            raise ValueError("portal_native_page_link_required")
        _, _, refs = _receipt(reader, receipt)
        # Retain the already observed native scope, with version in evidence and
        # the query snapshot namespace. It is never a statistical calendar day.
        dimensions = {**dimensions, **receipt.get("selectors", {})}
        return _identity(dataset, version, "snapshot", dimensions, page=job["page"]), refs, "native_source"
    reference = job.get("evidence")
    if not isinstance(reference, str):
        raise ValueError("portal_retained_receipt_required")
    proof = reader.json(reference)
    if (proof.get("version") != version or proof.get("menu_path") != queue.get("menu_path")
            or str(proof.get("page", 1)) != str(job["page"])
            or proof.get("parsed_path") != job.get("parsed_path")
            or proof.get("parsed_sha256") != job.get("parsed_sha256")):
        raise ValueError("portal_exact_snapshot_link_required")
    refs = [reader.ref(reference)]
    for kind in ("source", "parsed"):
        reader.raw(proof[kind + "_path"], proof[kind + "_sha256"])
        refs.append(reader.ref(proof[kind + "_path"]))
    if proof.get("overview_path"):
        reader.raw(proof["overview_path"], proof["overview_sha256"])
        refs.append(reader.ref(proof["overview_path"]))
    source_kind = proof.get("source_kind", "retained_ui_source")
    return _identity(dataset, version, "snapshot", dimensions, page=job["page"]), refs, source_kind


def _queues(reader, queue_base, artifact_base):
    units = artifact_base["units"]
    by_queue = {u["legacy_queue_key"]: u for u in units if u.get("legacy_queue_key")}
    by_dataset = {u["dataset"]: u for u in units}
    expected = {_relative(reader.root, x["path"]): x["sha256"] for x in queue_base["source_refs"]}
    rule_refs = {relative: reader.raw(relative, digest) for relative, digest in REVIEWED_RULES.items()}
    del rule_refs  # provenance only; no execution of production code
    output, before, bridges, unresolved, views = {}, {}, [], [], []
    plan = None
    for source in queue_base["source_views"]:
        if not source["unmapped"]:
            continue
        source_ref = source["source_ref"]
        try:
            if "#daily:" in source_ref:
                dataset = source_ref.split("#daily:")[-1]
                unit = by_dataset[dataset]
                if plan is None:
                    # The complete plan may roll; the exact derived view below
                    # must still match the old canonical SHA before use.
                    plan = reader.json("state/server_production_plan_20261023.json")
                current = next(u for u in plan["units"].values() if u["dataset"] == dataset)
                queue = {"jobs": {period: {"period": period, "status": "pending", "selectors": {"日期": period}}
                                  for period in current.get("pending_dates", [])}}
                if _sha(canonical(queue)) != source["sha256"]:
                    raise ValueError("baseline_derived_queue_sha256_changed")
                queue_ref = "state/server_production_plan_20261023.json"
            else:
                queue_ref = _relative(reader.root, source_ref)
                unit = by_queue[Path(queue_ref).stem.removeprefix("server_")]
                dataset = unit["dataset"]
                queue = reader.json(queue_ref, source["sha256"])
            attempt_ref = f"state/server_native_attempts_{dataset.lower()}.json"
            attempts = reader.json(attempt_ref, expected[attempt_ref]) if attempt_ref in expected else None
            context = QueueContext("anhui", "offline-shadow-review", dataset,
                "day" if unit["family"] in {"daily", "anchor", "intertie_web", "web_observed_scope"} else None)
            original = decode_jobs(queue, context, attempts=attempts)
            if (len(original) != source["records"]
                    or sum(t.identity is None for t in original) != source["unmapped"]
                    or dict(Counter(t.state for t in original)) != source["states"]):
                raise ValueError("baseline_queue_cohort_changed")
            before[source_ref] = original
            output[source_ref] = original
            listing = _listing_index(reader, dataset) if unit["family"] == "publication" else {}
            month_proofs = _monthly_context(reader, queue) if unit["family"] == "period" else []
            resolved = []
            for task in original:
                if task.identity is not None:
                    resolved.append(task)
                    continue
                key = task.legacy_key
                job = queue["jobs"].get(key, {})
                attempt = (attempts or {}).get(key, {})
                refs = [reader.ref(queue_ref)]
                if attempts is not None:
                    refs.append(reader.ref(attempt_ref))
                identity, reason, extra = None, "independent_identity_evidence_missing", {}
                try:
                    if unit["family"] == "inline":
                        period = attempt.get("period", job.get("period"))
                        if period != key:
                            raise ValueError("inline_old_key_period_mismatch")
                        dimensions = attempt.get("selectors", job.get("selectors", {}))
                        identity = _identity(dataset, period, "day", dimensions)
                        refs += [reader.ref("powercrawler/production_plan.py"), reader.ref("powercrawler/native_rollout.py")]
                        reason = "declared_inline_day_query_only"
                    elif unit["family"] == "daily" and attempt.get("receipt"):
                        receipt = attempt["receipt"]
                        if receipt.get("dataset") != dataset or receipt.get("period") != key:
                            raise ValueError("orphan_receipt_old_key_mismatch")
                        _, _, proof_refs = _receipt(reader, receipt)
                        identity = _identity(dataset, receipt["period"], "day", receipt["selectors"])
                        refs += proof_refs
                        reason = "exact_orphan_native_receipt"
                    elif unit["family"] == "period" and month_proofs:
                        period = job.get("period")
                        if key != period or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", period or ""):
                            raise ValueError("month_queue_old_key_mismatch")
                        identity = _identity(dataset, period, "month", {"月份": period})
                        refs += month_proofs + [reader.ref("powercrawler/period_reports.py")]
                        reason = "original_month_control_and_queue_schema"
                    elif unit["family"] == "publication":
                        if _article_key(job.get("title"), job.get("publication_date")) != key:
                            raise ValueError("legacy_article_key_mismatch")
                        matches = [row for row in listing.get(key, []) if row["title"] == job.get("title")
                                   and row["publication_date"] == job.get("publication_date")]
                        subjects = {(row["guid"], row["information_id"]) for row in matches}
                        if len(subjects) == 1:
                            guid, information = next(iter(subjects))
                            identity = _identity(dataset, "undated", "snapshot", {"record_id": guid}, operation="article")
                            refs += matches[0]["refs"] + [reader.ref("powercrawler/publications.py"), reader.ref("powercrawler/native_publications.py")]
                            reason = "exact_legacy_article_key_and_native_record"
                            extra = {"record_id_sha256": _sha(canonical(guid)),
                                     "information_id_sha256": _sha(canonical(information)),
                                     "publication_date_is_not_statistical_period": True}
                    elif unit["family"] == "portal":
                        identity, proof_refs, source_kind = _portal(reader, queue, job, attempt, dataset)
                        refs += proof_refs
                        reason = "retained_versioned_portal_page"
                        extra = {"source_kind": source_kind, "statistical_period_inferred": False}
                except (ValueError, KeyError, TypeError):
                    identity = None
                    reason = "exact_recovery_chain_incomplete"
                summary = {"source_ref": source_ref, "source_sha256": source["sha256"],
                    "legacy_key_sha256": _sha(key.encode()), "dataset": dataset,
                    "state": task.state, "consumed": task.consumed, "facts_sha256": task.facts_sha256,
                    "reason": reason, "dispatch_allowed": False}
                if identity is None:
                    unresolved.append(summary)
                    resolved.append(task)
                    continue
                repaired = replace(task, identity=identity,
                    blockers=tuple(blocker for blocker in task.blockers if blocker != "identity_mapping_required"))
                if (repaired.legacy_key, repaired.state, repaired.consumed, repaired.facts_sha256) != (
                        task.legacy_key, task.state, task.consumed, task.facts_sha256):
                    raise ValueError("legacy_facts_must_not_change")
                bridges.append({**summary, "identity_key": identity.key,
                    "identity_kind": identity.period["kind"], "operation": identity.operation,
                    "evidence_refs": sorted({x["path"] for x in refs}), **extra})
                resolved.append(repaired)
            output[source_ref] = resolved
            views.append({"source_ref": source_ref, "sha256": source["sha256"], "dataset": dataset,
                "family": unit["family"], "records": len(original),
                "unmapped_before": source["unmapped"], "unmapped_after": sum(t.identity is None for t in resolved),
                "states_unchanged": True, "consumed_unchanged": True})
        except (ValueError, KeyError, TypeError, OSError):
            views.append({"source_ref": source_ref, "sha256": source["sha256"],
                "records": source["records"], "unmapped_before": source["unmapped"],
                "unmapped_after": source["unmapped"], "reason": "baseline_source_chain_not_reproducible"})
    return output, bridges, unresolved, views, before


def _bindings(reader, artifact_base):
    recovered, missing = [], []
    for unit in artifact_base["units"]:
        for example in unit.get("examples", []):
            if example.get("binding_qualification") != "legacy_mutable_state_binding":
                continue
            parsed_ref = _relative(reader.root, example["parsed_ref"])
            source_ref = _relative(reader.root, example["source_ref"])
            item = {"dataset": unit["dataset"], "source_ref": example["source_ref"],
                "source_sha256": example["source_sha256"], "parsed_ref": example["parsed_ref"],
                "parsed_sha256": example["parsed_sha256"], "baseline_binding_ref": example["binding_ref"],
                "baseline_binding_sha256": example["binding_sha256"],
                "online_acceptance": "pending", "original_capture_was_mutable": True}
            try:
                parsed = reader.json(parsed_ref, example["parsed_sha256"])
                raw = reader.raw(source_ref, example["source_sha256"])
                digest = parsed["binding_sha256"]
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("exact_binding_digest_required")
                item["parsed_precommitted_binding_sha256"] = digest
                binding_ref = "data/2026/native_tables/_bindings/" + digest + ".json"
                binding = reader.json(binding_ref, digest)
                receipt = {"dataset": unit["dataset"], "source_path": source_ref,
                    "source_sha256": example["source_sha256"], "period": parsed["query_period"],
                    "selectors": parsed["selectors"]}
                if (parsed.get("source_sha256") != example["source_sha256"]
                        or parsed.get("query_period") != example["period"]):
                    raise ValueError("baseline_parsed_source_link_changed")
                validate_retained_binding(receipt, parsed, binding)
                if project_bound(json.loads(raw, parse_float=Decimal), binding["model"], period=receipt["period"]) != parsed["rows"]:
                    raise ValueError("exact_binding_projection_mismatch")
                recovered.append({**item, "binding_ref": str(Path(reader.root.name) / binding_ref),
                    "binding_sha256": digest, "qualification": "precommitted_digest_exact_immutable_copy_verified",
                    "model_kind": binding["model"]["kind"]})
            except (ValueError, KeyError, TypeError, OSError, Fault):
                missing.append({**item, "qualification": "legacy_mutable_binding_not_upgraded",
                                "reason": "precommitted_binding_digest_copy_not_verified"})
    return {"baseline_mutable_captures": len(recovered) + len(missing), "recovered_captures": len(recovered),
            "remaining_captures": len(missing), "recovered": recovered, "remaining": missing}


def _candidates(reader, artifact_base):
    relative = "state/server_metric_coverage_inventory.json"
    inventory = reader.json(relative)
    indexed = {(e["surface"], e["source_index"]): e for e in inventory["entries"]}
    rows = []
    for old in artifact_base["excluded_units"]:
        source_key = (old["surface"], old["source_index"])
        entry = indexed.get(source_key, {})
        rows.append({"surface": old["surface"], "source_index": old["source_index"],
            "legacy_entry_sha256": _sha(canonical(list(source_key))),
            "metadata_entry_found": bool(entry), "evidence_references": len(entry.get("evidence", [])),
            "sampled_dataset_present": bool(entry.get("sampled_dataset")),
            "retained_status": entry.get("runtime_status", "unknown"),
            "qualification": "candidate_not_admitted_evidence_required", "admission_changed": False,
            "evidence_refs": [reader.ref(relative)["path"]]})
    return {"baseline_candidates": len(rows), "metadata_entries_found": sum(x["metadata_entry_found"] for x in rows),
            "new_independent_original_qualifications": 0, "remaining_candidates": len(rows), "entries": rows}


def _carriers(reader, artifact_base, queue_bridges):
    units = {u["dataset"]: u for u in artifact_base["units"] if u["dataset"] in CARRIERS}
    refs = {name: {} for name in units}
    manifest_ref = "data/2026/original/manifest.jsonl"
    expected = next((r["sha256"] for r in artifact_base["public_input_hashes"]
                     if _relative(reader.root, r["path"]) == manifest_ref), None)
    for line in reader.raw(manifest_ref, expected).splitlines():
        entry = json.loads(line)
        dataset = entry.get("dataset")
        if dataset in units and Path(entry.get("path", "")).suffix.lower() in {".xlsx", ".pdf"}:
            refs[dataset][entry["path"]] = entry["sha256"]
    result = []
    for dataset, unit in units.items():
        queue_ref = "state/server_" + unit["legacy_queue_key"] + ".json"
        queue = reader.json(queue_ref)
        for job in queue.get("jobs", {}).values():
            for entry in job.get("page_captures", []):
                if entry.get("path", "").endswith(".json"):
                    refs[dataset][entry["path"]] = entry["sha256"]
            if dataset == "portal_historical_contracts" and job.get("parsed_path"):
                refs[dataset][job["parsed_path"]] = job["parsed_sha256"]
            if job.get("original_path", "").endswith(".xlsx"):
                refs[dataset][job["original_path"]] = job["sha256"]
        artifacts = []
        for reference, digest in refs[dataset].items():
            reader.raw(reference, digest)
            artifacts.append(reader.ref(reference))
        portal_links = [x for x in queue_bridges if x["dataset"] == dataset and x["reason"] == "retained_versioned_portal_page"]
        result.append({"dataset": dataset, "baseline_qualification": unit["offline_status"],
            "baseline_legacy_artifacts": unit.get("verified_legacy_artifacts", 0),
            "unchanged_artifacts_reverified": len(artifacts), "artifact_refs": artifacts,
            "independent_ui_source_chains_recovered": len(portal_links),
            "content_semantic_acceptance": "pending", "online_acceptance": "pending",
            "qualification_changed": False})
    return {"groups": result, "legacy_artifacts_reverified": sum(x["unchanged_artifacts_reverified"] for x in result),
            "independent_ui_source_chains_recovered": sum(x["independent_ui_source_chains_recovered"] for x in result),
            "new_domain_semantic_qualifications": 0}


def _baseline_drifts(reader, artifact_base):
    """Hash declared old inputs, without loading newer attempt records as data."""
    for entry in artifact_base["public_input_hashes"] + artifact_base["source_references"]:
        try:
            reader.raw(_relative(reader.root, entry["path"]), entry["sha256"])
        except ValueError:
            # Expected drift is evidence, never permission to use a new cohort.
            continue


def _compact(report):
    """Deduplicate reference paths and source metadata in the committed report."""
    references = report["references"]
    ref_ids = {entry["path"]: f"r{index}" for index, entry in enumerate(references)}
    report["references"] = {ref_ids[entry["path"]]: entry for entry in references}
    views = report["queue_views"]
    source_ids = {entry["source_ref"]: f"q{index}" for index, entry in enumerate(views)}
    report["queue_views"] = {source_ids[entry["source_ref"]]: entry for entry in views}
    reasons = sorted(report["recovery_reason_counts"])
    reason_ids = {reason: f"m{index}" for index, reason in enumerate(reasons)}
    shared = ("identity_kind", "operation", "publication_date_is_not_statistical_period", "statistical_period_inferred")
    report["mapping_rules"] = {reason_ids[reason]: {"reason": reason, **{
        key: value for row in report["queue_identity_bridges"] if row["reason"] == reason
        for key, value in row.items() if key in shared}} for reason in reasons}
    for kind in ("queue_identity_bridges", "unresolved_records"):
        rows = []
        for old in report[kind]:
            row = {key: value for key, value in old.items() if key not in {
                "source_ref", "source_sha256", "reason", "evidence_refs", "dataset", "dispatch_allowed", *shared}}
            row["source_view_id"] = source_ids[old["source_ref"]]
            if old["reason"] in reason_ids:
                row["mapping_rule_id"] = reason_ids[old["reason"]]
            else:
                row["reason"] = old["reason"]
            if "evidence_refs" in old:
                row["evidence_ids"] = [ref_ids[path] for path in old["evidence_refs"]]
            rows.append(row)
        report[kind] = rows
    for row in report["candidates"]["entries"]:
        row["evidence_ids"] = [ref_ids[path] for path in row.pop("evidence_refs")]
    report["queue_views_contain_only_affected_old_sources"] = True
    report["dispatch_allowed"] = False
    return report


def _run(root, *, queue_baseline=None, artifact_baseline=None, evidence=True):
    reader = _Reader(root)
    queue_base, qref = _baseline(reader.root, "anhui_queue_phase2.json", queue_baseline)
    artifact_base, aref = _baseline(reader.root, "anhui_phase2.json", artifact_baseline)
    records, bridges, unresolved, views, before = _queues(reader, queue_base, artifact_base)
    target_keys = {(row["source_ref"], row["legacy_key_sha256"]) for row in bridges}
    if len(target_keys) != len(bridges):
        raise ValueError("duplicate_recovered_legacy_key")
    if not evidence:
        if reader.recheck():
            raise ValueError("recovery_inputs_changed_during_read")
        return records, None, before
    bindings = _bindings(reader, artifact_base)
    candidates = _candidates(reader, artifact_base)
    carriers = _carriers(reader, artifact_base, bridges)
    _baseline_drifts(reader, artifact_base)
    changed = reader.recheck()
    if changed:
        raise ValueError("recovery_inputs_changed_during_read")
    recovered = len(bridges)
    report = {"schema_version": 1, "province": "anhui", "mode": "phase_two_cohort_readonly_evidence_recovery",
        "baseline_refs": [x for x in (qref, aref) if x],
        "baseline_queue_views": len(views), "baseline_normalized_records": queue_base["normalized_records"],
        "baseline_mapped_records": queue_base["mapped_records"], "baseline_unmapped_records": queue_base["unmapped_records"],
        "recovered_identity_records": recovered,
        "remaining_unmapped_records": queue_base["unmapped_records"] - recovered,
        "mapped_records_after": queue_base["mapped_records"] + recovered,
        "recovery_reason_counts": dict(Counter(x["reason"] for x in bridges)),
        "recovered_states": dict(Counter(x["state"] for x in bridges)),
        "queue_views": views, "queue_identity_bridges": bridges,
        "unresolved_records": unresolved, "bindings": bindings, "candidates": candidates, "legacy_carriers": carriers,
        "references": sorted(reader.refs.values(), key=lambda x: x["path"]),
        "baseline_reference_drifts": sorted({canonical(x): x for x in reader.drifts}.values(), key=lambda x: x["path"]),
        "inputs_changed_during_scan": changed,
        "rolling_read_not_frozen": True, "overlapping_snapshots_are_not_unique_tasks": True,
        "states_and_consumption_preserved": True, "production_writes": 0, "website_requests": 0,
        "queue_import_ready": False, "live_handoff_completed": False,
        "limitations": ["Only exact old-cohort keys are counted; current collector additions are not recovery.",
            "Article IDs and selectors remain in RAM; report contains their hashes only.",
            "Query periods are not assertions of statistical periods or disclosure dates.",
            "Exact immutable copies strengthen old proofs without changing original mutable-capture history.",
            "Source links do not prove domain semantics, attachments, all subjects, all dates or online acceptance."]}
    return records, _compact(report), before


def resolve_queue_records(root: Path, *, queue_baseline=None, artifact_baseline=None) -> dict[str, list[LegacyTask]]:
    """Return the 61 affected old source views with identities repaired in RAM.

    State, consumed, facts_sha256 and old IDs are unchanged. A drifted source is
    omitted, never replaced with the collector's newer cohort. No ledger import
    or output file is performed. Do not serialize the returned real selectors.
    """
    records, _, _ = _run(root, queue_baseline=queue_baseline, artifact_baseline=artifact_baseline, evidence=False)
    return records


def baseline_queue_records(root: Path, *, queue_baseline=None, artifact_baseline=None) -> dict[str, list[LegacyTask]]:
    """Return the original decoder records for the same exact old source views."""
    _, _, before = _run(root, queue_baseline=queue_baseline, artifact_baseline=artifact_baseline, evidence=False)
    return before


def recover(root: Path, *, queue_baseline=None, artifact_baseline=None) -> dict:
    """Return a bounded, metadata/hash-only report, with no filesystem writes."""
    _, report, _ = _run(root, queue_baseline=queue_baseline, artifact_baseline=artifact_baseline)
    return report
