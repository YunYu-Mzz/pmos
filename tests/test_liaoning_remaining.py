"""Synthetic checks for phase4 alias proof and frozen remainder boundaries."""
from copy import deepcopy
import io
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pmos.contracts import canonical
from pmos.queue_compat import LegacyTask
from pmos.recovery import liaoning_remaining as remaining
from pmos.recovery.liaoning import _Reader, _expanded_key, _sha


class RemainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "liaoning"
        self.root.mkdir()
        self.reader = _Reader(self.root)
        self.row = {"dataset": "source_alias", "prior_dataset": "original_target", "number": "005",
            "source": "native_source", "route": "native_route", "state": "prior_verified_report_sample",
            "reuse_basis": "existing_workflow_same_native_report_number"}
        self.catalog = {"dataset": "source_alias", "number": "005", "source_id": "native_source",
            "aliases": [], "routes": ["native_route"], "phase": "existing_workflow"}
        self.rule = {"period_field": "mdate", "selection_field": "name", "period_kind": "day"}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value))
        return path

    def test_same_request_alias_requires_all_native_bindings(self):
        proof = remaining._same_request(self.row, self.catalog)
        self.assertEqual(proof["target_dataset"], "original_target")
        self.assertEqual(len(proof["native_binding_sha256"]), 64)
        self.assertEqual(len(proof["native_endpoint_sha256"]), 64)
        for field, value in (("source_id", "other_source"), ("routes", ["other_route"]),
                             ("number", "006"), ("dataset", "other_dataset"), ("phase", "pending")):
            catalog = {**self.catalog, field: value}
            self.assertIsNone(remaining._same_request(self.row, catalog), field)

    def test_titles_and_alias_status_cannot_inherit_denied_state(self):
        catalog = {**self.catalog, "title": "same title"}
        for field, value in (("state", "denied_no_retry"), ("reuse_basis", "same_title"),
                             ("number", "006"), ("route", "other_route")):
            row = {**self.row, "title": "same title", field: value}
            self.assertIsNone(remaining._same_request(row, catalog), field)

    def test_alias_target_original_requires_exact_endpoint_and_dataset(self):
        binding = remaining._same_request(self.row, self.catalog)
        endpoint = "/px-dif-settlement-infpubquery/pdblnReportController/pdblnReport005"
        valid = SimpleNamespace(dataset="original_target", metadata={"endpoint_path": endpoint},
                                replay_status="sample_only_passed")
        wrong_endpoint = SimpleNamespace(dataset="original_target", metadata={"endpoint_path": endpoint + "0"},
                                         replay_status="sample_only_passed")
        wrong_dataset = SimpleNamespace(dataset="same_title", metadata={"endpoint_path": endpoint},
                                        replay_status="sample_only_passed")
        failed = SimpleNamespace(dataset="original_target", metadata={"endpoint_path": endpoint},
                                 replay_status="shape_review_required")
        self.assertEqual(remaining._alias_samples(binding, [valid, wrong_endpoint, wrong_dataset, failed]), [valid])

    def job(self, selection=1):
        return {"dataset": "original_target", "period": "2026-10-05",
                "params": {"mdate": "2026-10-05", "name": selection}}

    def test_old_key_query_and_body_sha_must_all_match(self):
        key = _expanded_key("original_target", {"period": "2026-10-05", "selection": 1})
        identity = remaining._exact_bridge("original_target", key, self.job(), "a" * 64, "a" * 64, self.rule)
        self.assertIsNotNone(identity)
        self.assertIsNone(remaining._exact_bridge("original_target", key, self.job(), "b" * 64, "a" * 64, self.rule))
        self.assertIsNone(remaining._exact_bridge("original_target", key, self.job("1"), "a" * 64, "a" * 64, self.rule))
        self.assertIsNone(remaining._exact_bridge("original_target", key, self.job(), None, None, self.rule))
        self.assertIsNone(remaining._exact_bridge("other_dataset", key, self.job(), "a" * 64, "a" * 64, self.rule))

    def test_extra_query_fields_and_changed_period_are_not_dropped(self):
        key = _expanded_key("original_target", {"period": "2026-10-05", "selection": 1})
        job = self.job()
        job["params"]["extra"] = "retained"
        self.assertIsNone(remaining._confirmed_job("original_target", key, job, self.rule))
        job = {**self.job(), "period": "2026-10-06"}
        self.assertIsNone(remaining._confirmed_job("original_target", key, job, self.rule))

    def test_native_list_snapshot_bridge_requires_no_report_adapter_rule(self):
        params = {"page": 2, "snapshot_date": "2026-10-05"}
        key = "original_target:snapshot:" + _sha(canonical(params))
        job = {"key": key, "dataset": "original_target", "params": params,
               "page": 2, "snapshot_date": "2026-10-05"}
        identity = remaining._exact_bridge("original_target", key, job, "a" * 64, "a" * 64, None)
        self.assertIsNotNone(identity)
        self.assertEqual(identity.document()["period"], {"kind": "snapshot", "anchor": "2026-10-05"})
        self.assertEqual(identity.document()["page"], 2)
        self.assertIsNone(remaining._exact_bridge("original_target", key, job, "b" * 64, "a" * 64, None))
        self.assertIsNone(remaining._confirmed_job("original_target", key, {**job, "page": 1}, None))
        self.assertIsNone(remaining._confirmed_job("original_target", key, {**job, "snapshot_date": "2026-10-06"}, None))
        self.assertIsNone(remaining._confirmed_job("original_target", key, {**job, "key": "different-old-key"}, None))
        self.assertIsNone(remaining._confirmed_job("original_target", key, {**job, "params": {**params, "extra": 1}}, None))

    def test_changed_inputs_never_return_alias_recovery_counts(self):
        self.write(remaining.CATALOG, {"jobs": [self.catalog]})
        self.write(remaining.COVERAGE, {"sources": [self.row]})
        raw = canonical({"status": 0, "data": [{"value": 1}]})
        path = self.write("data/2026/original/power_reports/original_target/sample/metadata.json", {
            "dataset": "original_target", "body_sha256": _sha(raw), "body_bytes": len(raw),
            "http_status": 200, "query_identity": {"mdate": "2026-10-05"},
            "endpoint_path": "/px-dif-settlement-infpubquery/pdblnReportController/pdblnReport005"})
        path.with_name("response.json").write_bytes(raw)
        with patch.object(remaining, "_phase3", return_value=({}, {"source_alias": {}}, {})), \
                patch.object(remaining, "_cohort_tasks", return_value={}), \
                patch.object(remaining, "_rules", return_value={}), \
                patch.object(remaining, "SNAPSHOTS", ()), \
                patch.object(remaining, "RECEIPTS", ()), \
                patch.object(remaining, "ARCHIVES", ()), \
                patch.object(_Reader, "changed", return_value=["liaoning/state/changed.json"]):
            with self.assertRaisesRegex(ValueError, "inputs_changed_during_remaining_recovery"):
                remaining.recover(self.root)

    def test_outcome_only_snapshot_is_not_a_job(self):
        value = {"sources": {"original_target": {"pending": [], "outcomes": {
            "old-key": {"outcome": "saved", "sha256": "a" * 64}}}}}
        before = canonical(value)
        candidates, groups = remaining._queue_candidates(value)
        self.assertEqual(candidates, [])
        self.assertIn("old-key", groups["original_target"]["outcomes"])
        self.assertEqual(canonical(value), before)

    def test_unsettled_active_is_not_removed_from_snapshot_view(self):
        value = {"sources": {"original_target": {"pending": [], "outcomes": {}}},
                 "active": {"job": {"key": "old-key", **self.job()}, "stage": "load_content"}}
        before = deepcopy(value)
        candidates, _ = remaining._queue_candidates(value)
        self.assertEqual(candidates[0][1], "old-key")
        self.assertEqual(value, before)

    def phase3_fixture(self):
        return {"source_evidence": {"records": [{"dataset": f"source_{n}", "status": "evidence_still_missing"}
                    for n in range(51)]}, "queue_evidence": {
            "record_kinds": {"missing": {"status": "evidence_still_missing"}},
            "source_views": [{"source_ref": "liaoning/state/known.json", "sha256": "a" * 64,
                "records": [{"legacy_key_sha256": f"{n:064x}", "dataset": "original_target", "record_kind": "missing"}
                            for n in range(11)]}]}}

    def test_phase3_cohort_sha_and_exact_remainder_sizes_are_required(self):
        value = self.phase3_fixture()
        path = self.root.parent / remaining.PHASE3
        path.parent.mkdir(parents=True)
        path.write_bytes(canonical(value))
        with self.assertRaisesRegex(ValueError, "cohort_sha256_changed"):
            remaining._phase3(self.reader)
        with patch.object(remaining, "PHASE3_SHA256", _sha(path.read_bytes())):
            _, sources, records = remaining._phase3(_Reader(self.root))
        self.assertEqual((len(sources), len(records)), (51, 11))
        value["source_evidence"]["records"].pop()
        path.write_bytes(canonical(value))
        with patch.object(remaining, "PHASE3_SHA256", _sha(path.read_bytes())):
            with self.assertRaisesRegex(ValueError, "cohort_shape_changed"):
                remaining._phase3(_Reader(self.root))

    def test_same_old_key_in_another_snapshot_cannot_enter_cohort(self):
        path = self.write("state/expected.json", {})
        source = str(path)
        other_source = str(self.root / "state/another.json")
        task = LegacyTask("old-key", "original_target", "saved", "saved", True,
                          facts_sha256="a" * 64)
        expected = {_sha(task.legacy_key.encode()): {"dataset": task.dataset, "state": task.state,
            "consumed": task.consumed, "facts_sha256": task.facts_sha256,
            "source_ref": "liaoning/state/expected.json", "source_sha256": _sha(path.read_bytes())}}
        with patch.object(remaining, "baseline_queue_records", return_value={other_source: [task], source: [task]}):
            selected = remaining._cohort_tasks(self.reader, expected)
        self.assertEqual(selected, {source: [task]})

    def test_archive_is_listed_without_payload_read_or_extraction(self):
        path = self.root / "exports/known.tar"
        path.parent.mkdir()
        with tarfile.open(path, "w") as archive:
            for name in ("business/source_alias/metadata.json", "../../outside.json", "profile/secret.json"):
                member = tarfile.TarInfo(name)
                payload = b"payload-is-not-parsed"
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        result = remaining._archive_inventory(self.reader, "exports/known.tar", {"source_alias"})
        self.assertEqual(result["excluded_unsafe_or_private_members"], 2)
        self.assertEqual(len(result["target_member_matches"]), 1)
        self.assertEqual(result["archive_member_payloads_read"], 0)
        self.assertEqual(result["archive_members_extracted"], 0)
        self.assertFalse((self.root / "business").exists())

    def test_dictionary_success_remains_non_business_evidence(self):
        path = self.write(remaining.DICTIONARY, {"dataset": "source_alias", "stage": "dictionary",
            "data_report_permission_verified": False, "body_sha256": _sha(canonical({"status": 0, "data": []}))})
        path.with_name("response.json").write_bytes(canonical({"status": 0, "data": []}))
        evidence = remaining._negative_evidence(self.reader, {"source_alias"})
        record = evidence["source_alias"][0]
        self.assertFalse(record["qualifies_as_business_original"])
        self.assertEqual(record["finding"], "dictionary_only_data_permission_explicitly_unverified")


if __name__ == "__main__":
    unittest.main()
