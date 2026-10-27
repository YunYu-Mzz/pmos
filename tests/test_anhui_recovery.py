"""Synthetic old-cohort recovery: no production imports, writes or requests."""
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pmos.contracts import canonical
from pmos.queue_compat import QueueContext, decode_jobs
from pmos.recovery import anhui


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "anhui"
        self.root.mkdir()
        self.queue_base = {"source_refs": [], "source_views": [], "normalized_records": 0,
                           "mapped_records": 0, "unmapped_records": 0}
        self.artifact_base = {"units": [], "excluded_units": [], "public_input_hashes": [], "source_references": []}
        self.rules = {}
        for relative in anhui.REVIEWED_RULES:
            raw = b"# Synthetic reviewed source, never imported.\n"
            self.write(relative, raw)
            self.rules[relative] = digest(raw)
        self.rule_patch = patch.object(anhui, "REVIEWED_RULES", self.rules)
        self.rule_patch.start()
        self.addCleanup(self.rule_patch.stop)
        self.write("data/2026/original/manifest.jsonl", b"")
        self.save("state/server_metric_coverage_inventory.json", {"entries": []})

    def write(self, reference, raw):
        path = self.root / reference
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return digest(raw)

    def save(self, reference, value):
        return self.write(reference, json.dumps(value, ensure_ascii=False).encode())

    def queue(self, dataset, family, value, attempts=None):
        attempts = attempts if attempts is not None else {}
        queue_key = dataset + "_queue"
        queue_ref = f"state/server_{queue_key}.json"
        queue_sha = self.save(queue_ref, value)
        attempt_ref = f"state/server_native_attempts_{dataset.lower()}.json"
        attempt_sha = self.save(attempt_ref, attempts)
        self.queue_base["source_refs"].append({"path": str(self.root / attempt_ref), "sha256": attempt_sha})
        context = QueueContext("anhui", "offline-shadow-review", dataset, "day" if family == "daily" else None)
        rows = decode_jobs(value, context, attempts=attempts)
        self.queue_base["source_views"].append({"source_ref": str(self.root / queue_ref), "sha256": queue_sha,
            "records": len(rows), "mapped": sum(row.identity is not None for row in rows),
            "unmapped": sum(row.identity is None for row in rows), "states": dict(Counter(row.state for row in rows))})
        self.queue_base["normalized_records"] += len(rows)
        self.queue_base["mapped_records"] += sum(row.identity is not None for row in rows)
        self.queue_base["unmapped_records"] += sum(row.identity is None for row in rows)
        self.artifact_base["units"].append({"dataset": dataset, "family": family,
            "legacy_queue_key": queue_key, "examples": []})
        return queue_ref

    def records(self, before=False):
        function = anhui.baseline_queue_records if before else anhui.resolve_queue_records
        return function(self.root, queue_baseline=self.queue_base, artifact_baseline=self.artifact_base)

    def report(self):
        return anhui.recover(self.root, queue_baseline=self.queue_base, artifact_baseline=self.artifact_base)

    def snapshot(self):
        return {str(path.relative_to(self.root)): digest(path.read_bytes())
                for path in self.root.rglob("*") if path.is_file()}

    def test_inline_query_day_preserves_all_consumed_and_failure_facts(self):
        self.queue("curve", "inline", {"jobs": {
            "2026-09-01": {"period": "2026-09-01", "status": "pending"},
            "2026-09-02": {"period": "2026-09-02", "status": "pending", "attempted": True},
            "2026-09-03": {"period": "2026-09-03", "status": "failed"}}})
        files_before = self.snapshot()
        old, new = self.records(True), self.records()
        self.assertEqual(old.keys(), new.keys())
        for source in old:
            self.assertEqual([row.legacy_key for row in old[source]], [row.legacy_key for row in new[source]])
            for before, after in zip(old[source], new[source]):
                self.assertIsNone(before.identity)
                self.assertEqual(after.identity.period["kind"], "day")
                self.assertEqual((before.state, before.consumed, before.legacy_state, before.facts_sha256),
                                 (after.state, after.consumed, after.legacy_state, after.facts_sha256))
                self.assertEqual(set(after.blockers), set(before.blockers) - {"identity_mapping_required"})
        self.assertEqual([row.state for row in next(iter(new.values()))], ["pending", "unknown", "failed"])
        self.assertEqual(self.snapshot(), files_before)

    def test_changed_queue_is_never_replaced_with_new_collector_output(self):
        reference = self.queue("curve", "inline", {"jobs": {
            "2026-09-01": {"period": "2026-09-01", "status": "pending"}}})
        self.save(reference, {"jobs": {"2026-09-02": {"period": "2026-09-02", "status": "saved"}}})
        report = self.report()
        self.assertEqual(report["recovered_identity_records"], 0)
        self.assertEqual(report["remaining_unmapped_records"], 1)
        self.assertTrue(report["baseline_reference_drifts"])
        self.assertEqual(self.records(), {})

    def test_changed_attempt_index_cannot_reset_unknown_to_pending(self):
        self.queue("curve", "inline", {"jobs": {"2026-09-01": {"period": "2026-09-01", "status": "pending"}}},
                   {"2026-09-01": {"phase": "attempting"}})
        self.save("state/server_native_attempts_curve.json", {})
        self.assertEqual(self.records(), {})
        self.assertEqual(self.report()["recovered_identity_records"], 0)

    def test_reviewed_production_rule_drift_fails_closed(self):
        self.write("powercrawler/native_rollout.py", b"# Changed rule\n")
        with self.assertRaisesRegex(ValueError, "sha256_changed"):
            self.records()

    def test_orphan_daily_receipt_recovers_period_without_removing_orphan_blocker(self):
        dataset, period = "load", "2026-09-01"
        source = "data/2026/native_tables/load/source.json"
        source_sha = self.save(source, {"data": {"value": 1}})
        parsed = "data/2026/native_tables/load/parsed.json"
        selectors = {"日期": period, "unit": "synthetic"}
        parsed_sha = self.save(parsed, {"dataset": dataset, "query_period": period, "selectors": selectors,
            "source_path": source, "source_sha256": source_sha})
        self.queue(dataset, "daily", {"jobs": {}}, {period: {"phase": "captured", "receipt": {
            "dataset": dataset, "period": period, "selectors": selectors, "source_path": source,
            "source_sha256": source_sha, "parsed_path": parsed, "parsed_sha256": parsed_sha}}})
        before = next(iter(self.records(True).values()))[0]
        after = next(iter(self.records().values()))[0]
        self.assertIsNone(before.identity)
        self.assertEqual(after.identity.period, {"kind": "day", "label": period})
        self.assertIn("orphan_attempt_preserved", after.blockers)
        self.assertEqual(after.state, "saved")
        self.assertTrue(after.consumed)

    def listing(self, dataset, title="Synthetic article", guid="synthetic-guid", information="synthetic-information"):
        date = "2026-09-01"
        millis = int(datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
        source = "data/2026/listing/synthetic-source.json"
        source_sha = self.save(source, {"data": {"pageNum": 1, "list": [
            {"title": title, "validDate": millis, "guid": guid, "informationId": information}]}})
        parsed = "data/2026/listing/synthetic-parsed.json"
        parsed_sha = self.save(parsed, {"dataset": dataset, "source_path": source, "source_sha256": source_sha,
            "publication_date_field": "validDate", "all_ui_listing_rows_matched": True,
            "publication_date_is_not_statistical_period": True,
            "articles": [{"title": title, "publication_date": date}]})
        self.save(f"state/server_native_listing_sources_{dataset}.json", {"1": {
            "source_path": source, "source_sha256": source_sha, "parsed_path": parsed,
            "parsed_sha256": parsed_sha, "source_rows": 1, "page": "1"}})
        return date

    def test_publication_needs_exact_original_id_and_never_uses_publish_day_as_period(self):
        title = "Synthetic article"
        date = self.listing("notice", title)
        key = anhui._article_key(title, date)
        missing = anhui._article_key("Other article", date)
        self.queue("notice", "publication", {"jobs": {
            key: {"title": title, "publication_date": date, "period": date, "status": "saved"},
            missing: {"title": "Other article", "publication_date": date, "period": date, "status": "pending"}}})
        rows = next(iter(self.records().values()))
        self.assertEqual(rows[0].identity.document()["period"], {"kind": "snapshot", "anchor": "undated"})
        self.assertEqual(rows[0].identity.operation, "article")
        self.assertEqual(rows[0].identity.dimensions, {"record_id": "synthetic-guid"})
        self.assertIsNone(rows[1].identity)
        report = self.report()
        encoded = json.dumps(report)
        for raw in (title, "synthetic-guid", "synthetic-information", key, date):
            self.assertNotIn(raw, encoded)
        self.assertEqual(report["recovered_identity_records"], 1)
        self.assertEqual(report["remaining_unmapped_records"], 1)

    def test_wrong_title_for_old_article_key_stays_unmapped(self):
        date = self.listing("notice")
        key = anhui._article_key("Synthetic article", date)
        self.queue("notice", "publication", {"jobs": {
            key: {"title": "Altered title", "publication_date": date, "status": "saved"}}})
        self.assertIsNone(next(iter(self.records().values()))[0].identity)

    def test_month_context_requires_verified_original_control_not_a_date_shaped_key(self):
        dataset, month = "monthly", "2026-09"
        original = "data/2026/original/synthetic.xlsx"
        sha = self.write(original, b"synthetic original bytes")
        queue = {"dataset": dataset, "menu_path": ["synthetic monthly"], "jobs": {
            month: {"period": month, "status": "saved", "original": {
                "dataset": dataset, "period": month, "path": original, "sha256": sha,
                "provenance": {"statistical_period": month, "selectors": {"月份": month},
                               "menu_path": ["synthetic monthly"]}}},
            "2026-10": {"period": "2026-10", "status": "pending"}}}
        self.queue(dataset, "period", queue)
        rows = next(iter(self.records().values()))
        self.assertTrue(all(row.identity.period["kind"] == "month" for row in rows))
        self.write(original, b"altered original bytes")
        self.assertTrue(all(row.identity is None for row in next(iter(self.records().values()))))

    def test_portal_requires_exact_version_and_preserves_hidden_dom_qualification(self):
        source, parsed = "data/reference/source.json", "data/reference/parsed.json"
        source_sha = self.save(source, {"data": {"tables": []}})
        parsed_sha = self.save(parsed, {"records": []})
        proof = "state/server_portal_page.json"
        proof_value = {"version": "retained-version", "menu_path": ["synthetic portal"], "page": 3,
            "source_path": source, "source_sha256": source_sha, "parsed_path": parsed,
            "parsed_sha256": parsed_sha, "source_kind": "retained_hidden_dom"}
        self.save(proof, proof_value)
        self.queue("portal", "portal", {"version": "retained-version", "menu_path": ["synthetic portal"],
            "selectors": {"group": "synthetic"}, "jobs": {"3": {"page": 3, "status": "saved",
                "evidence": proof, "parsed_path": parsed, "parsed_sha256": parsed_sha}}})
        task = next(iter(self.records().values()))[0]
        self.assertEqual(task.identity.period["kind"], "snapshot")
        self.assertEqual(task.identity.page, 3)
        self.assertEqual(self.report()["queue_identity_bridges"][0]["source_kind"], "retained_hidden_dom")
        self.save(proof, {**proof_value, "version": "other-version"})
        self.assertIsNone(next(iter(self.records().values()))[0].identity)

    def test_exact_precommitted_binding_digest_recovers_without_rewriting_old_parsed(self):
        source = "data/2026/native_tables/synthetic/source.json"
        source_sha = self.save(source, {"data": {"value": 1}})
        scope_sha = digest(json.dumps({}, ensure_ascii=False, sort_keys=True, default=str).encode())
        binding = {"dataset": "synthetic", "selectors": {}, "scope_sha256": scope_sha,
            "scope_key": "synthetic:" + scope_sha[:20], "model": {"kind": "raw_fields", "path": ["data"],
                "data_schema": {"value": "number"}, "numeric_fields": 1}}
        binding_raw = json.dumps(binding, ensure_ascii=False).encode()
        binding_sha = digest(binding_raw)
        binding_ref = f"data/2026/native_tables/_bindings/{binding_sha}.json"
        self.write(binding_ref, binding_raw)
        parsed_ref = "data/2026/native_tables/synthetic/parsed.json"
        parsed_sha = self.save(parsed_ref, {"dataset": "synthetic", "query_period": "2026-09-01",
            "selectors": {"日期": "2026-09-01"}, "source_path": source, "source_sha256": source_sha,
            "binding_sha256": binding_sha, "rows": []})
        base = {"units": [{"dataset": "synthetic", "examples": [{
            "binding_qualification": "legacy_mutable_state_binding", "source_ref": "anhui/" + source,
            "source_sha256": source_sha, "parsed_ref": "anhui/" + parsed_ref, "parsed_sha256": parsed_sha,
            "binding_ref": "anhui/state/server_old_binding.json", "binding_sha256": "f" * 64,
            "period": "2026-09-01"}]}]}
        before = self.snapshot()
        result = anhui._bindings(anhui._Reader(self.root), base)
        self.assertEqual((result["recovered_captures"], result["remaining_captures"]), (1, 0))
        self.assertEqual(self.snapshot(), before)
        self.write(binding_ref, binding_raw + b" ")
        result = anhui._bindings(anhui._Reader(self.root), base)
        self.assertEqual((result["recovered_captures"], result["remaining_captures"]), (0, 1))

    def test_private_symlink_and_parent_paths_are_never_read(self):
        reader = anhui._Reader(self.root)
        for reference in ("../outside.json", "/outside.json", "state/private/queue.json",
                          "state/browser_profile/queue.json", "state/server_auth.json"):
            with self.assertRaises(ValueError):
                reader.raw(reference)
        self.write("data/safe.json", b"{}")
        (self.root / "data/link.json").symlink_to(self.root / "data/safe.json")
        with self.assertRaises(ValueError):
            reader.raw("data/link.json")

    def test_recheck_rejects_a_file_swapped_to_a_symlink(self):
        reader = anhui._Reader(self.root)
        self.write("data/safe.json", b"{}")
        self.write("data/other.json", b"{}")
        reader.raw("data/safe.json")
        (self.root / "data/safe.json").unlink()
        (self.root / "data/safe.json").symlink_to(self.root / "data/other.json")
        self.assertEqual(reader.recheck(), ["anhui/data/safe.json"])

    def test_second_read_never_overwrites_first_digest_even_if_file_changes_back(self):
        reader = anhui._Reader(self.root)
        first_sha = self.write("data/safe.json", b'{"value":1}')
        reader.raw("data/safe.json")
        self.write("data/safe.json", b'{"value":2}')
        with self.assertRaisesRegex(ValueError, "inputs_changed_during_read"):
            reader.raw("data/safe.json")
        self.assertEqual(reader.refs["anhui/data/safe.json"]["sha256"], first_sha)
        self.write("data/safe.json", b'{"value":1}')
        self.assertEqual(reader.recheck(), ["anhui/data/safe.json"])


if __name__ == "__main__":
    unittest.main()
