"""Synthetic evidence recovery tests: no production fixtures or requests."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pmos.recovery.shanxi import _receipt, _safe, _snapshots, journal_summary, snapshot_summary


class ShanxiRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "verification").mkdir()

    def receipt(self, payload, **overrides):
        raw = json.dumps(payload).encode()
        target = self.root / "verification/query.json"
        target.write_bytes(raw)
        record = {"dataset": "synthetic", "period": "2026-01-01", "request": {"scope": "SECRET_SUBJECT"},
                  "path": "verification/query.json", "sha256": hashlib.sha256(raw).hexdigest(),
                  "size": len(raw), "http_status": 200, **overrides}
        return _receipt(self.root, "synthetic", record, {"path": "catalog.json", "sha256": "a" * 64}, "/targets/0", "query")

    def test_empty_response_is_evidence_only_and_scope_values_are_hashed(self):
        result = self.receipt({"status": 0, "data": {"list": []}})
        self.assertEqual(result["result"], "empty_query_original_verified")
        self.assertFalse(result["consumption_proven"])
        self.assertFalse(result["full_dataset_coverage_verified"])
        self.assertNotIn("SECRET_SUBJECT", json.dumps(result))

    def test_mismatched_hash_and_source_are_not_promoted(self):
        self.assertEqual(self.receipt({"status": 0, "data": []}, sha256="0" * 64)["result"], "hash_or_size_mismatch")
        self.assertEqual(self.receipt({"status": 0, "data": []}, dataset="other")["result"], "unbound_receipt")

    def test_old_denial_is_retained_independently_of_new_success(self):
        refused = self.receipt({"status": 40302, "data": None})
        success = self.receipt({"status": 0, "data": {"list": [{"value": 7}]}})
        self.assertEqual(refused["result"], "permission_denial_retained")
        self.assertEqual(success["result"], "nonempty_query_original_verified")
        self.assertFalse(refused["dispatch_allowed"])
        self.assertFalse(success["dispatch_allowed"])

    def test_auth_nested_refusal_and_boolean_status_use_shared_classifier(self):
        auth = self.receipt({"status": 401, "data": None})
        self.assertEqual(auth["result"], "authentication_failure_retained")
        self.assertEqual(auth["fault_category"], "auth")
        nested = self.receipt({"status": 0, "data": {"status": 40302, "data": None}})
        self.assertEqual(nested["result"], "permission_denial_retained")
        self.assertEqual(self.receipt({"status": False, "data": []})["result"], "business_response_rejected")

    def test_secret_traversal_and_symlink_paths_rejected(self):
        for path in ("../query.json", "profile/metadata.json", "state/auth_token.json", "config.json"):
            with self.assertRaises(ValueError):
                _safe(self.root, path)
        (self.root / "link").symlink_to(self.root / "verification", target_is_directory=True)
        with self.assertRaises(ValueError):
            _safe(self.root, "link/query.json")

    def database(self, name="queue.sqlite3"):
        path = self.root / "verification" / name
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE requests(identity TEXT,status TEXT)")
            conn.executemany("INSERT INTO requests VALUES(?,?)", [("a", "leased"), ("b", "denied"), ("c", "pending")])
        return path

    def test_rollback_snapshot_retains_unresolved_status_without_writes(self):
        path = self.database()
        original = path.read_bytes()
        result = snapshot_summary(self.root, "verification/queue.sqlite3", "requests")
        self.assertEqual(result["status_counts"], {"leased": 1, "denied": 1, "pending": 1})
        self.assertTrue(result["unchanged_after_read"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob("*-*")), [])

    def test_declared_snapshot_sha_mismatch_never_opens(self):
        self.database()
        with patch("pmos.recovery.shanxi.sqlite3.connect", side_effect=AssertionError("must not open mismatch")):
            result = snapshot_summary(self.root, "verification/queue.sqlite3", "requests", expected_sha256="0" * 64)
        self.assertFalse(result["opened"])
        self.assertEqual(result["result"], "snapshot_backup_sha_mismatch_not_opened")

    def test_wal_or_sidecar_snapshot_never_opens(self):
        path = self.database()
        raw = bytearray(path.read_bytes())
        raw[18] = raw[19] = 2
        path.write_bytes(raw)
        with patch("pmos.recovery.shanxi.sqlite3.connect", side_effect=AssertionError("must not open WAL")):
            self.assertFalse(snapshot_summary(self.root, "verification/queue.sqlite3", "requests")["opened"])
        raw[18] = raw[19] = 1
        path.write_bytes(raw)
        Path(str(path) + "-wal").write_bytes(b"in flight")
        with patch("pmos.recovery.shanxi.sqlite3.connect", side_effect=AssertionError("must not open sidecar")):
            self.assertFalse(snapshot_summary(self.root, "verification/queue.sqlite3", "requests")["opened"])

    def test_same_database_bytes_are_counted_once(self):
        original = self.database()
        (self.root / "verification/copy.sqlite3").write_bytes(original.read_bytes())
        (self.root / "verification/receipt.json").write_text("{}")
        files = [("verification/queue.sqlite3", "requests", "verification/receipt.json"),
                 ("verification/copy.sqlite3", "requests", "verification/receipt.json")]
        with patch("pmos.recovery.shanxi._SNAPSHOTS", files):
            result = _snapshots(self.root)
        self.assertEqual(result["file_views"], 2)
        self.assertEqual(result["unique_database_bytes_sha256"], 1)
        self.assertEqual(result["snapshot_table_old_identity_views"], 3)
        self.assertEqual(result["unique_table_old_identities"], 3)
        self.assertEqual(result["snapshots"][1]["duplicate_of"], "verification/queue.sqlite3")

    def test_new_pending_cannot_erase_old_denied_or_leased(self):
        original = self.database()
        copy = self.root / "verification/later.sqlite3"
        copy.write_bytes(original.read_bytes())
        with sqlite3.connect(copy) as conn:
            conn.execute("UPDATE requests SET status='pending'")
        (self.root / "verification/receipt.json").write_text("{}")
        files = [("verification/queue.sqlite3", "requests", "verification/receipt.json"),
                 ("verification/later.sqlite3", "requests", "verification/receipt.json")]
        with patch("pmos.recovery.shanxi._SNAPSHOTS", files):
            result = _snapshots(self.root)
        self.assertEqual(result["snapshot_table_old_identity_views"], 6)
        self.assertEqual(result["unique_table_old_identities"], 3)
        self.assertEqual(result["identity_status_set_counts"], {"denied|pending": 1, "leased|pending": 1, "pending": 1})
        self.assertEqual(result["protected_identity_states"]["count"], 2)
        self.assertTrue(all(not r["dispatch_allowed"] for r in result["protected_identity_states"]["hashed_examples"]))

    def test_acknowledged_reservation_is_still_unknown_and_keys_ignore_json_order(self):
        journal = {"schema_version": 1, "dataset": "synthetic", "year": 2026,
                   "dates": ["2026-01-01", "2026-01-02"], "outcomes": {"2026-01-01": "saved"},
                   "batch": {"batch_id": "fixture", "status": "acknowledged", "dates": ["2026-01-02"], "confirmed": {}}}
        result = journal_summary(journal)
        self.assertEqual(result["states"], {"saved": 1, "unknown": 1})
        self.assertFalse(result["dispatch_allowed"])
        reordered = dict(reversed(list(journal.items())))
        self.assertEqual(result["content_sha256"], journal_summary(reordered)["content_sha256"])
        self.assertEqual(result["legacy_task_keys_sha256"], journal_summary(reordered)["legacy_task_keys_sha256"])


if __name__ == "__main__":
    unittest.main()
