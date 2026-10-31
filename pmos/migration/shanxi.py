"""Shanxi: identity contracts for the PMOS specs, proven equal to the legacy queues.

The Shanxi native JSON lane keys each legacy queue row by its plaintext
request body (business fields, page number, reviewed page size). PMOS sends the
same query through the page's own component, which has no body of its own, so
each spec carries the legacy body template in ``source.identity_contract`` and
the engine derives the identity from it. :func:`native_parity` checks, for
every row of a legacy native-queue snapshot, that the engine identity equals
the legacy one.

Read-only on the legacy root; :func:`attach_contracts` writes only PMOS specs.
"""
from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

from ..engine import task_identity
from ..faults import Fault
from ..queue_compat import QueueContext, read_native_sqlite
from ..spec import DatasetSpec, load_all, scope_key

CONTRACTS = "native_menu_query_contracts.json"
SNAPSHOTS = ("verification/20261021/native_node_queue/queue_after19_formats.sqlite3",
             "verification/20261021/native_flat_curve/queue_after_two_flat_formats.sqlite3",
             "verification/20261021/native_json_queue/queue_after_selected_day.sqlite3")
SCOPE_FIELDS = ("strName", "tdName")


def native_contract(record: dict) -> dict:
    """Legacy native contract -> PMOS identity contract (body template)."""
    observed = record.get("observed_request") or {}
    body = deepcopy(observed.get("body"))
    fields = record.get("date_fields")
    if not isinstance(body, dict) or not isinstance(fields, list):
        raise ValueError("native_contract_body_and_date_fields_required")
    flat = record.get("request_envelope") == "flat" or not isinstance(body.get("data"), dict)
    data = body if flat else body["data"]
    for name in fields:
        if name not in data:
            raise ValueError("native_contract_date_field_missing")
        data[name] = "{date}"
    for name in SCOPE_FIELDS:
        if name in (record.get("required_scope_fields") or []):
            data[name] = "{scope[" + name + "]}"
    contract = {"body": {k: v for k, v in body.items() if k != "pageInfo"} if not flat else body,
                "date_fields": fields}
    if flat:
        # The legacy queue records page 1 for flat (unpaged) native requests.
        contract.update(request_envelope="flat", fixed_page=1)
    else:
        size = record.get("reviewed_page_size", (body.get("pageInfo") or {}).get("pageSize"))
        if type(size) is not int or size < 1:
            raise ValueError("native_contract_page_size_required")
        contract["page_size"] = size
    return contract


def contracts(root: Path) -> dict:
    value = json.loads((Path(root) / CONTRACTS).read_text(encoding="utf-8"))
    return value["records"]


def attach_contracts(root: Path, directory: Path) -> dict:
    """Add identity contracts to the PMOS Shanxi native specs that have a legacy contract."""
    directory = Path(directory).resolve()
    if directory.parts[-3:] != ("provinces", "shanxi", "datasets"):
        raise ValueError("pmos_shanxi_datasets_directory_required")
    records = contracts(root)
    done, skipped = [], {}
    for name, spec in load_all(directory).items():
        if spec.request.get("transport") != "component" or spec.source.get("strategy") is None:
            continue
        record = records.get(name)
        if record is None:
            skipped[name] = "no_legacy_native_contract"
            continue
        try:
            contract = native_contract(record)
        except ValueError as exc:
            skipped[name] = str(exc)
            continue
        spec.source["identity_contract"] = contract
        spec.save()
        done.append(name)
    return {"attached": sorted(done), "skipped": skipped}


def native_parity(root: Path, directory: Path, *, account_alias: str = "shanxi-production",
                  snapshots=SNAPSHOTS) -> dict:
    """Engine identity == legacy identity for every row of the native-queue snapshots."""
    specs = load_all(Path(directory))
    per = {}
    for relative in snapshots:
        path = Path(root) / relative
        if not path.is_file():
            continue
        # Each dataset is decoded with its own period kind (monthly tabs key "2026-07").
        decoded = {kind: read_native_sqlite(path, QueueContext("shanxi", account_alias, period_kind=kind))
                   for kind in ("day", "month")}
        kinds = {name: ("month" if spec.period["kind"] == "month" else "day") for name, spec in specs.items()}
        rows = [row for index, row in enumerate(decoded["day"])
                if kinds.get(row.dataset, "day") == "day"] + \
               [row for row in decoded["month"] if kinds.get(row.dataset) == "month"]
        for row in rows:
            tally = per.setdefault(row.dataset, Counter())
            tally["records"] += 1
            spec = specs.get(row.dataset)
            if spec is None or not spec.source.get("identity_contract"):
                tally["no_spec_contract"] += 1
                continue
            if row.identity is None:
                tally["unmapped"] += 1
                continue
            legacy = row.identity
            dims = {k: v for k, v in legacy.dimensions.items() if k != "_page_size"}
            scope = scope_key({k: dims[k] for k in SCOPE_FIELDS if k in dims}) if spec.scopes else ""
            label = legacy.period.get("label", legacy.period.get("anchor"))
            period = label if spec.period["kind"] != "snapshot" else label
            try:
                mine = task_identity("shanxi", account_alias, spec, period, scope,
                                     page=legacy.page if "pagination" in spec.request else None)
            except (Fault, KeyError, ValueError) as exc:
                tally["engine_identity_error"] += 1
                tally.setdefault("first_error", str(getattr(exc, "code", exc)))
                continue
            if mine.key == legacy.key:
                tally["matched"] += 1
            else:
                tally["mismatched"] += 1
                if "first_difference" not in tally:
                    a, b = mine.document(), legacy.document()
                    tally["first_difference"] = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    eligible = sorted(name for name, t in per.items() if t["records"] and t["matched"] == t["records"])
    return {"province": "shanxi", "account_alias": account_alias, "dispatch_allowed": False,
            "snapshots": [s for s in snapshots if (Path(root) / s).is_file()],
            "eligible": eligible, "per_source": {k: dict(v) for k, v in sorted(per.items())},
            "summary": {"sources": len(per), "eligible": len(eligible),
                        "records": sum(t["records"] for t in per.values()),
                        "matched": sum(t["matched"] for t in per.values())}}


# -- specs generated from the legacy queue (strategies added after the first migration)
NAMED_REVIEW = "verification/20261019/remaining_daily_source_review.json"


def _queue_rows(snapshot: Path, dataset: str) -> list[dict]:
    import sqlite3
    from contextlib import closing
    path = Path(snapshot) / "state/native_json_collection/queue.sqlite3"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        return [{"period": p, "body": json.loads(b), "contract": json.loads(c), "page": g} for p, b, c, g in
                db.execute("SELECT period, body, contract, page FROM requests WHERE dataset=? ORDER BY rowid", (dataset,))]


def named_scope_spec(legacy: Path, snapshot: Path, dataset: str) -> dict:
    """``native_named_scope_preflight``: one query per (day, unit name) on an informationRelease page.

    Same page call as the legacy dispatcher's section strategy: preflight the
    live form (selector and export URLs), then set ``date``, ``selectValue``
    and ``queryConfig`` and call ``queryPage()``; the page itself rewrites
    ``queryConfig.data`` from the date and the selected unit.
    """
    review = {r["id"]: r for r in json.loads((Path(legacy) / NAMED_REVIEW).read_text(encoding="utf-8"))["reports"]}
    source = review[dataset]
    form, route = source["form"], "/pxf-phbsx-infomation/#" + source["route"]["path"]
    controls = form["select_controls"]
    if len(controls) != 1 or controls[0]["value"] != "tdName" or form["date_fields"] != ["tdate"]:
        raise ValueError("named_source_form_changed")
    rows = _queue_rows(snapshot, dataset)
    kinds = {r["contract"].get("kind") for r in rows}
    if kinds != {"native_named_scope_preflight"} or not rows:
        raise ValueError("named_queue_contract_kind_changed")
    infos = {json.dumps(r["body"]["pageInfo"], sort_keys=True) for r in rows}
    page_info = {**json.loads(sorted(infos)[0]), "pageNum": 1}
    sizes = {r["body"]["pageInfo"]["pageSize"] for r in rows}
    if len(sizes) != 1 or any(set(r["body"]["data"]) != {"tdate", "tdName"} for r in rows):
        raise ValueError("named_queue_body_shape_changed")
    names = list(dict.fromkeys(r["body"]["data"]["tdName"] for r in rows))
    path = form["query_path"]
    component = {"component": "*", "match": {"queryUrl": path}}
    size = sizes.pop()
    return {
        "id": dataset, "name": source.get("title") or dataset, "status": "sampled",
        "period": {"kind": "day", "start": min(r["period"] for r in rows), "end": "today"},
        "request": {"transport": "component", "frame": route.split("#", 1)[1], "open_url": route, "path": path,
                    "method": "POST", **component,
                    "preflight": [{**component, "equals": {"selectUrl": controls[0]["url"], "exportUrl": form["export_path"]}}],
                    "set": {"date": "{date}", "selectValue": "{scope[tdName]}",
                            "queryConfig": {"data": {"tdate": "{date}", "tdName": "{scope[tdName]}"}, "pageInfo": page_info}},
                    "call": "queryPage",
                    "pagination": {"page": "queryConfig.pageInfo.pageNum", "records": "data.list", "total": "data.total",
                                   "last": "data.isLastPage", "page_size": size, "max_pages": 50}},
        "response": {"kind": "json"},
        "scopes": [{"tdName": name} for name in names],
        "source": {"legacy": "shanxi native queue (named scope)", "strategy": "named_scope", "lane": "native",
                   "legacy_kind": "native_named_scope_preflight", "source_sha256": source.get("source_sha256"),
                   "identity_contract": {"body": {"data": {"tdate": "{date}", "tdName": "{scope[tdName]}"}},
                                         "date_fields": ["tdate"], "page_size": size},
                   "note": "unit names are exactly those in the legacy queue; preflight mirrors the legacy live-form check"},
    }


TAB_PLAN = "verification/20261022/tab_section_completion/plan.json"
NATIVE_PAGE = {"records": "data.list", "total": "data.total", "last": "data.isLastPage", "max_pages": 50}


def _plan_profile(legacy: Path, dataset: str) -> dict:
    plan = json.loads((Path(legacy) / TAB_PLAN).read_text(encoding="utf-8"))
    return next(p for p in plan["profiles"] if p["id"] == dataset)


def _page_info(rows: list[dict]) -> tuple[dict, int]:
    sizes = {r["body"]["pageInfo"]["pageSize"] for r in rows}
    if len(sizes) != 1:
        raise ValueError("queue_page_size_not_uniform")
    info = {**rows[0]["body"]["pageInfo"], "pageNum": 1, "total": 0}
    return info, sizes.pop()


def alt_tab_spec(legacy: Path, snapshot: Path, alias: str) -> dict:
    """``native_alternate_tab``: a non-default tab of an informationRelease page.

    Legacy call: on the page component, set ``activeName`` and ``handleClick()``
    (switches the tab's query/export URLs, sends nothing), check them, then
    set ``date``/``rangeDate``/``queryConfig.data``/``pageInfo`` and call
    ``queryPage()``. The monthly price tab keys ``tdate``; the others
    ``startDate``/``endDate`` (both the day).
    """
    rows = _queue_rows(snapshot, alias)
    if not rows or {r["contract"].get("kind") for r in rows} != {"native_alternate_tab"}:
        raise ValueError("alt_tab_queue_contract_kind_changed")
    source_id = rows[0]["contract"]["source_dataset"]
    profile = _plan_profile(legacy, source_id)
    if profile.get("kind") != "tab" or profile["tab"] != rows[0]["contract"]["tab"] \
            or profile["query_path"] != rows[0]["contract"]["source_query_path"]:
        raise ValueError("alt_tab_plan_profile_changed")
    monthly = profile["tab"] == "chuqingdianjia"
    token = "{month}" if monthly else "{date}"
    fields = ["tdate"] if monthly else ["startDate", "endDate"]
    if any(set(r["body"]["data"]) != set(fields) for r in rows):
        raise ValueError("alt_tab_queue_body_shape_changed")
    info, size = _page_info(rows)
    data = {name: token for name in fields}
    tab = {"component": profile["component"]}
    switched = {**tab, "equals": {"activeName": profile["tab"], "queryUrl": profile["query_path"],
                                  "exportUrl": profile["export_path"]}}
    route = profile["route"]
    return {
        "id": alias, "name": profile.get("title") or alias, "status": "sampled",
        "period": {"kind": "month" if monthly else "day", "start": min(r["period"] for r in rows), "end": "today"},
        "request": {"transport": "component", "frame": route.split("#", 1)[1], "open_url": route,
                    "path": profile["query_path"], "method": "POST", **tab,
                    # The tab component mounts a moment after the route changes (production waits for it).
                    "prepare": [{"wait": tab},
                                {**tab, "set": {"activeName": profile["tab"]}, "call": "handleClick", "unless": switched}],
                    "preflight": [switched],
                    "set": {"date": token, "rangeDate": [token, token], "queryConfig.data": data, "queryConfig.pageInfo": info},
                    "call": "queryPage",
                    "pagination": {"page": "queryConfig.pageInfo.pageNum", "page_size": size, **NATIVE_PAGE}},
        "response": {"kind": "json"},
        "source": {"legacy": "shanxi native queue (alternate tab)", "strategy": "alternate_tab", "lane": "native",
                   "legacy_kind": "native_alternate_tab", "source_dataset": source_id,
                   "source_sha256": rows[0]["contract"].get("source_sha256"),
                   "identity_contract": {"body": {"data": data}, "date_fields": fields, "page_size": size},
                   "note": "component/route exactly as the legacy plan pairs them; no production send has completed yet"},
    }


def shadow_section_spec(legacy: Path, snapshot: Path, dataset: str = "querySectionConstraintsShadowPrice") -> dict:
    """``native_shadow_section_preflight``: one query per (day, section) with the strName selector."""
    rows = _queue_rows(snapshot, dataset)
    if not rows or {r["contract"].get("kind") for r in rows} != {"native_shadow_section_preflight"}:
        raise ValueError("shadow_queue_contract_kind_changed")
    profile = _plan_profile(legacy, dataset)
    path, selector = profile["query_path"], profile["selector_path"]
    if any(set(r["body"]["data"]) != {"infoDate", "strName"} for r in rows):
        raise ValueError("shadow_queue_body_shape_changed")
    info, size = _page_info(rows)
    component = {"component": "*", "match": {"queryUrl": path}}
    data = {"infoDate": "{date}", "strName": "{scope[strName]}"}
    route = profile["route"]
    return {
        "id": dataset, "name": profile.get("title") or dataset, "status": "sampled",
        "period": {"kind": "day", "start": min(r["period"] for r in rows), "end": "today"},
        "request": {"transport": "component", "frame": route.split("#", 1)[1], "open_url": route, "path": path,
                    "method": "POST", **component,
                    "preflight": [{**component, "equals": {"selectUrl": selector,
                                                           "exportUrl": "/px-phbsx-retail-shop/infoPub/exportShadowPrice"}}],
                    "set": {"date": "{date}", "selectValue": "{scope[strName]}",
                            "queryConfig": {"data": data, "pageInfo": info}},
                    "call": "queryPage",
                    "pagination": {"page": "queryConfig.pageInfo.pageNum", "page_size": size, **NATIVE_PAGE}},
        "response": {"kind": "json"},
        "scopes": [{"strName": n} for n in dict.fromkeys(r["body"]["data"]["strName"] for r in rows)],
        "source": {"legacy": "shanxi native queue (shadow section)", "strategy": "shadow_section", "lane": "native",
                   "legacy_kind": "native_shadow_section_preflight",
                   "identity_contract": {"body": {"data": data}, "date_fields": ["infoDate"], "page_size": size}},
    }


PLAIN_DATES = {
    "querySectionConstraints": ("/pxf-phbsx-infomation/#/pxf-phbsx-infomation/duanmianyueshu",
                                "/px-phbsx-retail-shop/infoGrid/dmys", "/px-phbsx-retail-shop/infoGrid/dmysExcel",
                                "native_plain_section_dates", "断面约束"),
    "candidate_2e1f8d6d3b00": ("/pxf-phbsx-infomation/#/pxf-phbsx-infomation/sjllxsdqk",
                               "/px-phbsx-retail-shop/infoGrid/sjllxsdqk", "/px-phbsx-retail-shop/infoGrid/sjllxsdqkExcel",
                               "native_plain_intertie_dates", "省间联络线输电情况"),
}


def plain_dates_spec(snapshot: Path, dataset: str) -> dict:
    """Plain informationRelease form without a selector: ``date`` + page via ``handleCurrentChange``."""
    route, path, export, kind, title = PLAIN_DATES[dataset]
    rows = _queue_rows(snapshot, dataset)
    if not rows or {r["contract"].get("kind") for r in rows} != {kind}:
        raise ValueError("plain_dates_queue_contract_kind_changed")
    if any(set(r["body"]["data"]) != {"date"} for r in rows):
        raise ValueError("plain_dates_queue_body_shape_changed")
    info, size = _page_info(rows)
    component = {"component": "*", "match": {"queryUrl": path}}
    return {
        "id": dataset, "name": title, "status": "sampled",
        "period": {"kind": "day", "start": min(r["period"] for r in rows), "end": "today"},
        "request": {"transport": "component", "frame": route.split("#", 1)[1], "open_url": route, "path": path,
                    "method": "POST", **component,
                    "preflight": [{**component, "equals": {"exportUrl": export}}],
                    "set": {"queryConfig.pageInfo": info, "date": "{date}"}, "call": "handleCurrentChange", "args": [1],
                    "pagination": {"page": "args.0", "page_size": size, **NATIVE_PAGE}},
        "response": {"kind": "json"},
        "source": {"legacy": "shanxi native queue (plain dates)", "strategy": "plain", "lane": "native",
                   "legacy_kind": kind,
                   "identity_contract": {"body": {"data": {"date": "{date}"}}, "date_fields": ["date"], "page_size": size}},
    }


# -- cutover --------------------------------------------------------------
NATIVE_QUEUE = "state/native_json_collection/queue.sqlite3"
PUBLICATION_QUEUE = "state/native_publication_collection/queue.sqlite3"
UNIFIED_PLAN = "state/server_unified_sequence_20261023_plan.json"
IN_FLIGHT = {"leased", "sent", "reserved", "interrupted", "attempting", "running"}
# Production's live "request in progress" markers: any of them pending means a
# send may be in flight; the freeze must happen after they clear.
INFLIGHT_MARKERS = ("state/server_report_inflight.json", "state/server_inspection_probe_inflight.json",
                    "state/server_transport_probe_inflight.json", "state/server_session_renewal.json")


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def legacy_sources(snapshot: Path, specs: dict, account_alias: str) -> list[tuple[str, str, list]]:
    """(source_id, snapshot_sha256, LegacyTask rows) for every declared legacy queue file."""
    from ..queue_compat import decode_journal, read_json
    snapshot = Path(snapshot)
    out = []
    path = snapshot / NATIVE_QUEUE
    by_kind = {kind: read_native_sqlite(path, QueueContext("shanxi", account_alias, period_kind=kind))
               for kind in ("day", "month")}
    monthly = {name for name, spec in specs.items() if spec.period["kind"] == "month"}
    rows = [r for r in by_kind["day"] if r.dataset not in monthly] + [r for r in by_kind["month"] if r.dataset in monthly]
    out.append((NATIVE_QUEUE, _sha(path), rows))
    path = snapshot / PUBLICATION_QUEUE
    if path.is_file():
        rows = read_native_sqlite(path, QueueContext("shanxi", account_alias, "shanxi_publications"), table="work")
        out.append((PUBLICATION_QUEUE, _sha(path), rows))
    for journal in sorted(snapshot.glob("state/crawlee_*/batch_journal.json")):
        value, digest = read_json(journal)
        # A journal without its own granularity: the legacy directory names monthly ones "_monthly".
        kind = value.get("granularity") or ("month" if journal.parent.name.endswith("_monthly") else "day")
        context = QueueContext("shanxi", account_alias, value.get("dataset", ""), kind, "export")
        out.append((str(journal.relative_to(snapshot)), digest, decode_journal(value, context)))
    return out


def _task_of(row, spec) -> tuple[str, str, str] | None:
    """The PMOS (dataset, period, scope) of a legacy native row, or None if it has no scope here."""
    legacy = row.identity
    dims = {k: v for k, v in legacy.dimensions.items() if k != "_page_size"}
    if spec.scopes:
        scope = scope_key({k: dims[k] for k in SCOPE_FIELDS if k in dims})
        if scope not in {key for key, _ in spec.scope_items()}:
            return None
    else:
        scope = ""
    return row.dataset, legacy.period.get("label", legacy.period.get("anchor")), scope


def pending_attachments(snapshot: Path, specs: dict) -> dict[str, list[dict]]:
    """Pending legacy attachment work, by PMOS announcement spec id (menu guid -> spec)."""
    import sqlite3
    from contextlib import closing
    by_menu = {spec.source.get("menu_guid"): name for name, spec in specs.items() if spec.documents}
    path = Path(snapshot) / PUBLICATION_QUEUE
    out: dict[str, list[dict]] = {}
    if not path.is_file():
        return out
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        rows = list(db.execute("SELECT identity, scope, command, binding FROM work"
                               " WHERE kind='attachment' AND status='pending' ORDER BY rowid"))
    for identity, scope, command, binding in rows:
        command, binding = json.loads(command), json.loads(binding)
        menu = binding["menu_result"]["menu"]["guid"]
        file_id = command["query"]["fileId"]
        if file_id != scope or command["arguments"] != [file_id, None] or (command["native_module"], command["native_export"]) != ("63a1", "a"):
            raise ValueError("legacy_attachment_command_changed")
        out.setdefault(by_menu.get(menu, "menu:" + menu), []).append(
            {"legacy_identity": identity, "file_id": file_id, "filename": command["filename"], "menu": menu})
    return out


def plan_cutover(snapshot: Path, directory: Path, *, account_alias: str = "shanxi-production") -> dict:
    """Which native datasets PMOS takes over from this frozen Shanxi snapshot, in production order."""
    snapshot = Path(snapshot)
    specs = load_all(Path(directory))
    parity = native_parity(snapshot, directory, account_alias=account_alias, snapshots=[NATIVE_QUEUE])
    plan_value = json.loads((snapshot / UNIFIED_PLAN).read_text(encoding="utf-8"))
    deferred = {key.split(":", 1)[1] for key, value in (plan_value.get("user_deferred_scopes") or {}).items()
                if key.startswith("native:") and value.get("approved")}
    by_menu = {spec.source.get("menu_guid"): name for name, spec in specs.items() if spec.documents}
    groups = plan_value["groups"] + (plan_value.get("remaining_attachment_groups") or [])
    order = list(dict.fromkeys(g["scope"] if g["lane"] == "native" else by_menu.get(g["scope"], "menu:" + g["scope"])
                               for g in groups))
    attachments = pending_attachments(snapshot, specs)
    sources = legacy_sources(snapshot, specs, account_alias)
    blockers, unknown, pending = Counter(), Counter(), {}
    manifest_path = snapshot / "MANIFEST.json"
    declared = (json.loads(manifest_path.read_text(encoding="utf-8")).get("inflight_markers") or {}) \
        if manifest_path.is_file() else {}
    for marker in INFLIGHT_MARKERS:
        path = snapshot / marker
        if not path.is_file():
            # Production treats a marker's existence as "in flight": an absent
            # file counts only if the freeze declares it checked and absent.
            if declared.get(marker) != "absent":
                blockers[f"{marker}:not_verified_by_freeze"] += 1
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("pending") is True or value.get("outcome") == "pending":
            blockers[f"{marker}:pending"] += 1
    for source_id, _, rows in sources:
        for row in rows:
            if row.legacy_state in IN_FLIGHT or "legacy_active_owner_unsettled" in row.blockers:
                # A historical unknown outcome: imported as a never-resend tombstone.
                unknown[f"{source_id}:{row.legacy_state}"] += 1
            if source_id == NATIVE_QUEUE and row.state == "pending" and not row.consumed:
                pending[row.dataset] = pending.get(row.dataset, 0) + 1
    eligible = set(parity["eligible"])
    # Announcement menus whose spec downloads with the legacy command (63a1.a(fileId, null)).
    eligible |= {name for name in attachments if name in specs and specs[name].documents
                 and specs[name].documents["download"].get("module") == "63a1"}
    for name, items in attachments.items():
        pending[name] = pending.get(name, 0) + len(items)
    scope = [name for name in order if name in eligible and name not in deferred and pending.get(name)]
    # (pending desc, name): a plan must hash identically in every process.
    scope += sorted((name for name in eligible - set(scope) if name not in deferred and pending.get(name)),
                    key=lambda name: (-pending[name], name))
    plan = {
        "province": "shanxi", "account_alias": account_alias,
        "inputs": [{"path": s, "sha256": d, "records": len(r)} for s, d, r in sources],
        "global_blockers": dict(blockers), "legacy_unknown_tombstones": dict(unknown), "scope": scope,
        "pending_in_scope": sum(pending.get(name, 0) for name in scope),
        "attachments_in_scope": sum(len(attachments.get(name, [])) for name in scope),
        "attachments_pending": sum(len(items) for items in attachments.values()),
        "pending_total": sum(pending.values()),
        "excluded_pending": {name: ("user_deferred" if name in deferred else "identity_parity_incomplete")
                             for name, n in sorted(pending.items()) if name not in scope},
        "native_parity": parity["summary"], "ready": bool(scope) and not blockers,
    }
    from .liaoning import digest
    plan["plan_sha256"] = digest(plan)
    return plan


def apply_cutover(snapshot: Path, directory: Path, instance: Path, plan: dict, *, legacy_owner: dict,
                  code_sha: str, approval: str, request_budget: int, cutover: dict) -> dict:
    """NEW ledger: every legacy record as a tombstone, the handoff, then the legacy pending tasks of the scope."""
    from ..ledger import Ledger
    from ..queue_compat import import_shadow
    if not plan.get("ready"):
        raise ValueError("cutover_plan_not_ready")
    if plan_cutover(snapshot, directory, account_alias=plan["account_alias"])["plan_sha256"] != plan["plan_sha256"]:
        raise ValueError("legacy_snapshot_changed_since_plan")
    instance = Path(instance)
    if instance.exists():
        raise ValueError("new_instance_directory_required")
    specs = load_all(Path(directory))
    sources = legacy_sources(snapshot, specs, plan["account_alias"])
    instance.mkdir(parents=True, mode=0o700)
    ledger = Ledger(instance / "ledger.sqlite3")
    try:
        snapshots = []
        for source_id, digest_value, rows in sources:
            import_shadow(ledger, rows, QueueContext("shanxi", plan["account_alias"]),
                          source_id=source_id, snapshot_sha256=digest_value)
            snapshots.append({"source_id": source_id, "snapshot_sha256": digest_value})
        receipt = ledger.grant_handoff({
            "version": 1, "province": "shanxi", "account_alias": plan["account_alias"], "legacy_owner": legacy_owner,
            "snapshots": snapshots, "scope": plan["scope"], "request_budget": request_budget, "code_sha": code_sha,
            "approval": approval + f" (plan {plan['plan_sha256']})", "session": cutover["session"],
            "seeding": cutover["seeding"]})
        seeded = Counter()
        from ..documents import derive_period, item_scope
        for name, items in pending_attachments(snapshot, specs).items():
            if name not in plan["scope"]:
                continue
            rules = specs[name].documents.get("period", [])
            listed = []
            for item in items:
                # The legacy work row has no title: the file name carries the period.
                period, basis = derive_period({}, item["filename"], rules, None)
                listed.append({"record_id": "legacy:" + item["legacy_identity"], "title": item["filename"],
                               "published": None, "period": period, "period_basis": basis,
                               "attachment": {"id": item["file_id"], "name": item["filename"]}, "record": {},
                               "scope": item_scope(item["file_id"]), "legacy_menu": item["menu"]})
            _, added = ledger.put_listing(name, [], listed)
            seeded[name] += added
        native = next(rows for source_id, _, rows in sources if source_id == NATIVE_QUEUE)
        for row in native:
            spec = specs.get(row.dataset)
            if row.dataset not in plan["scope"] or row.state != "pending" or row.consumed or row.identity is None:
                continue
            task = _task_of(row, spec)
            if task is None:
                raise ValueError("legacy_pending_scope_missing_from_spec:" + row.dataset)
            seeded[row.dataset] += ledger.seed(task[0], [task[1]], task[2])
        summary = ledger.legacy_summary()
    finally:
        ledger.close()
    return {"receipt": receipt, "seeded": dict(seeded), "ledger": summary}


def manifest_tombstones(manifest: Path, specs: dict, account_alias: str = "shanxi-production") -> dict:
    """The legacy main crawler's archive manifest as consumed export tombstones (for a late import).

    Each complete line names a saved export original (dataset, period). Its
    identity is the export identity the legacy Crawlee journals also carry
    (operation "export", no dimensions, no page), so a fetch spec's own task
    identity equals it and a native spec of the same indicator sees the period
    as completed. Lines of datasets without a spec or with a period that does
    not fit the spec's kind are counted, not imported.
    """
    import hashlib
    from ..contracts import TaskIdentity
    from .. import periods
    raw = Path(manifest).read_bytes()
    complete = raw[:raw.rfind(b"\n") + 1]  # a line being appended is not part of the snapshot
    rows, seen, skipped = [], set(), Counter()
    for line in complete.splitlines():
        value = json.loads(line)
        spec = specs.get(value.get("dataset"))
        if spec is None:
            skipped["no_spec"] += 1
            continue
        kind, label = spec.period["kind"], value.get("period")
        try:
            periods.parse(kind, label)
        except Exception:
            skipped["period_not_" + kind] += 1
            continue
        identity = TaskIdentity("shanxi", account_alias, spec.id, {"kind": kind, "label": label}, {}, operation="export")
        key = f"manifest:{value['sha256']}:{value['path']}"
        if key in seen:
            continue
        seen.add(key)
        rows.append((key, identity))
    return {"snapshot_sha256": hashlib.sha256(complete).hexdigest(), "rows": rows, "skipped": dict(skipped),
            "datasets": len({identity.dataset for _, identity in rows})}
