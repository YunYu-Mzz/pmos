"""Liaoning source-shape regression tests: no production imports or I/O."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pmos.adapters.liaoning import build_inventory, contract_for_sample, decode
from pmos.contracts import canonical
from pmos.faults import Fault


def payload(data, status=0):
    return canonical({"status": status, "message": "", "data": data})


class LiaoningOfflineTests(unittest.TestCase):
    def test_source_rows_keep_zero_negative_null_and_duplicates(self):
        rows = [{"type": "测试主体", "value": 0}, {"type": "测试主体", "value": -2},
                {"type": "测试主体", "value": None}, {"type": "测试主体", "value": 0}]
        raw = payload(rows)
        parsed = decode(raw, contract_for_sample(raw))
        self.assertEqual(parsed.rows, rows)
        self.assertIsNone(parsed.unit)
        self.assertFalse(parsed.summary()["full_dataset_coverage_verified"])

    def test_dynamic_columns_and_rows_remain_source_fields(self):
        raw = payload({"objectList": {"list": [{"column_a": 0}, {"column_a": None}],
                        "total": 2, "pageNum": 1, "pageSize": 10, "pages": 0},
                       "ipConfigDataColsList": [{"field": "column_a", "title": "原始列"}]})
        parsed = decode(raw, contract_for_sample(raw))
        self.assertEqual(parsed.pagination["expected_pages"], 1)
        self.assertEqual(parsed.pagination["reported_pages"], 0)
        self.assertEqual(parsed.rows, [{"column_a": 0}, {"column_a": None}])
        changed = json.loads(raw)
        changed["data"]["ipConfigDataColsList"][0]["field"] = "unreviewed_column"
        with self.assertRaisesRegex(Fault, "columns_changed"):
            decode(canonical(changed), contract_for_sample(raw))

    def test_partial_or_replayed_page_is_rejected(self):
        raw = payload({"list": [{"v": 1}], "total": 21, "pageNum": 1, "pageSize": 20, "pages": 2})
        with self.assertRaisesRegex(Fault, "page_count"):
            decode(raw, contract_for_sample(raw))
        raw = payload({"list": [{"v": 1}], "total": 21, "pageNum": 1, "pageSize": 20, "pages": 2})
        contract = contract_for_sample(raw, request_page=2)
        with self.assertRaisesRegex(Fault, "response_page"):
            decode(raw, contract)

    def test_zero_page_metadata_requires_explicit_review_and_request_size(self):
        raw = payload({"list": [{"v": 0}], "total": 1, "pageNum": 0, "pageSize": 0, "pages": 0})
        contract = {"shape": "records", "rows_path": ["data", "list"], "success_statuses": [0],
                    "pagination_path": ["data"], "request_page": 1, "page_size": 20}
        with self.assertRaisesRegex(Fault, "response_page"):
            decode(raw, contract)
        contract["zero_page_metadata_reviewed"] = True
        self.assertEqual(decode(raw, contract).pagination["reported_page"], 0)

    def test_nested_business_envelope_is_not_allowed_to_hide_refusal(self):
        for status in (401, 40302, 500):
            raw = payload({"status": status, "message": "", "data": {"list": []}})
            with self.assertRaises(Fault):
                decode(raw, {"shape": "records", "rows_path": ["data", "data", "list"]})
        raw = payload({"status": 0, "message": "", "data": {"list": [], "total": 0}})
        self.assertTrue(decode(raw, contract_for_sample(raw, request_page=1, page_size=20)).empty)

    def test_null_list_is_only_empty_with_zero_total_and_explicit_contract(self):
        raw = payload({"list": None, "total": 0})
        contract = contract_for_sample(raw, request_page=1, page_size=20)
        self.assertTrue(decode(raw, contract).empty)
        contract.pop("null_list_empty_reviewed")
        with self.assertRaises(Fault):
            decode(raw, contract)
        with self.assertRaises(Fault):
            contract_for_sample(payload({"list": None, "total": 2}))

    def test_field_drift_and_expected_grid_are_not_silently_accepted(self):
        raw = payload([{"time": "00:15", "v": 0}])
        contract = contract_for_sample(raw)
        contract.update(time_field="time", expected_times=["00:15", "00:30"])
        with self.assertRaisesRegex(Fault, "time_grid"):
            decode(raw, contract)
        contract.pop("expected_times")
        with self.assertRaisesRegex(Fault, "fields_changed"):
            decode(payload([{"time": "00:15", "other": 0}]), contract)

    def test_numeric_binding_rejects_boolean_infinity_and_preserves_missing(self):
        contract = {"shape": "records", "rows_path": ["data"], "finite_numeric_fields": ["v"],
                    "nullable_numeric_fields": ["v"]}
        self.assertIsNone(decode(payload([{"v": None}]), contract).rows[0]["v"])
        for cell in (False, "NaN", "Infinity", "unmapped"):
            with self.assertRaisesRegex(Fault, "numeric_field"):
                decode(payload([{"v": cell}]), contract)

    def test_source_bound_wide_grid_preserves_null_and_rejects_changed_values(self):
        contract = {"shape": "records", "rows_path": ["data"], "wide_time_fields": ["v0015", "v0030"]}
        raw = payload([{"v0015": 0, "v0030": None}])
        self.assertEqual(decode(raw, contract).rows[0], {"v0015": 0, "v0030": None})
        with self.assertRaisesRegex(Fault, "wide_grid_changed"):
            decode(payload([{"v0015": 0}]), contract)
        with self.assertRaisesRegex(Fault, "wide_value_changed"):
            decode(payload([{"v0015": 0, "v0030": False}]), contract)

    def test_credentials_and_malformed_json_do_not_enter_parsed_rows(self):
        for raw in (b'{"status":0,"data":[{"access_token":"synthetic"}]}', b'{"status":0,"data":[NaN]}'):
            with self.assertRaises((Fault, ValueError)):
                decode(raw, {"shape": "records", "rows_path": ["data"]})

    def test_native_grid_rebuilds_exact_96_cells_with_blank_distinct_from_zero(self):
        grid = []
        for first in range(1, 97, 16):
            times = [f"{n // 4:02d}:{n % 4 * 15:02d}" for n in range(first, first + 16)]
            values = ["" if n == 1 else "0" if n == 2 else str(-n) for n in range(first, first + 16)]
            grid.extend([times, values])
        html = '<div class="pageContentDIV"><table class="x-table test">' + ''.join(
            '<tr>' + ''.join('<td>' + cell + '</td>' for cell in row) + '</tr>' for row in grid) + '</table></div>'
        raw = canonical({"currentPage": 1, "totalPage": 1, "htmlTag": html})
        parsed = decode(raw, {"shape": "native_daily_table"})
        self.assertEqual(len(parsed.rows), 96)
        self.assertIsNone(parsed.rows[0]["value"])
        self.assertEqual(parsed.rows[1]["value"], "0")
        self.assertEqual(parsed.rows[-1]["time"], "24:00")
        broken = raw.replace(b"00:15", b"00:30", 1)
        with self.assertRaisesRegex(Fault, "time_grid"):
            decode(broken, {"shape": "native_daily_table"})

    def test_native_blank_is_not_a_synthetic_zero_day(self):
        html = '<div class="pageContentDIV"><table class="x-table test"><tr><td></td></tr><tr><td></td></tr></table></div>'
        parsed = decode(canonical({"currentPage": 1, "totalPage": 1, "htmlTag": html}), {"shape": "native_daily_table"})
        self.assertTrue(parsed.empty)
        self.assertEqual(parsed.rows, [])

    def test_attachment_container_acceptance_does_not_claim_document_semantics(self):
        raw = b'%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n'
        parsed = decode(raw, {"shape": "attachment", "content_type": "application/pdf"})
        self.assertEqual(parsed.rows[0]["format"], ".pdf")
        self.assertIn("container_only", parsed.semantic_state)
        with self.assertRaises(Fault):
            decode(raw[:-8], {"shape": "attachment", "content_type": "application/pdf"})

    def test_inventory_is_read_only_and_never_promotes_a_missing_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            c = root / "state/sitewide_expansion_20261016"
            c.mkdir(parents=True)
            catalog = c / "catalog_current.json"
            raw = canonical({"jobs": [{"dataset": "unverified", "title": "待核", "phase": "excluded_prior_decision"}]})
            catalog.write_bytes(raw)
            report = build_inventory(root)
            self.assertEqual(report["catalog_rows"], 1)
            self.assertEqual(report["entries"][0]["offline_status"], "evidence_missing_or_shape_review")
            self.assertEqual(catalog.read_bytes(), raw)
            self.assertEqual(report["website_requests"], 0)


if __name__ == "__main__":
    unittest.main()
