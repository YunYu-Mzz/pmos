"""Metadata-only followup boundaries using synthetic local evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pmos.recovery.evidence_followup import (
    Reader, _baseline, _bound_receipt, _candidate_ids, _files, _own_metadata,
    _shanxi, recover, strict_json,
)


class EvidenceFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest(), len(raw)

    def test_subset_does_not_read_or_call_unselected_provinces(self):
        with patch("pmos.recovery.evidence_followup._baseline", return_value=({}, {"path": "pmos/evidence/synthetic.json", "sha256": "a" * 64})), \
             patch("pmos.recovery.evidence_followup._shanxi", return_value={"fixed_missing_business_originals": 19, "boundary_count": 26}), \
             patch("pmos.recovery.evidence_followup._anhui", side_effect=AssertionError("unselected")), \
             patch("pmos.recovery.evidence_followup._liaoning", side_effect=AssertionError("unselected")):
            result = recover(self.root, provinces=("shanxi",))
        self.assertEqual(result["provinces"], ["shanxi"])
        self.assertEqual(result["summary"]["provinces_checked"], 1)
        self.assertNotIn("anhui_binding_capture_cohort", result["summary"])
        self.assertFalse(result["dispatch_allowed"])

    def test_invalid_subset_is_rejected_before_baseline_read(self):
        for provinces in ((), "shanxi", ("shanxi", "shanxi"), ("unknown",)):
            with self.subTest(provinces=provinces), patch("pmos.recovery.evidence_followup._baseline", side_effect=AssertionError("no read")):
                with self.assertRaises(ValueError):
                    recover(self.root, provinces=provinces)

    def test_baseline_must_match_fixed_sha(self):
        self.write("pmos/evidence/shanxi_phase4_remaining.json", {})
        with self.assertRaisesRegex(ValueError, "sha_changed"):
            _baseline(Reader(self.root), "shanxi")

    def test_reader_rejects_private_traversal_and_parent_symlink(self):
        reader = Reader(self.root)
        for path in ("shanxi/profile/file.json", "anhui/state/auth.json", "liaoning/config.json", "../escape.json", "/absolute.json", "pmos/other.json"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                reader.path(path)
        (self.root / "shanxi").mkdir()
        (self.root / "shanxi/state").symlink_to(self.root / "shanxi", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            _files(reader, "shanxi/state")

    def test_controlled_baseline_parent_symlink_is_rejected(self):
        (self.root / "pmos").mkdir()
        (self.root / "public").mkdir()
        (self.root / "pmos/evidence").symlink_to(self.root / "public", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            _baseline(Reader(self.root), "shanxi")

    def test_database_and_archive_bytes_are_rejected_before_open(self):
        reader = Reader(self.root)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read database or archive")):
            for name in ("shanxi/state/live.sqlite3", "anhui/state/live.db", "liaoning/state/snapshot.tar.gz"):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "json_metadata_or_original"):
                    reader.raw(name)

    def test_reader_keeps_first_sha_and_finish_detects_change(self):
        self.write("shanxi/state/proof.json", {})
        reader = Reader(self.root)
        reader.raw("shanxi/state/proof.json")
        self.write("shanxi/state/proof.json", {"changed": True})
        with self.assertRaisesRegex(ValueError, "sha_changed"):
            reader.finish()

    def test_caught_mid_scan_change_stays_sticky_after_file_is_restored(self):
        self.write("shanxi/state/proof.json", {})
        reader = Reader(self.root)
        reader.raw("shanxi/state/proof.json")
        self.write("shanxi/state/proof.json", {"changed": True})
        with self.assertRaises(ValueError):
            reader.raw("shanxi/state/proof.json")
        self.write("shanxi/state/proof.json", {})
        with self.assertRaisesRegex(ValueError, "prior_input_drift"):
            reader.finish()

    def test_duplicate_json_keys_and_nonfinite_values_are_rejected(self):
        for raw in (b'{"id":1,"id":2}', b'{"value":NaN}', b'{"value":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json(raw)

    def receipt(self, value=None):
        digest, size = self.write("shanxi/state/original.json", value or {"status": 0, "data": []})
        return {"dataset_candidate": "synthetic", "sample_path": "state/original.json", "sha256": digest,
                "size": size, "http_status": 200, "request": {"scope": "SYNTHETIC_PRIVATE_VALUE"}}

    def test_same_empty_bytes_from_other_dataset_do_not_supply_source_identity(self):
        receipt = self.receipt()
        receipt["dataset_candidate"] = "other"
        result = _bound_receipt(Reader(self.root), "synthetic", receipt)
        self.assertEqual(result["status"], "metadata_only_no_exact_original_receipt")

    def test_missing_original_path_is_not_replaced_by_existing_same_empty_body(self):
        receipt = self.receipt()
        receipt["sample_path"] = "state/missing.json"
        result = _bound_receipt(Reader(self.root), "synthetic", receipt)
        self.assertEqual(result["status"], "bound_original_path_still_missing")
        self.assertFalse((self.root / "shanxi/state/missing.json").exists())

    def test_empty_and_refusal_evidence_never_prove_consumption(self):
        for value, expected in [({"status": 0, "data": []}, "existing_empty_response_only"),
                                ({"status": 0, "data": {"status": 40302, "data": []}}, "existing_refusal_or_error_hold")]:
            receipt = self.receipt(value)
            result = _bound_receipt(Reader(self.root), "synthetic", receipt)
            self.assertEqual(result["status"], expected)
            self.assertFalse(result["consumption_proven"])
            self.assertFalse(result["dispatch_allowed"])
            self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", json.dumps(result))

    def test_bool_size_is_not_a_valid_source_byte_count(self):
        receipt = self.receipt()
        receipt["size"] = True
        result = _bound_receipt(Reader(self.root), "synthetic", receipt)
        self.assertEqual(result["status"], "receipt_size_or_http_unverified_hold")

    def test_own_directory_requires_matching_explicit_query_dataset(self):
        body_sha, size = self.write("liaoning/data/synthetic/response.json", {"status": 0, "data": [{"value": 1}]})
        self.write("liaoning/data/synthetic/metadata.json", {"dataset": "synthetic", "body_sha256": body_sha,
                    "http_status": 200, "query": {"dataset": "other", "period": "2026-01-01"}})
        result = _own_metadata(Reader(self.root), ["liaoning/data/synthetic/metadata.json"], {"synthetic"})
        self.assertEqual(result[0]["result"], "metadata_only_original_or_context_missing")
        self.assertIsNone(result[0]["body_ref"])

    def test_dictionary_filter_period_and_denied_permission_are_not_business_samples(self):
        digest, _ = self.write("liaoning/data/synthetic/response.json", {"status": 0, "data": [{"value": 1}]})
        for extra in ({"stage": "dictionary"}, {"stage": "filter"}, {"stage": "period"}, {"data_report_permission_verified": False}, {"data_report_permission_verified": "false"}):
            self.write("liaoning/data/synthetic/metadata.json", {"dataset": "synthetic", "body_sha256": digest,
                       "http_status": 200, "query_identity": {"period": "2026-01-01"}, **extra})
            with self.subTest(extra=extra):
                result = _own_metadata(Reader(self.root), ["liaoning/data/synthetic/metadata.json"], {"synthetic"})
                self.assertEqual(result[0]["result"], "non_business_stage_or_data_permission_unverified")

    def test_provenance_envelope_must_bind_same_dataset_and_declared_sha(self):
        digest, _ = self.write("liaoning/data/synthetic/response.json", {"status": 0, "data": []})
        metadata = {"dataset": "synthetic", "body_sha256": digest, "http_status": 200,
                    "query_identity": {"period": "2026-01-01"}}
        valid = {"dataset": "synthetic", "sha256": digest, "expected_sha256": digest, "metadata": metadata}
        self.write("liaoning/data/synthetic/provenance.json", valid)
        self.assertEqual(_own_metadata(Reader(self.root), ["liaoning/data/synthetic/provenance.json"], {"synthetic"})[0]["result"], "new_own_empty_sample_only")
        for changes in ({"dataset": "other"}, {"sha256": "a" * 64}, {"expected_sha256": None}):
            self.write("liaoning/data/synthetic/provenance.json", {**valid, **changes})
            result = _own_metadata(Reader(self.root), ["liaoning/data/synthetic/provenance.json"], {"synthetic"})
            self.assertEqual(result[0]["result"], "provenance_envelope_mismatch_hold")

    def test_candidate_same_title_does_not_override_menu_scope(self):
        baseline = {"candidates": {"entries": [{"surface": "disclosure", "source_index": 0, "legacy_entry_sha256": "a" * 64}]}}
        inventory = {"entries": [{"surface": "disclosure", "source_index": 0, "menu_path": ["other"], "sampled_dataset": "synthetic"}]}
        works = {"disclosure": [{"menu_path": ["expected"], "title": "same title", "publication_dataset": "synthetic"}]}
        result = _candidate_ids(baseline, inventory, works)
        self.assertEqual(result[0]["status"], "scope_index_changed_hold")
        self.assertEqual(result[0]["technical_ids"], [])

    def test_changed_candidate_indexes_cannot_reinterpret_old_array_positions(self):
        baseline = {"candidates": {"entries": [{"surface": "disclosure", "source_index": 0, "legacy_entry_sha256": "a" * 64}]}}
        inventory = {"entries": [{"surface": "disclosure", "source_index": 0, "menu_path": ["new"], "sampled_dataset": "new"}]}
        works = {"disclosure": [{"menu_path": ["new"], "publication_dataset": "new"}]}
        result = _candidate_ids(baseline, inventory, works, indexes_unchanged=False)
        self.assertEqual(result[0]["status"], "phase4_index_drift_hold")
        self.assertEqual(result[0]["technical_ids"], [])

    def test_alias_boundary_requires_parent_sha_and_empty_reference_is_not_verified(self):
        names = ["dataset_catalog.json", "verification/20261027/disclosure_timing/findings.json",
                 "verification/20261019/channel_forecast_empty_selector_evidence.json", "verification/20261019/discovered_special_form_review.json"]
        refs = []
        for name in names:
            digest, _ = self.write("shanxi/" + name, {"targets": []} if name == "dataset_catalog.json" else {})
            refs.append({"path": name, "sha256": digest})
        baseline = {"input_refs": refs, "remaining_sources": [], "phase3_historical_denials_retained": 4,
                    "phase3_refusal_evidence_sources_retained": 14, "historical_identity_search": {"phase3_unknown_set_preserved": {"count": 7}},
                    "phase3_boundary_rechecks": [
                        {"dataset": "alias", "phase3_class": "explicit_alias_to_existing_samples", "rechecked_receipts": [],
                         "alias_evidence": {"reference": refs[0]}},
                        {"dataset": "unbound", "phase3_class": "explicit_alias_to_existing_samples", "rechecked_receipts": []}]}
        checked = _shanxi(Reader(self.root), baseline)["boundary_sources"]
        self.assertTrue(checked[0]["all_receipt_original_links_unchanged"])
        self.assertFalse(checked[1]["all_receipt_original_links_unchanged"])
        self.assertEqual(checked[1]["link_check"], "not_applicable_no_bound_reference")
        self.write("shanxi/dataset_catalog.json", {"targets": [], "changed": True})
        changed = _shanxi(Reader(self.root), baseline)["boundary_sources"]
        self.assertFalse(changed[0]["all_receipt_original_links_unchanged"])


if __name__ == "__main__":
    unittest.main()
