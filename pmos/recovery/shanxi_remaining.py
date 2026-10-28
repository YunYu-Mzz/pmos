"""Phase-four Shanxi evidence checks; references and hashes, never dispatch.

All writes are the caller's responsibility. Only declared business receipts,
static source files and phase-three historical rollback snapshots are read.
No provincial runner is imported, and no business request can be produced.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from ..faults import Fault
from ..validate import Response, evaluate
from .shanxi import _read, _safe


_DENY = re.compile(r"profile|private|auth|config|cookie|token|credential|password|session|browser|\.har|__pycache__|node_modules|\.git", re.I)
_BOUNDARY_CLASSES = {"empty_query_evidence_only", "empty_selector_evidence_only", "explicit_alias_to_existing_samples", "refusal_evidence_recovered_hold"}
PHASE3_SHA256 = "a601421876b796f6fc96b21dd14f547bc5d50c1101095dbd47ef2c69ad032e84"


def _phase3_bytes(province: Path) -> bytes:
    controlled = province.parent / "pmos"
    path = controlled / "evidence/shanxi_phase3_recovery.json"
    if any(p.is_symlink() for p in (controlled, controlled / "evidence", path)):
        raise ValueError("controlled_phase_three_symlink_rejected")
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("bounded_phase_three_baseline_required")
    raw = path.read_bytes()
    if _hash(raw) != PHASE3_SHA256:
        raise ValueError("pinned_phase_three_sha_required")
    return raw


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class Reader:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.refs = {}
        self.cache = {}

    def raw(self, path: str, expected: str | None = None):
        raw = _read(self.root, path, limit=32 * 1024 * 1024)
        digest = _hash(raw)
        old = self.refs.get(path)
        if expected is not None and digest != expected:
            raise ValueError("declared_evidence_sha_changed")
        if old is not None and old["sha256"] != digest:
            raise ValueError("evidence_changed_during_remaining_scan")
        self.refs.setdefault(path, {"path": path, "sha256": digest, "bytes": len(raw)})
        return raw

    def json(self, path: str, expected: str | None = None):
        raw = self.raw(path, expected)
        if path not in self.cache:
            self.cache[path] = json.loads(raw)
        return self.cache[path]

    def ref(self, path: str, pointer: str | None = None):
        return {**self.refs[path], **({"pointer": pointer} if pointer is not None else {})}

    def finish(self):
        for path, ref in list(self.refs.items()):
            self.raw(path, ref["sha256"])


def pointer(value, path: str):
    if not path.startswith("/"):
        raise ValueError("explicit_json_pointer_required")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def embedded_selector(proof: dict, declared: dict) -> dict:
    """Verify one independently embedded payload without making a raw file."""
    receipt = proof["receipt"]
    keys = ("dataset_candidate", "step", "selected_period", "endpoint_path", "method", "request", "sha256", "size", "sample_path")
    if any(_canonical(receipt.get(key)) != _canonical(declared.get(key)) for key in keys):
        raise ValueError("embedded_receipt_context_conflicts_catalog")
    if receipt.get("queue_confirmed") is not False:
        raise ValueError("embedded_selector_not_queue_confirmation")
    raw = json.dumps(proof["payload"], ensure_ascii=False, separators=(",", ":")).encode()
    if _hash(raw) != receipt["sha256"] or len(raw) != receipt["size"]:
        raise ValueError("embedded_payload_does_not_reproduce_declared_bytes")
    outcome = evaluate({"kind": "json"}, Response(receipt["http_status"], receipt["mime"], raw))
    if outcome.result != "empty" or proof["payload"].get("data") != []:
        raise ValueError("embedded_selector_must_remain_empty")
    return {"dataset": receipt["dataset_candidate"], "context_sha256": _hash(_canonical({key: receipt.get(key) for key in keys[:6]})),
            "embedded_payload_sha256": _hash(raw), "embedded_payload_bytes": len(raw),
            "serialization": "utf8_compact_original_member_order", "original_path": receipt["sample_path"],
            "kind": "embedded_empty_selector_only", "business_original_recovered": False,
            "queue_confirmed": False, "consumption_proven": False, "new_original_created": False,
            "qualification_changed": False, "dispatch_allowed": False}


def _boundaries(reader: Reader, phase3: dict) -> list[dict]:
    output = []
    for source in phase3["sources"]:
        if source["recovery_status"] not in _BOUNDARY_CLASSES:
            continue
        entries = []
        for item in source["evidence"]:
            receipt_ref = item["receipt"]
            document = reader.json(receipt_ref["path"], receipt_ref["sha256"])
            value = pointer(document, receipt_ref["pointer"])
            # SHA-bound parent pointer rechecked, without serializing the real
            # request or selector values into the report.
            if value.get("sha256") != item["source_sha256"]:
                raise ValueError("phase_three_receipt_original_sha_link_changed")
            raw = reader.raw(item["source_ref"], item["source_sha256"])
            if value.get("size") is not None and len(raw) != value["size"]:
                raise ValueError("phase_three_original_size_changed")
            try:
                outcome = evaluate({"kind": "json"}, Response(value["http_status"], value.get("mime", "application/json"), raw))
                result = "empty_response_only" if outcome.result == "empty" else "observed_response_only"
            except Fault as fault:
                result = {"permission": "permission_denial_retained", "auth": "authentication_failure_retained"}.get(fault.category, "rejected_response_retained")
            if item["result"] == "permission_denial_retained" and result != "permission_denial_retained":
                raise ValueError("phase_three_refusal_fact_changed")
            entries.append({"receipt": reader.ref(receipt_ref["path"], receipt_ref["pointer"]),
                            "original": reader.ref(item["source_ref"]), "result": result,
                            "old_fact_sha256": _hash(_canonical(item)), "consumption_proven": False})
        record = {"dataset": source["dataset"], "phase3_class": source["recovery_status"],
                  "rechecked_receipts": entries, "phase3_class_changed": False, "dispatch_allowed": False}
        alias = source.get("declared_alias")
        if alias:
            parent = alias["receipt"]
            document = reader.json(parent["path"], parent["sha256"])
            if pointer(document, parent["pointer"]) != alias["target_ids"]:
                raise ValueError("declared_alias_targets_changed")
            record["alias_evidence"] = {"reference": reader.ref(parent["path"], parent["pointer"]),
                                       "target_technical_ids": alias["target_ids"], "consumption_inferred": False,
                                       "new_independent_source_count": 0}
        output.append(record)
    return output


def _form_relation(reader: Reader, catalog: dict) -> dict:
    dataset, related = "candidate_0f5a5b87e4ea", "menu_bf184857aa12"
    manifest_path = "verification/20261019/special_route_sources/manifest.json"
    review_path = "verification/20261019/discovered_special_form_review.json"
    contracts_path = "native_menu_query_contracts.json"
    manifest, review = reader.json(manifest_path), reader.json(review_path)
    index, source = next((i, r) for i, r in enumerate(manifest["reports"]) if r["id"] == dataset)
    review_index, form = next((i, r) for i, r in enumerate(review["reports"]) if r["dataset"] == dataset)
    raw = reader.raw(source["local_path"], source["sha256"])
    contract = reader.json(contracts_path)["records"][related]
    query_path = form["source_form_evidence"]["query_path"]
    selectors = form["source_form_evidence"]["select_controls"]
    if (form["source_sha256"] != source["sha256"] or contract["source_sha256"] != source["sha256"]
            or query_path != contract["source_query_path"] or query_path.encode() not in raw
            or len(selectors) != 1 or selectors[0]["url"].encode() not in raw):
        raise ValueError("exact_form_source_and_request_path_required")
    denial_path = "verification/20261021/menu_native_scope_refresh/evidence_sync_review.json"
    result_index, result = next((i, r) for i, r in enumerate(reader.json(denial_path)["state"]["results"]) if r["id"] == related)
    receipt = result["receipt"]
    if receipt["id"] != related or receipt["path"] != selectors[0]["url"]:
        raise ValueError("related_selector_context_mismatch")
    raw_denial = reader.raw(receipt["sample_path"], receipt["sha256"])
    try:
        evaluate({"kind": "json"}, Response(receipt["http_status"], receipt["mime"], raw_denial))
    except Fault as fault:
        if fault.category != "permission":
            raise ValueError("related_selector_permission_refusal_required") from None
    else:
        raise ValueError("related_selector_permission_refusal_required")
    return {"dataset": dataset, "related_technical_id": related,
            "relation": "exact_same_static_form_sha_and_query_selector_paths", "title_matching_used": False,
            "evidence": [reader.ref(manifest_path, f"/reports/{index}"), reader.ref(review_path, f"/reports/{review_index}"),
                         reader.ref(source["local_path"]), reader.ref(contracts_path, "/records/" + related),
                         reader.ref(denial_path, f"/state/results/{result_index}/receipt"), reader.ref(receipt["sample_path"])],
            "related_selector_denial_retained": True, "candidate_query_success_proven": False,
            "candidate_consumption_proven": False, "old_technical_ids_rewritten": False, "dispatch_allowed": False}


def _month_form(reader: Reader) -> dict:
    dataset = "candidate_eee315dab049"
    source_path, contract_path = "verification/20261019/remaining_special_source_review.json", "verification/20261019/special_form_contract_review.json"
    source_index, source = next((i, r) for i, r in enumerate(reader.json(source_path)["reports"]) if r["id"] == dataset)
    contract_index, contract = next((i, r) for i, r in enumerate(reader.json(contract_path)["contracts"]) if r["dataset"] == dataset)
    raw = reader.raw(source["local_path"], source["source_sha256"])
    if (source["source_sha256"] != contract["source_sha256"] or contract["query_path"].encode() not in raw
            or contract["kind"] != "month_range_query_only" or contract["enabled"] or contract["runnable"]):
        raise ValueError("disabled_month_range_query_only_form_required")
    return {"dataset": dataset, "kind": "month_range_query_only_static_form", "evidence": [
        reader.ref(source_path, f"/reports/{source_index}"), reader.ref(contract_path, f"/contracts/{contract_index}"), reader.ref(source["local_path"])],
        "business_original_recovered": False, "export_endpoint_invented": False, "enabled": False, "dispatch_allowed": False}


def _historical_identity_search(reader: Reader, phase3: dict, ids: list[str]) -> dict:
    snapshots, seen = [], set()
    paths = ["/px-phbsx-retail-shop/infoPub/querySsMarket", "/px-phbsx-retail-shop/infoGrid/ectpjzsyxx",
             "/px-phbsx-retail-shop/informationRelease/zxtjllxsdnlycParam", "/px-phbsx-retail-shop/informationRelease/yhccjqkQuery"]
    for ref in phase3["native_snapshots"]["snapshots"]:
        if ref["sha256"] in seen:
            continue
        seen.add(ref["sha256"])
        raw = reader.raw(ref["path"], ref["sha256"])
        path = _safe(reader.root, ref["path"])
        if (not ref["path"].startswith("verification/") or raw[:16] != b"SQLite format 3\0"
                or raw[18:20] != b"\x01\x01" or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))):
            raise ValueError("historical_rollback_without_sidecar_required")
        table = ref["table"]
        if table not in {"requests", "work"}:
            raise ValueError("explicit_historical_business_table_required")
        matches = []
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)) as conn:
            conn.execute("PRAGMA query_only=ON")
            if table == "requests":
                for dataset in ids:
                    count = conn.execute("SELECT COUNT(*) FROM requests WHERE dataset=?", (dataset,)).fetchone()[0]
                    if count:
                        matches.append({"dataset": dataset, "field": "dataset", "count": count})
                columns = ("contract", "receipt")
                endpoint_count = sum(conn.execute("SELECT COUNT(*) FROM requests WHERE path=?", (path,)).fetchone()[0] for path in paths)
            else:
                columns, endpoint_count = ("scope", "binding", "receipt"), 0
            for column in columns:
                for dataset in ids:
                    count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE instr({column},?)>0", (dataset,)).fetchone()[0]
                    if count:
                        matches.append({"dataset": dataset, "field": column, "count": count, "exact_json_identity_still_requires_review": True})
        reader.raw(ref["path"], ref["sha256"])
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("historical_snapshot_sidecar_changed")
        snapshots.append({"snapshot": reader.ref(ref["path"]), "table": table, "matched_id_fields": matches,
                          "known_endpoint_matches": endpoint_count, "database_unchanged": True, "dispatch_allowed": False})
    journal_path = "verification/20261025/pause_repair_1719/queues_before.json"
    expected = phase3["crawlee_journals"]["receipt"]["sha256"]
    journals = reader.json(journal_path, expected)
    matched = [{"dataset": j["dataset"], "journal_content_sha256": _hash(_canonical(j))} for j in journals.values() if j["dataset"] in ids]
    variants = []
    for j in journals.values():
        if j["dataset"] in {"queryMarketClearingDA", "queryMarketClearingRT"}:
            variants.append({"dataset": j["dataset"], "journal_content_sha256": _hash(_canonical(j)),
                             "outcome_counts": dict(Counter(j["outcomes"].values())), "alias_old_identity_rewritten": False})
    return {"search_ids": ids, "unique_historical_database_sha256": len(seen), "snapshots": snapshots,
            "journal_ref": reader.ref(journal_path), "embedded_journals_checked": len(journals), "matched_journal_ids": matched,
            "declared_clearing_alias_variant_journals": variants, "alias_target_consumption_inferred": False,
            "phase3_unknown_set_preserved": phase3["crawlee_journals"]["unknown_identity_states"],
            "newly_mapped_old_tasks": 0, "production_or_wal_database_opened": False}


def _filename_inventory(root: Path, ids: list[str]) -> dict:
    matches = []
    for top in ("state", "verification", "data"):
        if (root / top).is_symlink():
            raise ValueError("inventory_root_symlink_rejected")
        for directory, directories, files in os.walk(root / top, followlinks=False):
            directories[:] = sorted(name for name in directories if not _DENY.search(name) and not (Path(directory) / name).is_symlink()
                                    and name not in {"render", "deps", "native_excel_deps"})
            for name in sorted(files):
                path = Path(directory) / name
                if _DENY.search(name) or path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                if any(key in relative for key in ids) or "realunit_selected_query_20261016" in relative:
                    matches.append({"path": relative, "bytes": path.stat().st_size})
    return {"roots": ["state", "verification", "data"], "git_ignored_files_included": True,
            "private_auth_profile_config_har_pruned_before_descent": True, "matching_named_files": matches,
            "absence_is_not_proof_no_original_anywhere": True}


def recover(root: Path) -> dict:
    root = Path(root).resolve()
    province = root if root.name == "shanxi" else root / "shanxi"
    raw = _phase3_bytes(province)
    phase3 = json.loads(raw)
    reader = Reader(province)
    catalog_path = "dataset_catalog.json"
    catalog = reader.json(catalog_path)
    targets = {row["id"]: (i, row) for i, row in enumerate(catalog["targets"])}
    missing = sorted(row["dataset"] for row in phase3["sources"] if row["recovery_status"] == "still_missing_bound_original")
    embedded_path = "verification/20261019/channel_forecast_empty_selector_evidence.json"
    embedded = embedded_selector(reader.json(embedded_path), targets["candidate_3b346e6f33ea"][1]["selector_evidence"])
    embedded["evidence"] = [reader.ref(embedded_path), reader.ref(catalog_path, f"/targets/{targets['candidate_3b346e6f33ea'][0]}/selector_evidence")]
    embedded["original_path_exists"] = _safe(province, embedded["original_path"]).is_file()
    form_relation, month_form = _form_relation(reader, catalog), _month_form(reader)
    findings_path = "verification/20261027/disclosure_timing/findings.json"
    findings = reader.json(findings_path)
    derived = {}
    for i, row in enumerate(findings["records"]):
        for dataset in set(row.get("ids", [])) & set(missing):
            derived[dataset] = {"reference": reader.ref(findings_path, f"/records/{i}"), "disclosure_review_status": row["status"],
                                "business_original_proven_by_disclosure_review": False}
    rows = []
    for dataset in missing:
        index, entry = targets[dataset]
        group = {"candidate_0f5a5b87e4ea": "same_form_relation_with_related_selector_denial",
                 "candidate_3b346e6f33ea": "embedded_empty_selector_original_path_still_missing",
                 "candidate_eee315dab049": "static_month_range_query_only_no_business_original"}.get(dataset)
        if group is None:
            group = {"unverified": "catalog_placeholder_no_bound_original", "permission_denied": "catalog_permission_denied_no_raw_receipt",
                     "pending_live_verification": "catalog_pending_verification_no_bound_original"}.get(entry["status"], "catalog_only_no_bound_original")
        rows.append({"dataset": dataset, "phase4_evidence_group": group, "catalog_status": entry["status"],
                     "catalog_ref": reader.ref(catalog_path, f"/targets/{index}"), "later_disclosure_review": derived.get(dataset),
                     "business_original_still_missing": True, "phase2_or_phase3_qualification_changed": False, "dispatch_allowed": False})
    boundaries = _boundaries(reader, phase3)
    history = _historical_identity_search(reader, phase3, missing)
    inventory = _filename_inventory(province, missing)
    # A newer 113MB operating capture is not a bounded historical evidence
    # source for these IDs. Never load it wholesale to improve a recovery count.
    capture = province / "verification/20261028/business500_review/capture.json"
    excluded = [{"path": str(capture.relative_to(province)), "bytes": capture.stat().st_size,
                 "reason": "new_operating_capture_over_bounded_input_limit_not_read"}] if capture.is_file() else []
    reader.finish()
    if _phase3_bytes(province) != raw:
        raise ValueError("phase_three_baseline_changed_during_remaining_scan")
    return {"province": "shanxi", "mode": "phase4_read_only_remaining_evidence", "website_requests": 0, "production_writes": 0,
            "dispatch_allowed": False, "phase3_baseline": {"path": "pmos/evidence/shanxi_phase3_recovery.json", "sha256": _hash(raw), "pinned_sha_verified": True},
            "counts": {"phase3_missing_bound_business_originals": len(missing), "new_business_originals": 0,
                       "business_originals_still_missing": len(rows), "newly_mapped_old_tasks": 0,
                       "embedded_empty_selector_receipt_sha_verified": 1, "exact_static_form_relation_found": 1,
                       "month_range_query_only_static_form_verified": 1, "boundary_sources_rechecked": len(boundaries)},
            "remaining_groups": dict(sorted(Counter(row["phase4_evidence_group"] for row in rows).items())),
            "remaining_sources": rows, "embedded_selector": embedded, "exact_form_relation": form_relation,
            "month_range_form": month_form, "phase3_boundary_rechecks": boundaries, "historical_identity_search": history,
            "filename_inventory": inventory, "excluded_inputs": excluded, "input_refs": list(reader.refs.values()),
            "phase3_historical_denials_retained": phase3["baseline"]["historical_denials_retained"],
            "phase3_refusal_evidence_sources_retained": phase3["source_recovery_counts"]["refusal_evidence_recovered_hold"],
            "limitations": ["Same empty bytes from another source never repair a missing original path.",
                "An embedded SHA-verifiable empty selector receipt is not a recovered raw file or a business sample.",
                "Same static form source links technical entries without rewriting their old task identities or consumption.",
                "Derived disclosure reviews are supporting metadata, not original business or publication-time proof.",
                "No new capture is counted as historical consumption and no prior refusal or unknown is cleared."]}
