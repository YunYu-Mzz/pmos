"""Independent regressions for shared artifact and source-binding boundaries."""
import io
import json
import unittest
from unittest.mock import patch
import warnings
import zipfile
import hashlib

from pmos.adapters.shanxi import decode, decode_pages
from pmos.excel import ContainerError, check_document, check_xlsx
from pmos.faults import Fault
from pmos.workbooks import xlsx_cells

NAMESPACE = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def workbook(*, cells='<c r="A1"><v>0</v></c>', shared=None, empty_book=False, extra_parts=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        sheets = '' if empty_book else '<sheets><sheet name="合成表" sheetId="1" r:id="rId1"/></sheets>'
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{NAMESPACE}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">{sheets}</workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{NAMESPACE}"><sheetData><row r="1">{cells}</row></sheetData></worksheet>')
        if shared is not None:
            archive.writestr('xl/sharedStrings.xml', shared)
        for name, raw in extra_parts:
            archive.writestr(name, raw)
    return buffer.getvalue()


def page(rows, number, size, total):
    return json.dumps({'status': 0, 'data': {'list': rows, 'pageNum': number, 'pageSize': size, 'total': total}}).encode()


class IndependentWorkbookReview(unittest.TestCase):
    def test_expansion_gate_runs_before_any_crc_decompression(self):
        raw = workbook()
        original = zipfile.ZipFile.infolist
        def oversized(archive):
            infos = original(archive)
            infos[-1].file_size = 64 * 1024 * 1024 + 1
            return infos
        with patch.object(zipfile.ZipFile, 'infolist', oversized):
            with patch.object(zipfile.ZipFile, 'testzip', side_effect=AssertionError('CRC must follow size admission')):
                for checker in (xlsx_cells, check_xlsx, check_document):
                    with self.assertRaisesRegex(ContainerError, 'zip_size_limit'):
                        checker(raw)

    def test_duplicate_and_escaping_zip_parts_are_rejected(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            duplicate = workbook(extra_parts=[('xl/workbook.xml', b'<workbook/>')])
        escaping = workbook(extra_parts=[('../outside.xml', b'<x/>')])
        for raw, reason in ((duplicate, 'zip_duplicate_parts'), (escaping, 'zip_part_path_invalid')):
            with self.assertRaisesRegex(ContainerError, reason):
                xlsx_cells(raw)

    def test_utf16_entity_declaration_is_rejected_before_expansion(self):
        xml = ('<?xml version="1.0" encoding="UTF-16"?>'
               '<!DOCTYPE sst [<!ENTITY replaced "NOT_SOURCE_TEXT">]>'
               f'<sst xmlns="{NAMESPACE}"><si><t>&replaced;</t></si></sst>').encode('utf-16')
        raw = workbook(cells='<c r="A1" t="s"><v>0</v></c>', shared=xml)
        with self.assertRaisesRegex(Fault, 'xml_declaration_forbidden'):
            xlsx_cells(raw)

    def test_empty_workbook_is_structural_failure_not_successful_empty_data(self):
        with self.assertRaisesRegex(Fault, 'sheets_missing'):
            xlsx_cells(workbook(empty_book=True))

    def test_invalid_boolean_numeric_and_row_identity_are_rejected(self):
        examples = [('<c r="A1" t="b"><v>not-a-boolean</v></c>', 'boolean_invalid'),
                    ('<c r="A1"><v>not-a-number</v></c>', 'number_invalid'),
                    ('<c r="A2"><v>1</v></c>', 'cell_row_mismatch')]
        for cells, reason in examples:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(Fault, reason):
                    xlsx_cells(workbook(cells=cells))

    def test_phonetic_annotations_are_not_concatenated_into_cell_value(self):
        shared = f'<sst xmlns="{NAMESPACE}"><si><t>测试</t><rPh sb="0" eb="2"><t>ce shi</t></rPh></si></sst>'
        inline = '<c r="A1" t="inlineStr"><is><r><t>测试</t></r><rPh sb="0" eb="2"><t>ce shi</t></rPh></is></c>'
        first = xlsx_cells(workbook(cells='<c r="A1" t="s"><v>0</v></c>', shared=shared))
        second = xlsx_cells(workbook(cells=inline))
        self.assertEqual(first[0]['value'], '测试')
        self.assertEqual(second[0]['value'], '测试')

    def test_shared_formula_attributes_survive_without_execution(self):
        cells = '<c r="A1"><f t="shared" si="0" ref="A1:A2">2+3</f><v>5</v></c>'
        row = xlsx_cells(workbook(cells=cells))[0]
        self.assertEqual(row['formula'], '2+3')
        self.assertEqual(row['formula_attributes'], {'t': 'shared', 'si': '0', 'ref': 'A1:A2'})
        self.assertEqual(row['value'], '5')


class IndependentShanxiReview(unittest.TestCase):
    def test_day_ahead_section_binds_period_subject_and_observed_states(self):
        original = {'infoDate': '2026-10-18', 'sectionName': 'synthetic-section', 'value': '是'}
        contract = {'shape': 'paged_records', 'source_kind': 'native_day_ahead_section',
                    'selected_scope': 'synthetic-section', 'expected_period': '2026-10-18'}
        result = decode(page([original], 1, 20, 1), contract)
        self.assertEqual(result.rows[0]['value'], '是')
        for field, value in [('infoDate', '2026-10-19'), ('sectionName', 'other'), ('value', 'unknown')]:
            with self.subTest(field=field):
                with self.assertRaises(Fault):
                    decode(page([{**original, field: value}], 1, 20, 1), contract)

    def test_nonfirst_dynamic_empty_response_cannot_claim_empty_page(self):
        raw = b'{"status":0,"data":{"objectList":[]}}'
        with self.assertRaisesRegex(Fault, 'empty_dynamic_nonfirst_page'):
            decode(raw, {'shape': 'dynamic_table', 'request_page': 2})
        self.assertTrue(decode(raw, {'shape': 'dynamic_table', 'request_page': 1}).empty)

    def test_changed_page_size_cannot_combine_into_a_single_scope(self):
        first = page([{'v': 0}] * 3, 1, 3, 5)
        second = page([{'v': 0}], 2, 4, 5)
        pages = [{'raw': raw, 'sha256': hashlib.sha256(raw).hexdigest()} for raw in (first, second)]
        with self.assertRaisesRegex(Fault, 'pages_size_changed'):
            decode_pages(pages, {'shape': 'paged_records'})
