"""Pure Shanxi decoder contracts: no production modules or files imported."""
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile

from pmos.adapters.shanxi import decode, decode_pages
from pmos.faults import Fault

FIXTURE = Path(__file__).parent / "fixtures" / "shanxi_phase2_native.json"


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


def envelope(data):
    return encoded({"status": 0, "data": data})


def page(rows, number=1, size=2, total=None, **fields):
    return {"list": rows, "pageNum": number, "pageSize": size,
            "total": len(rows) if total is None else total, **fields}


def workbook():
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as book:
        book.writestr("[Content_Types].xml", "<Types/>")
        book.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="脱敏表" sheetId="1" r:id="rId1"/></sheets></workbook>')
        book.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        book.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>测试主体</t></si></sst>')
        book.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>0</v></c><c r="C1"><v>-12.50</v></c><c r="D1"/><c r="E1"><f>B1+C1</f><v>-12.50</v></c></row></sheetData></worksheet>')
    return target.getvalue()


class ShanxiOfflineTests(unittest.TestCase):
    def test_paged_native_rows_preserve_zero_negative_null_and_duplicates(self):
        payload = json.loads(FIXTURE.read_text())
        result = decode(encoded(payload), {"shape": "paged_records", "request_page": 1, "request_page_size": 3})
        self.assertEqual([row["record"] for row in result.rows], [0, -2, None])
        self.assertIsNone(result.unit)
        self.assertFalse(result.summary()["full_dataset_coverage_verified"])

    def test_dynamic_empty_is_explicit_not_business_error(self):
        self.assertTrue(decode(envelope({"objectList": []}), {"shape": "dynamic_table"}).empty)
        with self.assertRaises(Fault):
            decode(encoded({"status": 500, "data": None}), {"shape": "dynamic_table"})

    def test_named_tables_preserve_table_identity_and_raw_fields(self):
        result = decode(envelope({"TOP4": [{"value": 0}], "HHi": [{"value": -1}]}),
                        {"shape": "market_tables", "response_tables": ["TOP4", "HHi"]})
        self.assertEqual([r["source_table"] for r in result.rows], ["TOP4", "HHi"])
        self.assertEqual(result.rows[0]["fields"]["value"], 0)
        with self.assertRaises(Fault):
            decode(envelope({"TOP4": [], "HHi": [], "unexpected": []}),
                   {"shape": "market_tables", "response_tables": ["TOP4", "HHi"]})

    def test_single_record_requires_exact_source_fields(self):
        result = decode(envelope({"v0015": 0, "v0030": None}),
                        {"shape": "single_record", "required_fields": ["v0015", "v0030"]})
        self.assertEqual(result.rows[0]["v0015"], 0)
        with self.assertRaises(Fault):
            decode(envelope({"v0015": 0}), {"shape": "single_record", "required_fields": ["v0015", "v0030"]})

    def test_zero_pagination_requires_reviewed_source_and_request_bounds(self):
        raw = envelope(page([{"value": 0}], number=0, size=0))
        with self.assertRaises(Fault):
            decode(raw, {"shape": "paged_records"})
        result = decode(raw, {"shape": "paged_records", "allow_zero_page_metadata": True,
                              "request_page": 1, "request_page_size": 20})
        self.assertTrue(result.pagination["source_zero_page_metadata"])

    def test_scope_and_period_change_never_passes(self):
        raw = envelope(page([{"day": "2026-10-18", "scope": "A"}]))
        for contract in ({"response_period_fields": ["day"], "expected_period": "2026-10-19"},
                         {"expected_response_scope": {"scope": "B"}}):
            with self.assertRaises(Fault):
                decode(raw, {"shape": "paged_records", **contract})

    def test_unknown_unit_stays_null_without_unit_evidence(self):
        raw = envelope(page([{"record": 0}]))
        self.assertIsNone(decode(raw, {"unit": "MW"}).unit)
        self.assertIsNone(decode(raw, {"unit": "MW", "unit_evidence_sha256": "a" * 64}).unit)

    def test_pages_are_contiguous_hashed_and_keep_duplicate_source_rows(self):
        first = envelope(page([{"record": 0}, {"record": 0}], number=1, total=3))
        second = envelope(page([{"record": -1}], number=2, total=3))
        pages = [{"raw": raw, "sha256": hashlib.sha256(raw).hexdigest()} for raw in [first, second]]
        result = decode_pages(pages, {"shape": "paged_records"})
        self.assertEqual(len(result.rows), 3)
        self.assertTrue(result.pagination["all_pages_verified"])
        self.assertFalse(result.pagination["atomic_single_response_snapshot"])
        with self.assertRaises(Fault):
            decode_pages(list(reversed(pages)), {"shape": "paged_records"})
        pages[1]["sha256"] = "0" * 64
        with self.assertRaises(Fault):
            decode_pages(pages, {"shape": "paged_records"})

    def test_publication_list_never_claims_attachment_or_statistical_acceptance(self):
        raw = envelope({"list": [{"guid": "synthetic", "title": "测试公告", "content": "<p>0</p>"}], "total": 1})
        result = decode(raw, {"shape": "publication_list"})
        self.assertEqual(result.semantic_state, "publication_metadata_only")
        self.assertFalse(result.pagination["all_pages_verified"])

    def test_json_permissions_under_binary_mime_are_not_files(self):
        with self.assertRaises(Fault) as caught:
            decode(encoded({"status": 40302, "data": None}), {"kind": "workbook"})
        self.assertEqual(caught.exception.category, "permission")

    def test_workbook_keeps_cells_zero_negative_blank_and_cached_formula(self):
        result = decode(workbook(), {"kind": "workbook"})
        self.assertEqual([r["value"] for r in result.rows], ["测试主体", "0", "-12.50", None, "-12.50"])
        self.assertEqual(result.rows[-1]["formula"], "B1+C1")
        self.assertEqual(result.rows[-1]["coordinate"], "E1")
        self.assertEqual(result.semantic_state, "xlsx_source_cells_only_formulas_not_executed")

    def test_unavailable_xls_decoder_fails_closed(self):
        with patch.dict(sys.modules, {"xlrd": None}):
            with self.assertRaisesRegex(Fault, "xls_cell_decoder_unavailable"):
                decode(bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 504, {"kind": "workbook"})

    def test_nonfinite_json_does_not_enter_source_rows(self):
        with self.assertRaises(Fault):
            decode(b'{"status":0,"data":[{"record":NaN}]}', {"shape": "market_rows"})

    def test_secret_shaped_fields_are_rejected_without_values(self):
        with self.assertRaisesRegex(ValueError, "credential_field_forbidden"):
            decode(envelope([{"access_token": "synthetic-test"}]), {"shape": "market_rows"})

    def test_truncated_or_empty_workbook_is_not_data(self):
        for raw in (b"", b"PKtruncated"):
            with self.assertRaises(Fault):
                decode(raw, {"kind": "workbook"})

    def test_attachment_container_acceptance_does_not_claim_text_parse(self):
        raw = b"%PDF-1.7\n1 0 obj<<>>endobj\n%%EOF\n"
        result = decode(raw, {"kind": "attachment"})
        self.assertIn("container_only", result.semantic_state)
        self.assertNotIn("content", result.rows[0])

    def test_native_html_body_preserves_source_and_ignores_script_for_plaintext(self):
        raw = encoded([{"guid": "synthetic", "marketId": "PHBSX",
                        "content": "<p>预测值0</p><script>never execute</script>"}])
        result = decode(raw, {"kind": "native_body_rows"})
        self.assertEqual(result.rows[0]["plaintext"], "预测值0")
        self.assertIn("<script>", result.rows[0]["source_fields"]["content"])
        self.assertIn("semantics_pending", result.semantic_state)

    def test_institution_ownership_must_bind_to_same_company_and_source(self):
        data = {"ipBasicInfomation": {"guid": "company", "applyId": "notice"},
                "ipBasicInfomationShareList": [{"infoId": "company", "shareholdRatio": "0"}]}
        contract = {"shape": "institution_snapshot", "publication_guid": "notice"}
        self.assertEqual(len(decode(envelope(data), contract).rows), 2)
        data["ipBasicInfomationShareList"][0]["infoId"] = "other"
        with self.assertRaises(Fault):
            decode(envelope(data), contract)
