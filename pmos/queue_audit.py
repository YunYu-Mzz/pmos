"""Audit ONLY declared local queue documents; never freeze or modify a worker.

Source state may keep advancing. Every result records a rolling-read boundary
and protects missing/orphan/unknown facts instead of claiming a cutover export.
"""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import tempfile

from .contracts import canonical
from .ledger import Ledger
from .queue_compat import (QueueContext, decode_journal, decode_jobs, decode_liaoning, decode_frontier,
                           import_shadow, read_json, summarize)


def _source_rule_refs(root):
    rules, refs = {}, []
    for relative in ("lncrawler/expanded_adapters.py", "lncrawler/sitewide_specs.py"):
        path = root / relative
        if not path.is_file():
            continue
        raw = path.read_bytes()
        refs.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
        for node in ast.walk(ast.parse(raw)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "ReportAdapter" or len(node.args) < 4:
                continue
            try:
                dataset = ast.literal_eval(node.args[0])
                period_field = ast.literal_eval(node.args[3])
                keywords = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
            except (ValueError, TypeError):
                continue
            if isinstance(dataset, str) and period_field and keywords.get("kind", "report") == "report":
                rules[dataset] = {"period_field": period_field, "selection_field": keywords.get("selection_field")}
    return rules, refs


def audit_queues(province: str, root: Path) -> dict:
    if province not in {"liaoning", "anhui", "shanxi"}:
        raise ValueError("phase_two_province_required")
    root = root.resolve()
    records, missing, failures, refs, drifts = [], [], [], [], []
    counts = Counter()
    consumed = mapped = 0
    database = None
    with tempfile.TemporaryDirectory(prefix="pmos-queue-review-") as folder:
        database = Ledger(Path(folder) / "shadow.sqlite3")
        def source(relative):
            path = root / relative
            if not path.resolve().is_relative_to(root) or any(x in path.parts for x in ("reporting-private", "private")):
                raise ValueError("declared_public_queue_path_required")
            if not path.is_file():
                missing.append(relative)
                return None, None
            value, digest = read_json(path)
            refs.append({"path": str(path), "sha256": digest})
            return value, digest

        def review(relative, value, digest, decoder, context, **kwargs):
            nonlocal consumed, mapped
            if value is None:
                return
            try:
                tasks = decoder(value, context, **kwargs)
                result = import_shadow(database, tasks, context, source_id=relative, snapshot_sha256=digest)
                counts.update(result["states"])
                consumed += result["consumed"]
                mapped += result["mapped"]
                records.append({"source_ref": str(root / relative), "sha256": digest, **result})
            except (ValueError, TypeError, KeyError) as exc:
                failures.append({"source_ref": str(root / relative), "reason": type(exc).__name__})

        try:
            if province == "liaoning":
                manifest_path = "state/server_verified_expansion_20261023/active_manifest.json"
                manifest, _ = source(manifest_path)
                rules, code_refs = _source_rule_refs(root)
                refs.extend(code_refs)
                kinds = {name: spec.get("period", "snapshot") for name, spec in (manifest or {}).get("sources", {}).items()}
                context = QueueContext(province, "offline-shadow-review")
                for relative in ["state/server_verified_expansion_20261023/queue.json",
                                 "state/server_advanced_tasks_20261016.json", "state/server_sitewide_20261016_tasks.json"]:
                    value, digest = source(relative)
                    review(relative, value, digest, decode_liaoning, context, period_kinds=kinds, source_contracts=rules)
                # Earlier verified samples excluded from the current queue are
                # also consumed facts. Missing identity fields remain held.
                prior = {"sources": {name: {"tasks": {}, "outcomes": spec.get("prior_outcomes", {})}
                         for name, spec in (manifest or {}).get("sources", {}).items()}}
                if manifest:
                    digest = hashlib.sha256(canonical(prior)).hexdigest()
                    review(manifest_path + "#prior_outcomes", prior, digest, decode_liaoning, context,
                           period_kinds=kinds, source_contracts=rules)
                for path in sorted((root / "state/server_engine_collection_20261023").glob("*/queue.json")):
                    relative = str(path.relative_to(root))
                    value, digest = source(relative)
                    dataset = value.get("dataset", path.parent.name)
                    review(relative, value, digest, decode_frontier, QueueContext(province, context.account_alias, dataset, "day"))
            elif province == "anhui":
                plan_path = root / "state/server_production_plan_20261023.json"
                # This explicitly named public plan contains the historical
                # human authorization prose; never use a private configuration.
                plan_raw = plan_path.read_bytes()
                plan = json.loads(plan_raw)
                refs.append({"path": str(plan_path), "sha256": hashlib.sha256(plan_raw).hexdigest()})
                units = plan["units"]
                if not isinstance(units, dict):
                    raise ValueError("declared_plan_units_mapping_required")
                for unit in units.values():
                    dataset = unit["dataset"]
                    if not re.fullmatch(r"[A-Za-z0-9_]{1,120}", dataset):
                        raise ValueError("public_dataset_name_required")
                    attempt_ref = "state/server_native_attempts_" + dataset.lower() + ".json"
                    attempts, _ = source(attempt_ref)
                    queue_key = unit.get("queue_key")
                    if unit["family"] == "daily":
                        queue_ref = "state/server_production_plan_20261023.json#daily:" + dataset
                        value = {"jobs": {period: {"period": period, "status": "pending", "selectors": {"日期": period}}
                                          for period in unit.get("pending_dates", [])}}
                        digest = hashlib.sha256(canonical(value)).hexdigest()
                    elif isinstance(queue_key, str) and re.fullmatch(r"[A-Za-z0-9_]{1,160}", queue_key):
                        queue_ref = "state/server_" + queue_key + ".json"
                        value, digest = source(queue_ref)
                    else:
                        missing.append("declared_queue_for:" + dataset)
                        continue
                    context = QueueContext(province, "offline-shadow-review", dataset,
                        "day" if unit["family"] in {"daily", "anchor", "intertie_web", "web_observed_scope"} else None)
                    review(queue_ref, value, digest, decode_jobs, context, attempts=attempts)
            else:
                # No local native requests/work database is currently retained.
                # Do not connect to a server or pretend an inferred DB exists.
                for directory in ("native_json_collection", "native_publication_collection"):
                    missing.append("state/" + directory + "/queue.sqlite3")
            # Only explicitly named Crawlee journals; no .open(), reserve(),
            # reclaim(), acknowledge() or arbitrary recursive profile scan.
            crawlee_journals = 0
            for directory in ("state/crawlee_system_load", "state/crawlee"):
                base = root / directory
                if base.exists():
                    for path in sorted(base.rglob("batch_journal.json")):
                        crawlee_journals += 1
                        relative = str(path.relative_to(root))
                        value, digest = source(relative)
                        context = QueueContext(province, "offline-shadow-review", period_kind="day")
                        review(relative, value, digest, decode_journal, context)
            if not crawlee_journals:
                missing.append("crawlee_batch_journal_export_not_locally_available")
            for ref in refs:
                path = Path(ref["path"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
                    drifts.append(ref["path"])
            index = database.legacy_summary()
        finally:
            database.close()
    return {"province": province, "mode": "offline_queue_compatibility", "source_views": records,
        "normalized_records": sum(counts.values()), "states": dict(counts), "consumed_facts": consumed,
        "mapped_records": mapped, "unmapped_records": sum(counts.values()) - mapped,
        "missing_local_inputs": sorted(set(missing)), "decode_failures": failures, "source_refs": refs,
        "inputs_changed_during_scan": drifts, "rolling_read_not_frozen": True,
        "overlapping_snapshots_are_not_unique_tasks": True, "temporary_shadow_index": index,
        "production_writes": 0, "website_requests": 0, "live_handoff_completed": False}
