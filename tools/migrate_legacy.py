"""Generate dataset specs from the three legacy collectors (one-time migration).

Run with the legacy project's own interpreter so its declaration modules
import, e.g.::

    ../shanxi/.venv/bin/python tools/migrate_legacy.py shanxi ../shanxi
    ../liaoning/.venv/bin/python tools/migrate_legacy.py liaoning ../liaoning
    ../anhui/.venv/bin/python tools/migrate_legacy.py anhui ../anhui

Existing spec files are never overwritten (pass --replace to regenerate).
Legacy ids are kept so the old archives and receipts stay traceable. The
status mapping is conservative: only what the legacy collector proved with a
real sample becomes ``verified``; one dataset per province is ``enabled`` as
the session keepalive. Everything else is enabled by a human (``pmos <id>
enable``) after the new runtime has been validated on that site.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from pmos.spec import DatasetSpec  # noqa: E402


def spec(**fields) -> DatasetSpec:
    value = DatasetSpec(**fields)
    value.check()
    return value


# -- Shanxi: dataset_catalog.json export bindings (fetch transport) ---------
def _shanxi_export_pages(legacy: Path) -> dict[str, str]:
    """route -> export path of report pages whose cached front-end source shows
    the shared page template: ``exportData()`` posts ``queryConfig.data`` to
    ``exportUrl`` with the page's own auth headers (what the 导出 button runs)."""
    import re
    pages = {}
    for path in list(legacy.glob("verification/**/*.js")) + list(legacy.glob("state/**/*.js")):
        if not path.is_file() or "node_modules" in path.parts:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r'name:"(/pxf-phbsx-infomation/[^"]+)"', source):
            chunk = source[match.start(): match.start() + 20000]
            export = re.search(r'exportData:function\(\)\{var e=this;this\.exportLoading=!0,\w\.a\.post\(""\+""'
                               r'\.concat\(this\.exportUrl\),this\.queryConfig\.data,', chunk)
            url = re.search(r'type:"export",url:"([^"]+)"', chunk)
            if export and url:
                pages["/pxf-phbsx-infomation/#" + match.group(1)] = url.group(1)
    return pages


def shanxi(legacy: Path) -> list[DatasetSpec]:
    catalog = json.loads((legacy / "dataset_catalog.json").read_text(encoding="utf-8"))
    flags = ("query_verified", "export_verified", "browser_transport_verified", "programmatic_export_verified")
    pages = _shanxi_export_pages(legacy)
    out = []
    for target in catalog["targets"]:
        export = target.get("export") or {}
        if not export.get("endpoint") or not export.get("body_date_field"):
            continue
        kind = target.get("actual_granularity") or "day"
        if kind not in ("day", "month"):
            continue
        token = "{date}" if kind == "day" else "{month}"
        body = dict(export.get("fixed_fields") or {})
        body[export["body_date_field"]] = token
        if export.get("body_end_date_field"):
            body[export["body_end_date_field"]] = token
        base = target["id"] == "queryRqxtfhyc"
        qualified = base or all(target.get(k) is True for k in flags)
        route = (target.get("observed_page_url") or "").replace("https://pmos.sx.sgcc.com.cn", "")
        bootstrap = {"bootstrap": {"transport": "component", "frame": route.split("#", 1)[1], "open_url": route,
                                   "component": "*", "match": {"exportUrl": export["endpoint"]},
                                   "set": {"date": token, "queryConfig.data": body}, "call": "exportData",
                                   "path": export["endpoint"], "method": export.get("method", "POST")}} \
            if pages.get(route) == export["endpoint"] or (base and route.startswith("/pxf-phbsx-infomation/#")) else {}
        status = "enabled" if base else "verified" if qualified else "candidate"
        out.append(spec(
            id=target["id"], name=target.get("title") or target.get("name") or target["id"], status=status,
            period={"kind": kind, "start": "2026-01-01" if kind == "day" else "2026-01", "end": "today",
                    **({"complete_only": True} if kind == "month" else {})},
            request={"transport": "fetch", "method": export.get("method", "POST"), "path": export["endpoint"],
                     "body": body, **bootstrap},
            response={"kind": "xls" if export.get("original_extension") == ".xls" else "xlsx"},
            keepalive=base,
            source={"legacy": "shanxi/dataset_catalog.json", "legacy_status": target.get("status"),
                    "query_endpoint": (target.get("query") or {}).get("endpoint"),
                    "page_route": target.get("page_route"), "evidence_sha256": export.get("evidence_sha256"),
                    "note": "fetch replays the page's own export template; until the page has shown it, "
                            + ("the bootstrap runs the page's own exportData once" if bootstrap
                               else "this report waits (one 导出 click in the page provides it)")
                            + ("; bootstrap inferred: this page's source is not cached, the same app's "
                               "report pages share exportData" if bootstrap and pages.get(route) != export["endpoint"]
                               else "")}))
    return out


# -- Disclosure documents shared by every PMOS province ---------------------
DISCLOSURE_LIST = "/px-settlement-infpubquery/ipDisclosureApply/getData"
DISCLOSURE_FILE = "/px-settlement-infpubmeex/fileService/download"
# Business period from a disclosure title, most specific first (shared by all provinces).
TITLE_PERIODS = [
    {"title_regex": r"(20\d{2})年\s*(1[0-2]|0?[1-9])月\s*(3[01]|[12]\d|0?[1-9])日\s*[-—至~～]\s*"
                    r"(?:(?:1[0-2]|0?[1-9])月\s*)?(?:3[01]|[12]\d|0?[1-9])日[^，,]*周", "kind": "week_of_day"},
    {"title_regex": r"(20\d{2})\s*年?\s*第\s*(\d{1,2})\s*周", "kind": "week"},
    {"title_regex": r"(20\d{2})年\s*第?([一二三四1-4])季度", "kind": "quarter"},
    # "2026年8月1日-31日…": a whole month, not its first day
    {"title_regex": r"(20\d{2})年\s*(1[0-2]|0?[1-9])月\s*1日\s*[-—至~～]\s*(?:2[89]|3[01])日", "kind": "month"},
    {"title_regex": r"(20\d{2})年\s*(1[0-2]|0?[1-9])月\s*(3[01]|[12]\d|0?[1-9])日", "kind": "day"},
    {"title_regex": r"(20\d{2})[.\-/](1[0-2]|0?[1-9])[.\-/](3[01]|[12]\d|0?[1-9])", "kind": "day"},
    {"title_regex": r"(20\d{2})年\s*(1[0-2]|0?[1-9])月", "kind": "month"},
    {"title_regex": r"(20\d{2})年度?", "kind": "year"},
]
DOCUMENT_PERIODS = TITLE_PERIODS[:-1] + [{"fields": {"year": "fyear", "month": "fmonth", "day": "fday"}},
                                         TITLE_PERIODS[-1]]


def _documents(market: str, download: dict) -> dict:
    return {"fields": {"id": "guid", "title": "title", "published": "operateDate"},
            "published_format": "epoch_ms", "expect": {"marketId": market},
            "attachments": {"path": "attachment", "json_string": True, "id": "id", "name": "name"},
            "period": DOCUMENT_PERIODS, "stop_at_known": True, "download": download,
            "download_response": {"kind": "document"}}


def _document_id(code: str, guid: str, duplicate: bool) -> str:
    import re
    base = "pub_" + re.sub(r"[^A-Za-z0-9_]", "_", code)[:48]
    return f"{base}_{guid.replace('-', '')[:8]}" if duplicate else base


# -- Shanxi: publication menus (operationOrganizationQuery, one per menu) ---
SX_APP = "/pxf-settlement-outnetpub/#/pxf-settlement-outnetpub/columnHomeLeftMenuNew"
SX_HEADER_READY = {"component": "chHeader", "equals": {"marketHeader": "PHBSX"}}


def shanxi_publications(legacy: Path) -> list[DatasetSpec]:
    """77 disclosure menus. Legacy proved each list query (41 with rows); no
    attachment download was proven, so a menu with listed files is ``sampled``
    and becomes ``verified`` after ``sample --scope file``."""
    catalog = json.loads((legacy / "publication_adapter_catalog.json").read_text(encoding="utf-8"))
    codes = [row["menu"]["infoCode"] for row in catalog["records"]]
    download = {"transport": "native", "frame": "/pxf-settlement-outnetpub/", "open_url": SX_APP,
                "module": "63a1", "export": "a", "args": ["{attachment[id]}", None],
                "path": DISCLOSURE_FILE, "method": "GET"}
    out = []
    for row in catalog["records"]:
        menu, command = row["menu"], row["native_command"]
        guid = menu["guid"]
        query = json.loads(json.dumps(command["queryParam"], ensure_ascii=False))
        start = query["data"]["startDate"]
        query["data"].update(endDate="{date}", operateTimeArr=[start, "{date}"])
        mounted = {"component": "operationOrganizationQuery", "match": {"viewId": guid}}
        payload = {"viewName": "operationOrganizationQuery", "viewId": guid, "viewCode": menu["infoCode"],
                   "marketMain": "PHBSX", "openWay": "1", "pidTem": "1",
                   "uploadDepartment": menu.get("uploadDepartment"), "uploadDepartmentId": menu.get("uploadDepartmentId")}
        listed = row["query"].get("outcome") == "publication_rows" and bool(row.get("attachments"))
        out.append(spec(
            id=_document_id(menu["infoCode"], guid, codes.count(menu["infoCode"]) > 1),
            name="／".join(menu.get("chain") or [menu["label"]]), status="sampled" if listed else "candidate",
            period={"kind": "snapshot"},
            request={"transport": "component", "frame": "/pxf-settlement-outnetpub/", "open_url": SX_APP,
                     "path": DISCLOSURE_LIST, "method": "POST",
                     "prepare": [
                         {"component": "chHeader", "call": "setConfig", "await": True, "unless": SX_HEADER_READY},
                         {"component": "chHeader", "call": "getValues", "await": True, "unless": SX_HEADER_READY},
                         {"wait": SX_HEADER_READY},
                         {"component": "chHeader", "emit": "sendDataToMain", "payload": payload, "unless": mounted},
                         {"wait": mounted},
                         {"component": "chHeader", "emit": "sendShowTmToMain", "payload": True},
                         {"sleep": 0.5}],
                     **mounted, "call": "queryData",
                     "set": {"queryParam": query, "tabFlag": False, "resultValue": [], "total": 0},
                     "pagination": {"page": "queryParam.pageInfo.pageNum", "size": "queryParam.pageInfo.pageSize",
                                    "page_size": query["pageInfo"]["pageSize"], "first_page": 1,
                                    "records": "data.list", "total": "data.total", "max_pages": 200}},
            response={"kind": "json"},
            documents=_documents("PHBSX", download),
            source={"legacy": "shanxi/publication_adapter_catalog.json", "legacy_status": row["adapter_status"],
                    "menu_guid": guid, "info_code": menu["infoCode"], "listed_rows": row["query"].get("rows"),
                    "listed_files": len(row.get("attachments") or []),
                    "component_source_sha256": command.get("source_sha256"),
                    "note": "date filter is the publication date (from legacy startDate to today); "
                            "file download calls the page's own helper (webpack module 63a1.a)"}))
    return out


# -- Anhui: publication menus read through the page (table + article dialog)
AH_FRAME = "/pxf-settlement-outnetpub/"
AH_HOVER_MENUS = (["中长期市场", "市场结算信息", "需求响应执行情况"],)  # a hover menu covers the tree


def anhui_publications(legacy: Path) -> list[DatasetSpec]:
    """72 publication queues. The page offers no record ids or JSON: the listing
    is read from the table, each article is opened in its dialog and every
    attachment button (预览 excluded) is downloaded. Queues whose legacy run
    saved an original are ``sampled``."""
    state = _anhui_state(legacy)
    out = []
    for path in sorted(state.glob("server_publication_queue_*.json")):
        queue = json.loads(path.read_text(encoding="utf-8"))
        menu = queue["menu_path"]
        statuses = [job.get("status") for job in queue.get("jobs", {}).values()]
        proven = sum(s in ("saved", "captured") for s in statuses)
        hover = {"menu_hover_exit": True} if menu in AH_HOVER_MENUS else {}
        common = {"transport": "ui", "frame": AH_FRAME, "menu": menu, **hover}
        out.append(spec(
            id=queue["dataset"], name=" → ".join(menu), status="sampled" if proven else "candidate",
            period={"kind": "snapshot"},
            request={**common, "action": "list",
                     "pagination": {"page": "page", "records": "data.list", "total": "data.total",
                                    "last": "data.last", "page_size": 10, "max_pages": 100}},
            response={"kind": "json"},
            documents={"fields": {"id": "id", "title": "title", "published": "published"},
                       "published_format": "date", "record_only": True, "stop_at_known": True,
                       "period": TITLE_PERIODS,
                       "download": {**common, "action": "record", "record": "{record}", "skip_buttons": ["预览"]},
                       "download_response": {"kind": "document"}},
            source={"legacy": f"anhui/state/{path.name}", "legacy_status": "publication_queue",
                    "legacy_jobs": len(statuses), "legacy_saved_or_captured": proven,
                    "legacy_reported_none": statuses.count("reported_none")}))
    return out


# -- Shanxi: native JSON queries (the page's own components) ----------------
SX_PAGE = {"page": "args.0", "records": "data.list", "total": "data.total", "last": "data.isLastPage",
           "page_size": 20, "max_pages": 200}
SX_PENDING = {"scoped": "options come from a per-day selector request (option-driven scopes pending)",
              "rolling": "trade sequences differ per day (option-driven scopes pending)"}


def _dated(body: dict, fields: list[str]) -> dict:
    body = json.loads(json.dumps(body, ensure_ascii=False))
    for name in fields:
        target = body.get("data") if isinstance(body.get("data"), dict) and name in body["data"] else body
        if name in target:
            target[name] = "{date}"
    return body


def shanxi_native(legacy: Path) -> list[DatasetSpec]:
    """The legacy native JSON lane (24 validated profiles). Each strategy is the
    same page call the legacy dispatcher made; ``scoped`` and ``rolling`` wait
    for option-driven scopes."""
    import tempfile
    sys.path.insert(0, str(legacy))
    from adapters.native_dispatch import profiles  # legacy module, run with its venv
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as placeholder:  # profiles() only checks it exists
        rows = profiles(legacy, queue_path=placeholder.name)
    contracts = json.loads((legacy / "native_menu_query_contracts.json").read_text(encoding="utf-8"))["records"]
    out = []
    for key, row in rows.items():
        kind = row["strategy"]
        if kind in SX_PENDING:
            continue
        contract = row.get("spec") or contracts.get(key) or {}
        observed = contract.get("observed_request") or {}
        path = contract.get("source_query_path") or observed.get("path")
        fields = contract.get("date_fields") or []
        body = _dated(observed.get("body") or {}, fields)
        page_info = (observed.get("body") or {}).get("pageInfo")
        route = row["route"]
        frame = "/pxf-settlement-outnetpub/" if route == SX_APP else route
        request = {"transport": "component", "frame": frame, "open_url": route, "path": path, "method": "POST"}
        by_url = {"component": "*", "match": {"queryUrl": path}}
        response = {"kind": "json"}
        if kind in ("plain", "realname"):
            request.update(by_url, set={"queryConfig.pageInfo": page_info, **({"date": "{date}"} if fields else {})},
                           call="handleCurrentChange", args=[1], pagination=SX_PAGE)
        elif kind == "node":
            request.update(component=row.get("component") or "/pxf-phbsx-other-outer/dayTime",
                           set={"queryParam": body}, call="handleCurrentChange", args=[1], pagination=SX_PAGE)
        elif kind == "market":
            request.update(by_url, set={"date": "{date}"}, call="queryPage")
        elif kind == "public":
            request.update(component=row["component"], set={"queryParam": body}, call="queryInit")
        elif kind == "section":
            options = [{"strName": o["strName"]} for o in row["options"]]
            body["data"]["strName"] = "{scope[strName]}"
            request.update(by_url, set={"date": "{date}", "selectOption": row["options"],
                                        "selectValue": "{scope[strName]}", "queryConfig": body},
                           call="queryPage", pagination={**SX_PAGE, "page": "queryConfig.pageInfo.pageNum"})
        elif kind in ("dynamic", "member", "curve"):
            menu = row["menu"]
            view = {"dynamic": "table2", "member": "marketShowQueryZHC", "curve": "nintySixPoints"}[kind]
            identity = {"infoCode": menu["infoCode"]} if kind == "member" else {"viewId": menu["guid"]}
            mounted = {"component": view, "match": identity}
            payload = {"viewName": view, "viewId": menu["guid"], "viewCode": menu["infoCode"], "marketMain": "PHBSX",
                       "openWay": "1", "pidTem": "1", "uploadDepartment": menu.get("uploadDepartment"),
                       "uploadDepartmentId": menu.get("uploadDepartmentId")}
            request["prepare"] = [
                {"component": "chHeader", "call": "setConfig", "await": True, "unless": SX_HEADER_READY},
                {"component": "chHeader", "call": "getValues", "await": True, "unless": SX_HEADER_READY},
                {"wait": SX_HEADER_READY},
                {"component": "chHeader", "emit": "sendDataToMain", "payload": payload, "unless": mounted},
                {"wait": mounted},
                {"component": "chHeader", "emit": "sendShowTmToMain", "payload": True},
                {"sleep": 0.5}]
            request.update(mounted)
            if kind == "curve":
                request.update(set={"date": "{date}", "queryParam.dateTime": "{date}"}, call="queryData")
            elif kind == "dynamic":
                request.update(set={"queryParam": body}, call="queryPage",
                               pagination={**SX_PAGE, "page": "queryParam.pageInfo.pageNum",
                                           "records": "data.objectList.list", "total": "data.objectList.total",
                                           "last": "data.objectList.isLastPage"})
                response = {"kind": "json", "data_field": "data.objectList"}
            else:
                request.update(set={"queryParam": body, "pageObj.pageSize": 20}, call="handleCurrent", args=[1],
                               pagination={**SX_PAGE, "max_pages": 1000})
        else:
            raise SystemExit(f"unmapped Shanxi native strategy {kind}")
        title = contract.get("title") or row.get("title") or (row.get("menu") or {}).get("label") or key
        dated = bool(fields)
        out.append(spec(
            id=key, name=title, status="candidate" if kind == "public" else "sampled",
            period={"kind": "day", "start": "2026-01-01", "end": "today"} if dated else {"kind": "snapshot"},
            request=request, response=response,
            scopes=[{"strName": o["strName"]} for o in row["options"]] if kind == "section" else [],
            source={"legacy": "shanxi/adapters/native_dispatch.py", "legacy_status": "native_json_queue_validated",
                    "strategy": kind, "component_source_sha256": row.get("source_sha256"),
                    **({"note": "single page of the public notice cards (pageSize 6); paging not observed"}
                       if kind == "public" else {}),
                    **({"note": "full member list: hundreds of pages per snapshot at the response interval"}
                       if kind == "member" else {})}))
    return out


# -- Liaoning: disclosure menus of the column page --------------------------
LN_APP = "/pxf-settlement-outnetpub/#/pxf-settlement-outnetpub/columnHomeLeftMenuNew"
LN_DOCUMENT_VIEWS = ("attchViewQueryJY", "operationOrganizationQuery", "operationOrganizationQueryDwqy")


def liaoning_publications(legacy: Path) -> list[DatasetSpec]:
    """Menus whose legacy adapter proved the first list page. Opening a menu
    needs the page's encrypted menu tree, so it runs the province page script
    ``open_column_menu``. No attachment download was proven yet."""
    import ast
    def literal(value):
        if isinstance(value, str):
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return {}
        return value or {}
    adapters = [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((legacy / "state/local_adaptation_20261020/adapters").glob("*.json"))]
    adapters = [a for a in adapters if a.get("family") == "documents" and a.get("route") in LN_DOCUMENT_VIEWS]
    codes = [a["source"] for a in adapters]
    out = []
    for adapter in adapters:
        view, code, guid = adapter["route"], adapter["source"], adapter["guid"]
        mounted = {"component": view, "match": {"infoCode": code, "viewId": guid}}
        open_menu = {"script": "open_column_menu", "args": {"component": view, "source": code, "guid": guid},
                     "unless": {"component": view, "match": {"infoCode": code, "viewId": guid, "__pmosReady": code}}}
        sample = literal(adapter.get("sample"))
        files = 0
        response = legacy / sample["response_file"] if sample.get("response_file") else None
        if response is not None and response.exists():
            data = json.loads(response.read_text(encoding="utf-8")).get("data")
            rows = data.get("list") if isinstance(data, dict) else data
            for row in rows or []:
                attached = row.get("attachment") if isinstance(row, dict) else None
                files += len(json.loads(attached)) if isinstance(attached, str) and attached.strip() else 0
        mapping = literal(adapter.get("mapping"))
        needs_mapping = bool(mapping) and not mapping.get("not_required")
        download = {"transport": "component", "frame": "/pxf-settlement-outnetpub/", "open_url": LN_APP,
                    "prepare": [open_menu], **mounted, "call": "fileDownload", "args": ["{row}"],
                    "path": DISCLOSURE_FILE, "method": "GET"}
        size = adapter.get("native_page_size") or 20
        out.append(spec(
            id=_document_id(code, guid, codes.count(code) > 1), name=adapter.get("title") or code,
            status="sampled" if files and not needs_mapping else "candidate",
            period={"kind": "snapshot"},
            request={"transport": "component", "frame": "/pxf-settlement-outnetpub/", "open_url": LN_APP,
                     "path": DISCLOSURE_LIST, "method": "POST", "prepare": [open_menu],
                     **mounted, "call": "queryData",
                     "set": {"queryParam.data.menuId": guid, "tabFlag": False, "resultValue": [], "total": 0},
                     "pagination": {"page": "queryParam.pageInfo.pageNum", "size": "queryParam.pageInfo.pageSize",
                                    "page_size": size, "first_page": 1, "records": "data.list",
                                    "total": "data.total", "max_pages": 200}},
            response={"kind": "json"},
            documents=_documents("PDBLN", download),
            source={"legacy": "liaoning/state/local_adaptation_20261020/adapters", "legacy_status": adapter.get("scope"),
                    "legacy_dataset": adapter.get("dataset"), "menu_guid": guid, "info_code": code, "view": view,
                    "listed_rows": sample.get("rows") or adapter.get("sample_rows"),
                    "listed_total": sample.get("total") or adapter.get("sample_total"), "listed_files": files,
                    **({"note": "legacy needed the getIpc mapping call before listing; sample to confirm"}
                       if needs_mapping else {})}))
    return out


# -- Liaoning: report components inside pdbln report iframes ----------------
LN_PREFIX = "/px-dif-settlement-infpubquery/pdblnReportController/"
LN_ROUTE = "/pxf-dif-settlement-outnetpub/#/pxf-dif-settlement-outnetpub/pdbln/report/PdblnReport"


def _ln(dataset, name, number, field, status, *, period="day", method="initData", extra=None, keepalive=False,
        source=None):
    start = {"day": "2026-01-01", "month": "2026-01", "year": "2026"}[period]
    token = {"day": "{date}", "month": "{month}", "year": "{year}"}[period]
    return spec(id=dataset, name=name, status=status,
                period={"kind": period, "start": start, "end": "today",
                        **({"complete_only": True} if period != "day" else {})},
                request={"transport": "component", "method": "POST", "path": LN_PREFIX + "pdblnReport" + number,
                         "frame": f"/pdbln/report/PdblnReport{number}", "open_url": LN_ROUTE + number,
                         "component": "pdblnReport" + number, "call": method,
                         "set": {f"queryParam.{field}": token, **(extra or {})}},
                response={"kind": "json"}, keepalive=keepalive, source=source or {})


def liaoning(legacy: Path) -> list[DatasetSpec]:
    sys.path.insert(0, str(legacy))
    from lncrawler.power_live import API_SPECS
    from lncrawler.power_reports import REPORTS
    from lncrawler.sitewide_specs import ADAPTERS
    selected = set(json.loads((legacy / "power_adapters.json").read_text(encoding="utf-8"))["selected_datasets"])
    phases: dict[str, tuple[str, dict]] = {}
    for name in ("expanded_adapters_20261016", "sitewide_expansion_20261016", "continuous_expansion_20261019"):
        path = legacy / "state" / name / "admission_queue.json"
        if path.exists():
            for job in json.loads(path.read_text(encoding="utf-8"))["jobs"]:
                previous = phases.get(job["dataset"], ("", {}))[0]
                if previous != "admitted":
                    phases[job["dataset"]] = (job.get("phase", ""), job)
    status_of = {"admitted": "verified", "denied": "denied"}
    out = [_ln("system_load", "系统负荷预测（日）", "022", "date", "enabled", method="query", keepalive=True,
               source={"legacy": "browser_collector.js REPORTS", "menu": "信息披露/综合查询/供需与约束/预测信息/系统负荷预测（日）"})]
    titles = {r.id: r for r in REPORTS}
    for dataset, (number, field, _api) in API_SPECS.items():
        report = titles.get(dataset)
        if report is None or report.period not in ("day",):
            continue  # metadata catalogs and weekly reports need a reviewed period model first
        extra = {"queryParam.name": report.variant} if number == "017" else None
        status = "verified" if dataset in selected else status_of.get(phases.get(dataset, ("",))[0], "candidate")
        out.append(_ln(dataset, report.title + (f"（{report.variant}）" if extra else ""), number, field, status,
                       extra=extra, source={"legacy": "lncrawler/power_live.py API_SPECS",
                                            "legacy_phase": phases.get(dataset, ("",))[0] or None}))
    seen = {s.id for s in out}
    for adapter in ADAPTERS:
        if adapter.id in seen or adapter.kind != "report" or adapter.period not in ("day", "month", "year"):
            continue
        phase, job = phases.get(adapter.id, ("", {}))
        extra = None
        status = status_of.get(phase, "candidate")
        if adapter.selection_field:
            if job.get("selection"):
                extra = {f"queryParam.{adapter.selection_field}": job["selection"]}
            elif status == "verified":
                status = "candidate"  # option value not recorded; choose it during adaptation
        out.append(_ln(adapter.id, adapter.title + (f"（{job['selection']}）" if extra else ""), adapter.number,
                       adapter.date_field, status, period=adapter.period, extra=extra,
                       source={"legacy": "lncrawler/sitewide_specs.py", "legacy_phase": phase or None,
                               "options_api": adapter.options_api}))
        seen.add(adapter.id)
    return out


# -- Anhui: Element UI report forms (ui transport) --------------------------
AH_FRAME = "/pxf-phdah-settlement-oinfpubgrid/#/pxf-phdah-settlement-oinfpubgrid/spotGoods/spotGoods?infoCode="
AH_FULL = {"actual_load", "queryRqxtfhyc"}
AH_SAMPLED = {"renewables_output_forecast", "wind_output_forecast", "solar_output_forecast",
              "day_ahead_average_offer_price", "hydro_storage_output_forecast", "generation_output_forecast",
              "non_market_output_forecast", "renewables_output_actual", "wind_output_actual", "solar_output_actual",
              "hydro_storage_output_actual", "generation_output_actual", "non_market_output_actual",
              "daily_clearing_quantity_price", "day_ahead_node_marginal_price", "real_time_node_marginal_price",
              "day_ahead_uniform_settlement_price", "real_time_uniform_settlement_price", "system_reserve",
              "generation_types_energy_count"}


def anhui(legacy: Path) -> list[DatasetSpec]:
    sys.path.insert(0, str(legacy))
    from powercrawler.daily_reports import KNOWN_DENIED_CODES, REPORTS, UNCONFIRMED_SCOPE_CODES
    import ast
    tree = ast.parse((legacy / "adapters" / "local_session.py").read_text(encoding="utf-8"))
    menus = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                 and any(getattr(t, "id", None) == "MENU_LABELS" for t in node.targets))
    out = []
    for report in REPORTS.values():
        fields = [{"kind": "date", "placeholder": "选择日期", "value": "{date}"}]
        if report.data_type:
            fields.append({"kind": "select", "label": "数据类型", "value": report.data_type})
        fields += [{"kind": "select", "label": label, "value": value} for label, value in report.selectors]
        if report.code in KNOWN_DENIED_CODES:
            status = "denied"
        elif report.dataset == "actual_load":
            status = "enabled"
        elif report.dataset in AH_FULL:
            status = "verified"
        elif report.dataset in AH_SAMPLED and report.code not in UNCONFIRMED_SCOPE_CODES:
            status = "sampled"
        else:
            status = "candidate"
        name = report.title + (f"（{report.data_type}）" if report.data_type else "")
        request = {"transport": "ui", "frame": "infoCode=" + report.code, "action": "export", "fields": fields}
        if report.code in menus:
            request["menu"] = ["现货市场", "省内现货市场", *menus[report.code]]
        out.append(spec(id=report.dataset, name=name, status=status,
                        period={"kind": "day", "start": "2026-01-01", "end": "today"},
                        request=request, response={"kind": "excel"}, keepalive=report.dataset == "actual_load",
                        source={"legacy": "powercrawler/daily_reports.py", "info_code": report.code,
                                "value_layout": report.value_layout,
                                "note": "account-scoped selector" if report.selectors else None}))
    return out


PERIOD_LABELS = {"日期": "day", "月份": "month", "年份": "year"}
PLACEHOLDERS = {"day": ["请选择日期", "选择日期"], "month": ["请选择月份", "选择月份"], "year": ["请选择年份", "选择年份"]}
TOKENS = {"day": "{date}", "month": "{month}", "year": "{year}"}


def _anhui_state(legacy: Path) -> Path:
    """The latest full server state: live state/ if it has the worklist, else the newest snapshot."""
    if (legacy / "state" / "server_all_site_metric_worklist.json").exists():
        return legacy / "state"
    snapshots = sorted(legacy.glob("state/*/remote_snapshot/state/server_all_site_metric_worklist.json"))
    if not snapshots:
        raise SystemExit("no Anhui worklist found under state/")
    return snapshots[-1].parent


def _frame_needle(state: Path, entry: dict) -> tuple[str | None, str | None]:
    observation = (entry.get("adaptation_evidence") or {}).get("observation")
    path = state.parent / observation if observation else None
    if path is None or not path.exists():
        return None, None
    for frame in json.loads(path.read_text(encoding="utf-8")).get("frames", []):
        route, code = frame.get("route") or "", frame.get("report_code")
        if frame.get("main_frame") or not code:
            continue
        if route.endswith(("/spotGoods/spotGoods", "/spotGoods/fReport")):
            return "infoCode=" + code, code
        return route.split("/", 2)[-1] if route.count("/") > 1 else route, code
    return None, None


def anhui_period_reports(legacy: Path) -> list[DatasetSpec]:
    """Anhui 'period form' reports: menu path + period field + optional scope selects."""
    state = _anhui_state(legacy)
    worklist = json.loads((state / "server_all_site_metric_worklist.json").read_text(encoding="utf-8"))["entries"]
    worklist = list(worklist.values()) if isinstance(worklist, dict) else worklist
    by_dataset = {e["period_report_dataset"]: e for e in worklist if e.get("period_report_dataset")}
    out = []
    for path in sorted(state.glob("server_period_report_queue_*.json")):
        queue = json.loads(path.read_text(encoding="utf-8"))
        jobs = list(queue["jobs"].values()) if isinstance(queue["jobs"], dict) else queue["jobs"]
        if not jobs:
            continue
        entry = by_dataset.get(queue["dataset"], {})
        first = jobs[0]["period"]
        kind = {10: "day", 7: "month", 4: "year"}[len(first)]
        label = next((k for k in jobs[0].get("selectors", {}) if PERIOD_LABELS.get(k) == kind), None)
        scopes, seen = [], set()
        for job in jobs:
            scope = {k: v for k, v in job.get("selectors", {}).items() if k != label}
            marker = json.dumps(scope, sort_keys=True, ensure_ascii=False)
            if scope and marker not in seen:
                seen.add(marker)
                scopes.append(scope)
        needle, code = _frame_needle(state, entry)
        notes = []
        secondary = sorted({k for s in scopes for k in s if "日期" in k or "时间" in k})
        if secondary:
            notes.append("secondary date/time selector needs review: " + ",".join(secondary))
        if needle is None:
            notes.append("report frame not identified in the observation; set request.frame")
        saved = any(j.get("status") == "saved" for j in jobs)
        status = "sampled" if saved and not notes else "candidate"
        fields = [{"kind": "date", "placeholder": PLACEHOLDERS[kind], "value": TOKENS[kind]}]
        request = {"transport": "ui", "frame": needle or "REVIEW_FRAME", "action": "export", "fields": fields,
                   "menu": queue.get("menu_path") or entry.get("menu_path")}
        out.append(spec(
            id=queue["dataset"], name=entry.get("title") or queue["menu_path"][-1], status=status,
            period={"kind": kind, "start": min(j["period"] for j in jobs), "end": "today"},
            request=request, response={"kind": "excel"}, scopes=scopes,
            source={"legacy": "anhui period_report_queue", "report_code": code,
                    "legacy_status": entry.get("status"), "scope_coverage": entry.get("scope_coverage"),
                    "legacy_jobs": len(jobs), "legacy_saved": sum(j.get("status") == "saved" for j in jobs),
                    "note": "; ".join(notes) or None}))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("province", choices=["shanxi", "liaoning", "anhui"])
    parser.add_argument("legacy", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--only", help="write only dataset ids starting with this prefix")
    args = parser.parse_args()
    legacy = args.legacy.resolve()
    specs = {"shanxi": shanxi, "liaoning": liaoning, "anhui": anhui}[args.province](legacy)
    extra = {"anhui": [anhui_period_reports, anhui_publications], "shanxi": [shanxi_publications, shanxi_native],
             "liaoning": [liaoning_publications]}.get(args.province, [])
    for build in extra:
        known = {item.id for item in specs}
        specs += [item for item in build(legacy) if item.id not in known]
    directory = REPO / "provinces" / args.province / "datasets"
    directory.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    if args.only:
        specs = [item for item in specs if item.id.startswith(args.only)]
    for item in specs:
        target = directory / f"{item.id}.json"
        if target.exists() and not args.replace:
            skipped += 1
            continue
        item.note("migrated_from_legacy", legacy_status=item.source.get("legacy_status") or item.source.get("legacy_phase"))
        item.source = {k: v for k, v in item.source.items() if v is not None}
        item.save(target)
        written += 1
    counts: dict[str, int] = {}
    for item in specs:
        counts[item.status] = counts.get(item.status, 0) + 1
    print(json.dumps({"province": args.province, "written": written, "skipped": skipped, "statuses": counts},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
