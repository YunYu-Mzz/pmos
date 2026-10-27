"""Offline interpreter for source-bound Anhui response models.

No legacy imports, browser calls, endpoint generation or queue writes. Models
come from hash-verified retained bindings; this module never infers a new UI
mapping from matching zeroes or similar field names.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import calendar
import hashlib
import json
from pathlib import Path
import re

from ..contracts import ParsedArtifact, canonical, reject_secrets
from ..faults import Fault
from ..validate import Response, evaluate

SUPPORTED = frozenset({"cells", "scalar", "records", "raw_records", "matrix", "time_records",
    "raw_fields", "observed_records", "clock_codes", "multi_records", "portal_contract_list",
    "portal_contract_curve", "monthly_trade", "dynamic_sdk", "listing"})
DATES = {"日期", "交易日期", "月份", "年份", "页码"}
MAX_BYTES = 64 * 1024 * 1024


def _fail(code):
    raise Fault(code, "site_data")


def _load_json(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        _fail("anhui_original_size_invalid")
    try:
        return json.loads(raw, parse_float=Decimal,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        _fail("anhui_json_invalid")


def _verified_unit(raw, contract):
    from ..contracts import unit_from_response
    return unit_from_response(raw, contract)


def validate_retained_binding(receipt, parsed, binding):
    """Tie a reviewed model to this exact dataset and stable non-date scope."""
    dataset = receipt.get("dataset")
    if (not isinstance(dataset, str) or parsed.get("dataset") != dataset
            or binding.get("dataset") != dataset
            or parsed.get("source_path") != receipt.get("source_path")):
        raise ValueError("retained_dataset_or_source_binding_mismatch")
    selected, bound = receipt.get("selectors"), binding.get("selectors")
    if not isinstance(selected, dict) or not isinstance(bound, dict):
        raise ValueError("retained_scope_selectors_missing")
    selected = {key: value for key, value in selected.items() if key not in DATES}
    bound = {key: value for key, value in bound.items() if key not in DATES}
    scope_hash = hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    if (selected != bound or binding.get("scope_sha256") != scope_hash
            or binding.get("scope_key") != dataset + ":" + scope_hash[:20]):
        raise ValueError("retained_scope_binding_mismatch")


def _at(value, path):
    for part in path:
        value = value[part]
    return value


def _number(value):
    if isinstance(value, (bool, dict, list)) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _schema(value):
    if isinstance(value, dict):
        return {key: _schema(child) for key, child in value.items()}
    if isinstance(value, list):
        return ["list", _schema(value[0]) if value else None]
    return "number" if _number(value) is not None else type(value).__name__


def _walk(value, path=()):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, path + (index,))


def _display(value, places):
    number = _number(value)
    if number is None:
        _fail("anhui_number_missing")
    return format(number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), f".{places}f")


def _monthly(data, expected_period=None):
    if not isinstance(data, list) or not data:
        _fail("anhui_monthly_records_changed")
    quantities = [f"point{i}" for i in range(1, 25)] + ["ttEnergy"]
    prices = [f"price{i}" for i in range(1, 25)] + ["avgPrice"]
    month = data[0].get("stat_month")
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
        _fail("anhui_monthly_period_changed")
    if expected_period is not None and month != expected_period:
        _fail("anhui_response_period_changed")
    rows, displayed = [], []
    allowed = {"stat_month", "sequence_name", *quantities, *prices}
    for index, record in enumerate(data):
        if (not isinstance(record, dict) or not set(record) <= allowed or record.get("stat_month") != month
                or not isinstance(record.get("sequence_name"), str) or not record["sequence_name"]
                or not {"stat_month", "sequence_name", *quantities} <= set(record)):
            _fail("anhui_monthly_fields_changed")
        cells, pairs = [], []
        for quantity_field, price_field in zip(quantities, prices):
            number = _number(record[quantity_field])
            if number is None:
                _fail("anhui_monthly_quantity_missing")
            quantity = "0" if number == 0 else _display(number, 3)
            price = record.get(price_field)
            if price is None:
                if number != 0:
                    _fail("anhui_monthly_nonzero_price_missing")
                price = ""
            else:
                price = _display(price, 5)
            cells.extend([quantity, price])
            pairs.append((Decimal(quantity), Decimal(price) if price else None))
        rows.append(([month] if index == 0 else []) + [record["sequence_name"], *cells])
        displayed.append(pairs)
    total = []
    for column in range(25):
        quantity = sum((row[column][0] for row in displayed), Decimal(0))
        if quantity == 0:
            _fail("anhui_monthly_zero_total_unobserved")
        weighted = sum((row[column][0] * row[column][1] for row in displayed if row[column][1] is not None), Decimal(0))
        total.extend([_display(quantity, 3), _display(weighted / quantity, 5)])
    rows.append([f"{month[:4]}年{month[5:]}月合计", *total])
    return [["月份", "交易类别", *[f"时段{i}" for i in range(1, 25)], "汇总"], ["电量", "电价"] * 25, *rows]


def _dynamic_page(payload, columns):
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("objectList"), dict):
        _fail("anhui_dynamic_page_shape_changed")
    obj, definitions = data["objectList"], data.get("ipConfigDataColsList")
    if not isinstance(definitions, list) or not definitions:
        _fail("anhui_dynamic_columns_changed")
    if any(not isinstance(col, dict) or not {"colName", "colTitle", "isDisplay"} <= set(col) for col in definitions):
        _fail("anhui_dynamic_columns_changed")
    shown = [{"field": col["colName"], "title": col["colTitle"]} for col in definitions if col["isDisplay"] != "0"]
    if shown != columns:
        _fail("anhui_dynamic_columns_changed")
    page, size, total = (obj.get(key) for key in ("pageNum", "pageSize", "total"))
    records = obj.get("list")
    if (any(type(v) is not int for v in (page, size, total)) or page < 1 or size != 20 or total < 0
            or not isinstance(records, list) or len(records) != max(0, min(size, total - (page - 1) * size))
            or any(not isinstance(row, dict) or any(col["field"] not in row for col in columns) for row in records)):
        _fail("anhui_dynamic_page_count_changed")
    return records, page, total, max(1, (total + size - 1) // size)


def project_bound(payload, model, *, period=None):
    """Reconstruct only a previously proven model; no discovery or side effect."""
    try:
        kind = model["kind"]
        data = payload["data"]
        if kind not in SUPPORTED:
            _fail("anhui_model_unsupported")
        if kind in {"observed_records", "portal_contract_list", "listing"}:
            records = _at(payload, model.get("path", ["data", "list"]))
            if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
                _fail("anhui_recordset_changed")
            if kind == "listing":
                page, size, total = (data.get(key) for key in ("pageNum", "pageSize", "total"))
                if (any(type(value) is not int for value in (page, size, total)) or page < 1 or size < 1 or total < 0
                        or page > max(1, (total + size - 1) // size)
                        or len(records) != max(0, min(size, total - (page - 1) * size))):
                    _fail("anhui_listing_pagination_changed")
            if kind == "portal_contract_list":
                page, total = data.get("pageNum"), data.get("total")
                if (total != model["total"] or type(page) is not int or page < 1 or data.get("pageSize") != 10
                        or not records or len(records) != max(0, min(10, total - (page - 1) * 10))):
                    _fail("anhui_contract_pagination_changed")
                required_text = ("contractName", "typeName", "saleParticipantname", "vendeeParticipantname")
                if any(sorted(row) != model["fields"] or not row.get("contractId")
                        or any(not isinstance(row[field], str) or not row[field] for field in required_text)
                        or any(_number(row[field]) is None for field in ("contractQty", "contractPrice")) for row in records):
                    _fail("anhui_contract_fields_changed")
                if len({row["contractId"] for row in records}) != len(records):
                    _fail("anhui_duplicate_contract")
                if period is not None and page != int(period):
                    _fail("anhui_contract_page_changed")
            return []
        if kind == "monthly_trade":
            return _monthly(_at(payload, model["path"]), period)
        if kind == "clock_codes":
            if data == []:
                return []
            if not isinstance(data, dict) or sorted(data) != model["times"]:
                _fail("anhui_status_times_changed")
            rows = deepcopy(model["rows"])
            for row, column, clock in model["cells"]:
                if data[clock] is not None and _number(data[clock]) is None:
                    _fail("anhui_status_code_changed")
                rows[row][column] = "" if data[clock] is None else str(data[clock])
            return rows
        if kind == "multi_records":
            if data == [] and model.get("allow_observed_empty_data_array"):
                return []
            if not isinstance(data, dict) or list(data) != [section["source_key"] for section in model["sections"]]:
                _fail("anhui_multi_sections_changed")
            rows = []
            for section in model["sections"]:
                records = data[section["source_key"]]
                if not isinstance(records, list):
                    _fail("anhui_multi_records_changed")
                rows.append(section["columns"])
                for record in records:
                    if not isinstance(record, dict) or sorted(record) != section["fields"]:
                        _fail("anhui_multi_fields_changed")
                    for key, value in record.items():
                        if value is None or isinstance(value, str) and not value.strip():
                            continue
                        if key in {"序号", "容量", "必开总容量", "必停总容量"}:
                            valid = _number(value) is not None
                        else:
                            valid = _schema(value) in section["field_schemas"][key]
                        if not valid:
                            _fail("anhui_multi_value_type_changed")
                    rows.append(["" if record[key] is None else str(record[key]) for key in section["columns"]])
            return rows
        if kind == "portal_contract_curve":
            if not isinstance(data, dict) or not isinstance(data.get("list"), list) or not data["list"]:
                _fail("anhui_contract_curve_changed")
            records = data["list"]
            year, month, day = map(int, records[0]["monthAndDay"].split("/"))
            days = calendar.monthrange(year, month)[1]
            if len(records) != days + 1 or data.get("total") != days or day != 1:
                _fail("anhui_contract_month_incomplete")
            if period is not None and f"{year:04d}-{month:02d}" != period:
                _fail("anhui_contract_month_changed")
            rows = [["日期", "量价", "合计", *[f"{hour}点" for hour in range(1, 25)]]]
            for index, record in enumerate(records):
                label = f"{year}/{month:02d}/{index + 1:02d}" if index < days else "汇总"
                if sorted(record) != model["fields"] or record["monthAndDay"] != label:
                    _fail("anhui_contract_curve_fields_changed")
                for label_type, prefix in (("电量", "ttPoint"), ("电价", "avgPrice")):
                    values = [record[prefix], *[record[prefix + str(k)] for k in range(1, 25)]]
                    if any(_number(value) is None for value in values):
                        _fail("anhui_contract_curve_values_missing")
                    rows.append([label, label_type, *[str(value) for value in values]])
            return rows
        if kind == "dynamic_sdk":
            if not isinstance(data, dict) or not isinstance(data.get("pages"), list) or not data["pages"]:
                _fail("anhui_dynamic_bundle_missing")
            records, total, pages = [], None, None
            for index, payload_page in enumerate(data["pages"], 1):
                evaluate({"kind": "json"}, Response(200, "application/json", canonical(payload_page)))
                batch, number, reported, expected = _dynamic_page(payload_page, model["columns"])
                if number != index or total is not None and (reported != total or expected != pages):
                    _fail("anhui_dynamic_noncontiguous_pages")
                total, pages = reported, expected
                records.extend(batch)
            if len(data["pages"]) != pages or len(records) != total:
                _fail("anhui_dynamic_date_incomplete")
            selected = data.get("query_period")
            if not isinstance(selected, str) or period is not None and selected != period:
                _fail("anhui_dynamic_period_changed")
            if any(("pdate" in row and row["pdate"] != selected) or
                   ("businessTime" in row and row["businessTime"] != selected.replace("-", "")) for row in records):
                _fail("anhui_dynamic_response_period_changed")
            return [["序号", *[col["title"] for col in model["columns"]]], *[
                [str(index), *["" if row[col["field"]] is None else str(row[col["field"]]) for col in model["columns"]]]
                for index, row in enumerate(records, 1)]]
        if kind == "time_records":
            records = _at(payload, model["path"])
            if records == [] and model["path"] == ["data"]:
                return []
            tf, vf = model["time_field"], model["value_field"]
            if (not isinstance(records, list) or not records or any(not isinstance(row, dict) or sorted(row) != model["fields"] for row in records)
                    or sorted({row[tf] for row in records}) != model["times"]):
                _fail("anhui_time_records_changed")
            counts = Counter(row[tf] for row in records)
            multiples = set(counts.values())
            if multiples != {1}:
                if len(multiples) != 1 or next(iter(multiples)) not in model.get("identical_source_multiplicities", []):
                    _fail("anhui_repeated_times_unproven")
                for clock in model["times"]:
                    copies = [row for row in records if row[tf] == clock]
                    if any(row != copies[0] for row in copies):
                        _fail("anhui_repeated_times_conflict")
            if any(any(row[key] != value for key, value in model.get("scope_constants", {}).items()) for row in records):
                _fail("anhui_response_scope_changed")
            if any(any(_number(row[vf]) != _number(row[alias]) for alias in model.get("equal_value_aliases", [])) for row in records):
                _fail("anhui_time_value_alias_changed")
            rows, by_time = deepcopy(model["rows"]), {row[tf]: row[vf] for row in records}
            for row, column, clock in model["cells"]:
                value = by_time[clock]
                valid = isinstance(value, str) and bool(value) if model.get("value_schema") == "str" else _number(value) is not None
                if not valid:
                    _fail("anhui_time_value_missing")
                rows[row][column] = str(value)
            if period is not None and any(any(row[field] != period for field in model.get("response_period_fields", [])) for row in records):
                _fail("anhui_response_period_changed")
            return rows
        if kind == "raw_fields":
            value = _at(payload, model["path"])
            if _schema(value) != model["data_schema"] or sum(_number(child) is not None for _, child in _walk(value)) != model["numeric_fields"]:
                _fail("anhui_raw_fields_structure_changed")
            return []
        if kind in {"cells", "scalar"}:
            guard = model.get("list_length_guard")
            if guard and len(_at(payload, guard["path"])) != guard["length"]:
                _fail("anhui_record_count_changed")
            if kind == "cells" and sorted(_at(payload, model["container"])) != model["fields"]:
                _fail("anhui_cells_fields_changed")
            if kind == "scalar" and _schema(data) != model["data_schema"]:
                _fail("anhui_scalar_structure_changed")
            rows = deepcopy(model["rows"])
            for index, (row, column, path) in enumerate(model["cells"]):
                value = _at(payload, path)
                expected = model.get("types", ["number"] * len(model["cells"]))[index]
                valid = _number(value) is not None or value == "/" if expected == "number_or_report_slash" else _schema(value) == expected
                if not valid or value is None or value == "":
                    _fail("anhui_cell_value_missing")
                rows[row][column] = str(value)
            if period is not None and model.get("period_value_path") and _at(payload, model["period_value_path"]) != period:
                _fail("anhui_response_period_changed")
            return rows
        records = _at(payload, model["path"])
        if not isinstance(records, list):
            _fail("anhui_record_container_changed")
        if not records:
            if model["path"] != ["data"] or any(_number(value) is not None for row in model.get("headers", []) for value in row):
                _fail("anhui_empty_unproven")
            return [] if kind == "raw_records" else deepcopy(model.get("headers", []))
        if kind in {"records", "raw_records"}:
            for record in records:
                if not isinstance(record, dict) or sorted(record) != model["fields"]:
                    _fail("anhui_record_fields_changed")
                if "row_field_schemas" in model:
                    if any(value is not None and not (isinstance(value, str) and not value.strip())
                            and _schema(value) not in model["row_field_schemas"][key] for key, value in record.items()):
                        _fail("anhui_record_type_changed")
                elif _schema(record) != model["row_schema"]:
                    _fail("anhui_record_type_changed")
            if period is not None and model.get("response_period_fields") and any(any(row[field] != period for field in model["response_period_fields"]) for row in records):
                _fail("anhui_response_period_changed")
            if kind == "raw_records":
                return []
            rows = [[str(index) if col.get("ordinal") else "" if record[col["field"]] is None else str(record[col["field"]])
                     for col in model["columns"]] for index, record in enumerate(records, 1)]
        else:
            if len(records) != len(model["columns"]) or any(not isinstance(row, list) or len(row) != count for row, count in zip(records, model["columns"])):
                _fail("anhui_matrix_structure_changed")
            if [[_schema(value) for value in row] for row in records] != model["cell_schema"]:
                _fail("anhui_matrix_type_changed")
            rows = [["" if value is None else str(value) for value in row] for row in records]
        return deepcopy(model["headers"]) + rows
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        _fail("anhui_bound_structure_changed")


def decode(raw: bytes, contract: dict, *, http_status=200) -> ParsedArtifact:
    artifact = contract.get("artifact_kind")
    if artifact in {"xlsx", "pdf"}:
        content = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if artifact == "xlsx" else "application/pdf"
        evaluate({"kind": artifact}, Response(http_status, content, raw))
        if artifact == "xlsx":
            from ..workbooks import xlsx_cells
            rows = xlsx_cells(raw)
            return ParsedArtifact(rows, not rows, semantic_state="original_cells_domain_mapping_pending")
        return ParsedArtifact([{"source_sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}], False,
                              semantic_state="pdf_container_only_content_mapping_pending")
    if contract.get("capture_kind") in {"article_source", "derived_reference"}:
        if http_status != 200:
            raise Fault("anhui_capture_http_failure", "auth" if http_status == 401 else "unknown")
        value = _load_json(raw)
        reject_secrets(value)
        if contract["capture_kind"] == "article_source":
            required = {"dataset", "statistical_period", "publication_title", "publication_date", "body_text", "acquisition"}
            if not isinstance(value, dict) or not required <= set(value) or not isinstance(value["body_text"], str):
                _fail("anhui_article_source_changed")
            return ParsedArtifact([{key: deepcopy(value[key]) for key in required}], not value["body_text"],
                                  semantic_state="retained_article_text_period_semantics_not_inferred")
        if not isinstance(value, dict) or not isinstance(value.get("records"), list) or any(not isinstance(row, dict) for row in value["records"]):
            _fail("anhui_derived_reference_changed")
        return ParsedArtifact(deepcopy(value["records"]), not value["records"], semantic_state="legacy_derived_reference_original_proof_pending")
    evaluate({"kind": "json"}, Response(http_status, "application/json", raw))
    payload = _load_json(raw)
    reject_secrets(payload)
    code = payload.get("status", payload.get("code"))
    expected = contract.get("success_code", 0)
    if type(code) is not type(expected) or code != expected:
        raise Fault("anhui_success_code_changed", "unknown")
    model = contract["model"]
    grid = project_bound(payload, model, period=contract.get("period"))
    kind = model["kind"]
    if kind in {"raw_records", "observed_records", "portal_contract_list", "listing"}:
        records = _at(payload, model.get("path", ["data", "list"]))
        rows = [{"source_row": index, "fields": deepcopy(record)} for index, record in enumerate(records, 1)]
        state = "named_source_records_mapping_pending"
    elif kind == "raw_fields":
        rows = [{"source_path": list(path), "value": deepcopy(value)} for path, value in _walk(_at(payload, model["path"])) if not isinstance(value, (dict, list))]
        state = "named_source_fields_mapping_pending"
    else:
        rows = [{"source_row": index, "cells": row} for index, row in enumerate(grid, 1)]
        state = "bound_ui_projection_selected_scope"
        if kind == "clock_codes":
            state = "original_status_codes_not_prices"
    data = payload["data"]
    if rows and kind not in {"raw_fields", "raw_records", "observed_records", "portal_contract_list", "listing"}:
        # UI blanks and formatted strings are not replacements for source nulls
        # or source precision. Retain every original field alongside the grid.
        rows[0]["original_data"] = deepcopy(data)
    empty = data == [] or kind == "multi_records" and isinstance(data, dict) and all(value == [] for value in data.values())
    if kind in {"raw_records", "observed_records", "portal_contract_list", "listing"}:
        empty = not rows
    pagination = None
    if kind in {"listing", "portal_contract_list"}:
        page, size, total = (data[key] for key in ("pageNum", "pageSize", "total"))
        pages = max(1, (total + size - 1) // size)
        pagination = {"page": page, "page_size": size, "reported_total": total,
            "reported_pages": data.get("pages"), "expected_pages": pages, "last": page == pages,
            "all_pages_verified": pages == 1}
    elif kind == "dynamic_sdk":
        pagination = {"pages": len(data["pages"]), "all_pages_verified": True,
                      "atomic_single_response_snapshot": False}
    unit = _verified_unit(raw, contract)
    return ParsedArtifact(rows, empty, pagination=pagination, unit=unit, semantic_state=state)


def build_inventory(legacy_root: Path | str) -> dict:
    """Read selected public receipts/bindings and hash originals, never write.

    The output contains per-unit counts, fixed failure codes, controlled source
    references and hashes. It excludes raw models, selectors and business rows.
    This is a historical artifact audit, not a live queue snapshot or permission.
    """
    root = Path(legacy_root).resolve()
    if not (root / "powercrawler/native_tables.py").is_file():
        raise ValueError("anhui_legacy_root_required")
    state = root / "state"
    plan_path = state / "server_production_plan_20261023.json"
    registry_path = state / "server_native_rollout_registry.json"
    plan_raw, registry_raw = plan_path.read_bytes(), registry_path.read_bytes()
    input_hashes = {str(plan_path.relative_to(root)): hashlib.sha256(plan_raw).hexdigest(),
                    str(registry_path.relative_to(root)): hashlib.sha256(registry_raw).hexdigest()}
    artifact_hashes = {}
    plan = json.loads(plan_raw)
    registry = json.loads(registry_raw)
    units = {}
    for key, unit in plan["units"].items():
        units[unit["dataset"]] = {"unit_id": key, "dataset": unit["dataset"], "family": unit["family"],
            "title": unit["title"], "legacy_queue_key": unit.get("queue_key"),
            "legacy_current_phase": registry.get(unit["dataset"], {}).get("phase", "unregistered"),
            "offline_status": "evidence_missing", "verified_captures": 0, "verified_listings": 0,
            "model_kinds": [], "failures": {}, "examples": [], "online_acceptance": "pending",
            "scope_status": "only_retained_bound_scopes_no_expansion"}
    counters, errors, models = Counter(), Counter(), Counter()
    binding_cache, bindings = {}, {}
    source_chain = hashlib.sha256()

    def read_ref(reference, digest):
        path = (root / reference).resolve()
        if not path.is_relative_to(root / "data") or path.is_symlink() or not path.is_file():
            raise ValueError("controlled_original_path_required")
        if (Path(reference).is_absolute() or ".." in Path(reference).parts
                or any(re.search(r"(?i)private|profile|cookie|password|credential|authorization|token|browser|session", part)
                       for part in path.relative_to(root).parts)):
            raise ValueError("controlled_original_path_required")
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("controlled_original_size_limit")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("original_sha256_mismatch")
        artifact_hashes[str(path.relative_to(root))] = digest
        return raw

    def record_failure(dataset, exc):
        code = exc.code if isinstance(exc, Fault) else str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        # Never emit arbitrary exception values or raw payloads.
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,100}", code):
            code = "offline_audit_exception"
        errors[code] += 1
        if dataset in units:
            target = units[dataset]["failures"]
            target[code] = target.get(code, 0) + 1

    def bound_model(receipt, parsed):
        if parsed.get("binding_path"):
            reference, digest = parsed["binding_path"], parsed["binding_sha256"]
            if digest not in binding_cache:
                binding_cache[digest] = json.loads(read_ref(reference, digest))
            bindings[reference] = digest
            return binding_cache[digest], "immutable_binding", reference, digest
        selectors = {key: value for key, value in receipt["selectors"].items() if key not in DATES}
        scope_digest = hashlib.sha256(json.dumps(selectors, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        scope = receipt["dataset"] + ":" + scope_digest[:20]
        name = "server_native_table_binding_" + hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:24] + ".json"
        path = state / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("legacy_state_binding_missing")
        raw = path.read_bytes()
        input_hashes[str(path.relative_to(root))] = hashlib.sha256(raw).hexdigest()
        bindings[str(path.relative_to(root))] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw), "legacy_mutable_state_binding", str(path.relative_to(root)), hashlib.sha256(raw).hexdigest()

    for path in sorted(state.glob("server_native_attempts_*.json")):
        journal_raw = path.read_bytes()
        input_hashes[str(path.relative_to(root))] = hashlib.sha256(journal_raw).hexdigest()
        attempts = json.loads(journal_raw)
        for job in attempts.values():
            if not isinstance(job, dict):
                continue
            counters["attempt_phase_" + str(job.get("phase", "unknown"))] += 1
            if job.get("phase") != "captured":
                continue
            receipt = job.get("receipt", {})
            dataset = receipt.get("dataset")
            try:
                raw = read_ref(receipt["source_path"], receipt["source_sha256"])
                parsed_raw = read_ref(receipt["parsed_path"], receipt["parsed_sha256"])
                parsed = json.loads(parsed_raw)
                if (parsed.get("source_sha256") != receipt["source_sha256"] or parsed.get("query_period") != receipt["period"]
                        or parsed.get("selectors") != receipt["selectors"]):
                    raise ValueError("parsed_receipt_binding_mismatch")
                binding, qualifier, binding_ref, binding_sha = bound_model(receipt, parsed)
                validate_retained_binding(receipt, parsed, binding)
                model = binding["model"]
                contract = {"model": model, "success_code": binding["success_code"], "period": receipt["period"]}
                decoded = decode(raw, contract)
                payload = json.loads(raw, parse_float=Decimal)
                grid = project_bound(payload, model, period=receipt["period"])
                if grid != parsed["rows"]:
                    raise ValueError("retained_full_projection_mismatch")
                if model["kind"] in {"raw_records", "portal_contract_list", "observed_records"}:
                    expected = json.loads(json.dumps(_at(payload, model["path"]), ensure_ascii=False, default=str))
                    if parsed.get("source_records") != expected:
                        raise ValueError("retained_named_records_mismatch")
                if model["kind"] == "dynamic_sdk":
                    original_pages = binding["metadata"]["original_http_page_sources"]
                    if len(original_pages) != len(payload["data"]["pages"]):
                        raise ValueError("dynamic_original_page_count_mismatch")
                    for index, original in enumerate(original_pages):
                        page_raw = read_ref(original["path"], original["sha256"])
                        if json.loads(page_raw, parse_float=Decimal) != payload["data"]["pages"][index]:
                            raise ValueError("dynamic_original_page_bundle_mismatch")
                        counters["dynamic_original_pages_verified"] += 1
                counters["captured_sources_verified"] += 1
                counters[qualifier] += 1
                models[model["kind"]] += 1
                source_chain.update(canonical({"path": receipt["source_path"], "sha256": receipt["source_sha256"], "parsed_sha256": receipt["parsed_sha256"]}))
                if dataset in units:
                    row = units[dataset]
                    row["verified_captures"] += 1
                    row["offline_status"] = "selected_bound_samples_verified"
                    if model["kind"] not in row["model_kinds"]:
                        row["model_kinds"].append(model["kind"])
                    if len(row["examples"]) < 2:
                        row["examples"].append({"source_ref": str(Path(root.name) / receipt["source_path"]), "source_sha256": receipt["source_sha256"],
                            "parsed_sha256": receipt["parsed_sha256"], "model_kind": model["kind"],
                            "binding_qualification": qualifier, "binding_ref": str(Path(root.name) / binding_ref), "binding_sha256": binding_sha,
                            "parsed_ref": str(Path(root.name) / receipt["parsed_path"]), "period": receipt["period"], "rows": len(decoded.rows),
                            "scope_sha256": hashlib.sha256(canonical(receipt["selectors"])).hexdigest()})
            except Exception as exc:
                record_failure(dataset, exc)

    for path in sorted(state.glob("server_native_listing_sources_*.json")):
        dataset = path.stem.removeprefix("server_native_listing_sources_")
        matches = [name for name in units if name.lower() == dataset]
        dataset = matches[0] if len(matches) == 1 else dataset
        journal_raw = path.read_bytes()
        input_hashes[str(path.relative_to(root))] = hashlib.sha256(journal_raw).hexdigest()
        for receipt in json.loads(journal_raw).values():
            try:
                raw = read_ref(receipt["source_path"], receipt["source_sha256"])
                parsed = json.loads(read_ref(receipt["parsed_path"], receipt["parsed_sha256"]))
                result = decode(raw, {"model": {"kind": "listing", "path": ["data", "list"]}})
                value = json.loads(raw)
                if (parsed["source_sha256"] != receipt["source_sha256"] or len(result.rows) != parsed["source_rows"]
                        or int(parsed["page"]) != value["data"]["pageNum"]):
                    raise ValueError("listing_receipt_mismatch")
                counters["listing_sources_verified"] += 1
                source_chain.update(canonical({"path": receipt["source_path"], "sha256": receipt["source_sha256"]}))
                if dataset in units:
                    row = units[dataset]
                    row["verified_listings"] += 1
                    row["offline_status"] = "selected_listing_sources_verified"
                    if "listing" not in row["model_kinds"]:
                        row["model_kinds"].append("listing")
                    if len(row["examples"]) < 2:
                        row["examples"].append({"source_ref": str(Path(root.name) / receipt["source_path"]), "source_sha256": receipt["source_sha256"],
                            "model_kind": "listing", "rows": len(result.rows), "attachments_verified": False})
            except Exception as exc:
                record_failure(dataset, exc)
    # Remaining units have old export/body/reference evidence rather than a
    # captured native JSON binding. Keep those qualification levels separate.
    gaps = {name for name, row in units.items() if not row["verified_captures"] and not row["verified_listings"]}
    seen_artifacts = set()
    def review_old(dataset, reference, digest, contract):
        if reference in seen_artifacts:
            return
        raw = read_ref(reference, digest)
        decoded = decode(raw, contract)
        seen_artifacts.add(reference)
        counters["legacy_artifacts_verified"] += 1
        row = units[dataset]
        row["verified_legacy_artifacts"] = row.get("verified_legacy_artifacts", 0) + 1
        kind = contract.get("artifact_kind", contract.get("capture_kind"))
        if kind not in row["model_kinds"]:
            row["model_kinds"].append(kind)
        row["offline_status"] = {"xlsx": "source_cells_verified_domain_mapping_pending",
            "pdf": "container_verified_content_mapping_pending", "article_source": "retained_text_source_verified",
            "derived_reference": "derived_reference_verified_original_proof_pending"}[kind]
        source_chain.update(canonical({"path": reference, "sha256": digest, "qualification": row["offline_status"]}))
        if len(row["examples"]) < 2:
            row["examples"].append({"source_ref": str(Path(root.name) / reference), "source_sha256": digest,
                "model_kind": kind, "rows": len(decoded.rows), "semantic_state": decoded.semantic_state})
    manifest = root / "data/2026/original/manifest.jsonl"
    if manifest.is_file():
        manifest_raw = manifest.read_bytes()
        input_hashes[str(manifest.relative_to(root))] = hashlib.sha256(manifest_raw).hexdigest()
        for line in manifest_raw.splitlines():
            entry = json.loads(line)
            dataset = entry.get("dataset")
            if dataset not in gaps:
                continue
            suffix = Path(entry["path"]).suffix.lower()
            if suffix not in {".xlsx", ".pdf"}:
                continue
            try:
                review_old(dataset, entry["path"], entry["sha256"], {"artifact_kind": suffix[1:]})
            except Exception as exc:
                record_failure(dataset, exc)
    for dataset in gaps:
        key = units[dataset]["legacy_queue_key"]
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]+", key):
            continue
        path = state / ("server_" + key + ".json")
        if not path.is_file() or path.is_symlink():
            continue
        journal_raw = path.read_bytes()
        input_hashes[str(path.relative_to(root))] = hashlib.sha256(journal_raw).hexdigest()
        queue = json.loads(journal_raw)
        for job in queue.get("jobs", {}).values():
            if not isinstance(job, dict):
                continue
            try:
                for entry in job.get("page_captures", []):
                    if isinstance(entry, dict) and entry.get("path", "").endswith(".json"):
                        review_old(dataset, entry["path"], entry["sha256"], {"capture_kind": "article_source"})
                if dataset == "portal_historical_contracts" and job.get("parsed_path"):
                    review_old(dataset, job["parsed_path"], job["parsed_sha256"], {"capture_kind": "derived_reference"})
                if job.get("original_path", "").endswith(".xlsx"):
                    review_old(dataset, job["original_path"], job["sha256"], {"artifact_kind": "xlsx"})
            except Exception as exc:
                record_failure(dataset, exc)
    for row in units.values():
        if row["failures"]:
            row["offline_status"] = "partially_verified_needs_review" if row["verified_captures"] or row["verified_listings"] else "needs_review"
        if not row["verified_captures"] and not row["verified_listings"] and not row.get("verified_legacy_artifacts"):
            row["missing_evidence"] = "No hash-linked captured native source in selected public journals; legacy phase is not unified proof"
    source_files = ["powercrawler/native_tables.py", "powercrawler/native_text_clocks.py", "powercrawler/native_multi_records.py", "powercrawler/native_contract_list.py",
        "powercrawler/native_contract_curve.py", "powercrawler/native_monthly_trade.py", "powercrawler/native_dynamic_sdk.py", "powercrawler/native_publications.py",
        "state/server_production_plan_20261023.json", "state/server_native_rollout_registry.json"]
    return {"province": "anhui", "mode": "offline_readonly", "selected_units": len(units), "excluded_candidates": len(plan["excluded_entries"]),
        "counts": dict(counters), "model_counts": dict(models), "failure_counts": dict(errors),
        "unit_status_counts": dict(Counter(row["offline_status"] for row in units.values())),
        "verified_sources_chain_sha256": source_chain.hexdigest(), "binding_references_count": len(bindings),
        "source_references": [{"path": str(Path(root.name) / reference), "sha256": input_hashes.get(reference) or hashlib.sha256((root / reference).read_bytes()).hexdigest()} for reference in source_files],
        "rolling_read_not_frozen_snapshot": True,
        "public_inputs_changed_during_scan": [str(Path(root.name) / reference) for reference, digest in {**input_hashes, **artifact_hashes}.items()
            if hashlib.sha256((root / reference).read_bytes()).hexdigest() != digest],
        "verified_artifact_input_count": len(artifact_hashes),
        "public_input_hashes": [{"path": str(Path(root.name) / reference), "sha256": digest} for reference, digest in sorted(input_hashes.items())],
        "units": list(units.values()),
        "excluded_units": [{"source_index": entry["source_index"], "surface": entry["surface"],
            "title": entry["menu_path"][-1] if entry.get("menu_path") else "legacy_candidate",
            "legacy_status": entry.get("status"), "offline_status": "candidate_not_admitted_evidence_required",
            "reason_code": entry["reason"] if re.fullmatch(r"[a-z][a-z0-9_]{0,100}", entry.get("reason", "")) else "legacy_candidate_not_admitted",
            "online_acceptance": "pending", "automatic_retry": False} for entry in plan["excluded_entries"]],
        "website_requests": 0, "production_writes": 0,
        "limitations": ["All checks refer to retained exact scopes only; no full history/subject coverage", "Mutable legacy state bindings remain explicitly qualified",
            "Listing validation is not attachment download or disclosure-time semantic verification", "Current legacy pending_review/attempting identities remain protected and are never replayed"]}
