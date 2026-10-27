"""Synthetic recovery regressions: no production fixtures or response rows."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pmos.contracts import canonical
from pmos.queue_compat import QueueContext, decode_frontier, decode_liaoning
from pmos.recovery import liaoning as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "liaoning"
        self.root.mkdir()
        self.root = self.root.resolve()
        self.reader = recovery._Reader(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value))
        return path

    def rule(self, field="mdate", selection=None):
        return {"period_field": field, "selection_field": selection, "period_kind": "day",
                "source_ref": {"path": "liaoning/lncrawler/example.py", "sha256": "a" * 64}}

    def sample(self, dataset="example", *, selection=None):
        raw = canonical({"status": 0, "data": [{"value": 1}]})
        identity = {"period": "2026-10-05"}
        if selection is not None:
            identity["selection"] = selection
        path = self.write(f"data/2026/original/advanced_reports/{dataset}/sample/metadata.json", {
            "dataset": dataset, "body_sha256": hashlib.sha256(raw).hexdigest(),
            "body_bytes": len(raw), "http_status": 200, "query_identity": identity})
        (path.parent / "response.json").write_bytes(raw)
        return path

    def test_original_sha_and_dataset_are_both_required(self):
        path = self.sample()
        sample = recovery._sample(self.reader, path)
        self.assertEqual(sample.replay_status, "sample_only_passed")
        before = (path.parent / "response.json").read_bytes()
        changed = json.loads(path.read_bytes())
        changed["dataset"] = "another"
        path.write_bytes(canonical(changed))
        with self.assertRaisesRegex(ValueError, "dataset_path_mismatch"):
            recovery._sample(recovery._Reader(self.root), path)
        changed["dataset"], changed["body_sha256"] = "example", "0" * 64
        path.write_bytes(canonical(changed))
        with self.assertRaisesRegex(ValueError, "sha256_or_size_mismatch"):
            recovery._sample(recovery._Reader(self.root), path)
        self.assertEqual(before, (path.parent / "response.json").read_bytes())

    def test_equal_body_sha_never_replaces_exact_old_key(self):
        sample = recovery._sample(self.reader, self.sample(selection=1))
        rule = self.rule(selection="name")
        bridges = recovery._sample_jobs([sample], {"example": rule})
        key = recovery._expanded_key("example", {"period": "2026-10-05", "selection": 1})
        other = recovery._expanded_key("example", {"period": "2026-10-05", "selection": "1"})
        sha = sample.response_ref["sha256"]
        self.assertIn(("example", key, sha), bridges)
        self.assertNotIn(("example", other, sha), bridges)
        other_date = recovery._expanded_key("example", {"period": "2026-10-06", "selection": 1})
        self.assertNotIn(("example", other_date, sha), bridges)

    def test_native_receipt_bridge_does_not_require_or_claim_original(self):
        params = {"mdate": "2026-10-05"}
        self.write(recovery.MANIFEST, {"sources": {"example": {"proofs": [
            {"params": params, "http_status": 200, "outcome": "saved", "sha256": "a" * 64}]}}})
        bridges = recovery._receipt_jobs(self.reader, {"example": self.rule()})
        key = recovery._expanded_key("example", {"period": "2026-10-05"})
        self.assertIn(("example", key, "a" * 64), bridges)
        self.assertNotIn(("example", key, "b" * 64), bridges)
        self.assertFalse((self.root / "data").exists())

    def test_phase2_state_and_count_are_checked_as_well_as_sha(self):
        self.write(recovery.MANIFEST, {"sources": {"example": {"period": "day"}}})
        queue = {"sources": {"example": {"pending": [{"key": "old-key", "dataset": "example",
            "period": "2026-10-05", "params": {"mdate": "2026-10-05"}}], "outcomes": {}}}}
        queue_path = self.write(recovery.QUEUE, queue)
        baseline = {"normalized_records": 1, "unmapped_records": 1, "states": {"pending": 1},
            "source_views": [{"source_ref": str(queue_path), "sha256": hashlib.sha256(queue_path.read_bytes()).hexdigest(),
                "records": 1, "mapped": 0, "unmapped": 1, "states": {"pending": 1}, "consumed": 0}]}
        report_path = self.root.parent / "pmos/evidence/liaoning_queue_phase2.json"
        report_path.parent.mkdir(parents=True)
        report_path.write_bytes(canonical(baseline))
        _, views = recovery._baseline_views(self.reader)
        self.assertTrue(views[0]["phase2_counts_and_states_match"])
        baseline["source_views"][0]["consumed"] = 1
        report_path.write_bytes(canonical(baseline))
        with self.assertRaisesRegex(ValueError, "baseline_decoder_count_or_state_changed"):
            recovery._baseline_views(recovery._Reader(self.root))

    def test_monday_anchor_keeps_protocol_gap(self):
        rule = self.rule()
        identity, reason = recovery._identity("weekly", {
            "period": "2026-10-05", "params": {"mdate": "2026-10-05"}}, rule, "week_start_monday")
        self.assertIsNone(identity)
        self.assertIn("not_expressible", reason)
        _, reason = recovery._identity("weekly", {"period": "2026-10-06"}, rule, "week_start_monday")
        self.assertEqual(reason, "native_monday_query_invalid")
        identity, _ = recovery._identity("ordinary_date", {
            "period": "2026-10-06", "params": {"mdate": "2026-10-06"}}, rule, "day")
        self.assertEqual(identity.document()["period"], {"kind": "day", "label": "2026-10-06"})

    def test_native_engine_explicit_outcome_date(self):
        identity, reason = recovery._identity("example", {
            "period": "2026-10-05", "_explicit_engine_outcome_date": True}, self.rule(), "day")
        self.assertIsNone(reason)
        self.assertEqual(identity.document()["period"]["label"], "2026-10-05")

    def test_path_escape_symlink_and_private_paths_fail_closed(self):
        other = Path(self.temp.name) / "outside.json"
        other.write_text("{}")
        (self.root / "linked.json").symlink_to(other)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.reader.raw("linked.json")
        for relative in ("../../outside.json", "private/value.json", "config/value.json", "auth/value.json"):
            with self.assertRaises(ValueError):
                self.reader.raw(relative)

    def test_input_drift_keeps_initial_sha(self):
        path = self.write("state/business.json", {"value": 1})
        self.reader.raw(path)
        original = self.reader.ref(path)["sha256"]
        path.write_bytes(canonical({"value": 2}))
        self.assertEqual(self.reader.changed(), ["liaoning/state/business.json"])
        self.assertEqual(self.reader.ref(path)["sha256"], original)

    def test_transient_drift_is_sticky_after_source_restored(self):
        path = self.write("state/business.json", {"value": 1})
        original = path.read_bytes()
        self.reader.raw(path)
        path.write_bytes(canonical({"value": 2}))
        with self.assertRaisesRegex(ValueError, "changed_during_scan"):
            self.reader.raw(path)
        path.write_bytes(original)
        self.assertEqual(self.reader.changed(), ["liaoning/state/business.json"])

    def _view(self, *, stable=True):
        dataset = "example"
        value = {"dataset": dataset, "pending": [{"key": "old-key", "dataset": dataset,
            "period": "2026-10-05", "params": {"mdate": "2026-10-05"}}],
            "outcomes": {}, "hold": "legacy_hold",
            "active": {"job": {"key": "old-key", "dataset": dataset,
                "period": "2026-10-05", "params": {"mdate": "2026-10-05"}}, "stage": "load_content"}}
        tasks = decode_frontier(value, QueueContext("liaoning", "offline-shadow-review", dataset, "day"))
        return {"source_ref": str(self.root / "state/server_engine_collection_20261023/example/queue.json"),
            "relative": "state/server_engine_collection_20261023/example/queue.json", "value": value,
            "sha256": "a" * 64, "expected_sha256": ("a" if stable else "b") * 64,
            "tasks": tasks, "phase2_unmapped": 1, "kinds": {dataset: "day"}}

    def test_recovery_never_clears_unknown_consumption_or_other_holds(self):
        view = self._view()
        before = view["tasks"][0]
        with patch.object(recovery, "_baseline_views", return_value=({}, [view])):
            _, resolved, reports = recovery._resolve(self.reader, [], {"example": self.rule()})
        after = resolved[view["source_ref"]][0]
        self.assertIsNotNone(after.identity)
        self.assertEqual((after.state, after.consumed, after.facts_sha256),
                         (before.state, before.consumed, before.facts_sha256))
        self.assertEqual(after.state, "unknown")
        self.assertIn("legacy_active_owner_unsettled", after.blockers)
        self.assertIn("legacy_stage_requires_settlement", after.blockers)
        self.assertIn("legacy_source_hold", after.blockers)

    def test_changed_queue_is_not_baseline_recovery(self):
        view = self._view(stable=False)
        with patch.object(recovery, "_baseline_views", return_value=({}, [view])):
            _, resolved, reports = recovery._resolve(self.reader, [], {"example": self.rule()})
        self.assertIsNone(resolved[view["source_ref"]][0].identity)
        self.assertEqual(reports[0]["counts"], {"source_changed_not_baseline_recovery": 1})

    def test_denied_saved_or_failed_state_is_not_reclassified(self):
        for status in ("denied", "saved", "failed"):
            view = self._view()
            view["value"]["active"] = None
            view["value"]["pending"][0]["status"] = status
            view["tasks"] = decode_frontier(view["value"], QueueContext(
                "liaoning", "offline-shadow-review", "example", "day"))
            with patch.object(recovery, "_baseline_views", return_value=({}, [view])):
                _, resolved, _ = recovery._resolve(self.reader, [], {"example": self.rule()})
            task = resolved[view["source_ref"]][0]
            self.assertEqual(task.state, status)
            self.assertTrue(task.consumed)


if __name__ == "__main__":
    unittest.main()
