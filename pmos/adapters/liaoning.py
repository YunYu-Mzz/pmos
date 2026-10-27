"""Liaoning offline native records, lists, tables and FineReport originals.

Only retained source bytes and an explicit shape contract are accepted.  This
module imports no legacy crawler and has no network, browser or queue writer.
Rows remain source fields: structural acceptance is not semantic acceptance.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from .. import platform
from ..contracts import ParsedArtifact, canonical, reject_secrets
from ..faults import Fault
from ..validate import Response, evaluate

LIMIT = 16 * 1024 * 1024
SUCCESS = {"0", "200"}
ROW_PATHS = (("data",), ("data", "list"), ("data", "handleList"),
             ("data", "objectList", "list"), ("data", "data", "list"))


def _value(raw: bytes):
    if not isinstance(raw, bytes) or not raw or len(raw) > LIMIT:
        raise Fault("liaoning_raw_size_invalid", "site_data")
    try:
        value = json.loads(raw, parse_float=Decimal,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise Fault("liaoning_json_invalid", "site_data") from None
    reject_secrets(value)
    return value


def _native_value(raw: bytes, http_status: int = 200):
    """Status-free native HTML is opt-in, explicit business errors still win."""
    if http_status != 200:
        evaluate({"kind": "json"}, Response(http_status, "application/json", raw))
    value = _value(raw)
    if isinstance(value, dict) and ("status" in value or "code" in value):
        evaluate({"kind": "json"}, Response(http_status, "application/json", raw))
    elif platform.AUTH_TEXT.search(platform.message_text(value)):
        raise Fault("auth_expired", "auth", http_status=http_status)
    elif platform.RATE_LIMIT_TEXT.search(platform.message_text(value)):
        raise Fault("rate_limited", "rate_limit", http_status=http_status)
    return value


def _private_reference(reference):
    return any(re.search(r"(?i)private|profile|cookie|password|credential|authorization|token|browser|session", part)
               for part in Path(reference).parts)


def _at(value, path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise Fault("liaoning_rows_path_changed", "site_data")
        value = value[key]
    return value


class _Cells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.skip = [], None, None, 0
        self.merged = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"}:
            self.cell = []
            self.merged |= any(k in {"rowspan", "colspan"} and v != "1" for k, v in attrs)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append("".join(self.cell).strip())
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None and not self.skip:
            self.cell.append(data)


def _fine_report(value):
    if (not isinstance(value, dict) or type(value.get("currentPage")) is not int
            or value["currentPage"] != 1 or type(value.get("totalPage")) is not int or value["totalPage"] != 1
            or not isinstance(value.get("htmlTag"), str)):
        raise Fault("liaoning_native_table_shape_changed", "site_data")
    parser = _Cells()
    parser.feed(value["htmlTag"])
    if parser.merged:
        raise Fault("liaoning_native_table_merged_cells_unreviewed", "site_data")
    # Exact native blank form, not an invented zero-valued 96-point day.
    if parser.rows == [[""], [""]] and 'class="pageContentDIV"' in value["htmlTag"] and 'class="x-table ' in value["htmlTag"]:
        return ParsedArtifact([], True, semantic_state="native_blank_form_only")
    grid = [row for row in parser.rows if any(row)]
    if len(grid) != 12 or any(len(row) != 16 for row in grid):
        raise Fault("liaoning_native_daily_grid_changed", "site_data")
    rows = []
    for i in range(0, 12, 2):
        for column, (clock, cell) in enumerate(zip(grid[i], grid[i + 1])):
            if cell:
                try:
                    if not Decimal(cell).is_finite():
                        raise InvalidOperation()
                except InvalidOperation:
                    raise Fault("liaoning_native_value_invalid", "site_data") from None
            rows.append({"time": clock, "value": cell if cell else None,
                         "source_row": i + 2, "source_column": column + 1})
    expected = [f"{n // 4:02d}:{n % 4 * 15:02d}" for n in range(1, 97)]
    if [row["time"] for row in rows] != expected:
        raise Fault("liaoning_native_time_grid_changed", "site_data")
    return ParsedArtifact(rows, not any(row["value"] is not None for row in rows),
                          semantic_state="selected_native_grid_only")


def _pagination(container, count, contract):
    total = container.get("total")
    if type(total) is not int or total < count:
        raise Fault("liaoning_page_total_changed", "site_data")
    sent_page, sent_size = contract.get("request_page"), contract.get("page_size")
    page = sent_page if sent_page is not None else container.get("pageNum")
    size = sent_size if sent_size is not None else container.get("pageSize")
    if type(page) is not int or page < 1 or type(size) is not int or size < 1:
        raise Fault("liaoning_request_page_evidence_required", "site_data")
    zero_metadata = contract.get("zero_page_metadata_reviewed") is True
    if not zero_metadata:
        if container.get("pageNum") not in (None, page) or container.get("pageSize") not in (None, size):
            raise Fault("liaoning_response_page_changed", "site_data")
    elif (sent_page is None or sent_size is None
          or not all(container.get(k) == 0 for k in ("pageNum", "pageSize", "pages"))):
        raise Fault("liaoning_zero_page_contract_changed", "site_data")
    pages = max(1, (total + size - 1) // size)
    expected = min(size, max(0, total - (page - 1) * size))
    if page > pages or count != expected:
        raise Fault("liaoning_page_count_changed", "site_data")
    return {"page": page, "page_size": size, "reported_total": total,
            "reported_page": container.get("pageNum"), "reported_pages": container.get("pages"),
            "expected_pages": pages, "last": page == pages,
            "selected_page_complete": True, "all_pages_verified": pages == 1,
            "request_page_evidence_used": sent_page is not None and sent_size is not None,
            "zero_page_metadata_reviewed": zero_metadata}


def decode(raw: bytes, contract: dict, *, http_status: int = 200) -> ParsedArtifact:
    """Decode one explicit source shape; do not infer business meanings/units.

    ``shape`` is records/single_record/native_daily_table/attachment. Records
    require rows_path and can carry observed field_sets, pagination path and
    the sent page/size. FineReport's status-free shape is opt-in only.
    """
    shape = contract.get("shape")
    if shape == "attachment":
        outcome = evaluate({"kind": "document"}, Response(http_status, contract["content_type"], raw))
        return ParsedArtifact([{"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
                                "format": outcome.extension}], False,
                              semantic_state="container_only_no_document_content_claim")
    if shape == "native_daily_table":
        return _fine_report(_native_value(raw, http_status))
    evaluate({"kind": "json"}, Response(http_status, "application/json", raw))
    value = _value(raw)
    if "native_columns_sha256" in contract:
        columns = _at(value, tuple(contract["native_columns_path"]))
        if hashlib.sha256(canonical(columns)).hexdigest() != contract["native_columns_sha256"]:
            raise Fault("liaoning_native_columns_changed", "site_data")
    # Permit exactly the success codes already observed in the retained source.
    node = value
    while isinstance(node, dict) and ("status" in node or "code" in node):
        code = node.get("status", node.get("code"))
        if type(code) not in (int, str) or str(code) not in set(map(str, contract.get("success_statuses", (0,)))):
            raise Fault("liaoning_success_code_changed", "unknown")
        child = node.get("data")
        if not (isinstance(child, dict) and ("status" in child or "code" in child)):
            break
        node = child
    path = tuple(contract.get("rows_path", ()))
    if not path or path not in ROW_PATHS:
        raise Fault("liaoning_explicit_rows_path_required", "local")
    rows = _at(value, path)
    if rows is None and contract.get("null_list_empty_reviewed") is True:
        parent = _at(value, path[:-1])
        if isinstance(parent, dict) and parent.get("total") == 0:
            rows = []
    if shape == "single_record":
        if not isinstance(rows, dict):
            raise Fault("liaoning_record_shape_changed", "site_data")
        rows = [rows]
    elif shape != "records":
        raise Fault("liaoning_offline_shape_unsupported", "local")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise Fault("liaoning_records_shape_changed", "site_data")
    field_sets = contract.get("field_sets")
    if field_sets and any(sorted(row) not in field_sets for row in rows):
        raise Fault("liaoning_record_fields_changed", "site_data")
    wide_fields = contract.get("wide_time_fields")
    if wide_fields is not None:
        if not isinstance(wide_fields, list) or not wide_fields or len(set(wide_fields)) != len(wide_fields):
            raise Fault("liaoning_bound_wide_grid_invalid", "local")
        for row in rows:
            for key in wide_fields:
                if key not in row:
                    raise Fault("liaoning_bound_wide_grid_changed", "site_data")
                cell = row[key]
                # Native null/blank is retained as missing, never converted to 0.
                if cell is None or cell == "":
                    continue
                try:
                    valid = not isinstance(cell, bool) and Decimal(str(cell)).is_finite()
                except InvalidOperation:
                    valid = False
                if not valid:
                    raise Fault("liaoning_bound_wide_value_changed", "site_data")
    expected_times = contract.get("expected_times")
    time_field = contract.get("time_field")
    if expected_times is not None:
        if not isinstance(time_field, str) or [row.get(time_field) for row in rows] != expected_times:
            raise Fault("liaoning_bound_time_grid_changed", "site_data")
    for key in contract.get("finite_numeric_fields", ()):
        for row in rows:
            cell = row.get(key)
            if cell is None and key in contract.get("nullable_numeric_fields", ()):
                continue
            try:
                valid = not isinstance(cell, bool) and Decimal(str(cell)).is_finite()
            except InvalidOperation:
                valid = False
            if not valid:
                raise Fault("liaoning_bound_numeric_field_changed", "site_data")
    paging = _pagination(_at(value, tuple(contract["pagination_path"])), len(rows), contract) if "pagination_path" in contract else None
    return ParsedArtifact(deepcopy(rows), not rows, pagination=paging,
                          semantic_state="retained_source_fields_only_semantics_pending")


def contract_for_sample(raw: bytes, *, request_page: int | None = None, page_size: int | None = None) -> dict:
    """Create a structural contract from a selected retained original, not a URL.

    This helper never grants semantic/full-scope qualification or schedules work.
    """
    value = _value(raw)
    if isinstance(value, dict) and {"htmlTag", "currentPage", "totalPage"} <= set(value):
        _native_value(raw)
        return {"shape": "native_daily_table", "province": "liaoning"}
    statuses = []
    node = value
    while isinstance(node, dict) and ("status" in node or "code" in node):
        code = node.get("status", node.get("code"))
        if type(code) not in (int, str) or str(code) not in SUCCESS:
            raise Fault("liaoning_sample_not_business_success", "unknown")
        statuses.append(code)
        child = node.get("data")
        if not (isinstance(child, dict) and ("status" in child or "code" in child)):
            break
        node = child
    if not statuses:
        raise Fault("liaoning_sample_status_missing", "unknown")
    for path in ROW_PATHS:
        try:
            rows = _at(value, path)
        except Fault:
            continue
        parent = _at(value, path[:-1]) if len(path) > 1 else None
        null_empty = rows is None and isinstance(parent, dict) and parent.get("total") == 0
        if not isinstance(rows, list) and not null_empty:
            continue
        if rows and any(not isinstance(row, dict) for row in rows):
            continue
        contract = {"province": "liaoning", "shape": "records", "rows_path": list(path),
                    "success_statuses": list(dict.fromkeys(statuses)),
                    "field_sets": sorted({tuple(sorted(row)) for row in rows or []}),
                    "semantic_acceptance": "pending", "full_scope_acceptance": "pending"}
        contract["field_sets"] = [list(fields) for fields in contract["field_sets"]]
        if path == ("data", "objectList", "list") and isinstance(value.get("data"), dict) and "ipConfigDataColsList" in value["data"]:
            contract.update(native_columns_path=["data", "ipConfigDataColsList"],
                native_columns_sha256=hashlib.sha256(canonical(value["data"]["ipConfigDataColsList"])).hexdigest())
        if null_empty:
            contract["null_list_empty_reviewed"] = True
        if isinstance(parent, dict) and "total" in parent:
            contract.update(pagination_path=list(path[:-1]))
            if request_page is not None:
                contract["request_page"] = request_page
            if page_size is not None:
                contract["page_size"] = page_size
            if (request_page is not None and page_size is not None
                    and all(parent.get(k) == 0 for k in ("pageNum", "pageSize", "pages"))):
                contract["zero_page_metadata_reviewed"] = True
        return contract
    raise Fault("liaoning_sample_shape_requires_review", "site_data")


def build_inventory(root: Path | str) -> dict:
    """Reproducible read-only source/sample inventory. No legacy module imports.

    The 170-row site catalogue is retained separately from 121 admitted sources
    and portable adapters; counts must not be presented as 170 business metrics.
    """
    root = Path(root).resolve()
    refs = []

    def read(relative, default=None):
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or _private_reference(relative):
            raise ValueError("liaoning_controlled_reference_required")
        if not path.is_file():
            return default
        if path.stat().st_size > LIMIT:
            raise ValueError("liaoning_reference_size_limit")
        raw = path.read_bytes()
        if len(raw) > LIMIT:
            raise ValueError("liaoning_reference_size_limit")
        refs.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(raw).hexdigest()})
        return json.loads(raw)

    catalog = read("state/sitewide_expansion_20261016/catalog_current.json", {}).get("jobs", [])
    manifest = read("state/server_verified_expansion_20261023/active_manifest.json", {}).get("sources", {})
    adapters = {}
    for path in sorted((root / "state/local_adaptation_20261020/adapters").glob("*.json")):
        value = read(path.relative_to(root))
        adapters[value["dataset"]] = value
    grid_contracts = {}
    for relative in ("state/sitewide_expansion_20261016/report_component_sources.json",
                     "evidence/advanced_api_source_20261016.json",
                     "state/expanded_adapters_20261016/component_sources.json"):
        document = read(relative, {})
        for component in document.get("components", []):
            pairs = re.findall(r'\{label:"(\d\d:\d\d)",name:"([^"\n]+)"\}', component.get("data", ""))
            if pairs and len({item[0] for item in pairs}) == len(pairs) and len({item[1] for item in pairs}) == len(pairs):
                grid_contracts.setdefault(component.get("number"), {"wide_time_fields": [item[1] for item in pairs],
                    "wide_times": [item[0] for item in pairs], "grid_source_ref": relative,
                    "grid_source_sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest()})
    candidate_proofs = {}
    source_proofs = {}
    report_sources = {}
    for item in catalog:
        if isinstance(item.get("number"), str):
            report_sources.setdefault(item["number"], set()).add(item.get("canonical_dataset") or item["dataset"])
    for dataset, source in manifest.items():
        candidate_proofs.setdefault(dataset, []).extend(source.get("proofs", []))
    for dataset, adapter in adapters.items():
        for key in ("sample", "overview_sample", "portable_sample_evidence"):
            proof = adapter.get(key)
            if isinstance(proof, dict) and "response_file" in proof and "sha256" in proof:
                candidate_proofs.setdefault(dataset, []).append(proof)
    # Result metadata is credential-free by legacy writer contract. Never read
    # issued/session/browser files while locating a retained business response.
    for path in sorted((root / "state/local_adaptation_20261020/responses").glob("*/result.json")):
        result = read(path.relative_to(root))
        if result.get("http_status") != 200 or result.get("outcome") not in {"nonempty", "empty", "saved"}:
            continue
        if "response_file" not in result or "sha256" not in result:
            continue
        for dataset in set(manifest) | set(adapters) | {item["dataset"] for item in catalog}:
            if path.parent.name.startswith(dataset + "_"):
                candidate_proofs.setdefault(dataset, []).append(result)
        if isinstance(result.get("source"), str):
            source_proofs.setdefault(result["source"], []).append(result)
        # A native report number is usable only with its exact observed endpoint
        # and a unique catalogue binding; never merge by title or row shape.
        number = result.get("number")
        endpoint = result.get("endpoint")
        if (isinstance(number, str) and isinstance(endpoint, str)
                and endpoint == "/px-dif-settlement-infpubquery/pdblnReportController/pdblnReport" + number
                and len(report_sources.get(number, ())) == 1):
            dataset = next(iter(report_sources[number]))
            candidate_proofs.setdefault(dataset, []).append(result)
    # The three FineReport original directories are business originals only.
    for directory in sorted((root / "data/2026/original/report_engine").glob("*")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*/*/response.json"))[:3]:
            candidate_proofs.setdefault(directory.name, []).append({"response_file": str(path.relative_to(root)),
                "sha256": path.parent.name, "http_status": 200})
    for path in sorted((root / "data/2026/original/local_collection/system_load").glob("*/*/response.json"))[:3]:
        candidate_proofs.setdefault("system_load", []).append({"response_file": str(path.relative_to(root)),
            "sha256": path.parent.name, "http_status": 200})

    all_sources = {item["dataset"]: deepcopy(item) for item in catalog}
    for dataset, source in manifest.items():
        all_sources.setdefault(dataset, {"dataset": dataset, "title": source.get("title"), "family": source.get("kind")})
    for dataset, source in adapters.items():
        all_sources.setdefault(dataset, {"dataset": dataset, "title": source.get("title"), "family": source.get("family")})
    for dataset in candidate_proofs:
        all_sources.setdefault(dataset, {"dataset": dataset, "family": "retained_original"})
    entries = []
    for dataset, source in sorted(all_sources.items()):
        canonical_dataset = source.get("canonical_dataset") or dataset
        proofs = candidate_proofs.get(dataset, []) + (candidate_proofs.get(canonical_dataset, []) if canonical_dataset != dataset else []) + source_proofs.get(source.get("source_id"), [])
        samples, issues, used = [], [], set()
        for proof in proofs:
            relative, expected = proof.get("response_file"), proof.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected, str) or relative in used:
                continue
            used.add(relative)
            if (_private_reference(relative) or Path(relative).is_absolute()
                    or ".." in Path(relative).parts
                    or not relative.startswith(("data/2026/original/", "state/local_adaptation_20261020/responses/"))):
                issues.append("retained_proof_path_not_allowed")
                continue
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
                issues.append("retained_proof_missing_locally")
                continue
            resolved_reference = str(path.relative_to(root))
            if (_private_reference(resolved_reference) or not resolved_reference.startswith(
                    ("data/2026/original/", "state/local_adaptation_20261020/responses/"))):
                issues.append("retained_proof_path_not_allowed")
                continue
            if path.stat().st_size > LIMIT:
                issues.append("retained_proof_size_limit")
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                issues.append("retained_proof_sha256_mismatch")
                continue
            try:
                contract = contract_for_sample(raw, request_page=proof.get("page"))
                number = source.get("number") or manifest.get(dataset, {}).get("number") or proof.get("number")
                grid = grid_contracts.get(number)
                if grid and contract.get("shape") == "records":
                    candidate_rows = _at(_value(raw), tuple(contract["rows_path"]))
                    if candidate_rows and all(set(grid["wide_time_fields"]) <= set(row) for row in candidate_rows):
                        contract.update(grid)
                parsed = decode(raw, contract, http_status=proof.get("http_status", 200))
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError("liaoning_source_changed_during_scan")
                samples.append({"source_ref": relative, "sha256": expected, "size": len(raw),
                    "contract": contract, **parsed.summary(),
                    "parsed_sha256": hashlib.sha256(canonical(parsed.rows)).hexdigest(),
                    "source_unchanged": hashlib.sha256(path.read_bytes()).hexdigest() == expected})
            except (Fault, ValueError, TypeError, KeyError) as exc:
                issues.append(getattr(exc, "code", "retained_proof_requires_review"))
            if len(samples) >= 3:
                break
        entries.append({"dataset": dataset, "title": source.get("title") or adapters.get(dataset, {}).get("title") or dataset,
            "in_site_catalog": any(item["dataset"] == dataset for item in catalog),
            "legacy_family": source.get("family"), "legacy_phase": source.get("phase"),
            "canonical_dataset": source.get("canonical_dataset"),
            "admitted_manifest": dataset in manifest, "portable_adapter": dataset in adapters,
            "offline_status": "sample_only_passed" if samples else "evidence_missing_or_shape_review",
            "semantics": "not_accepted_by_structural_projection", "online_status": "pending",
            "samples": samples, "issues": sorted(set(issues)) or ([] if samples else ["no_local_sha_bound_business_sample"]),
            "full_dataset_coverage_verified": False})
    return {"schema": 1, "province": "liaoning", "mode": "offline_readonly", "website_requests": 0,
        "catalog_rows": len(catalog), "admitted_manifest_sources": len(manifest), "portable_adapters": len(adapters),
        "technical_source_union": len(entries), "not_business_metric_count": True,
        "counts": dict(Counter(item["offline_status"] for item in entries)),
        "catalog_counts": dict(Counter(item["offline_status"] for item in entries if item["in_site_catalog"])),
        "samples_replayed": sum(len(item["samples"]) for item in entries),
        "input_refs": list({item["path"]: item for item in refs}.values()), "entries": entries,
        "rolling_read_not_frozen_snapshot": True,
        "public_inputs_changed_during_scan": [item["path"] for item in {item["path"]: item for item in refs}.values()
            if hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() != item["sha256"]],
        "limitations": ["Structural projection is not semantic, unit or full-scope acceptance",
                        "Remote-only originals are not fetched", "Queues and login remain untouched"]}
