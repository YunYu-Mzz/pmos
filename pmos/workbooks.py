"""Shared bounded XLSX source-cell reader; no formula execution or unit inference.

Sheet, coordinate, style, raw cached numeric text, formula and missing cells are
preserved. Source cells are not an assertion of business-field semantics.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import io
from pathlib import PurePosixPath
import re
import xml.etree.ElementTree as ET
import zipfile
from xml.parsers import expat

from .excel import ContainerError, check_xlsx, zip_preflight
from .faults import Fault

MAX_BYTES = 64 * 1024 * 1024
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _fault(code):
    raise Fault(code, "site_data")


def _xml(raw):
    # Expat recognizes declarations in UTF-8/UTF-16 and declared encodings.
    # Byte-substring guards miss UTF-16's interleaved NUL bytes.
    parser = expat.ParserCreate()
    def reject_declaration(*_args):
        _fault("workbook_xml_declaration_forbidden")
    parser.StartDoctypeDeclHandler = reject_declaration
    parser.EntityDeclHandler = reject_declaration
    parser.ExternalEntityRefHandler = reject_declaration
    try:
        parser.Parse(raw, True)
        return ET.fromstring(raw)
    except (expat.ExpatError, ET.ParseError):
        _fault("workbook_xml_invalid")


def _source_string(item):
    # Phonetic guides (rPh) are annotations, not part of the cell value.
    parts = []
    for child in item:
        if child.tag == "{" + NS["m"] + "}t":
            parts.append(child.text or "")
        elif child.tag == "{" + NS["m"] + "}r":
            parts.extend(node.text or "" for node in child.findall("m:t", NS))
    return "".join(parts)


def xlsx_cells(raw: bytes) -> list[dict]:
    check_xlsx(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as book:
        zip_preflight(book)
        infos = book.infolist()
        if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_BYTES:
            _fault("workbook_size_limit")
        if len({i.filename for i in infos}) != len(infos):
            _fault("workbook_duplicate_parts")
        strings = []
        if "xl/sharedStrings.xml" in book.namelist():
            shared = _xml(book.read("xl/sharedStrings.xml"))
            strings = [_source_string(si)
                       for si in shared.findall("m:si", NS)]
        workbook = _xml(book.read("xl/workbook.xml"))
        relationships = _xml(book.read("xl/_rels/workbook.xml.rels"))
        targets = {}
        for relation in relationships:
            if relation.get("TargetMode") == "External":
                continue
            target = relation.get("Target", "")
            target = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if ".." in PurePosixPath(target).parts:
                _fault("workbook_part_path_invalid")
            identifier = relation.get("Id")
            if not identifier or identifier in targets:
                _fault("workbook_relationship_id_invalid")
            targets[identifier] = target
        rows = []
        sheets = workbook.findall("m:sheets/m:sheet", NS)
        if workbook.tag != "{" + NS["m"] + "}workbook" or not sheets:
            _fault("workbook_sheets_missing")
        names = set()
        for sheet in sheets:
            name = sheet.get("name")
            if not name or name in names:
                _fault("workbook_sheet_name_invalid")
            names.add(name)
            target = targets.get(sheet.get("{" + REL + "}id"))
            if not target:
                _fault("workbook_sheet_relationship_missing")
            xml = _xml(book.read(target))
            if xml.tag != "{" + NS["m"] + "}worksheet":
                _fault("workbook_worksheet_invalid")
            coordinates = set()
            for source_row in xml.findall("m:sheetData/m:row", NS):
                number = source_row.get("r")
                if number is not None and (not number.isdigit() or not 1 <= int(number) <= 1048576):
                    _fault("workbook_row_number_invalid")
                for source_cell in source_row.findall("m:c", NS):
                    address = source_cell.get("r", "")
                    matched = re.fullmatch(r"[A-Z]{1,3}([1-9][0-9]*)", address)
                    if number is not None and (matched is None or int(matched[1]) != int(number)):
                        _fault("workbook_cell_row_mismatch")
            for cell in xml.findall("m:sheetData/m:row/m:c", NS):
                coordinate = cell.get("r", "")
                if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]*", coordinate) or coordinate in coordinates:
                    _fault("workbook_cell_coordinate_invalid")
                coordinates.add(coordinate)
                kind = cell.get("t", "n")
                item = cell.find("m:v", NS)
                value = item.text if item is not None else None
                if kind == "s":
                    if value is None or not value.isdigit() or int(value) >= len(strings):
                        _fault("workbook_shared_string_invalid")
                    value = strings[int(value)]
                elif kind == "inlineStr":
                    value = _source_string(cell.find("m:is", NS)) if cell.find("m:is", NS) is not None else ""
                elif kind == "n" and value is not None:
                    try:
                        finite = Decimal(value).is_finite()
                    except InvalidOperation:
                        _fault("workbook_number_invalid")
                    if not finite:
                        _fault("workbook_nonfinite_number")
                elif kind == "b" and value not in {"0", "1", "true", "false", None}:
                    _fault("workbook_boolean_invalid")
                elif kind not in {"n", "b", "e", "str", "d"}:
                    _fault("workbook_cell_type_unsupported")
                formula = cell.find("m:f", NS)
                match = re.fullmatch(r"([A-Z]+)([0-9]+)", coordinate)
                column = 0
                for char in match[1]:
                    column = column * 26 + ord(char) - ord("A") + 1
                if column > 16384 or int(match[2]) > 1048576:
                    _fault("workbook_cell_coordinate_invalid")
                rows.append({"sheet": sheet.get("name"), "coordinate": coordinate, "type": kind,
                             "source_type": kind, "row": int(match[2]), "column": column,
                             "value": value, "formula": formula.text if formula is not None else None,
                             "formula_attributes": dict(formula.attrib) if formula is not None else None,
                             "style": cell.get("s")})
                if len(rows) > 500000:
                    _fault("workbook_cell_limit")
    return rows
