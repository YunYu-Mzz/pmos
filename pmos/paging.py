"""Page-numbered JSON listings (``request.pagination``).

PMOS list endpoints take ``pageInfo: {pageNum, pageSize}`` (or similar) and
answer ``{"data": {"list": [...], "total": N}}``. A paginated task is fetched
one page per request: every page is its own ledger attempt and waits the
shared spacing; the cursor is durable, so an interrupted task resumes at the
next page instead of starting over.

    "pagination": {"page": "pageInfo.pageNum", "size": "pageInfo.pageSize",
                   "page_size": 50, "first_page": 1,
                   "records": "data.list", "total": "data.total", "max_pages": 500}

``page`` is a body/query path, a component field path (``set``), or
``args.N`` when the component takes the page as a method argument
(``handleCurrentChange(page)``). ``last`` optionally names a boolean such as
``data.isLastPage``.
"""
from __future__ import annotations

import copy
import json

KEYS = {"page", "size", "page_size", "first_page", "records", "total", "last", "max_pages"}


class PagingError(ValueError):
    pass


def check(pagination: dict) -> None:
    if not isinstance(pagination, dict) or set(pagination) - KEYS:
        raise PagingError(f"pagination accepts only {sorted(KEYS)}")
    if not isinstance(pagination.get("page"), str) or not isinstance(pagination.get("records"), str):
        raise PagingError("pagination.page and pagination.records are required paths")
    if pagination["page"].startswith("args.") and not pagination["page"][5:].isdigit():
        raise PagingError("pagination.page args.N names a method argument by index")
    for name, low, high in (("page_size", 1, 1000), ("first_page", 0, 1), ("max_pages", 1, 10000)):
        value = pagination.get(name, {"page_size": 20, "first_page": 1, "max_pages": 500}[name])
        if type(value) is not int or not low <= value <= high:
            raise PagingError(f"pagination.{name} must be an integer {low}..{high}")
    for name in ("size", "total", "last"):
        if name in pagination and not isinstance(pagination[name], str):
            raise PagingError(f"pagination.{name} must be a dotted path")


def get_path(value, dotted: str):
    for key in dotted.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return None
    return value


def set_path(target: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    for key in keys[:-1]:
        target = target.setdefault(key, {})
        if not isinstance(target, dict):
            raise PagingError(f"cannot set {dotted}: {key} is not an object")
    target[keys[-1]] = value


def first_page(pagination: dict) -> int:
    return pagination.get("first_page", 1)


def apply(request: dict, pagination: dict, page: int) -> dict:
    """Return a copy of ``request`` asking for ``page``."""
    request = copy.deepcopy(request)
    size = pagination.get("page_size", 20)
    if pagination["page"].startswith("args."):
        # the page number is a method argument, e.g. handleCurrentChange(page)
        index = int(pagination["page"].split(".", 1)[1])
        request.setdefault("args", [])
        request["args"] += [None] * (index + 1 - len(request["args"]))
        request["args"][index] = page
        if "size" in pagination:
            request.setdefault("set", {})[pagination["size"]] = size
    elif request["transport"] == "ui":
        request["page"] = page  # the listing's own page-number box
    elif request["transport"] == "component":
        request.setdefault("set", {})
        request["set"][pagination["page"]] = page
        if "size" in pagination:
            request["set"][pagination["size"]] = size
    elif request.get("method") == "GET":
        request.setdefault("query", {})
        request["query"][pagination["page"]] = page
        if "size" in pagination:
            request["query"][pagination["size"]] = size
    else:
        request.setdefault("body", {})
        set_path(request["body"], pagination["page"], page)
        if "size" in pagination:
            set_path(request["body"], pagination["size"], size)
    return request


def page_result(body: bytes, pagination: dict, page: int) -> tuple[int, bool]:
    """(records on this page, is this the last page)."""
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise PagingError("pagination_records_missing") from None
    records = get_path(value, pagination["records"])
    parent = pagination["records"].rpartition(".")[0]
    if records is None and ((parent and get_path(value, parent) in (None, [], {}))
                            or ("total" in pagination and get_path(value, pagination["total"]) == 0)):
        records = []  # PMOS answers an empty listing with "data": null / [] or "list": null, "total": 0
    if not isinstance(records, list):
        raise PagingError("pagination_records_missing")
    count = len(records)
    index = page - first_page(pagination) + 1
    total = get_path(value, pagination["total"]) if "total" in pagination else None
    size = pagination.get("page_size", 20)
    last = get_path(value, pagination["last"]) if "last" in pagination else None
    if isinstance(last, bool):
        done = count == 0 or last  # the page says so (e.g. the next button is disabled)
    else:
        done = count == 0 or count < size or (isinstance(total, int) and index * size >= total)
    if not done and index >= pagination.get("max_pages", 500):
        raise PagingError("pagination_page_limit")
    return count, done
