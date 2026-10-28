"""Exact article identity bridges for the fixed phase-four remaining cohort.

Only existing business files are read. Old queue namespaces, keys, states,
consumed facts and protective blockers are preserved. Native IDs stay in RAM.
No legacy producer is imported and no browser, database or request is opened.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from . import anhui_remaining as prior
from .anhui import _article_key, _identity
from .invariants import validate_queue_recovery
from ..contracts import canonical
from ..queue_compat import QueueContext, decode_jobs


PHASE4_SHA256 = "4c77082676f5e69300229f9918a83ed81125428133ecca3c8a430191aedd3b2f"
RULES = {reference: digest for reference, digest in prior.RULES.items() if reference.startswith("powercrawler/")}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _load(root, phase4=None, phase3=None):
    references, checks = [], []
    if phase4 is None:
        raw = prior._baseline_bytes(root, "anhui_phase4_remaining.json", PHASE4_SHA256)
        phase4 = json.loads(raw)
        references.append({"path": "pmos/evidence/anhui_phase4_remaining.json", "sha256": _sha(raw)})
        checks.append(("anhui_phase4_remaining.json", _sha(raw)))
    if phase3 is None:
        expected = next((row["sha256"] for row in phase4["baseline_refs"]
                         if row["path"] == "pmos/evidence/anhui_phase3_recovery.json"), None)
        if expected is None:
            raise ValueError("phase4_committed_phase3_baseline_required")
        raw = prior._baseline_bytes(root, "anhui_phase3_recovery.json", expected)
        phase3 = json.loads(raw)
        references.append({"path": "pmos/evidence/anhui_phase3_recovery.json", "sha256": _sha(raw)})
        checks.append(("anhui_phase3_recovery.json", _sha(raw)))
    return phase4, phase3, references, checks


def _record_matches(task, old):
    return (task.identity is None and _sha(task.legacy_key.encode()) == old["legacy_key_sha256"]
        and all(getattr(task, field) == old[field] for field in ("dataset", "legacy_state", "state", "consumed", "facts_sha256"))
        and set(task.blockers) - {"identity_mapping_required"} == set(old["protective_blockers"]))


def _cohort(reader, phase4, phase3):
    required = phase4["remaining_article_records"]
    if len({(row["source_view_id"], row["legacy_key_sha256"]) for row in required}) != len(required):
        raise ValueError("duplicate_phase4_old_record_commitment")
    declared = {row["path"]: row["sha256"] for row in phase4["references"]}
    before, items, drift, scopes = {}, [], [], []
    for sid in sorted({row["source_view_id"] for row in required}):
        old_rows = [row for row in required if row["source_view_id"] == sid]
        namespace = phase3["queue_views"][sid]["source_ref"]
        dataset = old_rows[0]["dataset"]
        if (any(row["source_ref_sha256"] != _sha(namespace.encode()) or row["dataset"] != dataset for row in old_rows)
                or len({row["menu_scope_sha256"] for row in old_rows}) != 1):
            raise ValueError("phase4_namespace_or_scope_commitment_mismatch")
        expected_scope = old_rows[0]["menu_scope_sha256"]
        current_ref = prior._relative(reader.root, namespace)
        name = Path(current_ref).name
        candidates, menu_values, scope_refs = [], {}, []
        for reference, digest in declared.items():
            if Path(reference).name != name:
                continue
            relative = prior._relative(reader.root, reference)
            value = reader.json(relative)
            observed = reader.ref(relative)["sha256"]
            committed = observed == digest
            if not committed:
                drift.append({"path": reference, "baseline_sha256": digest, "observed_sha256": observed})
            if value.get("dataset") != dataset or not isinstance(value.get("menu_path"), list):
                continue
            menu_digest = _sha(canonical(value["menu_path"]))
            if committed and menu_digest == expected_scope:
                menu_values[menu_digest] = value["menu_path"]
                scope_refs.append(reader.ref(relative)["path"])
            # A changed whole queue may contain an exactly unchanged old job;
            # it may never provide the old menu anchor on its own.
            candidates.append((not committed, relative, value, committed))
        if len(menu_values) != 1:
            raise ValueError("phase4_committed_old_menu_source_required")
        menu = next(iter(menu_values.values()))
        scopes.append({"source_view_id": sid, "source_ref_sha256": _sha(namespace.encode()),
            "dataset": dataset, "menu_scope_sha256": expected_scope, "evidence_refs": sorted(set(scope_refs))})
        before[namespace] = []
        for old in old_rows:
            chosen = None
            for _, relative, value, committed in sorted(candidates, key=lambda row: (row[0], row[1])):
                if value.get("menu_path") != menu:
                    continue
                # The phase-four record includes the original decoder's
                # attempt-index blocker. Preserve that old decoder context.
                if "attempt_index_required" not in old["protective_blockers"]:
                    raise ValueError("additional_old_attempt_context_required")
                rows = decode_jobs(value, QueueContext("anhui", "offline-shadow-review", dataset), attempts=None)
                for task in rows:
                    if not _record_matches(task, old):
                        continue
                    job = value["jobs"].get(task.legacy_key)
                    if (not isinstance(job, dict)
                            or _article_key(job.get("title"), job.get("publication_date")) != task.legacy_key):
                        raise ValueError("exact_old_article_key_reconstruction_required")
                    chosen = {"task": task, "job": job, "menu_path": menu, "source_ref": namespace,
                        "source_view_id": sid, "menu_scope_sha256": expected_scope,
                        "cohort_proof": "phase4_committed_snapshot_exact_record" if committed else "exact_phase4_record_in_changed_queue",
                        "evidence_refs": sorted(set(scope_refs + [reader.ref(relative)["path"]]))}
                    break
                if chosen:
                    break
            if chosen is None:
                raise ValueError("exact_phase4_record_facts_not_reconstructed")
            before[namespace].append(chosen["task"])
            items.append(chosen)
    return before, items, scopes, drift


def _bridges(reader, before, items):
    after = {source: list(rows) for source, rows in before.items()}
    bridges, remaining = [], []
    for item in items:
        task = item["task"]
        summary = {"source_view_id": item["source_view_id"], "source_ref_sha256": _sha(item["source_ref"].encode()),
            "legacy_key_sha256": _sha(task.legacy_key.encode()), "dataset": task.dataset,
            "legacy_state": task.legacy_state, "state": task.state, "consumed": task.consumed,
            "facts_sha256": task.facts_sha256, "menu_scope_sha256": item["menu_scope_sha256"],
            "protective_blockers": sorted(set(task.blockers) - {"identity_mapping_required"}),
            "cohort_proof": item["cohort_proof"], "dispatch_allowed": False}
        # Reuse the reviewed strict implementation: shared evaluate, exact
        # source/parsed SHA links, genuine bool flags and integer pagination.
        matches = prior._listing_candidates(reader, item)
        subjects = {(row["guid"], row["information"]) for row in matches}
        if len(subjects) != 1:
            remaining.append({**summary, "qualification": "same_scope_native_article_identity_missing_or_ambiguous",
                              "candidate_identity_count": len(subjects)})
            continue
        guid, information = next(iter(subjects))
        identity = _identity(task.dataset, "undated", "snapshot", {"record_id": guid}, operation="article")
        repaired = replace(task, identity=identity,
            blockers=tuple(blocker for blocker in task.blockers if blocker != "identity_mapping_required"))
        after[item["source_ref"]] = [repaired if row.legacy_key == task.legacy_key else row for row in after[item["source_ref"]]]
        references = set(item["evidence_refs"])
        references.update(reference for match in matches for reference in match["refs"])
        references.update(reader.ref(reference)["path"] for reference in RULES)
        bridges.append({**summary, "identity_sha256": identity.key, "record_id_sha256": _sha(canonical(guid)),
            "information_id_sha256": _sha(canonical(information)), "identity_kind": "snapshot", "operation": "article",
            "publication_date_is_not_statistical_period": True, "new_sample_acceptance": False,
            "qualification": "same_menu_native_listing_record_identity_verified",
            "evidence_refs": sorted(references), "title_forms": sorted({row["title_form"] for row in matches})})
    return after, bridges, remaining


def _changes(reader, phase4, queue_drifts):
    old = {row["path"]: row["sha256"] for row in phase4["references"]}
    queues = {row["path"] for row in queue_drifts}
    groups = {"queue_documents_vs_phase4": queue_drifts, "listing_indexes_vs_phase4": [],
              "reviewed_code_vs_phase4": [], "other_declared_refs_vs_phase4": []}
    for path, current in reader.refs.items():
        expected = old.get(path)
        if expected is None or expected == current["sha256"] or path in queues:
            continue
        group = ("listing_indexes_vs_phase4" if "/state/server_native_listing_sources_" in path else
                 "reviewed_code_vs_phase4" if path.endswith(".py") else "other_declared_refs_vs_phase4")
        groups[group].append({"path": path, "baseline_sha256": expected, "observed_sha256": current["sha256"]})
    return groups


def _run(root, phase4=None, phase3=None):
    reader = prior.Reader(root)
    phase4, phase3, baseline_refs, checks = _load(reader.root, phase4, phase3)
    for reference, digest in RULES.items():
        reader.raw(reference, digest)
    before, items, scopes, queue_drifts = _cohort(reader, phase4, phase3)
    after, bridges, remaining = _bridges(reader, before, items)
    conservation = validate_queue_recovery(before, after)
    if reader.recheck():
        raise ValueError("phase5_bridge_inputs_changed_during_read")
    for name, digest in checks:
        prior._baseline_bytes(reader.root, name, digest)
    summary = {"fixed_article_cohort": len(phase4["remaining_article_records"]),
        "cohort_records_reconstructed": conservation["record_views"], "article_identities_recovered": len(bridges),
        "article_identities_remaining": len(remaining), "old_consumed_facts": conservation["consumed"],
        "new_sample_qualifications": 0, "new_semantic_qualifications": 0}
    old_refs = {row["path"]: row["sha256"] for row in phase4["references"]}
    origins = Counter("not_registered_in_phase4" if path not in old_refs else
                      "phase4_sha_unchanged" if old_refs[path] == row["sha256"] else "phase4_sha_changed"
                      for path, row in reader.refs.items())
    report = {"schema_version": 1, "province": "anhui", "mode": "fixed_phase4_remaining_article_bridges_readonly",
        "summary": summary, "baseline_refs": baseline_refs, "cohort_conservation": conservation,
        "old_scope_commitments": scopes, "queue_identity_bridges": bridges, "remaining_article_records": remaining,
        "drift_groups": _changes(reader, phase4, queue_drifts),
        "reference_origin_counts": dict(sorted(origins.items())),
        "references": sorted(reader.refs.values(), key=lambda row: row["path"]),
        "inputs_changed_during_scan": [], "production_writes": 0, "website_requests": 0,
        "dispatch_allowed": False, "queue_import_ready": False, "live_handoff_completed": False,
        "remaining_evidence_followup": {"binding_captures": phase4["bindings"]["remaining_captures"],
            "candidate_entries": phase4["candidates"]["remaining_candidates"],
            "qualification": "carried_forward_pending_separate_readonly_followup"},
        "limitations": ["Fixed old namespaces/keys/facts only; new collector successes never replace old states.",
            "An identity bridge is neither a newly accepted sample nor semantic or online acceptance.",
            "Article snapshots remain undated; publication dates only verify old article identity.",
            "Native IDs/titles/selectors remain in RAM; output is metadata, hashes and controlled references only."]}
    return before, after, report


def baseline_queue_records(root, *, phase4=None, phase3=None):
    """Return the fixed ten old records under the original source namespaces."""
    return _run(root, phase4, phase3)[0]


def resolve_queue_records(root, *, phase4=None, phase3=None):
    """Return RAM identity enrichment with exact old facts and protections."""
    return _run(root, phase4, phase3)[1]


def recover(root, *, phase4=None, phase3=None):
    """Return a hash/reference-only report, without any file or queue writes."""
    return _run(root, phase4, phase3)[2]
