"""Cross-province regressions discovered during independent contract review."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pmos.adapters import anhui, liaoning
from pmos.contracts import canonical
from pmos.faults import Fault


def native_blank(**fields):
    return canonical({"currentPage": 1, "totalPage": 1,
        "htmlTag": '<div class="pageContentDIV"><table class="x-table fixture"><tr><td></td></tr><tr><td></td></tr></table></div>', **fields})


class ReviewedProvinceContracts(unittest.TestCase):
    def test_native_html_cannot_hide_explicit_auth_permission_or_unknown_business_status(self):
        for status, category in ((401, "auth"), (40302, "permission"), (429, "rate_limit"), (500, "unknown")):
            with self.subTest(status=status):
                raw = native_blank(status=status)
                with self.assertRaises(Fault) as caught:
                    liaoning.decode(raw, {"shape": "native_daily_table"})
                self.assertEqual(caught.exception.category, category)
                with self.assertRaises(Fault):
                    liaoning.contract_for_sample(raw)

    def test_status_free_native_html_still_accepts_reviewed_empty_form(self):
        result = liaoning.decode(native_blank(), {"shape": "native_daily_table"})
        self.assertTrue(result.empty)
        self.assertEqual(result.semantic_state, "native_blank_form_only")

    def test_native_401_html_and_status_free_login_message_stop_auth(self):
        for raw, status in ((b"<html>login</html>", 401), (native_blank(message="请重新登录"), 200)):
            with self.assertRaises(Fault) as caught:
                liaoning.decode(raw, {"shape": "native_daily_table"}, http_status=status)
            self.assertEqual(caught.exception.category, "auth")

    def test_listing_empty_out_of_range_page_is_never_complete(self):
        contract = {"model": {"kind": "listing", "path": ["data", "list"]}}
        for page, total in ((2, 0), (2, 1), (3, 20)):
            with self.subTest(page=page, total=total):
                raw = canonical({"status": 0, "data": {"list": [], "pageNum": page, "pageSize": 10, "total": total}})
                with self.assertRaises(Fault):
                    anhui.decode(raw, contract)
        allowed = anhui.decode(canonical({"status": 0, "data": {"list": [], "pageNum": 1, "pageSize": 10, "total": 0}}), contract)
        self.assertTrue(allowed.empty)
        self.assertTrue(allowed.pagination["all_pages_verified"])

    def test_nonfinite_json_never_reaches_unmapped_source_records(self):
        contract = {"model": {"kind": "observed_records", "path": ["data"]}}
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                raw = ('{"status":0,"data":[{"value":' + constant + '}]}').encode()
                with self.assertRaises(Fault):
                    anhui.decode(raw, contract)

    def test_unit_requires_reference_and_sha_instead_of_truthy_flag(self):
        raw = canonical({"status": 0, "data": [{"value": 0}]})
        base = {"model": {"kind": "observed_records", "path": ["data"]}, "unit": "MW"}
        for evidence in (True, "unreviewed", {}, {"source_ref": "evidence/unit.json"},
                         {"source_ref": "../private/config.json", "sha256": "a" * 64}):
            self.assertIsNone(anhui.decode(raw, {**base, "unit_evidence": evidence}).unit)
        accepted = {"source_ref": "evidence/synthetic-unit.json", "sha256": "a" * 64}
        self.assertIsNone(anhui.decode(raw, {**base, "unit_evidence": accepted}).unit)
        raw = canonical({"status": 0, "data": [{"value": 0, "unit": "MW"}]})
        import hashlib
        direct = {"source_sha256": hashlib.sha256(raw).hexdigest(), "response_path": ["data", 0, "unit"]}
        self.assertEqual(anhui.decode(raw, {**base, "unit_evidence": direct}).unit, "MW")

    def binding_fixture(self):
        selectors = {"线路": "测试A", "日期": "2026-10-18"}
        scope = {"线路": "测试A"}
        digest = hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        receipt = {"dataset": "fixture", "source_path": "data/2026/fixture/source.json", "selectors": selectors}
        parsed = {**receipt}
        binding = {"dataset": "fixture", "selectors": {**selectors, "日期": "2026-10-17"},
                   "scope_sha256": digest, "scope_key": "fixture:" + digest[:20]}
        return receipt, parsed, binding

    def test_old_model_period_can_differ_when_stable_scope_matches(self):
        receipt, parsed, binding = self.binding_fixture()
        anhui.validate_retained_binding(receipt, parsed, binding)

    def test_same_zero_model_cannot_cross_dataset_scope_or_source_identity(self):
        receipt, parsed, binding = self.binding_fixture()
        bad_cases = [(dict(parsed, dataset="other"), binding),
                     (parsed, dict(binding, dataset="other")),
                     (dict(parsed, source_path="data/2026/other/source.json"), binding),
                     (parsed, dict(binding, selectors={"线路": "测试B"})),
                     (parsed, dict(binding, scope_sha256="0" * 64)),
                     (parsed, dict(binding, scope_key="other:" + binding["scope_sha256"][:20]))]
        for changed_parsed, changed_binding in bad_cases:
            with self.assertRaises(ValueError):
                anhui.validate_retained_binding(receipt, changed_parsed, changed_binding)

    def test_private_proof_is_rejected_before_any_file_read(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "state/server_verified_expansion_20261023"
            state.mkdir(parents=True)
            private = root / "state/private.json"
            private.write_bytes(b"{}")
            (state / "active_manifest.json").write_bytes(canonical({"sources": {"fixture": {
                "proofs": [{"response_file": "state/private.json", "sha256": hashlib.sha256(b"{}").hexdigest()}]}}}))
            original_read = Path.read_bytes
            def guarded_read(path):
                if path.resolve() == private.resolve():
                    raise AssertionError("private file must not be opened")
                return original_read(path)
            with patch.object(Path, "read_bytes", guarded_read):
                report = liaoning.build_inventory(root)
            self.assertIn("retained_proof_path_not_allowed", report["entries"][0]["issues"])
            self.assertEqual(report["samples_replayed"], 0)
