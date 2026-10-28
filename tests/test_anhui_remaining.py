"""Remaining recovery uses exact old commitments and never dispatches work."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pmos.queue_compat import QueueContext, decode_jobs
from pmos.recovery import anhui_remaining as remaining
from pmos.recovery.invariants import validate_queue_recovery
from pmos.faults import Fault


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class RemainingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "anhui"
        self.root.mkdir()
        self.base = {"unresolved_records": [], "queue_views": {}, "references": {},
            "bindings": {"remaining": []}, "candidates": {"entries": []}, "legacy_carriers": {"groups": []}}
        self.queue_base = {"source_refs": []}
        rules = {}
        for reference in remaining.RULES:
            rules[reference] = self.write(reference, b"# Synthetic inspected rule; never executed.\n")
        patcher = patch.object(remaining, "RULES", rules)
        patcher.start()
        self.addCleanup(patcher.stop)
        for reference in ["state/server_metric_coverage_inventory.json", "state/server_all_site_metric_worklist.json",
                          "state/server_portal_metric_worklist.json"]:
            self.save(reference, {"entries": []})

    def write(self, reference, raw):
        path = self.root / reference
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return sha(raw)

    def save(self, reference, value):
        return self.write(reference, json.dumps(value, ensure_ascii=False).encode())

    def declared(self, reference, digest):
        path = "anhui/" + reference
        self.base["references"]["r" + str(len(self.base["references"]))] = {"path": path, "sha256": digest}

    def queue(self, title="Synthetic article", status="pending", dataset="notice"):
        date = "2026-09-01"
        key = remaining._article_key(title, date)
        value = {"dataset": dataset, "menu_path": ["synthetic", "menu"], "jobs": {
            key: {"title": title, "publication_date": date, "period": "2026-08", "status": status}}}
        reference = f"state/server_publication_queue_{dataset}.json"
        digest = self.save(reference, value)
        task = decode_jobs(value, QueueContext("anhui", "offline-shadow-review", dataset), attempts=None)[0]
        sid = "q" + str(len(self.base["queue_views"]))
        self.base["queue_views"][sid] = {"dataset": dataset, "source_ref": str(self.root / reference), "sha256": digest}
        self.base["unresolved_records"].append({"source_view_id": sid, "legacy_key_sha256": sha(key.encode()),
            "facts_sha256": task.facts_sha256, "state": task.state, "consumed": task.consumed})
        self.declared(reference, digest)
        return dataset, key, value, reference

    def listing(self, dataset, rows, menu=None, commit=True):
        menu = menu if menu is not None else ["synthetic", "menu"]
        source = f"data/2026/native_listing_sources/{dataset}/page-1/source.json"
        source_sha = self.save(source, {"status": 0, "data": {
            "pageNum": 1, "pageSize": max(1, len(rows)), "total": len(rows), "list": rows}})
        parsed = f"data/2026/native_listing_sources/{dataset}/page-1/parsed.json"
        parsed_sha = self.save(parsed, {"dataset": dataset, "menu_path": menu, "source_path": source,
            "source_sha256": source_sha, "all_ui_listing_rows_matched": True,
            "publication_date_is_not_statistical_period": True, "publication_date_field": "validDate",
            "source_rows": len(rows), "page": "1",
            "articles": [{"title": row["title"], "publication_date": "2026-09-01"} for row in rows]})
        marker = f"state/server_native_listing_sources_{dataset}.json"
        self.save(marker, {"1": {"source_path": source, "source_sha256": source_sha,
            "parsed_path": parsed, "parsed_sha256": parsed_sha, "source_rows": len(rows), "page": 1}})
        if commit:
            self.declared(source, source_sha)
            self.declared(parsed, parsed_sha)
        return source, parsed

    def article_row(self, title="Synthetic article", guid="synthetic-guid"):
        millis = int(datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
        return {"title": title, "validDate": millis, "guid": guid, "informationId": "synthetic-information"}

    def report(self):
        return remaining.recover(self.root, baseline=self.base, queue_baseline=self.queue_base)

    def records(self, before=False):
        function = remaining.baseline_queue_records if before else remaining.resolve_queue_records
        return function(self.root, baseline=self.base, queue_baseline=self.queue_base)

    def test_whitespace_rule_requires_exact_old_key_same_menu_and_unique_original_id(self):
        dataset, key, _, _ = self.queue(title="Synthetic   article")
        self.listing(dataset, [self.article_row()])
        before, after = self.records(True), self.records()
        check = validate_queue_recovery(before, after)
        self.assertEqual(check["identities_recovered"], 1)
        row = next(iter(after.values()))[0]
        self.assertEqual(row.identity.document()["period"], {"kind": "snapshot", "anchor": "undated"})
        self.assertEqual(row.identity.operation, "article")
        self.assertEqual(row.identity.dimensions, {"record_id": "synthetic-guid"})
        self.assertIn("attempt_index_required", row.blockers)
        encoded = json.dumps(self.report())
        for raw in ["Synthetic   article", "Synthetic article", "synthetic-guid", "synthetic-information", key]:
            self.assertNotIn(raw, encoded)

    def test_same_title_and_date_in_other_menu_cannot_recover(self):
        dataset, _, _, _ = self.queue()
        self.listing(dataset, [self.article_row()], menu=["other", "menu"])
        with self.assertRaisesRegex(ValueError, "listing_chain_invalid"):
            self.report()

    def test_conflicting_guids_for_normalized_title_remain_unmapped(self):
        dataset, _, _, _ = self.queue()
        self.listing(dataset, [self.article_row(), self.article_row(guid="another-guid")])
        self.assertEqual(self.report()["summary"]["article_identities_recovered"], 0)

    def test_listing_source_tamper_is_detected_before_identity_recovery(self):
        dataset, _, _, _ = self.queue()
        source, _ = self.listing(dataset, [self.article_row()])
        self.write(source, b"{}")
        with self.assertRaisesRegex(ValueError, "sha256_changed"):
            self.report()

    def rewrite_listing_chain(self, dataset, *, payload_change=None, parsed_change=None, marker_change=None):
        marker_ref = f"state/server_native_listing_sources_{dataset}.json"
        marker = json.loads((self.root / marker_ref).read_text())["1"]
        payload = json.loads((self.root / marker["source_path"]).read_text())
        if payload_change:
            payload_change(payload)
        marker["source_sha256"] = self.save(marker["source_path"], payload)
        parsed = json.loads((self.root / marker["parsed_path"]).read_text())
        parsed["source_sha256"] = marker["source_sha256"]
        if parsed_change:
            parsed_change(parsed)
        marker["parsed_sha256"] = self.save(marker["parsed_path"], parsed)
        if marker_change:
            marker_change(marker)
        self.save(marker_ref, {"1": marker})

    def test_business_denial_with_retained_rows_cannot_supply_article_id(self):
        dataset, _, _, _ = self.queue()
        self.listing(dataset, [self.article_row()])
        self.rewrite_listing_chain(dataset, payload_change=lambda value: value.update(status=40302))
        with self.assertRaises(Fault):
            self.report()

    def test_nested_business_denial_is_rejected_before_listing_projection(self):
        dataset, _, _, _ = self.queue()
        self.listing(dataset, [self.article_row()])
        self.rewrite_listing_chain(dataset, payload_change=lambda value: value["data"].update(status=40302, message="denied"))
        with self.assertRaises(Fault):
            self.report()

    def test_string_false_proof_flag_is_not_a_verified_ui_binding(self):
        dataset, _, _, _ = self.queue()
        self.listing(dataset, [self.article_row()])
        self.rewrite_listing_chain(dataset, parsed_change=lambda value: value.update(all_ui_listing_rows_matched="false"))
        with self.assertRaisesRegex(ValueError, "listing_chain_invalid"):
            self.report()

    def test_bool_row_count_cannot_masquerade_as_one_verified_row(self):
        dataset, _, _, _ = self.queue()
        self.listing(dataset, [self.article_row()])
        self.rewrite_listing_chain(dataset, parsed_change=lambda value: value.update(source_rows=True))
        with self.assertRaisesRegex(ValueError, "listing_chain_invalid"):
            self.report()

    def test_bool_page_cannot_masquerade_as_first_page(self):
        dataset, _, _, _ = self.queue()
        self.listing(dataset, [self.article_row()])
        self.rewrite_listing_chain(dataset, marker_change=lambda value: value.update(page=True))
        with self.assertRaisesRegex(ValueError, "positive_listing_page"):
            self.report()

    def test_real_old_snapshot_restores_pending_while_current_success_is_ignored(self):
        dataset, key, old, reference = self.queue()
        self.listing(dataset, [self.article_row()])
        snapshot = remaining.SNAPSHOTS[0] + "/" + Path(reference).name
        self.save(snapshot, old)
        changed = deepcopy(old)
        changed["jobs"][key].update(status="saved", originals=[{"path": "data/new.pdf"}])
        self.save(reference, changed)
        report = self.report()
        row = report["queue_identity_bridges"][0]
        self.assertEqual(row["state"], "pending")
        self.assertFalse(row["consumed"])
        self.assertEqual(row["cohort_proof"], "exact_old_snapshot_record")
        self.assertTrue(row["menu_scope_sha256"])
        validate_queue_recovery(self.records(True), self.records())

    def test_changed_current_menu_cannot_reanchor_old_scope_even_with_matching_job_facts(self):
        dataset, _, old, reference = self.queue()
        self.listing(dataset, [self.article_row()])
        changed = deepcopy(old)
        changed["menu_path"] = ["foreign", "menu"]
        self.save(reference, changed)
        self.save(remaining.SNAPSHOTS[0] + "/" + Path(reference).name, changed)
        report = self.report()
        self.assertEqual(report["summary"]["cohort_records_reconstructed"], 0)
        self.assertEqual(report["summary"]["article_identities_recovered"], 0)
        self.assertEqual(report["summary"]["article_identities_remaining"], 1)

    def test_retained_article_marker_needs_dataset_key_and_content_addressed_source(self):
        dataset, key, _, _ = self.queue()
        raw = json.dumps(self.article_row()).encode()
        source = f"data/2026/native_publications/{dataset}/{key}/{sha(raw)}.json"
        self.write(source, raw)
        marker = f"state/server_native_article_attachment_{dataset}_{key}.json"
        value = {"article_key": key, "source_path": source, "source_sha256": sha(raw), "original_downloaded": False}
        self.save(marker, value)
        self.assertEqual(self.report()["summary"]["article_identities_recovered"], 1)
        wrong = source.replace("/notice/", "/foreign/")
        self.write(wrong, raw)
        self.save(marker, {**value, "source_path": wrong})
        with self.assertRaisesRegex(ValueError, "dataset_article_path"):
            self.report()

    def test_marker_with_other_article_key_is_not_identity_evidence(self):
        dataset, key, _, _ = self.queue()
        self.save(f"state/server_native_article_attachment_{dataset}_{key}.json", {
            "article_key": "different", "source_path": "data/foreign.json", "source_sha256": "f" * 64})
        self.assertEqual(self.report()["summary"]["article_identities_recovered"], 0)

    def test_private_and_live_database_paths_are_rejected_without_open(self):
        reader = remaining.Reader(self.root)
        for path in ["state/config.json", "state/private/a.json", "state/server_login.json", "state/live.sqlite3",
                     "state/capture.har", "../outside.json"]:
            with self.assertRaises(ValueError):
                reader.raw(path)

    def test_second_read_keeps_first_digest_and_sticky_drift(self):
        reader = remaining.Reader(self.root)
        first = self.write("data/value.json", b"{}")
        reader.raw("data/value.json")
        self.write("data/value.json", b"[]")
        with self.assertRaisesRegex(ValueError, "inputs_changed_during_read"):
            reader.raw("data/value.json")
        self.write("data/value.json", b"{}")
        self.assertEqual(reader.refs["anhui/data/value.json"]["sha256"], first)
        self.assertEqual(reader.recheck(), ["anhui/data/value.json"])

    def test_controlled_baseline_rejects_parent_symlink_and_oversize_before_read(self):
        workspace = self.root.parent
        (workspace / "pmos").mkdir()
        outside = workspace / "elsewhere"
        outside.mkdir()
        (outside / "baseline.json").write_bytes(b"{}")
        (workspace / "pmos/evidence").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "controlled_remaining_baseline"):
            remaining._baseline_bytes(self.root, "baseline.json")
        (workspace / "pmos/evidence").unlink()
        (workspace / "pmos/evidence").mkdir()
        path = workspace / "pmos/evidence/baseline.json"
        with path.open("wb") as stream:
            stream.truncate(4 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "controlled_remaining_baseline"):
            remaining._baseline_bytes(self.root, "baseline.json")

    def test_baseline_recheck_rejects_change_after_province_read(self):
        dataset, _, _, _ = self.queue()
        self.listing(dataset, [self.article_row()])
        workspace = self.root.parent / "pmos/evidence"
        workspace.mkdir(parents=True)
        qraw = json.dumps(self.queue_base).encode()
        (workspace / "anhui_queue_phase2.json").write_bytes(qraw)
        baseline = deepcopy(self.base)
        baseline["baseline_refs"] = [{"path": "pmos/evidence/anhui_queue_phase2.json", "sha256": sha(qraw)}]
        braw = json.dumps(baseline).encode()
        (workspace / "anhui_phase3_recovery.json").write_bytes(braw)
        original = remaining._article_recovery
        def mutate(*args):
            result = original(*args)
            (workspace / "anhui_phase3_recovery.json").write_bytes(braw + b" ")
            return result
        with patch.object(remaining, "PHASE3_SHA256", sha(braw)), patch.object(remaining, "_article_recovery", mutate):
            with self.assertRaisesRegex(ValueError, "baseline_sha256_changed"):
                remaining.recover(self.root)

    def test_false_ui_crosscheck_sidecar_is_retained_without_semantic_upgrade(self):
        original = "data/2026/original/synthetic.xlsx"
        original_sha = self.write(original, b"synthetic workbook bytes")
        self.base["legacy_carriers"]["groups"] = [{"dataset": "summary", "baseline_qualification": "source_cells_only",
            "artifact_refs": [{"path": "anhui/" + original, "sha256": original_sha}]}]
        self.save("data/portal/contract_summary/month/scope/server_verified_contract_rows.json", {
            "source_sha256": original_sha, "export_summary_crosschecked_with_ui": False, "records": []})
        report = self.report()["legacy_carriers"]
        self.assertEqual(report["parser_sidecars_source_sha_linked"], 1)
        self.assertEqual(report["ui_export_crosschecks_proven"], 0)
        self.assertFalse(report["parser_sidecar_receipts"][0]["ui_export_crosscheck_proven"])
        self.assertEqual(report["new_semantic_qualifications"], 0)


if __name__ == "__main__":
    unittest.main()
