"""Read-only continuation for Anhui's fixed phase-three remaining cohort.

Old keys/facts are recovered from exact current records or retained snapshots.
Article IDs and titles remain in RAM. Nothing here imports a legacy producer,
opens a database, sends a request, or writes a file.
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

from .anhui import _Reader, _article_key, _identity
from .invariants import validate_queue_recovery
from ..adapters.anhui import project_bound, validate_retained_binding
from ..contracts import canonical
from ..faults import Fault
from ..queue_compat import QueueContext, decode_jobs
from ..validate import Response, evaluate


PHASE3_SHA256 = "a39390f063c6b31d8479d370ccf63c3157a016d49cf407c794c410107e4a8814"
RULES = {
    "powercrawler/publications.py": "8dfd9c51054b72d2d02020b08878fd6ddb267e26c0fd2cc76c245557be96e420",
    "powercrawler/native_publications.py": "80b2792c78432f9bc92672be379edf88fe18ca917e676557709acd410533fe54",
    "deploy/mac_job_native_publication_adopt.py": "cb47f2c3463c017a7d0025ea8d1741f1beafe476b7206e92009a1864535518d3",
}
SNAPSHOTS = ("state/mz_migration_20261022_093815/remote_snapshot/state",
             "state/word_alignment_20261022/audit_workspace/state")
BINDING_STATE = {
    "publication_66d506c8a46471d9": "state/server_native_table_binding_ca372d45404fbfd9deea6aee.json",
    "day_ahead_average_offer_price": "state/server_native_table_binding_b09a9411524644878c4c5e13.json",
}
PRIVATE = re.compile(r"(?i)private|profile|credential|password|cookie|token|secret|auth|config|browser|session|proxy|login|keepalive|http_failure|runtime_control|\.har$|\.sqlite")
ALLOWED_SUFFIXES = {".json", ".jsonl", ".py", ".png", ".jpg", ".pdf", ".xlsx", ".md"}


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _normalized_title(value):
    if not isinstance(value, str):
        raise ValueError("article_title_required")
    return re.sub(r"\s+", " ", value).strip()


class Reader(_Reader):
    def raw(self, relative, expected=None):
        path = Path(relative)
        if (path.suffix not in ALLOWED_SUFFIXES or any(PRIVATE.search(part) for part in path.parts)
                or not path.parts or path.parts[0] not in {"state", "data", "exports", "docs", "powercrawler", "deploy"}):
            raise ValueError("public_remaining_evidence_path_required")
        return super().raw(relative, expected)


def _relative(root, reference):
    path = Path(reference)
    if path.is_absolute():
        return str(path.relative_to(root))
    if path.parts and path.parts[0] == root.name:
        return str(path.relative_to(root.name))
    return str(path)


def _baseline_bytes(root, name, expected=None):
    workspace = root.parent.resolve()
    parts = ("pmos", "evidence", name)
    path = workspace.joinpath(*parts)
    if (any(workspace.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts) + 1))
            or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024):
        raise ValueError("controlled_remaining_baseline_required")
    raw = path.read_bytes()
    if expected is not None and _sha(raw) != expected:
        raise ValueError("remaining_baseline_sha256_changed")
    return raw


def _load_baseline(root, supplied=None, queue_supplied=None):
    refs, checks = [], []
    if supplied is None:
        raw = _baseline_bytes(root, "anhui_phase3_recovery.json", PHASE3_SHA256)
        supplied = json.loads(raw)
        refs.append({"path": "pmos/evidence/anhui_phase3_recovery.json", "sha256": _sha(raw)})
        checks.append(("anhui_phase3_recovery.json", _sha(raw)))
    if queue_supplied is None:
        expected = next((r["sha256"] for r in supplied.get("baseline_refs", [])
                         if r["path"].endswith("anhui_queue_phase2.json")), None)
        if expected is None:
            raise ValueError("committed_phase2_queue_baseline_required")
        raw = _baseline_bytes(root, "anhui_queue_phase2.json", expected)
        queue_supplied = json.loads(raw)
        refs.append({"path": "pmos/evidence/anhui_queue_phase2.json", "sha256": _sha(raw)})
        checks.append(("anhui_queue_phase2.json", _sha(raw)))
    return supplied, queue_supplied, refs, checks


def _old_listing_scope(reader, baseline, dataset):
    """Anchor menu to a source/parsed pair already committed by phase three."""
    references = {row["path"]: row for row in baseline["references"].values()}
    prefix = str(Path(reader.root.name) / "data/2026/native_listing_sources" / dataset) + "/"
    menus, refs = {}, []
    for reference, proof in references.items():
        if not reference.startswith(prefix) or not reference.endswith(".json"):
            continue
        relative = _relative(reader.root, reference)
        value = reader.json(relative, proof["sha256"])
        if value.get("dataset") != dataset or not isinstance(value.get("menu_path"), list):
            continue
        source = value.get("source_path")
        source_proof = references.get(str(Path(reader.root.name) / (source or "")))
        if (not source_proof or source_proof["sha256"] != value.get("source_sha256")
                or value.get("all_ui_listing_rows_matched") is not True):
            continue
        source_raw = reader.raw(source, source_proof["sha256"])
        evaluate({"kind": "json"}, Response(200, "application/json", source_raw))
        menus[_sha(canonical(value["menu_path"]))] = value["menu_path"]
        refs.extend([reader.ref(relative)["path"], reader.ref(source)["path"]])
    if len(menus) != 1:
        raise ValueError("committed_old_menu_scope_required")
    return next(iter(menus.values())), sorted(set(refs))


def _same_fact(task, old):
    return (task.facts_sha256 == old["facts_sha256"] and task.state == old["state"]
            and task.consumed == old["consumed"] and task.identity is None)


def _cohort(reader, baseline, queue_base):
    required = {(row["source_view_id"], row["legacy_key_sha256"]): row for row in baseline["unresolved_records"]}
    expected_attempts = {_relative(reader.root, row["path"]): row["sha256"] for row in queue_base["source_refs"]}
    before, items, missing, drifts = {}, [], [], []
    for sid in sorted({key[0] for key in required}):
        view = baseline["queue_views"][sid]
        source_ref, dataset = view["source_ref"], view["dataset"]
        relative = _relative(reader.root, source_ref)
        current = reader.json(relative)
        queue_changed = reader.ref(relative)["sha256"] != view["sha256"]
        if queue_changed:
            drifts.append({"path": reader.ref(relative)["path"], "baseline_sha256": view["sha256"],
                           "observed_sha256": reader.ref(relative)["sha256"], "group": "current_queue_vs_phase3"})
            menu, scope_refs = _old_listing_scope(reader, baseline, dataset)
        else:
            menu, scope_refs = current.get("menu_path"), [reader.ref(relative)["path"]]
        attempt_ref = f"state/server_native_attempts_{dataset.lower()}.json"
        attempts, attempt_refs = None, []
        if attempt_ref in expected_attempts:
            attempts = reader.json(attempt_ref, expected_attempts[attempt_ref])
            attempt_refs.append(reader.ref(attempt_ref)["path"])
        context = QueueContext("anhui", "offline-shadow-review", dataset)
        queues = [(relative, current)] if current.get("menu_path") == menu else []
        for prefix in SNAPSHOTS:
            snapshot = prefix + "/" + Path(relative).name
            if (reader.root / snapshot).is_file():
                value = reader.json(snapshot)
                if value.get("menu_path") == menu:
                    queues.append((snapshot, value))
        before[source_ref] = []
        for (source_id, key_sha), old in required.items():
            if source_id != sid:
                continue
            chosen = None
            for reference, queue in queues:
                for task in decode_jobs(queue, context, attempts=attempts):
                    if _sha(task.legacy_key.encode()) == key_sha and _same_fact(task, old):
                        job = queue["jobs"].get(task.legacy_key)
                        if (isinstance(job, dict) and _article_key(job.get("title"), job.get("publication_date")) == task.legacy_key):
                            chosen = (task, job, queue, reference)
                            break
                if chosen:
                    break
            if chosen is None:
                missing.append({"source_view_id": sid, "legacy_key_sha256": key_sha,
                    "facts_sha256": old["facts_sha256"], "state": old["state"], "consumed": old["consumed"],
                    "qualification": "old_record_not_reconstructed_exactly_no_recovery"})
                continue
            task, job, queue, reference = chosen
            before[source_ref].append(task)
            mode = "exact_old_snapshot_record" if reference != relative else (
                "exact_phase3_queue" if reader.ref(relative)["sha256"] == view["sha256"] else "exact_phase3_record_in_changed_queue")
            items.append({"sid": sid, "source_ref": source_ref, "task": task, "job": job,
                "menu_path": queue.get("menu_path"), "cohort_proof": mode,
                "menu_scope_sha256": _sha(canonical(menu)),
                "evidence_refs": [reader.ref(reference)["path"], *attempt_refs, *scope_refs]})
    return before, items, missing, drifts


def _published(row, field):
    value = row.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value / 1000, ZoneInfo("Asia/Shanghai")).date().isoformat()


def _subject(row, job, fields):
    if _normalized_title(row.get("title")) != _normalized_title(job.get("title")):
        return None
    dates = [field for field in fields if _published(row, field) == job.get("publication_date")]
    guid, information = row.get("guid"), row.get("informationId")
    if (not dates or not isinstance(guid, str) or not guid.strip()
            or not isinstance(information, str) or not information.strip()):
        return None
    return guid, information, dates


def _page_number(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    raise ValueError("strict_positive_listing_page_required")


def _listing_candidates(reader, item):
    dataset, job = item["task"].dataset, item["job"]
    marker_ref = f"state/server_native_listing_sources_{dataset.lower()}.json"
    if not (reader.root / marker_ref).is_file():
        return []
    markers = reader.json(marker_ref)
    found = []
    for marker in markers.values():
        raw = reader.raw(marker["source_path"], marker["source_sha256"])
        evaluate({"kind": "json"}, Response(200, "application/json", raw))
        parsed = reader.json(marker["parsed_path"], marker["parsed_sha256"])
        payload = json.loads(raw)
        data = payload.get("data", {})
        rows = data.get("list")
        field = parsed.get("publication_date_field")
        if (parsed.get("dataset") != dataset or parsed.get("menu_path") != item["menu_path"]
                or parsed.get("source_path") != marker["source_path"]
                or parsed.get("source_sha256") != marker["source_sha256"]
                or parsed.get("all_ui_listing_rows_matched") is not True
                or parsed.get("publication_date_is_not_statistical_period") is not True
                or field not in {"validDate", "operateDate"} or not isinstance(rows, list)
                or type(marker.get("source_rows")) is not int or type(parsed.get("source_rows")) is not int
                or len(rows) != marker["source_rows"] or parsed["source_rows"] != marker["source_rows"]
                or type(data.get("pageNum")) is not int or type(data.get("pageSize")) is not int
                or type(data.get("total")) is not int or data["pageSize"] < 1 or data["total"] < 0
                or data["pageNum"] != _page_number(marker["page"])
                or data["pageNum"] != _page_number(parsed["page"])
                or not 1 <= data["pageNum"] <= max(1, (data["total"] + data["pageSize"] - 1) // data["pageSize"])
                or len(rows) != max(0, min(data["pageSize"], data["total"] - (data["pageNum"] - 1) * data["pageSize"]))):
            raise ValueError("remaining_listing_chain_invalid")
        shown = {(a["title"], a["publication_date"]) for a in parsed["articles"]}
        for row in rows:
            subject = _subject(row, job, [field])
            if subject is None or (row["title"], _published(row, field)) not in shown:
                continue
            guid, information, fields = subject
            found.append({"guid": guid, "information": information, "date_fields": fields,
                "kind": "same_menu_listing_source_and_parsed_sha_verified",
                "title_form": "exact" if row["title"] == job["title"] else "legacy_whitespace_normalization",
                "refs": [reader.ref(marker_ref)["path"], reader.ref(marker["source_path"])["path"],
                         reader.ref(marker["parsed_path"])["path"]]})
    return found


def _native_candidates(reader, item):
    task, job = item["task"], item["job"]
    dataset, key = task.dataset, task.legacy_key
    found, markers = [], []
    for kind in ("attachment", "image"):
        reference = f"state/server_native_article_{kind}_{dataset.lower()}_{key}.json"
        if (reader.root / reference).is_file():
            value = reader.json(reference)
            if value.get("article_key") == key:
                markers.append((value, [reader.ref(reference)["path"]], "retained_article_marker_and_source_sha_verified"))
    if dataset == "interregional_exchange_monthly" and isinstance(job.get("native_discovery_receipt"), str):
        name = job["native_discovery_receipt"]
        if re.fullmatch(r"[a-zA-Z0-9_]{1,160}", name):
            discovery_ref = "state/server_" + name + ".json"
            discovery = reader.json(discovery_ref)
            adoption_ref = "state/server_native_publication_adoption_20261023.json"
            adoption = reader.json(adoption_ref)
            if (discovery.get("dataset") == dataset and discovery.get("job_key") == key
                    and all(discovery.get("job", {}).get(field) == job.get(field) for field in ("title", "publication_date"))):
                captures = {(x["path"], x["sha256"]) for x in job.get("page_captures", [])}
                for result in adoption.get("results", []):
                    record = result.get("record", {})
                    if (result.get("dataset") == dataset and result.get("source_path")
                            and record.get("dataset") == dataset
                            and (record.get("path"), record.get("sha256")) in captures):
                        capture = reader.json(record["path"], record["sha256"])
                        if (capture.get("dataset") != dataset or capture.get("menu_path") != item["menu_path"]
                                or capture.get("publication_title") != job.get("title")
                                or capture.get("publication_date") != job.get("publication_date")):
                            raise ValueError("adopted_capture_same_menu_identity_required")
                        markers.append((result, [reader.ref(discovery_ref)["path"], reader.ref(adoption_ref)["path"],
                            reader.ref(record["path"])["path"]], "exact_discovery_adoption_capture_and_article_source_sha_verified"))
    for marker, refs, kind in markers:
        source = marker["source_path"]
        parent = Path(source).parts
        if (parent[:4] != ("data", "2026", "native_publications", dataset) or len(parent) != 6 or parent[4] != key):
            raise ValueError("exact_dataset_article_path_required")
        raw = reader.raw(source, marker["source_sha256"])
        if Path(source).stem != _sha(raw):
            raise ValueError("article_content_addressed_source_required")
        row = json.loads(raw)
        subject = _subject(row, job, ["validDate", "operateDate"])
        if subject is not None:
            guid, information, fields = subject
            found.append({"guid": guid, "information": information, "date_fields": fields, "kind": kind,
                "title_form": "exact" if row["title"] == job["title"] else "legacy_whitespace_normalization",
                "refs": [*refs, reader.ref(source)["path"]]})
    return found


def _article_recovery(reader, before, items):
    after = {source: list(rows) for source, rows in before.items()}
    recovered, remaining = [], []
    for item in items:
        task = item["task"]
        key_sha = _sha(task.legacy_key.encode())
        row = {"source_view_id": item["sid"], "source_ref_sha256": _sha(item["source_ref"].encode()),
            "legacy_key_sha256": key_sha, "dataset": task.dataset, "legacy_state": task.legacy_state,
            "state": task.state, "consumed": task.consumed, "facts_sha256": task.facts_sha256,
            "protective_blockers": sorted(set(task.blockers) - {"identity_mapping_required"}),
            "cohort_proof": item["cohort_proof"], "menu_scope_sha256": item["menu_scope_sha256"],
            "dispatch_allowed": False}
        candidates = _listing_candidates(reader, item) + _native_candidates(reader, item)
        subjects = {(x["guid"], x["information"]) for x in candidates}
        if len(subjects) != 1:
            remaining.append({**row, "qualification": "independent_article_identity_missing_or_ambiguous",
                              "candidate_identity_count": len(subjects)})
            continue
        guid, information = next(iter(subjects))
        identity = _identity(task.dataset, "undated", "snapshot", {"record_id": guid}, operation="article")
        repaired = replace(task, identity=identity,
            blockers=tuple(value for value in task.blockers if value != "identity_mapping_required"))
        after[item["source_ref"]] = [repaired if old.legacy_key == task.legacy_key else old
                                      for old in after[item["source_ref"]]]
        refs = set(item["evidence_refs"])
        refs.update(reader.ref(relative)["path"] for relative in RULES)
        refs.update(reference for value in candidates for reference in value["refs"])
        recovered.append({**row, "identity_sha256": identity.key, "record_id_sha256": _sha(canonical(guid)),
            "information_id_sha256": _sha(canonical(information)), "identity_kind": "snapshot", "operation": "article",
            "evidence_refs": sorted(refs), "evidence_kinds": sorted({x["kind"] for x in candidates}),
            "title_forms": sorted({x["title_form"] for x in candidates}),
            "publication_date_is_not_statistical_period": True})
    return after, recovered, remaining


def _bindings(reader, baseline):
    rows = []
    for old in baseline["bindings"]["remaining"]:
        expected = old["parsed_precommitted_binding_sha256"]
        item = {"dataset": old["dataset"], "source_ref": old["source_ref"], "source_sha256": old["source_sha256"],
            "parsed_ref": old["parsed_ref"], "parsed_sha256": old["parsed_sha256"], "precommitted_binding_sha256": expected,
            "qualification": "exact_binding_copy_still_missing", "original_capture_was_mutable": True}
        parsed = reader.json(_relative(reader.root, old["parsed_ref"]), old["parsed_sha256"])
        raw = reader.raw(_relative(reader.root, old["source_ref"]), old["source_sha256"])
        if parsed.get("binding_sha256") != expected:
            raise ValueError("remaining_binding_commitment_changed")
        reference = f"data/2026/native_tables/_bindings/{expected}.json"
        if (reader.root / reference).is_file():
            try:
                binding = reader.json(reference, expected)
                receipt = {"dataset": old["dataset"], "source_path": _relative(reader.root, old["source_ref"]),
                    "period": parsed["query_period"], "selectors": parsed["selectors"]}
                validate_retained_binding(receipt, parsed, binding)
                if project_bound(json.loads(raw, parse_float=Decimal), binding["model"], period=receipt["period"]) != parsed["rows"]:
                    raise ValueError("remaining_binding_projection_mismatch")
                item.update(qualification="precommitted_exact_binding_copy_verified", binding_ref=reader.ref(reference))
            except (ValueError, KeyError, TypeError, Fault):
                item["qualification"] = "present_binding_not_verified"
        state_ref = BINDING_STATE.get(old["dataset"])
        if state_ref and (reader.root / state_ref).is_file():
            current = reader.json(state_ref)
            item["current_mutable_binding_ref"] = reader.ref(state_ref)
            item["current_mutable_canonical_sha256"] = _sha(json.dumps(current, ensure_ascii=False, sort_keys=True, default=str).encode())
            item["current_mutable_matches_commitment"] = item["current_mutable_canonical_sha256"] == expected
        rows.append(item)
    count = sum(row["qualification"] == "precommitted_exact_binding_copy_verified" for row in rows)
    return {"fixed_capture_cohort": len(rows), "exact_copies_recovered": count, "remaining_captures": len(rows) - count, "entries": rows}


def _candidate_paths(value):
    if isinstance(value, str):
        if value.startswith(("state/", "data/", "docs/", "verification/")):
            return [value]
        if value.startswith("server_") and value.endswith(".json"):
            return ["state/" + value]
        if re.fullmatch(r"[a-zA-Z0-9_]{1,180}", value):
            return ["state/server_" + value + ".json"]
    if isinstance(value, dict):
        return [path for child in value.values() for path in _candidate_paths(child)]
    if isinstance(value, list):
        return [path for child in value for path in _candidate_paths(child)]
    return []


def _candidates(reader, baseline):
    inventory_ref = "state/server_metric_coverage_inventory.json"
    inventory = reader.json(inventory_ref)
    inventory_entries = {(x["surface"], x["source_index"]): x for x in inventory["entries"]}
    works = {surface: reader.json(reference)["entries"] for surface, reference in {
        "disclosure": "state/server_all_site_metric_worklist.json", "portal": "state/server_portal_metric_worklist.json"}.items()}
    rows = []
    for old in baseline["candidates"]["entries"]:
        surface, index = old["surface"], old["source_index"]
        entry = inventory_entries.get((surface, index), {})
        work = works[surface][index]
        matched = bool(entry) and entry.get("menu_path") == work.get("menu_path")
        refs, absent, excluded = {reader.ref(inventory_ref)["path"]}, [], 0
        if matched:
            paths = {path for field, value in work.items() if "receipt" in field or "evidence" in field
                     for path in _candidate_paths(value)}
            for reference in sorted(paths):
                if PRIVATE.search(reference) or not reference.startswith(("state/", "data/", "docs/")):
                    excluded += 1
                    continue
                path = reader.root / reference
                if not path.is_file():
                    absent.append(reference)
                    continue
                if path.suffix not in ALLOWED_SUFFIXES:
                    excluded += 1
                    continue
                reader.raw(reference)
                refs.add(reader.ref(reference)["path"])
                if path.suffix == ".json":
                    value = reader.json(reference)
                    if isinstance(value, dict):
                        for kind in ("source", "parsed", "overview"):
                            ref, sha = value.get(kind + "_path"), value.get(kind + "_sha256")
                            if isinstance(ref, str) and re.fullmatch(r"[0-9a-f]{64}", sha or ""):
                                reader.raw(ref, sha)
                                refs.add(reader.ref(ref)["path"])
        rows.append({"surface": surface, "source_index": index, "legacy_entry_sha256": old["legacy_entry_sha256"],
            "same_inventory_and_worklist_menu": matched, "evidence_refs": sorted(refs),
            "missing_public_references": absent, "excluded_unreviewed_references": excluded,
            "qualification": "candidate_not_admitted_independent_business_original_required", "admission_changed": False})
    return {"fixed_candidate_cohort": len(rows), "entries_with_receipt_or_image_chain": sum(len(x["evidence_refs"]) > 1 for x in rows),
            "new_independent_qualifications": 0, "remaining_candidates": len(rows), "entries": rows}


def _carriers(reader, baseline):
    rows, original_hashes = [], set()
    for old in baseline["legacy_carriers"]["groups"]:
        refs = []
        for entry in old["artifact_refs"]:
            relative = _relative(reader.root, entry["path"])
            reader.raw(relative, entry["sha256"])
            refs.append(reader.ref(relative)["path"])
            if Path(relative).suffix == ".xlsx":
                original_hashes.add(entry["sha256"])
        rows.append({"dataset": old["dataset"], "qualification": old["baseline_qualification"],
            "artifact_refs": refs, "semantic_acceptance": "pending", "qualification_changed": False})
    sidecars = []
    for path in sorted((reader.root / "data/portal/contract_summary").glob("*/*/server_verified_contract_rows.json")):
        relative = str(path.relative_to(reader.root))
        value = reader.json(relative)
        if value.get("source_sha256") in original_hashes:
            sidecars.append({"path": reader.ref(relative)["path"], "sha256": reader.ref(relative)["sha256"],
                "source_sha256": value["source_sha256"],
                "ui_export_crosscheck_proven": value.get("export_summary_crosschecked_with_ui") is True,
                "qualification": "prior_parser_sidecar_source_sha_linked_no_semantic_upgrade"})
    return {"fixed_groups": len(rows), "original_artifacts_reverified": sum(len(x["artifact_refs"]) for x in rows),
            "parser_sidecars_source_sha_linked": len(sidecars),
            "ui_export_crosschecks_proven": sum(x["ui_export_crosscheck_proven"] for x in sidecars),
            "new_semantic_qualifications": 0, "groups": rows, "parser_sidecar_receipts": sidecars}


def _reference_changes(reader, baseline, queue_drifts):
    previous = {row["path"]: row["sha256"] for row in baseline["references"].values()}
    queues = {row["path"] for row in queue_drifts}
    groups = {"current_queue_vs_phase3": queue_drifts, "listing_indexes_vs_phase3": [],
              "reviewed_code_vs_phase3": [], "other_declared_refs_vs_phase3": []}
    for reference, current in reader.refs.items():
        expected = previous.get(reference)
        if expected is None or expected == current["sha256"] or reference in queues:
            continue
        group = ("listing_indexes_vs_phase3" if "/state/server_native_listing_sources_" in reference else
                 "reviewed_code_vs_phase3" if reference.endswith(".py") else "other_declared_refs_vs_phase3")
        groups[group].append({"path": reference, "baseline_sha256": expected, "observed_sha256": current["sha256"]})
    return groups


def _run(root, baseline=None, queue_baseline=None, *, full=True):
    reader = Reader(root)
    baseline, queue_base, baseline_refs, baseline_checks = _load_baseline(reader.root, baseline, queue_baseline)
    for reference, digest in RULES.items():
        reader.raw(reference, digest)
    before, items, missing, drifts = _cohort(reader, baseline, queue_base)
    after, recovered, remaining = _article_recovery(reader, before, items)
    conservation = validate_queue_recovery(before, after)
    if not full:
        if reader.recheck():
            raise ValueError("remaining_inputs_changed_during_read")
        for name, digest in baseline_checks:
            _baseline_bytes(reader.root, name, digest)
        return before, after, None
    bindings = _bindings(reader, baseline)
    candidates = _candidates(reader, baseline)
    carriers = _carriers(reader, baseline)
    changed = reader.recheck()
    if changed:
        raise ValueError("remaining_inputs_changed_during_read")
    for name, digest in baseline_checks:
        _baseline_bytes(reader.root, name, digest)
    summary = {"fixed_article_cohort": len(baseline["unresolved_records"]), "cohort_records_reconstructed": len(items),
        "article_identities_recovered": len(recovered), "article_identities_remaining": len(remaining) + len(missing),
        "binding_copies_recovered": bindings["exact_copies_recovered"], "binding_captures_remaining": bindings["remaining_captures"],
        "candidates_remaining": candidates["remaining_candidates"], "new_semantic_qualifications": 0}
    report = {"schema_version": 1, "province": "anhui", "mode": "fixed_phase3_remaining_cohort_readonly",
        "summary": summary, "baseline_refs": baseline_refs, "cohort_conservation": conservation,
        "queue_identity_bridges": recovered, "remaining_article_records": remaining, "unavailable_old_records": missing,
        "bindings": bindings, "candidates": candidates, "legacy_carriers": carriers,
        "drift_groups": _reference_changes(reader, baseline, drifts),
        "references": sorted(reader.refs.values(), key=lambda x: x["path"]),
        "inputs_changed_during_scan": [], "production_writes": 0, "website_requests": 0,
        "dispatch_allowed": False, "queue_import_ready": False, "live_handoff_completed": False,
        "limitations": ["Only the fixed old key/facts commitments are eligible; current successes never replace old pending facts.",
            "Whitespace normalization follows inspected legacy code, with exact old key, source/menu and unique ID proofs.",
            "Publication dates prove listing identity, never statistical periods; article snapshots stay undated.",
            "Receipt links do not grant candidate admission, attachment completeness, field semantics or online acceptance."]}
    return before, after, report


def baseline_queue_records(root, *, baseline=None, queue_baseline=None):
    """Return only the exact remaining old records, under their phase3 namespaces."""
    return _run(root, baseline, queue_baseline, full=False)[0]


def resolve_queue_records(root, *, baseline=None, queue_baseline=None):
    """Enrich identities in RAM, retaining all states/facts/protective blockers."""
    return _run(root, baseline, queue_baseline, full=False)[1]


def recover(root, *, baseline=None, queue_baseline=None):
    """Return metadata/hash-only results; the caller owns any permitted output."""
    return _run(root, baseline, queue_baseline)[2]
