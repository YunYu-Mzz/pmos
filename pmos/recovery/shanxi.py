"""Bounded, read-only recovery of explicitly retained Shanxi evidence.

This module never imports a provincial runner or writes a queue.  It reports
evidence links and historical status counts, without copying business rows or
selector values.  Finding bytes does not prove consumption or authorize replay.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import tarfile

from ..queue_compat import QueueContext, decode_journal
from ..validate import Response, evaluate
from ..faults import Fault


_FORBIDDEN = re.compile(r"profile|private|config|cookie|password|credential|authorization|token|(?:^|[/_.-])auth(?:[/_.-]|$)|\.har$", re.I)
_MAX_JSON = 32 * 1024 * 1024
_MAX_FILE = 64 * 1024 * 1024


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _safe(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or _FORBIDDEN.search(relative):
        raise ValueError("evidence_path_rejected")
    path = root / rel
    if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise ValueError("evidence_symlink_rejected")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("evidence_path_rejected")
    return path


def _read(root: Path, relative: str, *, limit=_MAX_FILE) -> bytes:
    path = _safe(root, relative)
    before = path.stat()
    if not path.is_file() or before.st_size > limit:
        raise ValueError("bounded_regular_evidence_required")
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("evidence_changed_during_read")
    return raw


def _json(root: Path, relative: str):
    raw = _read(root, relative, limit=_MAX_JSON)
    return json.loads(raw), {"path": relative, "sha256": _hash(raw), "bytes": len(raw)}


def _receipt(root: Path, dataset: str, value: dict, parent: dict, pointer: str, role: str) -> dict:
    """Check explicit source/scope/receipt binding; expose hashes and shape only."""
    ref = value.get("sample_path") or value.get("path")
    declared = value.get("id") or value.get("dataset") or value.get("dataset_candidate")
    result = {"role": role, "receipt": {**parent, "pointer": pointer},
              "source_id_matches": declared == dataset, "source_ref": ref,
              "expected_sha256": value.get("sha256"), "dispatch_allowed": False,
              "consumption_proven": False, "full_dataset_coverage_verified": False}
    if declared != dataset or not isinstance(ref, str) or not value.get("sha256"):
        result["result"] = "unbound_receipt"
        return result
    # Preserve type and all selector values in a digest, never in the report.
    scope = {key: value.get(key) for key in ("period", "selected_period", "kind", "at", "request") if key in value}
    result["receipt_context_sha256"] = _hash(_canonical(scope))
    result["stable_task_identity_proven"] = False
    result["scope_kind"] = value.get("kind") or ("observed_period" if value.get("period", value.get("selected_period")) else "unspecified")
    result["http_status"] = value.get("http_status")
    result["endpoint_path"] = value.get("endpoint_path") or (value.get("path") if value.get("sample_path") else None)
    try:
        raw = _read(root, ref)
    except FileNotFoundError:
        result["result"] = "referenced_original_missing"
        return result
    result.update(source_sha256=_hash(raw), bytes=len(raw))
    if result["source_sha256"] != value["sha256"] or (value.get("size") is not None and value["size"] != len(raw)):
        result["result"] = "hash_or_size_mismatch"
        return result
    json_source = ref.endswith((".json", ".raw"))
    try:
        evaluate({"kind": "json" if json_source else "binary"},
                 Response(value.get("http_status", 0), value.get("mime", "application/json" if json_source else "application/octet-stream"), raw))
    except Fault as fault:
        result.update(fault_code=fault.code, fault_category=fault.category, business_status=fault.business_status)
        result["result"] = {"permission": "permission_denial_retained", "auth": "authentication_failure_retained"}.get(fault.category, "business_response_rejected")
        return result
    if value.get("http_status") != 200:
        result["result"] = "http_not_verified"
        return result
    if not json_source:
        result["result"] = "export_original_bytes_verified"
        return result
    data = json.loads(raw)
    status = data.get("status")
    result["business_status"] = status
    if isinstance(status, bool) or status not in (0, "0"):
        result["result"] = "business_status_unverified"
        return result
    body = data.get("data")
    # These shape checks describe an observed response, not semantic acceptance.
    if isinstance(body, dict) and isinstance(body.get("list"), list):
        result["row_count"] = len(body["list"])
        result["result"] = "nonempty_query_original_verified" if body["list"] else "empty_query_original_verified"
    elif isinstance(body, list):
        result["row_count"] = len(body)
        result["result"] = "nonempty_selector_original_verified" if body else "empty_selector_original_verified"
    elif isinstance(body, dict):
        lists = [v for k, v in body.items() if isinstance(v, list)]
        result["row_count"] = sum(map(len, lists))
        result["result"] = "nonempty_query_original_verified" if result["row_count"] else "empty_selector_original_verified"
    else:
        result["result"] = "unsupported_business_shape"
    return result


def _sources(root: Path, baseline: dict) -> tuple[list[dict], list[dict]]:
    catalog, catalog_ref = _json(root, "dataset_catalog.json")
    targets = {t["id"]: (i, t) for i, t in enumerate(catalog["targets"])}
    blocked = sorted(s["dataset"] for s in baseline["sources"] if s["qualification"] == "blocked_evidence_or_dependency")
    output = {key: {"dataset": key, "baseline_qualification": "blocked_evidence_or_dependency", "evidence": [],
                    "phase2_qualification_changed": False, "dispatch_allowed": False} for key in blocked}
    inputs = [catalog_ref]
    for key in blocked:
        if key not in targets:
            continue
        index, target = targets[key]
        for name in ("query_evidence", "special_query_evidence", "selector_evidence"):
            value = target.get(name)
            if isinstance(value, dict):
                output[key]["evidence"].append(_receipt(root, key, value, catalog_ref, f"/targets/{index}/{name}", name))
        for role in ("ui_original", "program_original"):
            value = target.get("special_acquisition", {}).get(role)
            if isinstance(value, dict):
                output[key]["evidence"].append(_receipt(root, key, value, catalog_ref, f"/targets/{index}/special_acquisition/{role}", role))
        if key == "queryMarketClearing":
            variants = target.get("report_variant_ids", [])
            passed = {s["dataset"] for s in baseline["sources"] if s["qualification"] == "sample_only_online_pending"}
            output[key]["declared_alias"] = {"target_ids": variants, "receipt": {**catalog_ref, "pointer": f"/targets/{index}/report_variant_ids"},
                "all_targets_have_phase2_samples": bool(variants) and all(v in passed for v in variants), "new_samples_added": 0}
    # These four historical reviews contain business receipts, not credentials.
    reviews = [
        ("verification/20261021/plain_empty_refresh/runtime_latest.json", None),
        ("verification/20261021/menu_native_scope_refresh/evidence_sync_review.json", "state"),
        ("verification/20261021/native_section_scope/runtime_latest.json", None),
        ("verification/20261021/native_remaining_scoped/evidence_sync_review.json", "source_state"),
    ]
    for path, wrapper in reviews:
        value, parent = _json(root, path)
        inputs.append(parent)
        view = value[wrapper] if wrapper else value
        for index, result in enumerate(view.get("results", [])):
            key = result.get("id")
            if key not in output:
                continue
            for name in ("query", "selector", "receipt"):
                receipt = result.get(name)
                if isinstance(receipt, dict) and receipt.get("sample_path"):
                    ptr = (f"/{wrapper}" if wrapper else "") + f"/results/{index}/{name}"
                    evidence = _receipt(root, key, receipt, parent, ptr, name)
                    evidence["historical_outcome"] = result.get("outcome")
                    output[key]["evidence"].append(evidence)
    # Monthly empty-query capture names are insufficient by themselves.  Bind
    # both endpoint and SHA to the explicit target query contract in the catalog.
    path = "verification/20261013/additional_three_samples_snapshot.json"
    value, parent = _json(root, path)
    inputs.append(parent)
    key = "queryMonthlySystemLoadForecast"
    if key in output and key in targets:
        _, target = targets[key]
        query = target.get("query", {})
        for index, receipt in enumerate(value):
            if receipt.get("endpoint_path") == query.get("endpoint") and receipt.get("sha256") == query.get("evidence_sha256"):
                bound = {**receipt, "dataset": key}
                record = _receipt(root, key, bound, parent, f"/{index}", "query")
                record["source_binding"] = {**catalog_ref, "pointer": f"/targets/{targets[key][0]}/query"}
                output[key]["evidence"].append(record)
    for record in output.values():
        kinds = {e["result"] for e in record["evidence"]}
        if "permission_denial_retained" in kinds:
            status = "refusal_evidence_recovered_hold"
        elif "nonempty_query_original_verified" in kinds:
            status = "nonempty_sample_chain_recovered_pending_scope_review"
        elif "empty_query_original_verified" in kinds:
            status = "empty_query_evidence_only"
        elif "empty_selector_original_verified" in kinds:
            status = "empty_selector_evidence_only"
        elif record.get("declared_alias", {}).get("all_targets_have_phase2_samples"):
            status = "explicit_alias_to_existing_samples"
        else:
            status = "still_missing_bound_original"
        record["recovery_status"] = status
    return list(output.values()), inputs


_SNAPSHOTS = [
    ("verification/20261021/native_json_queue/queue_after_selected_day.sqlite3", "requests", "verification/20261021/native_json_queue/queue_backup_and_final_caption_review.json"),
    ("verification/20261021/native_node_queue/queue_after19_formats.sqlite3", "requests", "verification/20261021/native_node_queue/queue_backup_review.json"),
    ("verification/20261021/native_flat_curve/queue_after_two_flat_formats.sqlite3", "requests", "verification/20261021/native_flat_curve/queue_backup_review.json"),
    ("verification/20261021/native_remaining_scoped/queue24.sqlite3", "requests", "verification/20261021/native_remaining_scoped/queue24_backup_review.json"),
    ("verification/20261025/pause_repair_1719/native_json_collection_before.sqlite3", "requests", "verification/20261025/pause_repair_1719/review.json"),
    ("verification/20261025/pause_repair_1719/native_publication_collection_before.sqlite3", "work", "verification/20261025/pause_repair_1719/review.json"),
    ("verification/20261025/resume_hold_2104/first_recovery_state/native_json_collection.sqlite3", "requests", "verification/20261025/resume_hold_2104/first_recovery_state/initial.json"),
    ("verification/20261025/resume_hold_2104/activation_recovery_state/native_json_collection.sqlite3", "requests", "verification/20261025/resume_hold_2104/activation_recovery_state/initial.json"),
    ("verification/20261025/resume_hold_2104/first_recovery_state/native_publication_collection.sqlite3", "work", "verification/20261025/resume_hold_2104/first_recovery_state/initial.json"),
    ("verification/20261025/resume_hold_2104/activation_recovery_state/native_publication_collection.sqlite3", "work", "verification/20261025/resume_hold_2104/activation_recovery_state/initial.json"),
]

_BACKUP_SHA_POINTERS = {
    "verification/20261021/native_json_queue/queue_backup_and_final_caption_review.json": "/queue_sha256",
    "verification/20261021/native_node_queue/queue_backup_review.json": "/sha256",
    "verification/20261021/native_flat_curve/queue_backup_review.json": "/queue_sha256",
    "verification/20261021/native_remaining_scoped/queue24_backup_review.json": "/sha256",
}


def _set_summary(rows: list[dict]) -> dict:
    """A canonical set commitment plus bounded hashed examples, reproducible locally."""
    return {"count": len(rows), "canonical_set_sha256": _hash(_canonical(rows)), "hashed_examples": rows[:8]}


def snapshot_summary(root: Path, relative: str, table: str, *, identity_statuses=False, expected_sha256=None) -> dict:
    """Open a static rollback snapshot for one bounded aggregate read only."""
    if table not in {"requests", "work"} or not relative.startswith("verification/"):
        raise ValueError("historical_business_snapshot_required")
    path = _safe(root, relative)
    raw = _read(root, relative)
    result = {"path": relative, "sha256": _hash(raw), "bytes": len(raw), "table": table,
              "dispatch_allowed": False, "opened": False}
    if expected_sha256 is not None and result["sha256"] != expected_sha256:
        result["result"] = "snapshot_backup_sha_mismatch_not_opened"
        return result
    sidecars = [suffix for suffix in ("-wal", "-shm", "-journal") if Path(str(path) + suffix).exists()]
    result["sidecars"] = sidecars
    if len(raw) < 100 or raw[:16] != b"SQLite format 3\0" or raw[18:20] != b"\x01\x01" or sidecars:
        result["result"] = "wal_unknown_or_sidecar_snapshot_not_opened"
        return result
    before = path.stat()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
        conn.execute("PRAGMA query_only=ON")
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not {"identity", "status"}.issubset(columns):
            raise ValueError("explicit_business_table_schema_required")
        counts = dict(conn.execute(f"SELECT status,COUNT(*) FROM {table} GROUP BY status"))
        facts = []
        if identity_statuses:
            if sum(counts.values()) > 100_000:
                raise ValueError("snapshot_row_limit")
            cursor = conn.execute(f"SELECT identity,status FROM {table}")
            while batch := cursor.fetchmany(128):
                facts.extend(batch)
    after = path.stat()
    unchanged = before.st_mtime_ns == after.st_mtime_ns and _hash(_read(root, relative)) == result["sha256"]
    if not unchanged or any(Path(str(path) + s).exists() for s in ("-wal", "-shm", "-journal")):
        raise ValueError("snapshot_or_sidecar_changed")
    result.update(opened=True, result="historical_static_rollback_aggregate_only", columns=columns,
                  status_counts=counts, row_count=sum(counts.values()), unchanged_after_read=True,
                  unresolved_states_retained={k: v for k, v in counts.items() if k in {"leased", "sent", "unknown", "failed", "denied", "denied_scope"}})
    if identity_statuses:
        result["identity_statuses"] = [{"old_identity_sha256": _hash(str(identity).encode()), "status": status} for identity, status in facts]
    return result


def _snapshots(root: Path) -> dict:
    views, unique = [], {}
    identity_states = {}
    snapshot_identity_views = set()
    for path, table, receipt in _SNAPSHOTS:
        # Read the independently retained historical review before touching DB.
        review, proof = _json(root, receipt)
        pointer = _BACKUP_SHA_POINTERS.get(receipt)
        expected = review.get(pointer[1:]) if pointer else None
        if pointer and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError("explicit_snapshot_backup_sha_required")
        result = snapshot_summary(root, path, table, identity_statuses=True, expected_sha256=expected)
        result["historical_snapshot_review"] = proof
        result["review_scope"] = "exact_sha_backup" if expected == result["sha256"] else "same_directory_review_without_independent_database_sha"
        if pointer:
            result["historical_snapshot_review"] = {**proof, "pointer": pointer, "declared_database_sha256": expected,
                "matches_database_sha256": expected == result["sha256"]}
        digest = result["sha256"]
        result["duplicate_of"] = unique.get(digest)
        unique.setdefault(digest, path)
        facts = result.pop("identity_statuses", [])
        for fact in facts:
            identity = fact["old_identity_sha256"]
            snapshot_identity_views.add((digest, table, identity))
            entry = identity_states.setdefault((table, identity), {"statuses": set(), "snapshot_sha256": set()})
            entry["statuses"].add(fact["status"])
            entry["snapshot_sha256"].add(digest)
        result["identity_status_set_sha256"] = _hash(_canonical(sorted(facts, key=lambda f: (f["old_identity_sha256"], f["status"]))))
        views.append(result)
    def state_rows(predicate):
        return [{"table": table, "old_identity_sha256": identity, "statuses": sorted(v["statuses"]),
                 "snapshot_sha256": sorted(v["snapshot_sha256"]), "dispatch_allowed": False}
                for (table, identity), v in sorted(identity_states.items()) if predicate(v["statuses"])]
    protected = {"leased", "sent", "unknown", "failed", "denied", "denied_scope", "attempting", "pending_review"}
    return {"file_views": len(views), "unique_database_bytes_sha256": len(unique), "snapshots": views,
            "snapshot_table_old_identity_views": len(snapshot_identity_views),
            "unique_table_old_identities": len(identity_states),
            "identity_status_set_counts": dict(sorted(Counter("|".join(sorted(v["statuses"])) for v in identity_states.values()).items())),
            "conflicting_identity_states": _set_summary(state_rows(lambda states: len(states) > 1)),
            "protected_identity_states": _set_summary(state_rows(lambda states: bool(states & protected))),
            "not_additive_task_counts": True, "current_production_snapshot": False,
            "snapshot_status_does_not_override_prior_attempts": True}


def journal_summary(value: dict) -> dict:
    """Keep legacy keys and state facts; all identities are non-dispatchable."""
    dataset = value.get("dataset", "")
    # The legacy DailyRequestQueue explicitly defaults omitted granularity to
    # day.  No dataset/period is inferred from a file or worksheet title.
    granularity = value.get("granularity", "day")
    tasks = decode_journal(value, QueueContext("shanxi", "offline-review", period_kind=granularity, operation="export"))
    keys = sorted(_hash(_canonical([dataset, granularity, t.legacy_key])) for t in tasks)
    return {"dataset": dataset, "granularity": granularity, "content_sha256": _hash(_canonical(value)),
            "legacy_task_keys_sha256": _hash(_canonical(keys)), "task_count": len(tasks),
            "states": dict(sorted(Counter(t.state for t in tasks).items())),
            "legacy_states": dict(sorted(Counter(t.legacy_state for t in tasks).items())),
            "consumed_or_held_count": sum(t.consumed for t in tasks),
            "unresolved_kept": sum(t.state in {"unknown", "held"} for t in tasks),
            "dispatch_allowed": False}


def _journals(root: Path) -> dict:
    path = "verification/20261025/pause_repair_1719/queues_before.json"
    value, proof = _json(root, path)
    _, queue_code = _json_text_ref(root, "powercrawler/queue.py")
    rows = []
    unique_keys = set()
    identity_states = {}
    def retain(key, journal, receipt):
        row = journal_summary(journal)
        row.update(legacy_path=key, receipt=receipt)
        rows.append(row)
        granularity = journal.get("granularity", "day")
        tasks = decode_journal(journal, QueueContext("shanxi", "offline-review", period_kind=granularity, operation="export"))
        for task in tasks:
            key_hash = _hash(_canonical([journal["dataset"], granularity, task.legacy_key]))
            unique_keys.add(key_hash)
            entry = identity_states.setdefault(key_hash, {"states": set(), "journal_sha256": set()})
            entry["states"].add(task.state)
            entry["journal_sha256"].add(row["content_sha256"])

    for key, journal in sorted(value.items()):
        if not re.fullmatch(r"state/crawlee_[A-Za-z0-9_]+/batch_journal\.json", key):
            raise ValueError("unexpected_journal_snapshot_key")
        retain(key, journal, {**proof, "pointer": "/" + key.replace("/", "~1")})
    embedded_count = len(rows)
    archive_path = "state/tencent_redeploy_20261014.P8SetV/tencent_redeploy_progress_20261014.tar.gz"
    archive_raw = _read(root, archive_path)
    archive_ref = {"path": archive_path, "sha256": _hash(archive_raw), "bytes": len(archive_raw)}
    # Member names were inventoried first.  Read exactly five business journals,
    # without unpacking other state, credentials or source members to disk.
    allowed_members = {"state/crawlee_" + key + "/batch_journal.json" for key in
                       ("load", "queryTradePrice96", "queryMarketClearingDA", "queryMarketClearingRT", "queryNewEnergyOutputForecast")}
    with tarfile.open(fileobj=io.BytesIO(archive_raw), mode="r:gz") as archive:
        for member in archive:
            if member.name not in allowed_members:
                continue
            if not member.isfile() or member.size > _MAX_JSON:
                raise ValueError("bounded_business_archive_member_required")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("archive_member_unreadable")
            with source:
                raw = source.read(_MAX_JSON + 1)
            if len(raw) != member.size:
                raise ValueError("archive_member_size_mismatch")
            retain(member.name, json.loads(raw), {**archive_ref, "member": member.name, "member_sha256": _hash(raw)})
    # Later files contain hashes only, not journal bodies; they are not counted
    # as extra task records and cannot be reconstructed from the older body.
    hashes = []
    for label in ("first_recovery_state", "activation_recovery_state"):
        _, ref = _json(root, f"verification/20261025/resume_hold_2104/{label}/queue_journal_sha256.json")
        hashes.append(ref)
    state_rows = [{"legacy_task_key_sha256": key, "states": sorted(v["states"]),
                   "journal_sha256": sorted(v["journal_sha256"]), "dispatch_allowed": False}
                  for key, v in sorted(identity_states.items())]
    return {"receipt": proof, "queue_class_source": queue_code, "embedded_journal_views": embedded_count,
            "archive_journal_views": len(rows) - embedded_count, "journal_views": len(rows),
            "unique_canonical_journals": len({r["content_sha256"] for r in rows}),
            "legacy_task_views": sum(r["task_count"] for r in rows), "unique_legacy_task_keys": len(unique_keys),
            "states": dict(sum((Counter(r["states"]) for r in rows), Counter())),
            "identity_state_set_counts": dict(sorted(Counter("|".join(r["states"]) for r in state_rows).items())),
            "conflicting_identity_states": _set_summary([r for r in state_rows if len(r["states"]) > 1]),
            "unknown_identity_states": _set_summary([r for r in state_rows if set(r["states"]) & {"unknown", "held"}]),
            "journals": rows, "later_digest_only_snapshots": hashes,
            "production_storage_or_requestqueue_export_complete": False, "dispatch_allowed": False}


def _json_text_ref(root: Path, path: str):
    raw = _read(root, path)
    return raw, {"path": path, "sha256": _hash(raw), "bytes": len(raw)}


def _workbooks(root: Path) -> dict:
    manifest_path = "data/2026/original/manifest.jsonl"
    raw, proof = _json_text_ref(root, manifest_path)
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    indexed = {r["path"] for r in rows}
    paths = sorted(p.relative_to(root).as_posix() for p in (root / "data/2026/original").rglob("*") if p.suffix.lower() in {".xls", ".xlsx"} and p.is_file())
    extra = [p for p in paths if p not in indexed]
    # Bytes/hashes only.  No attempt is made to invent source receipt/identity
    # from folder names, title equality, or a physically present workbook.
    records = [{"path": path, "sha256": _hash(_read(root, path)), "consumption_proven": False} for path in extra]
    return {"manifest": proof, "manifest_records": len(rows), "manifest_unique_paths": len(indexed),
            "physical_workbooks": len(paths), "outside_manifest": len(extra),
            "outside_manifest_index_sha256": _hash(_canonical(records)), "outside_manifest_files": records,
            "newly_confirmed_tasks": 0, "meaning": "bytes_only_not_source_scope_receipt_or_consumption_proof"}


def _archives(root: Path, missing_ids: list[str]) -> list[dict]:
    directory = root / "verification/20261022/tab_section_completion"
    result = []
    # List metadata only in the explicitly identified historical handoff folder.
    for path in sorted(directory.glob("*.tar")):
        matches = []
        count = 0
        with tarfile.open(path, "r:") as archive:
            for member in archive:
                count += 1
                name = member.name
                if _FORBIDDEN.search(name) or ".." in Path(name).parts or not member.isfile():
                    continue
                if any(key in name for key in missing_ids) or re.search(r"queue.*\.sqlite3$|batch_journal\.json$", name):
                    matches.append({"member": name, "bytes": member.size})
        result.append({"path": path.relative_to(root).as_posix(), "member_count": count,
                       "target_business_members": matches, "extracted": False})
    return result


def recover(root: Path) -> dict:
    """Return a new metadata-only report; caller decides where to save it.

    ``root`` may be the repository root or its Shanxi directory.  All inputs
    are explicit local originals, receipts, or historical snapshots.
    """
    root = Path(root).resolve()
    province = root if root.name == "shanxi" else root / "shanxi"
    baseline_path = province.parent / "pmos/evidence/shanxi_phase2.json"
    baseline_raw = baseline_path.read_bytes()
    baseline = json.loads(baseline_raw)
    sources, inputs = _sources(province, baseline)
    return {"province": "shanxi", "mode": "read_only_local_evidence_recovery", "website_requests": 0,
            "production_writes": 0, "production_queue_or_service_touched": False, "dispatch_allowed": False,
            "baseline": {"path": "pmos/evidence/shanxi_phase2.json", "sha256": _hash(baseline_raw),
                "source_count": baseline["source_count"], "missing_source_count": len(sources),
                "phase2_qualification_counts_unchanged": baseline["source_qualification_counts"],
                "historical_denials_retained": baseline["historical_denials_retained"]},
            "source_recovery_counts": dict(sorted(Counter(s["recovery_status"] for s in sources).items())),
            "sources": sources, "receipt_inputs": inputs, "native_snapshots": _snapshots(province),
            "crawlee_journals": _journals(province), "workbook_inventory": _workbooks(province),
            "archive_member_inventory": _archives(province, [s["dataset"] for s in sources]),
            "limitations": ["Historical snapshot views overlap and are not additive unique tasks.",
                "Recovered empty queries and selectors do not prove nonempty data or a full historical range.",
                "A newer successful sample never clears older denied, leased, sent, failed or unknown facts.",
                "No source qualification, production queue, owner, pacing or keepalive was changed.",
                "Workbook bytes outside the manifest do not prove consumption; source scope must be bound independently.",
                "Raw business rows and selector values are not copied into this report."]}
