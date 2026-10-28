"""Verify the exact phase-three Monday cohort against current offline identities.

No scheduling or legacy writes occur. Real query dates and selectors remain in
RAM; the report contains identity hashes and references to retained evidence.
"""
from __future__ import annotations

from collections import Counter
import hashlib

from ..contracts import canonical
from . import liaoning
from .invariants import MAPPING_BLOCKERS, validate_queue_recovery

PHASE_THREE_SHA256 = "433ec2090a1741bda88534b12212db0f89d79f065831b1485aa0c61bef0c5eaf"


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def verify_transition(previous, current):
    """Compare all originally-unmapped record views, including prior mappings."""
    views = previous["queue_evidence"]["source_views"]
    if len(views) != len(current) or {view["source_ref"] for view in views} != set(current):
        raise ValueError("phase_three_source_views_changed")
    kinds = previous["queue_evidence"]["record_kinds"]
    monday, prior_mapped, remaining, cohort = [], 0, 0, 0
    for view in views:
        if len({old["legacy_key_sha256"] for old in view["records"]}) != len(view["records"]):
            raise ValueError("duplicate_phase_three_record")
        rows = current[view["source_ref"]]
        by_key = {_sha(row.legacy_key.encode()): row for row in rows}
        if len(by_key) != len(rows):
            raise ValueError("duplicate_legacy_key_in_source_view")
        for old in view["records"]:
            cohort += 1
            row = by_key.get(old["legacy_key_sha256"])
            if row is None or any(getattr(row, field) != old[field]
                                  for field in ("dataset", "state", "consumed", "facts_sha256")):
                raise ValueError("phase_three_record_facts_changed")
            record_kind = kinds[old["record_kind"]]
            blockers = set(record_kind["remaining_blockers"])
            if old["identity_key_sha256"] is not None:
                if row.identity is None or row.identity.key != old["identity_key_sha256"] or set(row.blockers) != blockers:
                    raise ValueError("phase_three_existing_mapping_changed")
                prior_mapped += 1
            elif old.get("native_period_kind") == "week_start_monday":
                if (record_kind["status"] != "evidence_found_protocol_pending"
                        or old.get("native_monday_confirmed") is not True or row.identity is None):
                    raise ValueError("native_monday_cohort_not_proven")
                period = row.identity.document()["period"]
                if (period.get("kind") != "week_start_monday"
                        or _sha(canonical(period.get("anchor"))) != old["native_query_period_sha256"]
                        or set(row.blockers) != blockers - MAPPING_BLOCKERS):
                    raise ValueError("native_monday_anchor_or_protection_changed")
                monday.append({"source_ref": view["source_ref"], "legacy_key_sha256": old["legacy_key_sha256"],
                    "dataset": row.dataset, "state": row.state, "consumed": row.consumed,
                    "legacy_state": row.legacy_state, "facts_sha256": row.facts_sha256,
                    "identity_key_sha256": row.identity.key, "period_kind": period["kind"],
                    "query_anchor_sha256": old["native_query_period_sha256"],
                    "phase_three_evidence_ids": old["evidence_refs"],
                    "remaining_blockers": list(row.blockers), "dispatch_allowed": False})
            else:
                if row.identity is not None or set(row.blockers) != blockers:
                    raise ValueError("unrelated_missing_mapping_changed")
                remaining += 1
    if cohort != previous["queue_evidence"]["baseline_unmapped_record_views"]:
        raise ValueError("phase_three_cohort_count_changed")
    return {"original_unmapped_record_views": cohort, "phase_three_mappings_preserved": prior_mapped,
            "native_monday_identity_views_recovered": len(monday),
            "native_monday_states_preserved": dict(Counter(row["state"] for row in monday)),
            "native_monday_consumed_preserved": sum(row["consumed"] for row in monday),
            "remaining_unmapped_record_views": remaining, "records": monday,
            "query_period_is_not_statistical_week": True, "dispatch_allowed": False}


def _run(root):
    reader = liaoning._Reader(root)
    baseline_ref = reader.workspace / "pmos/evidence/liaoning_phase3_recovery.json"
    previous, digest = reader.json(baseline_ref)
    if digest != PHASE_THREE_SHA256:
        raise ValueError("immutable_phase_three_report_sha256_changed")
    _, baseline_views = liaoning._baseline_views(reader)
    before = {view["source_ref"]: view["tasks"] for view in baseline_views}
    samples, _ = liaoning._samples(reader)
    _, resolved, views = liaoning._resolve(reader, samples, liaoning._rules(reader))
    phase_three_views = {view["source_ref"]: view for view in previous["queue_evidence"]["source_views"]}
    for view in views:
        if not view["phase2_sha256_matches"] or view["sha256"] != phase_three_views[view["source_ref"]]["sha256"]:
            raise ValueError("historical_queue_snapshot_changed")
    current = {view["source_ref"]: resolved[str(reader.workspace / view["source_ref"])] for view in views}
    transition = verify_transition(previous, current)
    relevant = {ref for row in transition["records"] for ref in row["phase_three_evidence_ids"]}
    declarations = {"liaoning/lncrawler/expanded_adapters.py", "liaoning/lncrawler/goal92_candidate_specs.py"}
    declaration_ids = {key for key, ref in previous["evidence_refs"].items() if ref["path"] in declarations}
    if len(declaration_ids) != len(declarations):
        raise ValueError("native_monday_parameter_writer_evidence_required")
    relevant.update(declaration_ids)
    references = {}
    for ref_id in sorted(relevant):
        ref = previous["evidence_refs"][ref_id]
        if _sha(reader.raw(reader.workspace / ref["path"])) != ref["sha256"]:
            raise ValueError("phase_three_native_monday_evidence_changed")
        references[ref_id] = ref
    conservation = validate_queue_recovery(before, resolved)
    if reader.changed():
        raise ValueError("business_inputs_changed_during_monday_review")
    report = {"schema": "pmos-native-monday-recovery-v1", "province": "liaoning",
        "mode": "read_only_native_monday_identity_review", "phase_three_ref": reader.ref(baseline_ref),
        "summary": {key: transition[key] for key in ("phase_three_mappings_preserved", "native_monday_identity_views_recovered",
            "native_monday_states_preserved", "native_monday_consumed_preserved", "remaining_unmapped_record_views")},
        "transition": transition, "queue_conservation": conservation, "evidence_refs": references,
        "source_views": [{key: view[key] for key in ("source_ref", "sha256", "phase2_sha256_matches")} for view in views],
        "identity_contract": {"version": 1, "period_kind": "week_start_monday", "canonical_field": "anchor",
            "requires_canonical_monday": True, "week_boundaries_inferred": False, "old_identity_hashes_rewritten": False},
        "website_requests": 0, "production_writes": 0, "dispatch_allowed": False,
        "live_handoff_completed": False,
        "limitations": ["Native Monday anchors describe the retained query, not a statistical week or calendar expansion.",
            "Every prior consumed, unknown, denied, failed and held fact remains protected.",
            "Identity recovery does not grant permissions, semantic acceptance or online scheduling."]}
    return resolved, report


def resolve_queue_records(root):
    """Return RAM-only rows after checking the entire saved phase-three cohort."""
    return _run(root)[0]


def recover(root):
    """Return metadata only; never write the legacy queue or an output file."""
    return _run(root)[1]
