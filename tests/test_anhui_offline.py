"""Anhui models are fixed synthetic proofs, never copied production payloads."""
import calendar
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from pmos.adapters.anhui import decode, project_bound, SUPPORTED
from pmos.contracts import canonical
from pmos.faults import Fault


def response(data):
    return {"status": 0, "data": data}


def run(data, model, period=None):
    return decode(canonical(response(data)), {"model": model, "success_code": 0, "period": period})


class AnhuiOfflineTests(unittest.TestCase):
    def test_cells_keep_zero_negative_and_report_missing_marker(self):
        data = {"zero": 0, "negative": -4, "absent": "/"}
        model = {"kind": "cells", "container": ["data"], "fields": sorted(data),
                 "rows": [["0", "0", "/"]], "cells": [[0, i, ["data", key]] for i, key in enumerate(data)],
                 "types": ["number", "number", "number_or_report_slash"]}
        result = run(data, model)
        self.assertEqual(result.rows[0]["cells"], ["0", "-4", "/"])
        self.assertFalse(result.empty)
        self.assertIsNone(result.unit)
        with self.assertRaises(Fault):
            run({**data, "zero": None}, model)

    def test_record_schema_preserves_raw_order_and_duplicate_rows(self):
        data = [{"value": 0, "note": None}, {"value": 0, "note": None}]
        model = {"kind": "raw_records", "path": ["data"], "fields": ["note", "value"],
                 "row_field_schemas": {"value": ["number"], "note": ["NoneType"]}, "headers": []}
        result = run(data, model)
        self.assertEqual(len(result.rows), 2)
        self.assertIsNone(result.rows[0]["fields"]["note"])
        self.assertEqual(result.rows[0]["fields"], result.rows[1]["fields"])
        self.assertEqual(result.semantic_state, "named_source_records_mapping_pending")

    def test_time_records_reject_alias_conflict_scope_drift_and_unapproved_duplicates(self):
        data = [{"time": "00:15", "value": 0, "alias": 0, "scope": "synthetic-a"},
                {"time": "00:30", "value": -2, "alias": -2, "scope": "synthetic-a"}]
        model = {"kind": "time_records", "path": ["data"], "fields": sorted(data[0]),
                 "time_field": "time", "value_field": "value", "equal_value_aliases": ["alias"],
                 "scope_constants": {"scope": "synthetic-a"}, "times": ["00:15", "00:30"],
                 "rows": [["00:15", "0"], ["00:30", "0"]], "cells": [[0, 1, "00:15"], [1, 1, "00:30"]]}
        self.assertEqual(project_bound(response(data), model)[1][1], "-2")
        for change in ("alias", "scope", "duplicates"):
            modified = deepcopy(data)
            if change == "duplicates":
                modified *= 2
            else:
                modified[0][change] = "changed"
            with self.assertRaises(Fault):
                run(modified, model)

    def test_proven_identical_multiplicity_is_not_two_series(self):
        records = [{"time": "24:00", "value": 0}] * 2
        model = {"kind": "time_records", "path": ["data"], "fields": ["time", "value"],
                 "time_field": "time", "value_field": "value", "times": ["24:00"],
                 "scope_constants": {}, "identical_source_multiplicities": [2],
                 "rows": [["24:00", "0"]], "cells": [[0, 1, "24:00"]]}
        self.assertFalse(run(records, model).empty)
        self.assertEqual(len(project_bound(response(records), model)), 1)

    def test_text_status_is_not_converted_to_price(self):
        data = [{"clock": "24:00", "statusText": "测试状态"}]
        model = {"kind": "time_records", "path": ["data"], "fields": sorted(data[0]),
                 "time_field": "clock", "value_field": "statusText", "value_schema": "str",
                 "times": ["24:00"], "rows": [["24:00"], ["测试状态"]], "cells": [[1, 0, "24:00"]]}
        self.assertEqual(run(data, model).rows[1]["cells"], ["测试状态"])

    def test_clock_codes_keep_unknown_code_and_null_without_label_invention(self):
        model = {"kind": "clock_codes", "times": ["00:15", "00:30"],
                 "rows": [["00:15", "00:30"], ["状态A", "状态B"]], "cells": [[1, 0, "00:15"], [1, 1, "00:30"]]}
        result = run({"00:15": 9, "00:30": None}, model)
        self.assertEqual(result.rows[1]["cells"], ["9", ""])
        self.assertEqual(result.semantic_state, "original_status_codes_not_prices")

    def test_multi_table_rows_are_dynamic_not_frozen_headers(self):
        section = {"source_key": "first", "columns": ["value"], "fields": ["value"], "field_schemas": {"value": ["number"]}}
        model = {"kind": "multi_records", "sections": [section, {**section, "source_key": "second"}], "allow_observed_empty_data_array": True}
        first = project_bound(response({"first": [{"value": 1}], "second": [{"value": 2}]}), model)
        second = project_bound(response({"first": [{"value": -3}], "second": [{"value": 0}]}), model)
        self.assertEqual(first, [["value"], ["1"], ["value"], ["2"]])
        self.assertEqual(second, [["value"], ["-3"], ["value"], ["0"]])
        self.assertTrue(run([], model).empty)

    def test_observed_empty_and_raw_fields_keep_mapping_pending(self):
        self.assertTrue(run([], {"kind": "observed_records", "path": ["data"]}).empty)
        model = {"kind": "raw_fields", "path": ["data"], "data_schema": {"a": "number", "b": "NoneType"}, "numeric_fields": 1}
        result = run({"a": 0, "b": None}, model)
        self.assertEqual(result.rows[1]["value"], None)
        self.assertEqual(result.semantic_state, "named_source_fields_mapping_pending")

    def test_matrix_and_scalar_require_observed_types(self):
        model = {"kind": "matrix", "path": ["data"], "columns": [2], "cell_schema": [["number", "str"]], "headers": [["值", "备注"]]}
        self.assertEqual(run([[0, "测试"]], model).rows[-1]["cells"], ["0", "测试"])
        with self.assertRaises(Fault):
            run([[0, 9]], model)
        scalar = {"kind": "scalar", "data_schema": "number", "rows": [["0"]], "cells": [[0, 0, ["data"]]]}
        self.assertEqual(run(-1, scalar).rows[0]["cells"], ["-1"])

    def test_monthly_quantity_missing_price_is_only_allowed_for_zero(self):
        row = {"stat_month": "2026-09", "sequence_name": "合成类别", **{f"point{i}": 1 for i in range(1, 25)},
               **{f"price{i}": 2 for i in range(1, 25)}, "ttEnergy": 24, "avgPrice": 2}
        model = {"kind": "monthly_trade", "path": ["data"]}
        self.assertEqual(project_bound(response([row]), model, period="2026-09")[-1][0], "2026年09月合计")
        bad = {**row, "price1": None}
        with self.assertRaises(Fault):
            run([bad], model, "2026-09")
        with self.assertRaises(Fault):
            run([row], model, "2026-08")

    def test_contract_month_is_complete_and_not_actual_load(self):
        fields = {"monthAndDay": "", "ttPoint": 0, "avgPrice": 0,
                  **{f"ttPoint{i}": 0 for i in range(1, 25)}, **{f"avgPrice{i}": -1 for i in range(1, 25)}}
        records = [{**fields, "monthAndDay": f"2026/02/{day:02d}"} for day in range(1, 29)] + [{**fields, "monthAndDay": "汇总"}]
        model = {"kind": "portal_contract_curve", "fields": sorted(fields)}
        result = run({"list": records, "total": 28}, model, "2026-02")
        self.assertEqual(len(result.rows), 59)
        self.assertIsNone(result.unit)
        with self.assertRaises(Fault):
            run({"list": records[:-1], "total": 28}, model, "2026-02")

    def test_listing_is_original_rows_not_attachment_success_or_statistical_date(self):
        raw = {"list": [{"title": "脱敏公告", "validDate": "2026-10-01", "articleContent": "合成正文"}],
               "total": 1, "pageNum": 1, "pageSize": 10}
        result = run(raw, {"kind": "listing", "path": ["data", "list"]})
        self.assertEqual(result.rows[0]["fields"]["validDate"], "2026-10-01")
        self.assertEqual(result.pagination["reported_total"], 1)
        self.assertTrue(result.pagination["all_pages_verified"])
        self.assertFalse(result.summary()["full_dataset_coverage_verified"])

    def test_dynamic_bundle_requires_contiguous_complete_pages_and_matching_columns(self):
        columns = [{"field": "value", "title": "合成值"}]
        definition = [{"colName": "value", "colTitle": "合成值", "isDisplay": "1"}]
        page = response({"objectList": {"list": [{"value": 0}], "pageNum": 1, "pageSize": 20, "total": 1}, "ipConfigDataColsList": definition})
        data = {"query_period": "2026-10-18", "pages": [page]}
        result = run(data, {"kind": "dynamic_sdk", "columns": columns}, "2026-10-18")
        self.assertEqual(result.rows[-1]["cells"], ["1", "0"])
        with self.assertRaises(Fault):
            run({**data, "pages": [page, page]}, {"kind": "dynamic_sdk", "columns": columns}, "2026-10-18")

    def test_auth_denial_unknown_and_bool_success_never_become_data(self):
        for status in (401, 40302, 500, False):
            with self.assertRaises(Fault):
                decode(canonical({"status": status, "data": []}), {"model": {"kind": "observed_records", "path": ["data"]}})

    def test_model_list_is_explicit(self):
        self.assertIn("dynamic_sdk", SUPPORTED)
        self.assertIn("monthly_trade", SUPPORTED)
        self.assertNotIn("infer_from_zeroes", SUPPORTED)

    def test_original_null_is_retained_alongside_display_blank(self):
        model = {"kind": "clock_codes", "times": ["24:00"], "rows": [["24:00"], ["状态"]], "cells": [[1, 0, "24:00"]]}
        result = run({"24:00": None}, model)
        self.assertIsNone(result.rows[0]["original_data"]["24:00"])
        self.assertEqual(result.rows[1]["cells"], [""])

    def test_legacy_article_preserves_declared_period_without_inferring_first_release(self):
        source = {"dataset": "fixture", "statistical_period": None, "publication_title": "合成标题", "publication_date": "2026-10-18",
                  "body_text": "测试正文", "acquisition": "fixture_article_source"}
        result = decode(canonical(source), {"capture_kind": "article_source"})
        self.assertIsNone(result.rows[0]["statistical_period"])
        self.assertIn("not_inferred", result.semantic_state)

    def test_derived_contract_reference_is_not_original_source_acceptance(self):
        source = {"records": [{"quantity": "0", "price": None}], "actual_load": False}
        result = decode(canonical(source), {"capture_kind": "derived_reference"})
        self.assertIsNone(result.rows[0]["price"])
        self.assertIn("original_proof_pending", result.semantic_state)

    def test_shared_workbook_reader_retains_numeric_lexical_value_and_formula(self):
        import io
        import zipfile
        buffer = io.BytesIO()
        main = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('xl/workbook.xml', f'<workbook xmlns="{main}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="合成表" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/></Relationships>')
            archive.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{main}"><sheetData><row r="1"><c r="A1"><v>0</v></c><c r="B1"><f>A1-2</f><v>-2</v></c><c r="C1"/></row></sheetData></worksheet>')
        result = decode(buffer.getvalue(), {"artifact_kind": "xlsx"})
        values = {row['coordinate']: row for row in result.rows}
        self.assertEqual(values['A1']['value'], '0')
        self.assertEqual(values['B1']['formula'], 'A1-2')
        self.assertEqual(values['B1']['value'], '-2')
        self.assertIsNone(values['C1']['value'])
        self.assertIn('mapping_pending', result.semantic_state)

    def test_pdf_only_proves_container_and_empty_export_is_rejected(self):
        raw = b'%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n'
        result = decode(raw, {"artifact_kind": "pdf"})
        self.assertIn('container_only', result.semantic_state)
        with self.assertRaises(Fault):
            decode(b'', {"artifact_kind": "xlsx"})
