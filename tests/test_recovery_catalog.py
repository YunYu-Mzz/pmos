from pathlib import Path
import json
import tempfile
import unittest
import hashlib

from pmos.recovery.catalog import crosswalk, safe_reference


class RecoveryCatalogTests(unittest.TestCase):
    def test_only_exact_province_and_id_matches_are_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "liaoning/evidence").mkdir(parents=True)
            source = root / "liaoning/evidence/proof.json"
            source.write_bytes(b"{}")
            sha = hashlib.sha256(b"{}").hexdigest()
            catalog = root / "public_catalog.json"
            catalog.write_text(json.dumps({"datasets": [
                {"province": "liaoning", "dataset": "exact", "title": "same title", "evidence_ids": ["a"]},
                {"province": "liaoning", "dataset": "different", "title": "same title", "evidence_ids": ["a"]},
                {"province": "anhui", "dataset": "exact", "evidence_ids": ["a"]}],
                "evidence": [{"id": "a", "path": "liaoning/evidence/proof.json", "sha256": sha, "kind": "receipt"}]}))
            before = source.read_bytes()
            result = crosswalk(root, {"liaoning": {"exact"}}, catalog_path=catalog)
            self.assertEqual(result["datasets_matched"], {"liaoning": 1})
            self.assertEqual(result["entries"][0]["evidence_refs"][0]["status"], "catalog_sha_verified")
            self.assertEqual(result["acceptance_upgrades"], 0)
            self.assertEqual(source.read_bytes(), before)

    def test_private_paths_and_traversal_are_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("../escape.json", "/absolute.json", "anhui/reporting-private/config.json",
                         "shanxi/state/browser_profile/Default/data.json", "liaoning/state/cookies.json",
                         "anhui/state/session.json", "liaoning/state/authorization.json",
                         "shanxi/config.json", "shanxi/evidence/trace.har"):
                self.assertIsNone(safe_reference(root, name))

    def test_parent_symlink_to_same_province_private_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            private = root / "liaoning/private"
            private.mkdir(parents=True)
            (private / "proof.json").write_bytes(b"not public")
            (root / "liaoning/evidence").symlink_to(private, target_is_directory=True)
            self.assertIsNone(safe_reference(root, "liaoning/evidence/proof.json"))

    def test_changed_catalog_reference_is_not_a_verified_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "shanxi/evidence").mkdir(parents=True)
            (root / "shanxi/evidence/proof.json").write_bytes(b"changed")
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"datasets": [{"province": "shanxi", "dataset": "id", "evidence_ids": ["a"]}],
                "evidence": [{"id": "a", "path": "shanxi/evidence/proof.json", "sha256": "a" * 64}]}))
            result = crosswalk(root, {"shanxi": {"id"}}, catalog_path=catalog)
            self.assertEqual(result["entries"][0]["evidence_refs"][0]["status"], "catalog_reference_sha_changed")
