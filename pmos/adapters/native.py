"""Three reviewed native JSON shapes, not a guessed universal site API."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json

from ..contracts import ParsedArtifact, reject_secrets, unit_from_response
from ..faults import Fault
from ..validate import Response, evaluate

CLOCKS = [f"{n // 4:02d}:{n % 4 * 15:02d}" for n in range(1, 97)]
FIELDS = ["v" + clock.replace(":", "") for clock in CLOCKS]


def decode(raw: bytes, contract: dict, *, http_status: int = 200) -> ParsedArtifact:
    # Check HTTP and nested business errors before looking at any data.
    evaluate({"kind": "json"}, Response(http_status, "application/json", raw))
    payload = json.loads(raw, parse_float=Decimal)
    reject_secrets(payload)
    if not isinstance(payload, dict) or type(payload.get("status")) is not int:
        raise Fault("contract_status_changed", "unknown")
    if payload["status"] not in contract["success_statuses"]:
        raise Fault("contract_status_changed", "unknown")
    data = payload.get("data")
    shape = contract["shape"]
    if shape in ("wide_curve_list", "wide_curve_object"):
        if shape == "wide_curve_list":
            if not isinstance(data, list) or len(data) > 1:
                raise Fault("curve_shape_changed", "site_data")
            if not data:
                return ParsedArtifact([], True)
            data = data[0]
        if not isinstance(data, dict) or set(data) != set(FIELDS):
            raise Fault("curve_fields_changed", "site_data")
        rows = []
        for clock, key in zip(CLOCKS, FIELDS):
            value = data[key]
            try:
                number = Decimal(str(value))
            except InvalidOperation:
                raise Fault("curve_value_unproven", "site_data") from None
            if isinstance(value, bool) or not number.is_finite():
                raise Fault("curve_value_unproven", "site_data")
            rows.append({"time": clock, "value": str(value), "source_field": key})
        # Units need a reviewed contract; the field names alone prove no unit.
        return ParsedArtifact(rows, False, unit=unit_from_response(raw, contract),
                              semantic_state="selected_curve_grid_only")
    if shape in ("paged_records", "dynamic_records"):
        if shape == "dynamic_records":
            data = data.get("objectList") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise Fault("page_shape_changed", "site_data")
        rows = data["list"]
        if any(not isinstance(row, dict) for row in rows):
            raise Fault("page_row_changed", "site_data")
        page, size, total = (data.get(k) for k in ("pageNum", "pageSize", "total"))
        if any(type(v) is not int for v in (page, size, total)) or page < 1 or size < 1 or total < 0:
            raise Fault("page_bounds_changed", "site_data")
        expected_pages = max(1, (total + size - 1) // size)
        expected_rows = min(size, max(0, total - (page - 1) * size))
        if page > expected_pages or len(rows) != expected_rows:
            raise Fault("page_count_mismatch", "site_data")
        return ParsedArtifact(rows, not rows, pagination={"page": page, "page_size": size,
            "reported_total": total, "reported_pages": data.get("pages"),
            "expected_pages": expected_pages, "last": page == expected_pages,
            "selected_page_complete": True, "all_pages_verified": expected_pages == 1})
    raise ValueError("offline_adapter_shape_unsupported")
