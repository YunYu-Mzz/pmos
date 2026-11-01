"""Disclosure documents: list pages -> records -> one download task per file.

All PMOS provinces publish announcements through the same backend
(``ipDisclosureApply/getData`` or ``unstructuredcommon/getData``) with the same
record shape: ``{guid, title, operateDate, attachment: "[{id, name}]", fyear,
fmonth, fday, ...}``. A documents spec keeps the list call in ``request``
(paginated, any transport) and describes the rest in ``documents``::

    "documents": {
      "fields": {"id": "guid", "title": "title", "published": "operateDate"},
      "published_format": "epoch_ms",              # or "date"
      "filter": {"publishType": "公众"},            # keep only matching records
      "expect": {"marketId": "PDBLN"},             # any mismatch is a site change
      "attachments": {"path": "attachment", "json_string": true, "id": "id", "name": "name"},
      "period": [{"fields": {"year": "fyear", "month": "fmonth", "day": "fday"}},
                 {"title_regex": "(20\\d{2})年\\s*(1[0-2]|0?[1-9])月", "kind": "month"}],
      "stop_at_known": true,                       # stop listing at a page of known records
      "daily_listing": true,                       # list again every day, after a handoff too
      "business_year": 2026,                       # download only this year's files (see _year_admits)
      "download": {"transport": "fetch", "method": "GET",
                   "path": "/px-settlement-infpubmeex/fileService/download",
                   "query": {"fileId": "{attachment[id]}"}},
      "download_response": {"kind": "document"}
    }

Each attachment becomes a ledger task ``(dataset, period, "f<hash>")``; its
record and attachment metadata live in the ledger's ``items`` table so the
download request can be rebuilt after a restart.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime

from .paging import get_path
from .periods import CHINA

# week_of_day: the ISO week of a matched date (a weekly report titled by its date range).
TITLE_KINDS = ("day", "month", "year", "week", "quarter", "week_of_day")
KEYS = {"fields", "published_format", "filter", "expect", "attachments", "period", "stop_at_known",
        "download", "download_response", "record_only", "daily_listing", "business_year"}
YEAR_IN_TEXT = re.compile(r"(?<!\d)(20\d{2})\s*年")


class DocumentError(ValueError):
    pass


def check(block: dict) -> None:
    if not isinstance(block, dict) or set(block) - KEYS:
        raise DocumentError(f"documents accepts only {sorted(KEYS)}")
    fields = block.get("fields", {})
    if not isinstance(fields, dict) or not isinstance(fields.get("id"), str):
        raise DocumentError("documents.fields.id is required")
    if not isinstance(block.get("download"), dict) or "transport" not in block["download"]:
        raise DocumentError("documents.download must be a request with a transport")
    if "business_year" in block and (type(block["business_year"]) is not int or not 2000 <= block["business_year"] <= 2099):
        raise DocumentError("documents.business_year must be a year")
    if "daily_listing" in block and type(block["daily_listing"]) is not bool:
        raise DocumentError("documents.daily_listing must be true or false")
    for rule in block.get("period", []):
        if not isinstance(rule, dict) or not ({"fields", "title_regex"} & set(rule)):
            raise DocumentError("each documents.period rule needs fields or title_regex")
        if "title_regex" in rule:
            re.compile(rule["title_regex"])
            if rule.get("kind") not in TITLE_KINDS:
                raise DocumentError(f"title_regex rules need kind {'|'.join(TITLE_KINDS)}")


def item_scope(file_id: str) -> str:
    return "f" + hashlib.sha256(str(file_id).encode("utf-8")).hexdigest()[:15]


def _published(value, fmt: str | None) -> str | None:
    if value in (None, ""):
        return None
    if fmt == "epoch_ms" and isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, CHINA).date().isoformat()
    text = str(value)
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    return "-".join(match.groups()) if match else None


def derive_period(record: dict, title: str, rules: list[dict], published: str | None,
                  attachment_name: str | None = None) -> tuple[str, str]:
    """(period, basis). Business period from fields or title; else the publication date."""
    for rule in rules:
        if "fields" in rule:
            parts = {k: get_path(record, path) for k, path in rule["fields"].items()}
            year = parts.get("year")
            if year not in (None, ""):
                value = f"{int(year):04d}"
                if parts.get("month") not in (None, ""):
                    value += f"-{int(parts['month']):02d}"
                    if parts.get("day") not in (None, ""):
                        value += f"-{int(parts['day']):02d}"
                return value, "record_fields"
        else:
            for text in ((attachment_name or ""), title) if rule.get("attachment_name") else (title,):
                match = re.search(rule["title_regex"], text)
                if not match:
                    continue
                groups = [g for g in match.groups() if g is not None]
                kind = rule["kind"]
                if kind == "year":
                    return f"{int(groups[0]):04d}", "title"
                if kind == "month":
                    return f"{int(groups[0]):04d}-{int(groups[1]):02d}", "title"
                if kind == "day":
                    return f"{int(groups[0]):04d}-{int(groups[1]):02d}-{int(groups[2]):02d}", "title"
                if kind == "week":
                    return f"{int(groups[0]):04d}-W{int(groups[1]):02d}", "title"
                if kind == "week_of_day":
                    try:
                        year, week, _ = date(int(groups[0]), int(groups[1]), int(groups[2])).isocalendar()
                    except ValueError:
                        continue
                    return f"{year:04d}-W{week:02d}", "title"
                quarter = "一二三四".index(groups[1]) + 1 if groups[1] in "一二三四" else int(groups[1])
                return f"{int(groups[0]):04d}-Q{quarter}", "title"
    if published:
        return "pub-" + published, "publication_date"
    return "undated", "none"


def _attachments(record: dict, spec: dict) -> list[dict]:
    raw = get_path(record, spec.get("path", "attachment"))
    if spec.get("json_string") and isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            raise DocumentError("attachment_json_invalid") from None
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise DocumentError("attachment_list_invalid")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        file_id = item.get(spec.get("id", "id"))
        if file_id in (None, ""):
            continue
        out.append({**item, "id": str(file_id), "name": str(item.get(spec.get("name", "name"), ""))})
    return out


def _year_admits(record: dict, title: str, name: str, year: int) -> bool:
    """Is this file of the business year? The rule the Liaoning legacy collector kept (attachment_dimensions.expand_records).

    The year is ``fyear``, else ``businessYear``, else the first year in the title; never the publication date, and a
    record that names none is out. A title naming another year is out whatever the fields say: the legacy collector
    held those for review and never asked them. ``fyear`` can be the year a record was published instead: such a record
    is still in when the title names only this year, ``businessYear`` does not disagree, and this file's own name names
    only it too.
    """
    target = str(year)
    title_years = set(YEAR_IN_TEXT.findall(title))
    if title_years - {target}:
        return False
    for key in ("fyear", "businessYear"):
        if re.fullmatch(r"20\d{2}", str(record.get(key, ""))):
            selected = record[key]
            break
    else:
        selected = next(iter(title_years), None)
    if str(selected) == target:
        return True
    return (title_years == {target} and str(record.get("businessYear") or "") in ("", target)
            and set(YEAR_IN_TEXT.findall(name)) == {target})


def extract(records: list | None, block: dict) -> list[dict]:
    """Download items of one list page: {record, attachment, period, ...}."""
    fields = block.get("fields", {})
    if not isinstance(records, list):
        return []
    items = []
    for record in records:
        if not isinstance(record, dict):
            raise DocumentError("record_not_object")
        for key, value in block.get("expect", {}).items():
            if get_path(record, key) != value:
                raise DocumentError("record_expectation_failed")
        if any(get_path(record, k) != v for k, v in block.get("filter", {}).items()):
            continue
        record_id = get_path(record, fields["id"])
        if record_id in (None, ""):
            raise DocumentError("record_id_missing")
        title = str(get_path(record, fields.get("title", "title")) or "")
        published = _published(get_path(record, fields.get("published", "operateDate")), block.get("published_format"))
        attachments = [{"id": f"record:{record_id}", "name": ""}] if block.get("record_only") else \
            _attachments(record, block.get("attachments", {}))
        for attachment in attachments:
            if "business_year" in block and not _year_admits(record, title, attachment["name"], block["business_year"]):
                continue
            period, basis = derive_period(record, title, block.get("period", []), published, attachment["name"])
            items.append({"record_id": str(record_id), "title": title, "published": published,
                          "period": period, "period_basis": basis, "attachment": attachment,
                          "record": record, "scope": item_scope(attachment["id"])})
    return items
