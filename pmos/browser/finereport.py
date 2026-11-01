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
from ..finereport import PATH, SESSION_END, STAGES
from ..validate import Response
from . import ui

REOPEN_IDLE_SECONDS = 600
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

_COMMIT_JS = """date => { const f = _g(), v = f.parameterEl.getWidgetByName('日期'); v.setValue(date);
  if (v.getValue() !== date) throw new Error('date_not_applied'); f.parameterCommit(); return v.getValue(); }"""

_RELEASE_JS = "() => { const o = window.__pmosFrQueue.shift(); window.__pmosFrAjax.call(FR, o); return true; }"


def _unsent(code: str, detail: dict | None = None) -> Fault:
    return Fault(code, "local", unsent=True, detail=detail)


async def _ready(frame, timeout: float) -> None:
    """The page's form is up; its queue is installed once per document."""
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
    units = await frame.evaluate(_UNITS_JS)
    if "单位:MW" not in units:
        raise _unsent("finereport_unit_changed", {"units": units[:3]})


async def open_report(page, request: dict, timeout: float, last: dict):
    """The report's frame, ready; its session is renewed first when it idled too long or is unknown."""
    source = request["frame"]
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
            await page.evaluate(_REOPEN_JS, source)
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
    await _ready(frame, timeout)
    return frame


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


async def query_day(page, frame, date: str, expect, timeout: float, last: dict, source: str) -> Response:
    """The three stages of one day; the last answer, or the first that was no answer to go on from."""
    if await frame.evaluate(_PEEK_JS):
        last.pop(source, None)
        raise _unsent("finereport_queue_not_empty")
    try:
        await frame.evaluate(_COMMIT_JS, date)
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
