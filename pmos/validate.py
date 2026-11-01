"""Turn one raw HTTP response into a stored-artifact decision or a Fault.

Order matters: transport/HTTP problems first, then a JSON error envelope
(an export endpoint answers JSON when it refuses), then the expected payload.
Only a response that passes all checks may be archived and confirmed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import platform
from .excel import ContainerError, check_document, check_xls, check_xlsx
from .faults import Fault

# Page text/tables captured by the ui transport, not a site response.
CAPTURE_TYPE = "application/x-pmos-capture+json"
EXCEL_TYPES = {"application/vnd.ms-excel",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               "application/octet-stream", "application/x-download", "application/force-download"}


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes
    name: str = ""                 # file name offered by the site, if any
    parts: tuple = ()              # several files from one UI action (an article's attachments)
    operation: dict | None = None  # requests the page made during the operation (gate summary)

    @property
    def media_type(self) -> str:
        return self.content_type.split(";", 1)[0].strip().lower()


@dataclass(frozen=True)
class Outcome:
    result: str          # "saved" or "empty"
    extension: str       # archive extension
    business_status: str | None = None
    detail: dict | None = None


def _json(response: Response) -> object | None:
    if (response.media_type not in {"application/json", "text/json", "text/plain"}
            and not response.body.lstrip().startswith((b"{", b"["))):
        return None
    try:
        return json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        return None


def classify_envelope(body: object, http_status: int, _depth: int = 0) -> Fault | None:
    """A JSON envelope that is not a business success, as a Fault."""
    status = platform.business_status(body)
    message = platform.message_text(body)
    if _depth > 8:
        return Fault("envelope_depth_exceeded", "unknown", http_status=http_status)
    if status in platform.AUTH_STATUS or platform.AUTH_TEXT.search(message):
        return Fault("auth_expired", "auth", http_status=http_status, business_status=status)
    if status in platform.RATE_LIMIT_STATUS or platform.RATE_LIMIT_TEXT.search(message):
        return Fault("rate_limited", "rate_limit", http_status=http_status, business_status=status)
    if status in platform.PERMISSION_STATUS:
        return Fault("permission_denied", "permission", http_status=http_status, business_status=status)
    if status in platform.SUCCESS_STATUS:
        child = body.get("data") if isinstance(body, dict) else None
        if isinstance(child, dict) and ("status" in child or "code" in child) and ("data" in child or "message" in child):
            return classify_envelope(child, http_status, _depth + 1)
        return None
    return Fault("unexpected_business_status", "unknown", http_status=http_status, business_status=status)


def _path(value: object, dotted: str) -> object:
    """``data`` or a nested ``data.objectList``."""
    for key in dotted.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _empty(value: object) -> bool:
    if value is None or value == [] or value == {} or value == "":
        return True
    if isinstance(value, dict):
        for key in ("list", "records", "rows", "dataList", "tableData"):
            if key in value and isinstance(value[key], list):
                return not value[key]
    return False


def evaluate(spec_response: dict, response: Response) -> Outcome:
    if response.parts:
        # Every file of the action must pass on its own.
        return Outcome("saved", ".parts", detail={"parts": [evaluate(spec_response, part) for part in response.parts]})
    if response.media_type == CAPTURE_TYPE and 200 <= response.status < 300:
        # Our own record of what the page showed (an article without files).
        try:
            json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            raise Fault("capture_invalid", "local") from None
        return Outcome("saved", ".json")
    kind = spec_response.get("kind", "json")
    if response.status == 401:
        raise Fault("auth_expired", "auth", http_status=401)
    if response.status == 429:
        raise Fault("rate_limited", "rate_limit", http_status=429)
    if response.status == 403:
        raise Fault("permission_denied", "permission", http_status=403)
    if not 200 <= response.status < 300:
        raise Fault("http_error", "unknown", http_status=response.status)
    if response.media_type == "text/html":
        # Usually the login page or a gateway error page, never an export.
        raise Fault("unexpected_html", "unknown", http_status=response.status)
    body = _json(response)
    if kind == "finereport_daily":
        # Not the platform's envelope: {currentPage, totalPage, htmlTag}, no status code.
        if body is None:
            raise Fault("json_expected", "unknown", http_status=response.status)
        from .finereport import daily_values
        try:
            values = daily_values(body, text=bool(spec_response.get("text_values")))
        except ValueError as exc:
            raise Fault(str(exc), "site_data", http_status=response.status) from None
        return Outcome("saved" if any(item["value"] is not None for item in values) else "empty", ".json")
    if kind == "finereport_table":
        # The same answer, a table of any shape.
        if body is None:
            raise Fault("json_expected", "unknown", http_status=response.status)
        from .finereport import table_rows
        try:
            rows = table_rows(body)
        except ValueError as exc:
            raise Fault(str(exc), "site_data", http_status=response.status) from None
        return Outcome("saved" if rows else "empty", ".json")
    if body is not None:
        fault = classify_envelope(body, response.status)
        if fault is not None:
            raise fault
    status = platform.business_status(body)
    if kind == "json":
        if body is None:
            raise Fault("json_expected", "unknown", http_status=response.status)
        data = _path(body, spec_response.get("data_field", "data")) if isinstance(body, dict) else body
        empty = spec_response.get("empty_when_no_data", True) and _empty(data)
        return Outcome("empty" if empty else "saved", ".json", status)
    if body is not None:
        raise Fault("json_instead_of_file", "unknown", http_status=response.status, business_status=status)
    try:
        if kind in ("xlsx", "xls", "excel"):
            if response.media_type not in EXCEL_TYPES:
                raise Fault("file_type_changed", "unknown", http_status=response.status)
            if response.body.startswith(b"PK") and kind in ("xlsx", "excel"):
                return Outcome("saved", ".xlsx", detail=check_xlsx(response.body))
            if response.body.startswith(bytes.fromhex("d0cf11e0")) and kind in ("xls", "excel"):
                return Outcome("saved", ".xls", detail=check_xls(response.body))
            raise ContainerError("workbook_format_unexpected")
        if kind == "pdf":
            if not response.body.startswith(b"%PDF-") or b"%%EOF" not in response.body[-2048:]:
                raise ContainerError("pdf_container_invalid")
            return Outcome("saved", ".pdf")
        if kind == "document":
            detail = check_document(response.body)
            return Outcome("saved", detail["format"], detail=detail)
        if kind == "binary":
            if not response.body:
                raise ContainerError("empty_file")
            return Outcome("saved", spec_response.get("extension", ".bin"))
    except ContainerError as exc:
        # HTTP 200 with a file that fails local checks: keep it out of the
        # archive, quarantine only this dataset.
        raise Fault(str(exc), "site_data", http_status=response.status) from None
    raise Fault("response_kind_unknown", "local")
