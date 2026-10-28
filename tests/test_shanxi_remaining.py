"""Synthetic phase-four cases; originals and request fields never enter fixtures."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pmos.recovery.shanxi_remaining import Reader, embedded_selector, pointer, _filename_inventory, _historical_identity_search, _phase3_bytes


class ShanxiRemainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def proof(self):
        payload = {"status": 0, "message": "Success", "data": []}
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        receipt = {"dataset_candidate": "synthetic", "step": "synthetic_selector", "selected_period": "2026-01-01",
                   "endpoint_path": "/synthetic/selector", "method": "POST", "request": {"period": "2026-01-01"},
                   "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "sample_path": "state/missing.json",
                   "queue_confirmed": False, "http_status": 200, "mime": "application/json"}
        return {"receipt": receipt, "payload": payload}, dict(receipt)

    def test_embedded_exact_empty_payload_does_not_create_original_or_confirm_task(self):
        proof, declared = self.proof()
        result = embedded_selector(proof, declared)
        self.assertEqual(result["embedded_payload_sha256"], declared["sha256"])
        self.assertFalse(result["business_original_recovered"])
        self.assertFalse(result["queue_confirmed"])
        self.assertFalse(result["new_original_created"])
        self.assertFalse(result["dispatch_allowed"])
        self.assertFalse((self.root / "state/missing.json").exists())
        self.assertNotIn('"request"', json.dumps(result))

    def test_same_empty_payload_cannot_bridge_other_dataset_or_scope(self):
        for key, replacement in [("dataset_candidate", "other"), ("endpoint_path", "/other/selector"),
                                 ("selected_period", "2026-01-02"), ("request", {"period": "2026-01-02"})]:
            proof, declared = self.proof()
            declared[key] = replacement
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "context_conflicts"):
                embedded_selector(proof, declared)

    def test_embedded_serialization_must_match_original_sha_and_size(self):
        proof, declared = self.proof()
        proof["payload"] = {"data": [], "status": 0, "message": "Success"}
        with self.assertRaisesRegex(ValueError, "reproduce_declared_bytes"):
            embedded_selector(proof, declared)

    def test_embedded_context_preserves_boolean_and_integer_scope_types(self):
        proof, declared = self.proof()
        proof["receipt"]["request"] = {"scope": 1}
        declared["request"] = {"scope": True}
        with self.assertRaisesRegex(ValueError, "context_conflicts"):
            embedded_selector(proof, declared)

    def test_embedded_receipt_must_never_imply_queue_confirmation(self):
        proof, declared = self.proof()
        proof["receipt"]["queue_confirmed"] = True
        with self.assertRaisesRegex(ValueError, "queue_confirmation"):
            embedded_selector(proof, declared)

    def test_reader_retains_first_sha_and_rejects_mid_scan_changes(self):
        path = self.root / "proof.json"
        path.write_text("{}")
        reader = Reader(self.root)
        reader.raw("proof.json")
        path.write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, "changed_during_remaining_scan"):
            reader.raw("proof.json")

    def test_reader_rejects_private_auth_and_parent_symlinks_before_read(self):
        reader = Reader(self.root)
        for path in ["private/value.json", "state/auth_token.json", "config.json", "../outside.json"]:
            with self.assertRaises(ValueError):
                reader.raw(path)
        (self.root / "public").mkdir()
        (self.root / "alias").symlink_to(self.root / "public", target_is_directory=True)
        with self.assertRaises(ValueError):
            reader.raw("alias/file.json")

    def test_filename_inventory_includes_ignored_names_but_prunes_private(self):
        for base in ["state/.ignored_business", "state/profile", "verification/auth", "data/private"]:
            path = self.root / base
            path.mkdir(parents=True)
            (path / "synthetic.json").write_text("{}")
        result = _filename_inventory(self.root, ["synthetic"])
        self.assertEqual([x["path"] for x in result["matching_named_files"]], ["state/.ignored_business/synthetic.json"])
        self.assertTrue(result["absence_is_not_proof_no_original_anywhere"])

    def test_inventory_starting_directory_symlink_is_rejected(self):
        (self.root / "public").mkdir()
        (self.root / "state").symlink_to(self.root / "public", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "inventory_root_symlink"):
            _filename_inventory(self.root, ["synthetic"])

    def test_phase_three_baseline_requires_pinned_sha_and_no_controlled_symlink(self):
        province = self.root / "shanxi"
        province.mkdir()
        directory = self.root / "pmos/evidence"
        directory.mkdir(parents=True)
        source = directory / "shanxi_phase3_recovery.json"
        source.write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "pinned_phase_three"):
            _phase3_bytes(province)
        with patch("pmos.recovery.shanxi_remaining.PHASE3_SHA256", hashlib.sha256(b"{}").hexdigest()):
            self.assertEqual(_phase3_bytes(province), b"{}")
        source.unlink()
        (self.root / "safe.json").write_bytes(b"{}")
        source.symlink_to(self.root / "safe.json")
        with self.assertRaisesRegex(ValueError, "phase_three_symlink"):
            _phase3_bytes(province)

    def test_phase_three_oversize_is_rejected_before_read(self):
        province = self.root / "shanxi"
        province.mkdir()
        directory = self.root / "pmos/evidence"
        directory.mkdir(parents=True)
        source = directory / "shanxi_phase3_recovery.json"
        with source.open("wb") as stream:
            stream.truncate(1024 * 1024 + 1)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read oversized baseline")):
            with self.assertRaisesRegex(ValueError, "bounded_phase_three"):
                _phase3_bytes(province)

    def baseline(self, database):
        journal_path = self.root / "verification/20261025/pause_repair_1719/queues_before.json"
        journal_path.parent.mkdir(parents=True)
        journal_path.write_text("{}")
        return {"native_snapshots": {"snapshots": [{"path": "verification/queue.sqlite3", "sha256": hashlib.sha256(database).hexdigest(), "table": "requests"}]},
                "crawlee_journals": {"receipt": {"sha256": hashlib.sha256(journal_path.read_bytes()).hexdigest()},
                                    "unknown_identity_states": {"count": 7, "canonical_set_sha256": "a" * 64, "hashed_examples": []}}}

    def test_wal_header_refuses_sqlite_connect(self):
        (self.root / "verification").mkdir()
        raw = bytearray(b"SQLite format 3\0" + b"\0" * 84)
        raw[18] = raw[19] = 2
        (self.root / "verification/queue.sqlite3").write_bytes(raw)
        baseline = self.baseline(raw)
        with patch("pmos.recovery.shanxi_remaining.sqlite3.connect", side_effect=AssertionError("must not open")):
            with self.assertRaisesRegex(ValueError, "rollback_without_sidecar"):
                _historical_identity_search(Reader(self.root), baseline, ["synthetic"])

    def test_historical_name_match_is_metadata_only_and_does_not_map_or_erase_unknown(self):
        (self.root / "verification").mkdir()
        path = self.root / "verification/queue.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE requests(dataset TEXT,contract TEXT,receipt TEXT,path TEXT)")
            conn.execute("INSERT INTO requests VALUES(?,?,?,?)", ("synthetic", "{}", "{}", "/synthetic/query"))
        original = path.read_bytes()
        baseline = self.baseline(original)
        result = _historical_identity_search(Reader(self.root), baseline, ["synthetic"])
        self.assertEqual(result["snapshots"][0]["matched_id_fields"], [{"dataset": "synthetic", "field": "dataset", "count": 1}])
        self.assertEqual(result["newly_mapped_old_tasks"], 0)
        self.assertEqual(result["phase3_unknown_set_preserved"], baseline["crawlee_journals"]["unknown_identity_states"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob("*-wal")), [])

    def test_json_pointer_escapes_do_not_guess_by_title(self):
        self.assertEqual(pointer({"rows": [{"a/b~c": {"id": "synthetic"}}]}, "/rows/0/a~1b~0c/id"), "synthetic")
        with self.assertRaises(ValueError):
            pointer({}, "synthetic title")


if __name__ == "__main__":
    unittest.main()
