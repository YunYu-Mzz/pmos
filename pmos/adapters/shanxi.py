"""Offline Shanxi sources: explicit native contracts and unchanged originals.

No legacy module, queue, browser or request builder is imported. Response
fields stay source fields; container/cell decoding is not semantic acceptance.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from ..contracts import ParsedArtifact, canonical, reject_secrets
from ..excel import ContainerError, check_document, check_xls
from ..faults import Fault
from ..validate import Response, evaluate

MAX_BYTES = 64 * 1024 * 1024


def _fault(code):
    raise Fault(code, "site_data")


def _unit(contract, raw=None):
    from ..contracts import unit_from_response
    return unit_from_response(raw, contract) if raw is not None else None


def _page(data, rows, contract):
    if not isinstance(data, dict) or type(data.get("total")) is not int or data["total"] < len(rows):
        _fault("shanxi_page_total_changed")
    total = data["total"]
    page, size = data.get("pageNum"), data.get("pageSize")
    if (page == 0 and size == 0 and contract.get("allow_zero_page_metadata") is True):
        page, size = contract.get("request_page"), contract.get("request_page_size")
    if type(page) is not int or type(size) is not int or page < 1 or size < 1:
        _fault("shanxi_reviewed_request_pagination_required")
    if contract.get("request_page") not in (None, page) or contract.get("request_page_size") not in (None, size):
        _fault("shanxi_response_request_page_mismatch")
    expected_pages = max(1, (total + size - 1) // size)
    if page > expected_pages or len(rows) != min(size, max(0, total - (page - 1) * size)):
        _fault("shanxi_page_count_changed")
    return {"page": page, "page_size": size, "reported_total": total,
            "reported_pages": data.get("pages"), "expected_pages": expected_pages,
            "selected_page_complete": True, "all_pages_verified": expected_pages == 1,
            "source_zero_page_metadata": data.get("pageNum") == 0 and data.get("pageSize") == 0}


def _json(raw, contract, http_status):
    if contract.get("source_kind") == "native_day_ahead_section":
        scope = contract.get("selected_scope")
        if not isinstance(scope, str) or not scope or not isinstance(contract.get("expected_period"), str):
            _fault("shanxi_source_scope_period_required")
        contract = {**contract, "response_period_fields": ["infoDate"],
                    "expected_response_scope": {"sectionName": scope},
                    "response_value_enums": {"value": ["是", "否"]}}
    evaluate({"kind": "json"}, Response(http_status, "application/json", raw))
    value = json.loads(raw, parse_float=Decimal, parse_constant=lambda _: _fault("shanxi_nonfinite_json"))
    reject_secrets(value)
    if not isinstance(value, dict) or type(value.get("status")) not in (int, str):
        _fault("shanxi_status_not_observed")
    allowed = contract.get("success_statuses", [0, "0", 200, "200"])
    if value["status"] not in allowed:
        _fault("shanxi_success_code_changed")
    data = value.get("data")
    shape = contract.get("shape", contract.get("response_shape", "paged_records"))
    pagination = None
    if shape == "institution_snapshot":
        company = data.get("ipBasicInfomation") if isinstance(data, dict) else None
        owners = data.get("ipBasicInfomationShareList") if isinstance(data, dict) else None
        if not isinstance(company, dict) or company.get("applyId") != contract.get("publication_guid"):
            _fault("shanxi_institution_source_binding_changed")
        if not isinstance(owners, list) or any(not isinstance(row, dict) or row.get("infoId") != company.get("guid") for row in owners):
            _fault("shanxi_institution_ownership_binding_changed")
        for row in owners:
            value = row.get("shareholdRatio")
            if value not in (None, ""):
                try:
                    valid = Decimal(0) <= Decimal(str(value)) <= Decimal(100)
                except InvalidOperation:
                    valid = False
                if not valid:
                    _fault("shanxi_share_ratio_out_of_range")
        rows = [{"source_table": "company", "fields": company}]
        rows.extend({"source_table": "ownership", "fields": row} for row in owners)
    elif shape == "single_record":
        required = contract.get("required_fields")
        if not isinstance(required, list) or not required or not isinstance(data, dict) or set(data) != set(required):
            _fault("shanxi_single_record_fields_changed")
        rows = [data]
    elif shape == "market_tables":
        tables = contract.get("response_tables")
        if not isinstance(data, dict) or not isinstance(tables, list) or not tables or set(data) != set(tables):
            _fault("shanxi_named_tables_changed")
        rows = []
        for name in tables:
            if not isinstance(data[name], list) or any(not isinstance(row, dict) for row in data[name]):
                _fault("shanxi_named_table_rows_changed")
            rows.extend({"source_table": name, "source_row": index, "fields": row}
                        for index, row in enumerate(data[name], 1))
    elif shape in {"publication_list", "dynamic_table", "paged_records", "list"}:
        if shape == "dynamic_table":
            data = data.get("objectList") if isinstance(data, dict) else None
        if shape == "publication_list" and (data is None or data == []):
            return ParsedArtifact([], True, semantic_state="publication_metadata_only")
        if shape == "dynamic_table" and data == []:
            if type(contract.get("request_page", 1)) is not int or contract.get("request_page", 1) != 1:
                _fault("shanxi_empty_dynamic_nonfirst_page")
            return ParsedArtifact([], True, semantic_state="explicit_empty_dynamic_table")
        if not isinstance(data, dict):
            _fault("shanxi_native_table_shape_changed")
        rows = data.get("list")
        if shape == "publication_list" and data.get("total") == 0 and rows is None:
            rows = []
        if not isinstance(rows, list):
            _fault("shanxi_native_table_rows_changed")
        if shape == "publication_list" and (not rows and data.get("total") == 0 or
                not (type(data.get("pageNum")) is int and data["pageNum"] > 0
                     and type(data.get("pageSize")) is int and data["pageSize"] > 0)):
            if type(data.get("total")) is not int or data["total"] < len(rows):
                _fault("shanxi_page_total_changed")
            pagination = {"reported_total": data["total"], "selected_page_complete": False,
                          "all_pages_verified": False, "page_metadata_unverified": True}
        else:
            pagination = _page(data, rows, contract)
    elif shape == "market_rows":
        rows = data
    else:
        _fault("shanxi_response_shape_not_reviewed")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        _fault("shanxi_row_schema_changed")
    if shape not in {"market_tables", "institution_snapshot"}:
        for row in rows:
            if any(field not in row for field in contract.get("required_row_fields", [])):
                _fault("shanxi_required_row_field_missing")
            for field in contract.get("response_period_fields", []):
                if str(row.get(field, ""))[:10] != contract.get("expected_period"):
                    _fault("shanxi_response_period_mismatch")
            for field, expected in contract.get("expected_response_scope", {}).items():
                if row.get(field) != expected:
                    _fault("shanxi_response_scope_mismatch")
            for field, allowed_values in contract.get("response_value_enums", {}).items():
                if row.get(field) not in allowed_values:
                    _fault("shanxi_response_value_not_observed")
    semantic = "publication_metadata_only" if shape == "publication_list" else "source_fields_only"
    return ParsedArtifact(deepcopy(rows), not rows, pagination=pagination, unit=_unit(contract, raw), semantic_state=semantic)


class _SourceText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if tag in {"p", "div", "br", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"p", "div", "tr"}:
            self.parts.append(" ")

    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text)


def _body_rows(raw):
    rows = json.loads(raw)
    reject_secrets(rows)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        _fault("shanxi_native_body_rows_required")
    result = []
    for row in rows:
        html = row.get("content")
        if html in (None, ""):
            continue
        if not isinstance(html, str) or row.get("marketId") != "PHBSX":
            _fault("shanxi_native_body_source_changed")
        parser = _SourceText()
        parser.feed(html)
        parser.close()
        result.append({"source_fields": row, "plaintext": re.sub(r"\s+", " ", "".join(parser.parts)).strip()})
    return ParsedArtifact(result, not result, semantic_state="publication_source_body_only_numeric_semantics_pending")


def _xlsx(raw):
    from ..workbooks import xlsx_cells
    rows = xlsx_cells(raw)
    return ParsedArtifact(rows, not rows,
                          semantic_state="xlsx_source_cells_only_formulas_not_executed")


def _xls(raw):
    try:
        import xlrd
    except ImportError:
        raise Fault("shanxi_xls_cell_decoder_unavailable", "local") from None
    check_xls(raw)
    book = xlrd.open_workbook(file_contents=raw, on_demand=True)
    rows = []
    try:
        for sheet in book.sheets():
            if len(rows) + sheet.nrows * sheet.ncols > 500000:
                _fault("shanxi_workbook_cell_limit")
            for r in range(sheet.nrows):
                for c in range(sheet.ncols):
                    cell = sheet.cell(r, c)
                    rows.append({"sheet": sheet.name, "row": r + 1, "column": c + 1,
                                 "type": cell.ctype, "value": cell.value, "datemode": book.datemode})
    finally:
        book.release_resources()
    return ParsedArtifact(rows, not rows, semantic_state="xls_cached_cells_only_formulas_not_executed")


def decode(raw: bytes, contract: dict, *, http_status: int = 200) -> ParsedArtifact:
    """Decode a reviewed response/file; no request parameters are generated."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        _fault("shanxi_original_size_invalid")
    kind = contract.get("kind", "json")
    if kind == "native_body_rows":
        if http_status != 200:
            evaluate({"kind": "json"}, Response(http_status, "application/json", raw))
        return _body_rows(raw)
    if kind == "json":
        return _json(raw, contract, http_status)
    # Business-error envelopes must be classified before any file signature.
    if raw.lstrip().startswith((b"{", b"[")):
        evaluate({"kind": "excel"}, Response(http_status, "application/octet-stream", raw))
    if http_status != 200:
        evaluate({"kind": "binary"}, Response(http_status, "application/octet-stream", raw))
    try:
        if kind == "workbook":
            if raw.startswith(b"PK"):
                return _xlsx(raw)
            if raw.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
                return _xls(raw)
            _fault("shanxi_workbook_format_changed")
        if kind == "attachment":
            facts = check_document(raw)
            return ParsedArtifact([{"format": facts["format"], "size": len(raw),
                                    "sha256": hashlib.sha256(raw).hexdigest()}], False,
                                  semantic_state="attachment_container_only_not_text_or_numeric_acceptance")
    except ContainerError as exc:
        raise Fault(str(exc), "local" if str(exc).endswith("unavailable") else "site_data") from None
    except (ValueError, KeyError, ET.ParseError, zipfile.BadZipFile):
        _fault("shanxi_workbook_structure_changed")
    raise ValueError("shanxi_offline_kind_unsupported")


def decode_pages(pages: list[dict], contract: dict) -> ParsedArtifact:
    """Combine independently hashed contiguous pages, retaining duplicate rows."""
    if not isinstance(pages, list) or not pages:
        raise ValueError("shanxi_pages_required")
    rows = []
    total = expected = page_size = None
    receipts = []
    for number, item in enumerate(pages, 1):
        raw = item["raw"]
        digest = hashlib.sha256(raw).hexdigest()
        if item.get("sha256") != digest:
            _fault("shanxi_page_sha256_changed")
        per_page = {**contract, "request_page": number}
        parsed = decode(raw, per_page, http_status=item.get("http_status", 200))
        page = parsed.pagination
        if not page or page.get("page") != number:
            _fault("shanxi_contiguous_page_receipts_required")
        if total is None:
            total, expected, page_size = page["reported_total"], page["expected_pages"], page["page_size"]
        if page["reported_total"] != total or page["expected_pages"] != expected:
            _fault("shanxi_pages_total_changed")
        if page["page_size"] != page_size:
            _fault("shanxi_pages_size_changed")
        rows.extend(parsed.rows)
        receipts.append({"page": number, "sha256": digest, "rows": len(parsed.rows)})
    complete = len(pages) == expected and len(rows) == total
    return ParsedArtifact(rows, not rows, pagination={"received_pages": len(pages), "expected_pages": expected,
        "reported_total": total, "all_pages_verified": complete, "page_receipts": receipts,
        "atomic_single_response_snapshot": False}, unit=_unit(contract))


def _safe_source(root, reference):
    path = (root / reference).resolve()
    if (not path.is_relative_to(root.resolve()) or path.is_symlink()
            or any(re.search(r"(?i)private|profile|cookie|credential|token", part) for part in path.parts)):
        raise ValueError("shanxi_source_path_not_public_evidence")
    return path


def _reference(root, path):
    return {"path": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def audit_inventory(root: Path, *, samples_per_source: int | None = None) -> dict:
    """Reproducible local audit, returns only refs/hashes/counts; writes nothing.

    Catalogues are local retained snapshots. Every source is listed, including
    absent/denied/missing evidence. This never opens a production queue DB.
    """
    root = Path(root).resolve()
    if samples_per_source is not None and not 1 <= samples_per_source <= 1000:
        raise ValueError("bounded_samples_required")
    extra_names = ("publication_content_adaptations.json", "institution_detail_adaptations.json",
                   "publication_attachment_adaptations.json", "publication_workbook_adaptations.json",
                   "publication_pdf_document_adaptations.json", "publication_image_document_adaptations.json",
                   "publication_word_document_adaptations.json")
    files = {name: root / name for name in ("dataset_catalog.json", "query_adapter_catalog.json",
             "native_menu_query_contracts.json", "publication_adapter_catalog.json", "reporting/indicator_registry.json", *extra_names)}
    catalog = json.loads(files["dataset_catalog.json"].read_text())["targets"]
    query = json.loads(files["query_adapter_catalog.json"].read_text())["records"]
    contracts = json.loads(files["native_menu_query_contracts.json"].read_text())["records"]
    publications = json.loads(files["publication_adapter_catalog.json"].read_text())["records"]
    registry = json.loads(files["reporting/indicator_registry.json"].read_text())
    definitions = {}
    for row in catalog:
        definitions.setdefault(row["id"], {})["legacy_catalog_status"] = row.get("status")
    for row in query:
        definitions.setdefault(row["id"], {})["query_record"] = row
    for dataset, contract in contracts.items():
        definitions.setdefault(dataset, {})["contract"] = contract
    for row in publications:
        definitions.setdefault("pub_" + row["menu"]["guid"].replace("-", ""), {})["publication_record"] = row
    for name in extra_names:
        if not files[name].is_file():
            continue
        for row in json.loads(files[name].read_text()).get("records", []):
            dataset = row.get("id") or "body_" + row["menu"]["guid"].replace("-", "")
            definitions.setdefault(dataset, {}).setdefault("extra_records", []).append((name, row))
    # Manifest is an immutable-artifact index, not a queue ownership proof.
    manifest = root / "data/2026/original/manifest.jsonl"
    originals = {}
    with manifest.open() as stream:
        for line in stream:
            row = json.loads(line)
            originals.setdefault(row["dataset"], []).append(row)
            definitions.setdefault(row["dataset"], {})
    def selected(values):
        return values if samples_per_source is None else values[:samples_per_source]
    output = []
    dependency_versions = {}
    for name in ("xlrd", "olefile"):
        try:
            dependency_versions[name] = __import__(name).__version__
        except ImportError:
            dependency_versions[name] = None
    for dataset, entry in sorted(definitions.items()):
        receipts = []
        semantic_states = set()
        pending = []
        contract = deepcopy(entry.get("contract", {}))
        samples = []
        q_record = entry.get("query_record", {})
        if (not contract and q_record.get("adapter") == "adapters.public_disclosure"
                and q_record.get("query", {}).get("path") == "/px-settlement-infpubquery/ipDisclosureApplyApi/scfwQueryData"):
            contract = {"kind": "json", "shape": "paged_records", "request_page_size": 6,
                        "expected_response_scope": {"marketId": "PHBSX"}}
        if contract:
            original_contract = entry.get("contract", {})
            request_info = contract.get("observed_request", {}).get("body", {}).get("pageInfo", {})
            contract.update(kind="json", shape="market_tables" if contract.get("response_tables") else
                            contract.get("shape", contract.get("response_shape", "paged_records")))
            contract["request_page_size"] = request_info.get("pageSize", contract.get("request_page_size"))
            contract["allow_zero_page_metadata"] = dataset == "menu_53e8d0ac6968" or original_contract.get("kind") in {
                "current_realname_snapshot", "native_day_ahead_section", "native_date_scoped_remaining"}
            if original_contract.get("kind") == "native_rolling_market":
                contract["shape"] = "market_rows"
            for path in selected(sorted((root / "data/2026/native_query" / dataset).glob("*/*.json"))):
                digest = path.stem.split("_")[-1]
                per_sample = deepcopy(contract)
                per_sample["request_page"] = int(path.stem.split("_")[0])
                per_sample["expected_period"] = path.parent.name
                if dataset == "queryRealtimeNodeMarginalPrice":
                    per_sample["response_period_fields"] = ["atDate"]
                elif original_contract.get("kind") == "native_day_ahead_section":
                    per_sample["source_kind"] = "native_day_ahead_section"
                elif original_contract.get("response_date_field"):
                    per_sample["response_period_fields"] = [original_contract["response_date_field"]]
                    per_sample["expected_response_scope"] = {
                        original_contract["response_scope_field"]: original_contract.get("selected_scope")}
                elif dataset in {"disclosure_38cb41a3fa114b61b826543b1ff98924", "disclosure_c9e82547327b4959a81a5a923d869ec2"}:
                    per_sample["response_period_fields"] = ["pdate"]
                samples.append((path, digest, per_sample))
        q = entry.get("query_record", {}).get("query", {})
        if not samples and q.get("sample_path") and q.get("sha256"):
            path = _safe_source(root, q["sample_path"])
            if not contract:
                pending.append("source_contract_missing")
            elif path.is_file():
                per_sample = deepcopy(contract)
                per_sample["request_page"] = entry.get("contract", {}).get("observed_request", {}).get("body", {}).get("pageInfo", {}).get("pageNum", 1)
                samples.append((path, q["sha256"], per_sample))
        pub = entry.get("publication_record")
        if pub:
            q = pub.get("query", {})
            path = _safe_source(root, q["sample_path"]) if q.get("sample_path") else None
            if path and path.is_file():
                samples.append((path, q["sha256"], {"kind": "json", "shape": "publication_list", "success_statuses": [0]}))
            else:
                pending.append("publication_original_not_present")
        for name, extra in entry.get("extra_records", []):
            ref = extra.get("source_file", extra.get("native_plaintext_source", extra.get("query", {})))
            reference = ref.get("sample_path") or ref.get("path")
            if not reference or not ref.get("sha256"):
                pending.append("registered_source_reference_missing")
                continue
            path = _safe_source(root, reference)
            if not path.is_file():
                pending.append("registered_source_not_present")
                continue
            if name == "publication_content_adaptations.json":
                sample_contract = {"kind": "native_body_rows"}
            elif name == "institution_detail_adaptations.json":
                sample_contract = {"kind": "json", "shape": "institution_snapshot",
                                   "publication_guid": extra.get("publication_guid")}
            else:
                sample_contract = {"kind": "workbook" if "workbook" in name else "attachment"}
                if sample_contract["kind"] == "attachment":
                    pending.append("document_numeric_or_text_semantic_parser_pending")
            samples.append((path, ref["sha256"], sample_contract))
        for row in selected(originals.get(dataset, [])):
            path = _safe_source(root, row["path"])
            if path.is_file():
                samples.append((path, row["sha256"], {"kind": "workbook"}))
        for path, expected, per_sample in samples:
            if not path.is_file():
                pending.append("referenced_original_missing")
                continue
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            receipt = {"source_ref": str(path.relative_to(root)), "sha256": digest, "size": len(raw)}
            try:
                if digest != expected:
                    raise ValueError("source_sha256_mismatch")
                parsed = decode(raw, per_sample)
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError("source_changed_during_offline_review")
                semantic_states.add(parsed.semantic_state)
                receipt.update(result="offline_passed", rows=len(parsed.rows), empty=parsed.empty,
                               parsed_sha256=hashlib.sha256(canonical(parsed.rows)).hexdigest())
                if parsed.pagination is not None:
                    receipt["pagination"] = parsed.pagination
            except (Fault, ValueError) as exc:
                reason = getattr(exc, "code", str(exc))
                receipt.update(result="blocked", reason=reason if re.fullmatch(r"[a-z0-9_]{1,100}", reason) else "decoder_rejected_structure")
            receipts.append(receipt)
        if not receipts:
            pending.append("no_retained_supported_original")
        passed = sum(r["result"] == "offline_passed" for r in receipts)
        output.append({"dataset": dataset, "legacy_status": entry.get("legacy_catalog_status"),
                       "contract_source_sha256": entry.get("contract", {}).get("source_sha256"),
                       "qualification": "sample_only_online_pending" if passed else "blocked_evidence_or_dependency",
                       "samples": receipts, "missing_evidence": sorted(set(pending)),
                       "semantic_states": sorted(semantic_states),
                       "full_dataset_coverage_verified": False})
    by_id = {row["dataset"]: row for row in output}
    indicator_rows = []
    for indicator in registry:
        ids = indicator.get("ids", [])
        known = [x for x in ids if x in by_id]
        indicator_rows.append({"number": indicator.get("number"), "ids": ids,
            "offline_sources": known, "missing_source_bindings": [x for x in ids if x not in by_id],
            "all_listed_sources_have_sample": bool(ids) and len(known) == len(ids) and all(
                by_id[x]["qualification"] == "sample_only_online_pending" for x in known),
            "online_acceptance": "pending"})
    return {"province": "shanxi", "mode": "offline_only", "website_requests": 0,
            "queue_databases_opened": 0, "catalog_references": [_reference(root, p) for p in files.values() if p.is_file()],
            "dependency_versions": dependency_versions, "samples_per_source_limit": samples_per_source,
            "manifest_row_count": sum(len(rows) for rows in originals.values()),
            "manifest_distinct_original_paths": len({r["path"] for rows in originals.values() for r in rows}),
            "physical_workbook_files": sum(1 for p in (root / "data/2026/original").rglob("*")
                                           if p.is_file() and p.suffix.lower() in {".xls", ".xlsx"}),
            "source_count": len(output), "indicator_registry_rows": len(registry),
            "source_qualification_counts": dict(Counter(r["qualification"] for r in output)),
            "sample_result_counts": dict(Counter(r["result"] for d in output for r in d["samples"])),
            "unique_original_paths_checked": len({r["source_ref"] for d in output for r in d["samples"]}),
            "unique_original_paths_passed": len({r["source_ref"] for d in output for r in d["samples"] if r["result"] == "offline_passed"}),
            "decoder_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "sources": output, "indicator_rows": indicator_rows,
            "limitations": ["Local catalog snapshots, not a live health check", "Source sample acceptance is not full semantic or historical coverage", "Attachments require independent source-listed identity and receipt; list acceptance does not prove downloaded contents", "No queue ownership migration or release of consumed/unknown work"]}
