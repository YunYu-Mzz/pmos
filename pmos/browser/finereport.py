"""One day of a FineReport report, through the page's own form (Liaoning's report engine lane).

The site's ``spotgoods`` component maps its report and sets its iframe when it mounts, so PMOS opens the report from
the menu like any other (request.site_menu) and needs none of the legacy worker's isolated page: no disabled scripts,
no injected libraries, no webpack. Its queue is kept, as proven there: FR.ajax only queues, and each stage is checked,
then released on its own, the spacing kept. Telemetry and form_getsource are not sent.

A FineReport session ends when idle (never within 11.5 min, always after 66): after REOPEN_IDLE_SECONDS the component
maps again and its frame opens a new session.
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

from ..faults import Fault
from ..finereport import MAPPING_PATH, PATH, SESSION_END, STAGES, html_rows
from ..validate import Response
from . import ui

REOPEN_IDLE_SECONDS = 600
DATE_WIDGET = "日期"
UNIT = "单位:MW"
STAGE_PAUSE_SECONDS = 1.0

_REOPEN_JS = """async source => {
  const seen = new Set(), vms = [], queue = [...document.querySelectorAll('*')].map(e => e.__vue__).filter(Boolean);
  while (queue.length) { const v = queue.shift(); if (seen.has(v)) continue; seen.add(v); vms.push(v); queue.push(...(v.$children || [])); }
  const found = vms.filter(v => v.$options && v.$options.name === 'spotgoods' && v.infoCode === source);
  if (found.length !== 1 || typeof found[0].queryIpSpotgoodsInfoBytableName !== 'function') throw new Error('component_missing');
  found[0].src = '';
  await found[0].$nextTick();
  found[0].queryIpSpotgoodsInfoBytableName();
  return true;
}"""

_INSTALL_JS = r"""() => {
  if (typeof FR === 'undefined' || typeof _g !== 'function' || !_g() || !_g().parameterEl) throw new Error('not_ready');
  if (window.__pmosFrQueue) return false;
  window.__pmosFrQueue = []; window.__pmosFrAjax = FR.ajax;
  FR.ajax = function (o) {
    const u = new URL(o.url || FR.servletURL, location.href), d = o.data || {}, op = d.op || u.searchParams.get('op'), cmd = d.cmd || u.searchParams.get('cmd');
    if (cmd === 'form_getsource' || op === 'js_log' || /\/(preview|adaptive)\/info\/collect$/.test(u.pathname)) return;
    window.__pmosFrQueue.push(o);
  };
  return true;
}"""

_UNITS_JS = """() => Object.values(_g().parameterEl.name_widgets).map(w => w.getValue && w.getValue())
  .filter(v => typeof v === 'string' && v.includes('单位'))"""

_PEEK_JS = """() => window.__pmosFrQueue.map(o => { const u = new URL(o.url || FR.servletURL, location.href), d = o.data || {};
  return {path: u.pathname, method: (o.type || $.ajaxSettings.type || 'GET').toUpperCase(),
          op: d.op || u.searchParams.get('op'), cmd: d.cmd || u.searchParams.get('cmd')}; })"""

_COMMIT_JS = """x => { const f = _g(), v = f.parameterEl.getWidgetByName(x.widget); v.setValue(x.date);
  if (v.getValue() !== x.date) throw new Error('date_not_applied'); f.parameterCommit(); return v.getValue(); }"""

_RELEASE_JS = "() => { const o = window.__pmosFrQueue.shift(); window.__pmosFrAjax.call(FR, o); return true; }"


# What the form asks for: every widget's name, kind and current value (no secrets: a form's own defaults).
_WIDGETS_JS = """() => Object.entries(_g().parameterEl.name_widgets || {}).map(([name, w]) => {
  let value = null;
  try { const v = w.getValue && w.getValue(); value = v === null || typeof v === 'object' ? null : v; } catch (e) {}
  const o = w.options || {};
  return {name, type: o.type || null, value, items: Array.isArray(o.items) ? o.items.length : null, loads_items: !!o.widgetUrl,
          depends_on: o.dependence || null};
})"""

# A form that never became ready (Liaoning's .cpt reports): what the page has instead. Names only, no session values.
_PAGE_JS = """() => { const p = window.contentPane, out = {FR: typeof FR, g: typeof _g, contentPane: typeof p, title: document.title};
  try { out.g_keys = typeof _g === 'function' && _g() ? Object.keys(_g()).filter(k => /param|report|page|widget/i.test(k)).slice(0, 20) : null; } catch (e) {}
  try { if (p) { out.parameterEl = !!p.parameterEl; out.currentPageIndex = p.currentPageIndex; out.reportTotalPage = p.reportTotalPage;
                 out.pane_keys = Object.keys(p).filter(k => /param|report|page/i.test(k)).slice(0, 20);
                 const el = p.parameterEl; if (el && el.name_widgets) out.pane_widgets = Object.keys(el.name_widgets); } } catch (e) {}
  out.inputs = [...document.querySelectorAll('input,select')].slice(0, 20).map(i => ({name: i.name || i.id || null, type: i.type}));
  out.table_cells = document.querySelectorAll('td').length;
  return out; }"""


# Opens one dropdown as a person would: with the queue installed its load waits to be seen, and is released on its own.
_COMBO_JS = """x => { const f = _g(), date = x.date_widget && f.parameterEl.getWidgetByName(x.date_widget);
  if (date && x.date) date.setValue(x.date);
  const w = f.parameterEl.getWidgetByName(x.name);
  if (!w) throw new Error('combo_missing');
  const methods = ['onTriggerClick', 'doClick', 'loadData', 'reload'].filter(m => typeof w[m] === 'function');
  if (typeof w.onTriggerClick === 'function') w.onTriggerClick();
  return methods; }"""


# A probe's one day: every field set, then the form's own way of asking (a .cpt report may only have its button).
_FILL_COMMIT_JS = """x => { const f = _g();
  for (const [name, value] of Object.entries(x)) {
    const w = f.parameterEl.getWidgetByName(name);
    if (!w) throw new Error('widget_missing');
    w.setValue(value);
  }
  if (typeof f.parameterCommit === 'function') { f.parameterCommit(); return 'parameterCommit'; }
  const button = f.parameterEl.getWidgetByName('SEARCH');
  if (button && button.fireEvent) { button.fireEvent('click'); return 'search_button'; }
  throw new Error('no_way_to_ask'); }"""


def _unsent(code: str, detail: dict | None = None) -> Fault:
    return Fault(code, "local", unsent=True, detail=detail)


async def _ready(frame, timeout: float, unit: str | None = UNIT) -> None:
    """The page's form is up; its queue is installed once per document. ``unit``: what its unit widget must say (None: it has none)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            await frame.evaluate(_INSTALL_JS)
            break
        except Exception:
            if loop.time() > deadline:
                raise _unsent("finereport_not_ready") from None
            await asyncio.sleep(0.2)
    if unit is None:
        return
    units = await frame.evaluate(_UNITS_JS)
    if unit not in units:
        raise _unsent("finereport_unit_changed", {"units": units[:3]})


async def open_report(page, request: dict, timeout: float, last: dict):
    """The report's frame, ready; its session is renewed first when it idled too long or is unknown."""
    source = request["frame"]
    code = (request.get("site_menu") or {}).get("source") or source
    found = ui.frames_matching(page, source)
    if len(found) > 1:
        raise _unsent("report_frame_ambiguous")
    if not found:
        try:
            frame = await ui.ensure_frame(page, request, timeout)
        except Fault as fault:
            raise _unsent(fault.code, fault.detail) from None
    elif time.monotonic() - last.get(source, float("-inf")) <= REOPEN_IDLE_SECONDS:
        frame = found[0]
    else:
        try:
            await page.evaluate(_REOPEN_JS, code)
        except Exception:
            raise _unsent("finereport_reopen_failed") from None
        last.pop(source, None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await asyncio.sleep(0.2)
        while len(found := ui.frames_matching(page, source)) != 1:
            if loop.time() > deadline:
                raise _unsent("report_frame_open_timeout")
            await asyncio.sleep(0.2)
        frame = found[0]
    await _ready(frame, timeout, (request.get("form") or {}).get("unit", UNIT))
    return frame


def _answers_stage(response, frame, stage: dict) -> bool:
    """The answer to this very queued request: one path also serves the form's images and scripts (a GIF, 2026-11-01)."""
    try:
        request = response.request
        if response.frame != frame or urlsplit(response.url).path != stage["path"] or request.method != stage["method"]:
            return False
        sent = {**parse_qs(urlsplit(response.url).query), **parse_qs(request.post_data or "")}
    except Exception:
        return False
    return all(stage.get(key) is None or sent.get(key, [None])[0] == stage[key] for key in ("op", "cmd"))


def _summary(raw: bytes) -> dict:
    """What an answer looks like, for a person to write a spec from. Nothing here is saved as data."""
    found = {"bytes": len(raw)}
    try:
        value = json.loads(raw)
    except ValueError:
        return {**found, "head": raw[:800].decode("utf-8", "replace")}
    if not isinstance(value, dict):
        return {**found, "json": type(value).__name__, "head": raw[:800].decode("utf-8", "replace")}
    found.update(keys=list(value)[:12], currentPage=value.get("currentPage"), totalPage=value.get("totalPage"))
    if isinstance(value.get("htmlTag"), str):
        rows = html_rows(value["htmlTag"])
        found.update(rows=len(rows), cols=sorted({len(r) for r in rows}), sample=[[c[:16] for c in r[:8]] for r in rows[:4]])
    return found


async def _follow(page, frame, form: dict, expect, timeout: float) -> dict:
    """One day asked as the form asks it, each request seen and released on its own: its stages, and what the last one answers."""
    fields = {form.get("date_widget", DATE_WIDGET): form["follow_day"], **(form.get("follow_fields") or {})}
    try:
        how = await frame.evaluate(_FILL_COMMIT_JS, fields)
    except Exception as exc:
        return {"error": str(exc)[:160]}
    loop = asyncio.get_running_loop()
    stages, last = [], None
    while len(stages) < 8:
        deadline = loop.time() + (timeout if not stages else 3)
        while not (queue := await frame.evaluate(_PEEK_JS)) and loop.time() < deadline:
            await asyncio.sleep(0.1)
        if not queue:
            break
        stage = queue[0]
        try:
            async with expect(page, "response", lambda r, s=stage: _answers_stage(r, frame, s), timeout=timeout * 1000) as captured:
                await frame.evaluate(_RELEASE_JS)
            answer = await captured.value
            raw = await answer.body()
        except Exception as exc:
            return {"how": how, "stages": stages, "error": type(exc).__name__, "at": stage}
        last = _summary(raw)
        stages.append({**stage, "http_status": answer.status, "bytes": len(raw)})
        await asyncio.sleep(STAGE_PAUSE_SECONDS)
    return {"how": how, "stages": stages, "last": last}


async def _combo_items(page, frame, name: str, form: dict, expect, timeout: float) -> dict:
    """The options one dropdown loads, by its own request (one, read-only). The answer is reported as it came, cut short."""
    try:
        methods = await frame.evaluate(_COMBO_JS, {"name": name, "date_widget": form.get("date_widget", DATE_WIDGET), "date": form.get("probe_date")})
    except Exception as exc:
        return {"name": name, "error": str(exc)[:120]}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(timeout, 5)
    queue = []
    while not (queue := await frame.evaluate(_PEEK_JS)) and loop.time() < deadline:
        await asyncio.sleep(0.1)
    found = {"name": name, "methods": methods, "queued": queue[:3]}
    if not queue:
        return found
    stage = queue[0]
    try:
        async with expect(page, "response", lambda r: _answers_stage(r, frame, stage), timeout=timeout * 1000) as captured:
            await frame.evaluate(_RELEASE_JS)
        answer = await captured.value
        raw = await answer.body()
        found.update(http_status=answer.status, bytes=len(raw), answer=raw[:30000].decode("utf-8", "replace"))
    except Exception as exc:
        found["error"] = type(exc).__name__
    await frame.evaluate("() => { window.__pmosFrQueue.length = 0; }")
    return found


async def probe(page, request: dict, expect, timeout: float) -> Response:
    """What a report's form asks for, without asking it: the mapping's answer and the form's widgets. No data request.

    A mapping refused (40302, as two of these were on 2026-10-16) is the answer, no fault: the widgets are then absent.
    """
    await ui.bring_forward(page)
    try:
        async with expect(page, "response", lambda r: urlsplit(r.url).path == MAPPING_PATH, timeout=timeout * 1000) as captured:
            await page.evaluate(ui._SITE_MENU_JS, request["site_menu"])
        answer = await captured.value
        raw = await answer.body()
    except Fault:
        raise
    except Exception as exc:
        raise _unsent("site_menu_entry_missing" if "site_menu_entry_missing" in str(exc) else "finereport_mapping_no_answer") from None
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    data = body.get("data") if isinstance(body, dict) else None
    url = data.get("url") if isinstance(data, dict) else None
    found = {"mapping": {"http_status": answer.status, "business_status": body.get("status") if isinstance(body, dict) else None,
                         "mapped": bool(url)}}
    if url:
        viewlet = parse_qs(urlsplit(url).query).get("viewlet", [None])[0]
        found["viewlet"] = viewlet
        found["path"] = urlsplit(url).path     # a .cpt report is not served from the form's path
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        frame = None
        while frame is None and loop.time() < deadline:
            frame = next((f for f in page.frames if parse_qs(urlsplit(f.url).query).get("viewlet", [None])[0] == viewlet), None)
            if frame is None:
                await asyncio.sleep(0.2)
        found["frame_found"] = frame is not None
        found["form_ready"] = False
        while frame is not None and loop.time() < deadline:
            try:
                await frame.evaluate(_INSTALL_JS)
                found["widgets"] = await frame.evaluate(_WIDGETS_JS)
                found["form_ready"] = True
                form = request.get("form") or {}
                if form.get("combos"):
                    found["combos"] = [await _combo_items(page, frame, name, form, expect, timeout) for name in form["combos"]]
                if form.get("follow_day"):
                    found["follow"] = await _follow(page, frame, form, expect, timeout)
                break
            except Exception:
                await asyncio.sleep(0.2)
        if frame is not None and not found["form_ready"]:
            try:
                found["page"] = await frame.evaluate(_PAGE_JS)
            except Exception as exc:
                found["page"] = {"error": type(exc).__name__}
    return Response(200, "application/json", json.dumps({"status": 0, "data": found}, ensure_ascii=False).encode())


async def _next_stage(frame, stage: tuple, timeout: float, *, first: bool) -> None:
    """Exactly this stage's request waits in the queue (the form queues it from its previous answer)."""
    cmd, op, method = stage
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not (queue := await frame.evaluate(_PEEK_JS)):
        if loop.time() > deadline:
            raise Fault("finereport_stage_missing", "local", unsent=first, detail={"stage": cmd})
        await asyncio.sleep(0.05)
    if queue != [{"path": PATH, "method": method, "op": op, "cmd": cmd}]:
        raise Fault("finereport_stage_changed", "local", unsent=first, detail={"stage": cmd, "queued": len(queue)})


def _is_stage(request, frame, cmd: str) -> bool:
    try:
        if request.frame != frame or urlsplit(request.url).path != PATH:
            return False
        names = parse_qs(urlsplit(request.url).query).get("cmd", []) + parse_qs(request.post_data or "").get("cmd", [])
    except Exception:
        return False
    return cmd in names


async def query_day(page, frame, date: str, expect, timeout: float, last: dict, source: str, *, widget: str = DATE_WIDGET) -> Response:
    """The three stages of one day; the last answer, or the first that was no answer to go on from."""
    if await frame.evaluate(_PEEK_JS):
        last.pop(source, None)
        raise _unsent("finereport_queue_not_empty")
    try:
        await frame.evaluate(_COMMIT_JS, {"date": date, "widget": widget})
    except Exception:
        raise _unsent("finereport_date_not_applied") from None
    for index, stage in enumerate(STAGES):
        cmd = stage[0]
        await _next_stage(frame, stage, timeout, first=index == 0)
        try:
            async with expect(page, "response", lambda r, c=cmd: _is_stage(r.request, frame, c), timeout=timeout * 1000) as captured:
                await frame.evaluate(_RELEASE_JS)
            answer = await captured.value
            raw = await answer.body()
        except Fault:
            raise
        except Exception:
            last.pop(source, None)            # its state is unknown: the next day opens a new session
            raise Fault("network_error", "network") from None
        last[source] = time.monotonic()
        if answer.status != 200:
            return Response(answer.status, answer.headers.get("content-type", ""), raw)
        if SESSION_END in raw:
            last.pop(source, None)
            raise _unsent("finereport_session_ended", {"stage": cmd})
        if cmd == "parameters_d":
            try:
                settled = json.loads(raw) == {"status": "success"}
            except ValueError:
                settled = False
            if not settled:
                last.pop(source, None)
                raise Fault("finereport_parameter_result_changed", "site_data", http_status=200)
        elif cmd == "load_content" and not (raw.startswith(b"{") and b'"elementCases"' in raw):
            last.pop(source, None)
            raise Fault("finereport_report_structure_changed", "site_data", http_status=200)
        elif cmd == "load_report_content":
            return Response(200, "application/json", raw)
        await asyncio.sleep(STAGE_PAUSE_SECONDS)
    raise Fault("finereport_stage_missing", "local")
