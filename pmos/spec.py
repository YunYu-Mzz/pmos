"""Declarative dataset specifications (one JSON file per dataset).

A province adapts a report by writing ``provinces/<id>/datasets/<dataset>.json``;
no Python is needed for the common cases. See docs/DATASET_SPEC.md.

Lifecycle (``status``)::

    candidate ──sample ok──▶ sampled ──second period ok──▶ verified ──enable──▶ enabled
        │                       │                                                 │
        └──── 40302/403 ──▶ denied        payload fails checks ──▶ quarantined ◀───┘
    retired: kept for history, never scheduled
"""
from __future__ import annotations

import hashlib
import json
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import periods
from .paging import set_path

STATUSES = ("candidate", "sampled", "verified", "enabled", "denied", "quarantined", "retired")
TRANSPORTS = ("fetch", "component", "native", "ui", "finereport")
RESPONSE_KINDS = ("json", "xlsx", "xls", "excel", "pdf", "binary", "document", "finereport_daily")
PLACEHOLDERS = set(periods.variables("day", "2026-01-01"))
# A documents download request is rendered per attachment, not per period.
DOWNLOAD_PLACEHOLDERS = {"record", "row", "attachment", "title", "record_id", "published", "period"}
RENDERED = ("body", "query", "set", "args", "fields", "match", "prepare", "record", "preflight", "template_body_set")
STEP_KEYS = {"unless", "component", "match", "set", "emit", "payload", "bus", "call", "args", "await"}
SCRIPT = re.compile(r"[a-z][a-z0-9_]{1,63}")
# Mixed case is allowed so migrated datasets keep their legacy ids (archives
# and receipts of the old collectors stay traceable).
ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,63}")


class SpecError(ValueError):
    pass


def _placeholders(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        # "{scope[断面名称]}" refers to the task's scope values.
        found |= {name.split("[", 1)[0] for _, name, _, _ in string.Formatter().parse(value) if name}
    elif isinstance(value, dict):
        for item in value.values():
            found |= _placeholders(item)
    elif isinstance(value, list):
        for item in value:
            found |= _placeholders(item)
    return found


WHOLE = re.compile(r"^\{([A-Za-z_]\w*)\}$")


def render(template: Any, variables: dict) -> Any:
    """Fill ``{date}``-style placeholders in strings nested in dicts/lists.

    A string that is exactly ``{name}`` naming an object (e.g. ``{attachment}``)
    becomes that object, so a component method can receive it as an argument.
    """
    if isinstance(template, str):
        whole = WHOLE.match(template)
        if whole and isinstance(variables.get(whole.group(1)), (dict, list)):
            return json.loads(json.dumps(variables[whole.group(1)]))
        return template.format_map(variables) if "{" in template else template
    if isinstance(template, dict):
        return {key: render(value, variables) for key, value in template.items()}
    if isinstance(template, list):
        return [render(value, variables) for value in template]
    return template


@dataclass
class DatasetSpec:
    id: str
    name: str
    status: str
    period: dict
    request: dict
    response: dict
    keepalive: bool = False
    scopes: list = field(default_factory=list)
    documents: dict | None = None
    source: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "DatasetSpec":
        value = json.loads(path.read_text(encoding="utf-8"))
        known = {"id", "name", "status", "period", "request", "response", "keepalive", "scopes", "documents",
                 "source", "history"}
        unknown = set(value) - known
        if unknown:
            raise SpecError(f"{path.name}: unknown fields {sorted(unknown)}")
        spec = cls(**value, path=path)
        spec.check()
        if path.stem != spec.id:
            raise SpecError(f"{path.name}: file name must equal id")
        return spec

    def to_json(self) -> dict:
        value = {"id": self.id, "name": self.name, "status": self.status, "period": self.period,
                 "request": self.request, "response": self.response}
        if self.keepalive:
            value["keepalive"] = True
        if self.scopes:
            value["scopes"] = self.scopes
        if self.documents:
            value["documents"] = self.documents
        if self.source:
            value["source"] = self.source
        if self.history:
            value["history"] = self.history
        return value

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise SpecError("spec_path_missing")
        self.check()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
        self.path = target

    def check(self) -> None:
        where = self.id if isinstance(self.id, str) else "?"
        if not isinstance(self.id, str) or not ID.fullmatch(self.id):
            raise SpecError(f"{where}: id must match {ID.pattern}")
        if not isinstance(self.name, str) or not self.name.strip():
            raise SpecError(f"{where}: name required")
        if self.status not in STATUSES:
            raise SpecError(f"{where}: status must be one of {STATUSES}")
        kind = self.period.get("kind")
        if kind not in periods.KINDS:
            raise SpecError(f"{where}: period.kind must be one of {periods.KINDS}")
        if kind != "snapshot":
            try:
                periods.parse(kind, self.period["start"])
                end = self.period.get("end", "today")
                if end != "today":
                    periods.parse(kind, end)
            except (KeyError, periods.PeriodError):
                raise SpecError(f"{where}: period.start/end must be {kind} values (end may be 'today')") from None
        check_request(where, self.request)
        if "bootstrap" in self.request:
            bootstrap = self.request["bootstrap"]
            check_request(where + ".bootstrap", bootstrap)
            if (self.request["transport"] != "fetch" or bootstrap.get("transport") == "fetch" or "bootstrap" in bootstrap
                    or "pagination" in self.request):
                raise SpecError(f"{where}: request.bootstrap is a component/native/ui request for an unpaged fetch spec")
        if (not isinstance(self.scopes, list)
                or any(not isinstance(item, dict) or not item
                       or any(not isinstance(k, str) or not isinstance(v, str) for k, v in item.items())
                       for item in self.scopes)
                or len({json.dumps(item, sort_keys=True, ensure_ascii=False) for item in self.scopes}) != len(self.scopes)):
            raise SpecError(f"{where}: scopes must be a list of distinct non-empty {{label: value}} objects")
        unknown = _placeholders(self.request) - PLACEHOLDERS - ({"scope"} if self.scopes else set())
        if unknown:
            raise SpecError(f"{where}: unknown placeholders {sorted(unknown)}")
        if self.response.get("kind") not in RESPONSE_KINDS:
            raise SpecError(f"{where}: response.kind must be one of {RESPONSE_KINDS}")
        echo = self.response.get("echo")
        if echo is not None and (self.response["kind"] != "json" or not isinstance(echo, dict) or not echo
                                 or not all(isinstance(k, str) and isinstance(v, str) for k, v in echo.items())):
            raise SpecError(f"{where}: response.echo maps a JSON record field to a scope label")
        transport = self.request["transport"]
        if "pagination" in self.request:
            from .paging import PagingError, check as check_paging
            try:
                check_paging(self.request["pagination"])
            except PagingError as exc:
                raise SpecError(f"{where}: {exc}") from None
            if self.response["kind"] != "json" or (transport == "ui" and self.request.get("action") != "list"):
                raise SpecError(f"{where}: pagination needs a json response (fetch, component, native or ui list)")
        if self.documents is not None:
            from .documents import DocumentError, check as check_documents
            try:
                check_documents(self.documents)
            except DocumentError as exc:
                raise SpecError(f"{where}: {exc}") from None
            if "pagination" not in self.request or kind != "snapshot":
                raise SpecError(f"{where}: a documents listing must be paginated and use a snapshot period")
            if self.keepalive or self.scopes:
                raise SpecError(f"{where}: documents specs cannot be keepalive or scoped")
            download = self.documents["download"]
            check_request(where + ".download", download)
            if download["transport"] == "ui" and (download.get("action") != "record" or not self.documents.get("record_only")):
                raise SpecError(f"{where}: a ui documents.download is action record with record_only listings")
            unknown = _placeholders(download) - DOWNLOAD_PLACEHOLDERS
            if unknown:
                raise SpecError(f"{where}: unknown download placeholders {sorted(unknown)}")
            if self.documents.get("download_response", {"kind": "document"}).get("kind") not in RESPONSE_KINDS:
                raise SpecError(f"{where}: documents.download_response.kind must be one of {RESPONSE_KINDS}")
        if self.keepalive and (self.status != "enabled" or kind == "snapshot" or self.scopes):
            raise SpecError(f"{where}: only an enabled, dated, unscoped dataset may serve as keepalive")

    # -- requests ---------------------------------------------------------
    def periods_to_collect(self, *, on=None) -> list[str]:
        kind = self.period["kind"]
        if kind == "snapshot":
            return [periods.value_of("snapshot", on or periods.today())]
        return periods.expand(kind, self.period["start"], self.period.get("end", "today"), on=on,
                              lag_days=self.period.get("lag_days", 0),
                              complete_only=self.period.get("complete_only", False))

    def newest_due(self, *, on=None) -> str | None:
        """The newest day that may be asked for (``lag_days`` back), or None when nothing holds it back.

        A task seeded before (a legacy owner's open candidate of the running
        day) waits for it too: an empty or partial answer would be saved for good.
        """
        kind = self.period["kind"]
        if kind not in ("day", "week_start_monday") or not self.period.get("lag_days"):
            return None
        from datetime import timedelta
        return periods.value_of(kind, (on or periods.today()) - timedelta(days=self.period["lag_days"]))

    # -- scopes: extra dimensions of one period (unit, node, section ...) ----
    def scope_items(self) -> list[tuple[str, dict]]:
        """(key, values) per task dimension; an unscoped dataset has one ("", {})."""
        return [(scope_key(item), item) for item in self.scopes] or [("", {})]

    def scope_index(self) -> dict[str, dict]:
        """key -> values, rebuilt when the scope list is replaced or changes length (thousands of nodes)."""
        marker = (id(self.scopes), len(self.scopes or []))
        cached = self.__dict__.get("_scope_index")
        if cached is None or cached[0] != marker:
            cached = (marker, dict(self.scope_items()))
            self.__dict__["_scope_index"] = cached
        return cached[1]

    def scope_values(self, key: str) -> dict:
        found = self.scope_index().get(key)
        if found is None:
            raise SpecError(f"{self.id}: unknown scope {key}")
        return found

    def build(self, period: str, scope: dict | None = None) -> dict:
        """The concrete request for one period and scope (still without auth headers).

        For the ``ui`` transport each scope value becomes a select field of the
        same label, unless the spec already lists a field with that label.
        """
        kind = self.period["kind"]
        values = {**periods.variables("day" if kind == "snapshot" else kind, period), "scope": scope or {}}
        request = {key: value for key, value in self.request.items()
                   if key not in RENDERED + ("pagination", "bootstrap")}
        for key in RENDERED:
            if key in self.request:
                request[key] = render(self.request[key], values)
        if "bootstrap" in self.request:
            # Used while the fetch template is unobserved: the page makes the
            # same request itself, which also provides the template.
            bootstrap = self.request["bootstrap"]
            request["bootstrap"] = {**{k: v for k, v in bootstrap.items() if k not in RENDERED},
                                    **{k: render(bootstrap[k], values) for k in RENDERED if k in bootstrap}}
            request["bootstrap"].setdefault("method", request.get("method", "POST"))
        if request["transport"] == "ui" and scope:
            labels = {f.get("label") for f in request.get("fields", [])}
            request["fields"] = request.get("fields", []) + [
                {"kind": "select", "label": label, "value": value} for label, value in scope.items() if label not in labels]
        request.setdefault("method", "POST")
        if request["transport"] == "fetch":
            request.setdefault("template", self.request["path"])
        return request

    def build_download(self, item: dict) -> dict:
        """The download request of one documents item (see documents.extract)."""
        block = self.documents["download"]
        # ``row``: the record as the page shows it, narrowed to this one file.
        row = json.loads(json.dumps(item["record"]))
        set_path(row, self.documents.get("attachments", {}).get("path", "attachment"), [item["attachment"]])
        values = {"record": item["record"], "row": row, "attachment": item["attachment"], "title": item["title"],
                  "record_id": item["record_id"], "published": item.get("published") or "", "period": item["period"]}
        request = {key: value for key, value in block.items() if key not in RENDERED}
        for key in RENDERED:
            if key in block:
                request[key] = render(block[key], values)
        request.setdefault("method", "GET")
        if request["transport"] == "fetch":
            request.setdefault("template", block["path"])
        return request

    def with_page_size(self, size: int) -> "DatasetSpec":
        """This spec asking ``size`` records per page, identity included (a period begun at that size)."""
        import copy
        value = copy.deepcopy(self)
        value.request["pagination"]["page_size"] = size
        contract = value.source.get("identity_contract")
        if contract and "page_size" in contract:
            contract["page_size"] = size
        return value

    def scripts(self) -> set[str]:
        """Page scripts (provinces/<id>/scripts/<name>.js) this spec relies on."""
        requests = [self.request] + ([self.documents["download"]] if self.documents else [])
        return {step["script"] for request in requests for step in request.get("prepare", []) if "script" in step}

    def note(self, event: str, **fields: Any) -> None:
        from .state import now_iso
        self.history.append({"at": now_iso(), "event": event, **fields})


def check_request(where: str, request: dict) -> None:
    """Shape of one request block (a dataset request or a documents download)."""
    if not isinstance(request, dict):
        raise SpecError(f"{where}: request must be an object")
    transport = request.get("transport")
    if transport not in TRANSPORTS:
        raise SpecError(f"{where}: request.transport must be one of {TRANSPORTS}")
    path = request.get("path")
    if path is None and transport == "ui":
        pass  # a UI export is identified by its form, not an endpoint
    elif not isinstance(path, str) or not path.startswith("/") or "?" in path or "//" in path:
        raise SpecError(f"{where}: request.path must be an absolute site path without query")
    if request.get("method", "POST") not in ("GET", "POST"):
        raise SpecError(f"{where}: request.method must be GET or POST")
    if transport == "component":
        for key in ("frame", "component", "call"):
            if not isinstance(request.get(key), str):
                raise SpecError(f"{where}: component transport needs request.{key}")
    if transport == "finereport":
        date_field = request.get("date_field")
        if (not isinstance(request.get("frame"), str) or not isinstance(request.get("site_menu"), dict)
                or not isinstance(request.get("set"), dict) or date_field not in request["set"]):
            raise SpecError(f"{where}: finereport transport needs request.frame, site_menu, set and its date_field")
    if transport == "ui":
        if not isinstance(request.get("frame"), str) or request.get("action") not in ("query", "export", "list", "record",
                                                                                      "options"):
            raise SpecError(f"{where}: ui transport needs request.frame and action query|export|list|record")
        if request["action"] == "record" and request.get("record") != "{record}":
            raise SpecError(f"{where}: a ui record action takes \"record\": \"{{record}}\" (a documents download)")
        for item in request.get("fields", []):
            placeholder = item.get("placeholder") if isinstance(item, dict) else None
            if (not isinstance(item, dict) or item.get("kind", "date") not in ("date", "select")
                    or not (item.get("label") or placeholder) or not isinstance(item.get("value"), str)
                    or (placeholder is not None and not isinstance(placeholder, (str, list)))):
                raise SpecError(f"{where}: ui fields need kind date|select, label or placeholder, value")
    open_url = request.get("open_url")
    if open_url is not None and (not isinstance(open_url, str) or not open_url.startswith("/")):
        raise SpecError(f"{where}: request.open_url must be a same-site path (with #route)")
    template_set = request.get("template_body_set")
    if template_set is not None and (transport != "fetch" or "body" in request or not isinstance(template_set, dict)
                                     or not template_set or not all(isinstance(k, str) and k for k in template_set)):
        raise SpecError(f"{where}: request.template_body_set is a fetch's field -> value map, without its own body")
    menu_entry = request.get("site_menu")
    if menu_entry is not None and not (isinstance(menu_entry, dict)
                                       and (str(menu_entry.get("route", "")).startswith("/") or menu_entry.get("route") == "spotgoodsSum")
                                       and (menu_entry.get("source") is None
                                            or (isinstance(menu_entry["source"], str) and menu_entry["source"]))):
        raise SpecError(f"{where}: request.site_menu needs the menu entry's route (and optionally its infoCode source)")
    menu = request.get("menu")
    if menu is not None and (not isinstance(menu, list) or not all(isinstance(m, str) and m for m in menu)):
        raise SpecError(f"{where}: request.menu must be a list of menu labels")
    if transport == "native":
        for key in ("frame", "module"):
            if not isinstance(request.get(key), str) or not request[key]:
                raise SpecError(f"{where}: native transport needs request.{key}")
    if "args" in request and (transport not in ("component", "native") or not isinstance(request["args"], list)):
        raise SpecError(f"{where}: request.args is a list for component or native calls")
    if "match" in request and (transport != "component" or not isinstance(request["match"], dict)):
        raise SpecError(f"{where}: request.match is an object of component field values")
    if transport == "component" and request.get("component") == "*" and not request.get("match"):
        raise SpecError(f"{where}: component \"*\" (any name) needs request.match to pick one instance")
    if "capture" in request and (transport not in ("component", "native") or request["capture"] not in ("response", "blob")):
        raise SpecError(f"{where}: request.capture is response|blob for component or native calls")
    if "preflight" in request:
        if transport not in ("component", "native") or not isinstance(request["preflight"], list):
            raise SpecError(f"{where}: request.preflight is a list of conditions for component or native calls")
        for condition in request["preflight"]:
            _check_condition(where, condition)
    if "prepare" in request:
        if transport not in ("component", "native") or not isinstance(request["prepare"], list):
            raise SpecError(f"{where}: request.prepare is a list of steps for component or native calls")
        for step in request["prepare"]:
            _check_step(where, step)


def _check_condition(where: str, condition) -> None:
    if (not isinstance(condition, dict) or set(condition) - {"component", "match", "equals"}
            or not isinstance(condition.get("component"), str)
            or not all(isinstance(condition.get(k, {}), dict) for k in ("match", "equals"))):
        raise SpecError(f"{where}: a condition is {{component, match?, equals?}}")


def _check_step(where: str, step) -> None:
    if not isinstance(step, dict):
        raise SpecError(f"{where}: prepare steps are objects")
    if "wait" in step or "sleep" in step:
        if len(step) != 1:
            raise SpecError(f"{where}: wait and sleep steps stand alone")
        if "wait" in step:
            _check_condition(where, step["wait"])
        elif type(step["sleep"]) not in (int, float) or not 0 < step["sleep"] <= 5:
            raise SpecError(f"{where}: sleep is 0..5 seconds")
        return
    if "script" in step:
        if set(step) - {"script", "args", "unless"} or not isinstance(step["script"], str) \
                or not SCRIPT.fullmatch(step["script"]) or not isinstance(step.get("args", {}), dict):
            raise SpecError(f"{where}: a script step is {{script: <provinces/<id>/scripts/name>, args?, unless?}}")
        if "unless" in step:
            _check_condition(where, step["unless"])
        return
    if (set(step) - STEP_KEYS or not isinstance(step.get("component"), str)
            or not ({"set", "emit", "call"} & set(step))):
        raise SpecError(f"{where}: a prepare step is {{component, set|emit|call, ...}}, {{script}}, {{wait}} or {{sleep}}")
    if "unless" in step:
        _check_condition(where, step["unless"])
    if "args" in step and not isinstance(step["args"], list):
        raise SpecError(f"{where}: step args is a list")


def scope_key(values: dict) -> str:
    """Stable, filesystem-safe key of one scope ({} -> "")."""
    if not values:
        return ""
    canonical = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "s" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:15]


def load_all(directory: Path) -> dict[str, DatasetSpec]:
    specs: dict[str, DatasetSpec] = {}
    for path in sorted(directory.glob("*.json")):
        spec = DatasetSpec.load(path)
        specs[spec.id] = spec
    return specs
