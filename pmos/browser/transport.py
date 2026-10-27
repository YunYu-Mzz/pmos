"""Send one dataset request through the logged-in page.

Three transports cover every adapted PMOS report so far:

``fetch``      Replay the page's own request *template* (frame + gateway headers
               such as X-Token/CurrentRoute, captured when the human or the page
               made the same call) with a new body, via ``fetch`` inside that
               frame. Cookies are sent by the browser. Used for exports/APIs
               whose headers stay valid across bodies (Shanxi).
``component``  Set parameters on the report's own Vue component and call its
               query method, then read the network response it triggers. The
               front end computes every token/signature itself (Liaoning).
``native``     Call a function of the page's own webpack bundle (``module`` id,
               ``export`` name, ``args``) and read the response it triggers.
               The site's file helper adds its own SSO exchange and signature;
               used for disclosure attachments (Shanxi, Liaoning).
``ui``         Fill the report form like a human (date input, selects), click
               查询 and, for exports, 导出; keep the query response or the
               downloaded file (Anhui). ``list`` reads an article listing page
               (table + el-pagination) into JSON; ``record`` opens one article
               dialog and returns each attachment it offers (or its images, or
               its text when it has neither) as parts of one response.

``component``, ``native`` and ``ui`` open the report frame through the site
menu (``request.menu``) or ``request.open_url`` when it is not already open.
While a component or native call runs, the page's own "save file" anchor
clicks and ``window.open`` are suppressed, so nothing lands in the browser's
download folder or opens a tab; the bytes come from the network response.

Templates hold auth headers in memory only; they are never written to disk.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .. import platform
from ..faults import Fault
from ..validate import CAPTURE_TYPE, Response
from . import ui

MAX_RESPONSE_BYTES = 64 * 1024 * 1024

_FETCH_JS = """async args => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), args.timeout);
  try {
    const init = {method: args.method, headers: args.headers, credentials: 'include',
                  redirect: 'error', cache: 'no-store', signal: controller.signal};
    if (args.body !== null) init.body = args.body;
    const response = await fetch(args.url, init);
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.length > args.limit) throw new Error('response_size_limit');
    let raw = '';
    for (let i = 0; i < bytes.length; i += 16384) raw += String.fromCharCode(...bytes.subarray(i, i + 16384));
    return {status: response.status, contentType: response.headers.get('content-type') || '', base64: btoa(raw)};
  } finally { clearTimeout(timer); }
}"""

# Vue 2 component lookup shared by the component scripts: by component name
# (case-insensitive; "*" = any name) and optional exact field values, e.g.
# {"viewId": "<menu guid>"} or {"queryUrl": "<the path it queries>"}.
_FIND = """
  const pmosFind = (name, match) => {
    const roots = [...document.querySelectorAll('*')].map(e => e.__vue__).filter(Boolean);
    const seen = new Set(), found = [];
    const walk = v => { if (!v || seen.has(v)) return; seen.add(v);
      if (v.$options && (name === '*' || String(v.$options.name || '').toLowerCase() === name.toLowerCase())
          && Object.entries(match || {}).every(([k, x]) => JSON.stringify(v[k]) === JSON.stringify(x))) found.push(v);
      (v.$children || []).forEach(walk); };
    roots.forEach(r => walk(r.$root || r));
    return found;
  };
  const pmosOne = (name, match) => {
    const found = pmosFind(name, match);
    if (found.length !== 1) throw new Error(found.length ? 'component_ambiguous' : 'component_missing');
    return found[0];
  };
  const pmosSet = (vm, values) => {
    for (const [path, value] of Object.entries(values || {})) {
      const keys = path.split('.'); let target = vm;
      for (const key of keys.slice(0, -1)) {
        if (target[key] === undefined || target[key] === null) throw new Error('component_param_missing');
        target = target[key];
      }
      target[keys[keys.length - 1]] = value;
    }
  };
"""

_COMPONENT_JS = "args => {" + _FIND + """
  const vm = pmosOne(args.component, args.match);
  pmosSet(vm, args.set);
  if (typeof vm[args.call] !== 'function') throw new Error('component_method_missing');
  const result = vm[args.call](...(args.args || []));
  if (result && typeof result.catch === 'function') result.catch(() => {});
  return true;
}"""

_CHECK_JS = "cond => {" + _FIND + """
  const found = pmosFind(cond.component, cond.match);
  return found.length === 1
    && Object.entries(cond.equals || {}).every(([k, x]) => JSON.stringify(found[0][k]) === JSON.stringify(x));
}"""

# One ``prepare`` step: set fields, emit on the page's event bus, call a method.
_STEP_JS = "async step => {" + _FIND + """
  const vm = pmosOne(step.component, step.match);
  if (step.set) pmosSet(vm, step.set);
  if (step.emit !== undefined) {
    const bus = vm[step.bus || '$EventBus'];
    if (!bus || typeof bus.$emit !== 'function') throw new Error('component_bus_missing');
    bus.$emit(step.emit, step.payload);
  }
  if (step.call !== undefined) {
    if (typeof vm[step.call] !== 'function') throw new Error('component_method_missing');
    const result = vm[step.call](...(step.args || []));
    if (step.await) await result;
    else if (result && typeof result.catch === 'function') result.catch(() => {});
  }
  return true;
}"""


# Installed once per frame; active only while window.__pmosQuietUntil lies ahead.
_QUIET_JS = """ms => {
  if (!window.__pmosQuietInstalled) {
    const quiet = () => (window.__pmosQuietUntil || 0) > Date.now();
    const click = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () {
      if (quiet() && (this.hasAttribute('download') || /^blob:|^data:/.test(this.href || ''))) return;
      return click.apply(this, arguments);
    };
    const open = window.open;
    window.open = function () { return quiet() ? null : open.apply(this, arguments); };
    window.__pmosQuietInstalled = true;
  }
  window.__pmosQuietUntil = Date.now() + ms;
  return true;
}"""

_NATIVE_JS = """args => {
  if (typeof window.__pmosRequire !== 'function') {
    const chunks = window.webpackJsonp;
    if (!chunks || typeof chunks.push !== 'function') throw new Error('webpack_runtime_missing');
    const id = 'pmos_require_' + Date.now();
    chunks.push([[], {[id]: (module, exports, require) => { window.__pmosRequire = require; }}, [[id]]]);
    if (typeof window.__pmosRequire !== 'function') throw new Error('webpack_require_unavailable');
  }
  let exported;
  try { exported = window.__pmosRequire(args.module); } catch (e) { throw new Error('native_module_missing'); }
  const fn = args.export ? (exported && (exported[args.export] || (exported.default || {})[args.export]))
                         : (exported && (exported.default || exported));
  if (typeof fn !== 'function') throw new Error('native_export_missing');
  const result = fn(...(args.args || []));
  if (result && typeof result.catch === 'function') result.catch(() => {});
  return true;
}"""


class Templates:
    """Latest observed request per site path, captured from the page itself."""

    def __init__(self, origin: str, clock=time.monotonic):
        self.origin = origin
        self.clock = clock
        self.items: dict[str, dict] = {}
        self.sending = 0
        self.pending: set = set()

    def observe(self, context, spawn) -> None:
        context.on("request", lambda request: self._request(request, spawn))

    def _request(self, request, spawn) -> None:
        if self.sending:
            return  # our own replays must not overwrite the page's template
        parsed = urlsplit(request.url)
        if (f"{parsed.scheme}://{parsed.netloc}" != self.origin or request.method not in ("GET", "POST")
                or parsed.path in platform.NATIVE_BACKGROUND_PATHS or request.resource_type not in ("xhr", "fetch")):
            return
        spawn(self._capture(request, parsed.path))

    async def _capture(self, request, path: str) -> None:
        try:
            headers = {k.lower(): v for k, v in (await request.all_headers()).items()
                       if k.lower() in platform.REPLAY_HEADERS}
            self.items[path] = {"frame": request.frame, "headers": headers, "method": request.method,
                                "body": request.post_data, "at": self.clock()}
        except Exception:
            pass  # a vanished frame simply leaves no template

    def get(self, path: str, max_age: float | None = None) -> dict:
        template = self.items.get(path)
        if template is None:
            raise Fault("request_template_missing", "local")
        if max_age is not None and self.clock() - template["at"] > max_age:
            raise Fault("request_template_stale", "local")
        if template["frame"].is_detached():
            raise Fault("request_template_frame_detached", "local")
        return template


class PageTransport:
    def __init__(self, page, origin: str, templates: Templates, timeout_seconds: float,
                 scripts: dict[str, str] | None = None):
        self.page = page
        self.origin = origin
        self.templates = templates
        self.timeout = timeout_seconds
        self.scripts = scripts or {}

    def ready(self, request: dict) -> bool:
        """Could ``send`` run now without a local failure? Never sends anything."""
        if request["transport"] != "fetch":
            return True
        template = self.templates.items.get(request.get("template", request["path"]))
        if template is None:
            return False
        max_age = request.get("max_template_age")
        if max_age is not None and self.templates.clock() - template["at"] > max_age:
            return False
        try:
            return not template["frame"].is_detached()
        except Exception:
            return False

    async def send(self, request: dict) -> Response:
        kind = request["transport"]
        if kind == "fetch":
            return await self._fetch(request)
        if kind in ("component", "native"):
            return await self._call(request)
        if kind == "ui":
            return await self._ui(request)
        raise Fault("transport_unknown", "local")

    async def _fetch(self, request: dict) -> Response:
        template = self.templates.get(request.get("template", request["path"]), request.get("max_template_age"))
        url = self.origin + request["path"]
        if request.get("query"):
            url += "?" + urlencode(request["query"])
        if "body" in request:
            body = json.dumps(request["body"], ensure_ascii=False, separators=(",", ":"))
        else:
            body = template["body"] if request["method"] == "POST" else None
        self.templates.sending += 1
        try:
            result = await template["frame"].evaluate(_FETCH_JS, {
                "url": url, "method": request["method"], "headers": template["headers"], "body": body,
                "timeout": int(self.timeout * 1000), "limit": MAX_RESPONSE_BYTES})
        except Exception:
            # Timeout, abort, redirect or a dropped connection: the server may
            # or may not have handled it. The ledger keeps it unknown.
            raise Fault("network_error", "network") from None
        finally:
            self.templates.sending -= 1
        return Response(int(result["status"]), result["contentType"], base64.b64decode(result["base64"]))

    async def _call(self, request: dict) -> Response:
        """Trigger the page's own code and keep the response it causes on ``path``."""
        frame = await ui.ensure_frame(self.page, request, self.timeout)
        if request.get("prepare"):
            await self._prepare(frame, request["prepare"])
        if request["transport"] == "component":
            target = {"component": request["component"], "match": request.get("match")}
            await self._wait_for(frame, target, "component_missing")
            script, args = _COMPONENT_JS, {**target, "set": request.get("set", {}), "call": request["call"],
                                           "args": request.get("args", [])}
        else:
            script, args = _NATIVE_JS, {"module": request["module"], "export": request.get("export"),
                                        "args": request.get("args", [])}
        path = request["path"]
        try:
            await frame.evaluate(_QUIET_JS, int(self.timeout * 1000) + 5000)
            async with self.page.expect_response(
                    lambda r: urlsplit(r.url).path == path and r.request.method == request["method"],
                    timeout=self.timeout * 1000) as captured:
                try:
                    await frame.evaluate(script, args)
                except Exception as exc:
                    raise Fault(_page_error(exc, "page_call_failed"), "local") from None
            response = await captured.value
            body = await response.body()
        except Fault:
            raise
        except Exception:
            raise Fault("network_error", "network") from None
        finally:
            try:
                await frame.evaluate(_QUIET_JS, 3000)  # the save click follows the response
            except Exception:
                pass
        return Response(response.status, response.headers.get("content-type", ""), body)

    async def _prepare(self, frame, steps: list) -> None:
        """Bring the page into the state the call needs (mount a menu's view ...).

        Steps run in order; ``unless`` skips a step whose effect is already
        visible, so an open view is not re-initialized for every page.
        """
        for step in steps:
            if "wait" in step:
                await self._wait_for(frame, step["wait"], "prepare_wait_timeout")
            elif "sleep" in step:
                await asyncio.sleep(step["sleep"])
            elif not ("unless" in step and await self._check(frame, step["unless"])):
                if "script" in step:
                    if step["script"] not in self.scripts:
                        raise Fault("page_script_missing", "local")
                    script, argument = self.scripts[step["script"]], step.get("args", {})
                else:
                    script, argument = _STEP_JS, {k: v for k, v in step.items() if k != "unless"}
                try:
                    await asyncio.wait_for(frame.evaluate(script, argument), self.timeout)
                except asyncio.TimeoutError:
                    raise Fault("prepare_step_timeout", "local") from None
                except Exception as exc:
                    raise Fault(_page_error(exc, "prepare_step_failed"), "local") from None
        await ui.settled(frame, self.timeout)

    async def _check(self, frame, condition: dict) -> bool:
        try:
            return bool(await frame.evaluate(_CHECK_JS, condition))
        except Exception:
            return False

    async def _wait_for(self, frame, condition: dict, code: str) -> None:
        """A freshly opened view mounts its component a moment after load."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while loop.time() < deadline:
            if await self._check(frame, condition):
                return
            await asyncio.sleep(0.2)
        raise Fault(code, "local")

    async def _ui(self, request: dict) -> Response:
        frame = await ui.ensure_frame(self.page, request, self.timeout)
        if request["action"] in ("list", "record"):
            try:
                if request.get("menu"):
                    await ui.select_menu(self.page, request["menu"], self.timeout,
                                         hover_exit=bool(request.get("menu_hover_exit")))
                    await ui.settled(frame, self.timeout)
                if request["action"] == "list":
                    return await self._ui_list(frame, request)
                return await self._ui_record(frame, request)
            except Fault:
                raise
            except Exception:
                raise Fault("network_error", "network") from None
        for field in request.get("fields", []):
            await ui.fill(frame, field, self.timeout)

        def from_report(response) -> bool:
            req = response.request
            path = urlsplit(req.url).path
            return (req.frame == frame and req.resource_type in ("xhr", "fetch")
                    and path not in platform.NATIVE_BACKGROUND_PATHS
                    and (not request.get("path") or path == request["path"]))

        try:
            async with self.page.expect_response(from_report, timeout=self.timeout * 1000) as captured:
                await (await ui.button(frame, request.get("query_button", "查询"))).click()
            query = await captured.value
            query_body = await query.body()
            await ui.settled(frame, self.timeout)
            if request["action"] == "query":
                return Response(query.status, query.headers.get("content-type", ""), query_body)
            async with self.page.expect_download(timeout=self.timeout * 1000) as event:
                await (await ui.button(frame, request.get("export_button", "导出"))).click()
            return await self._downloaded(await event.value)
        except Fault:
            raise
        except Exception:
            raise Fault("network_error", "network") from None

    async def _downloaded(self, download, name: str = "") -> Response:
        if await download.failure():
            raise Fault("download_failed", "network")
        if not (download.url.startswith(self.origin + "/") or download.url.startswith("blob:" + self.origin + "/")):
            raise Fault("download_origin_changed", "unknown")
        payload = Path(await download.path()).read_bytes()
        suffix = Path(download.suggested_filename).suffix.lower()
        return Response(200, DOWNLOAD_TYPES.get(suffix, "application/octet-stream"), payload,
                        name=download.suggested_filename or name)

    async def _ui_list(self, frame, request: dict) -> Response:
        """One listing page as JSON: rows {id, title, published, page}, total, last."""
        target = request.get("page", 1)
        await ui.goto_page(frame, target, self.timeout)
        rows = await ui.list_rows(frame, request.get("columns"))
        state = await ui.pager(frame)
        if not rows and state and state["total"]:
            raise Fault("listing_rows_changed", "site_data")
        page = (state or {}).get("page") or target
        for row in rows:
            row.update(id=hashlib.sha256(f"{row['title']}\n{row['published']}".encode()).hexdigest()[:16], page=page)
        body = {"status": 0, "data": {"list": rows, "page": page,
                                      "total": state["total"] if state and state["total"] is not None else len(rows),
                                      "last": state is None or state["next_disabled"]}}
        return Response(200, "application/json", json.dumps(body, ensure_ascii=False).encode())

    async def _ui_record(self, frame, request: dict) -> Response:
        record = request["record"]
        selector = request.get("dialog", ".el-dialog:visible")
        box = await ui.open_record(frame, record, self.timeout, columns=request.get("columns"), dialog=selector)
        skip = set(request.get("skip_buttons", ["预览"]))
        names = [n.strip() for n in await box.locator("button:visible").all_text_contents()]
        names = [n for n in names if n and n not in skip]
        if len(set(names)) != len(names):
            raise Fault("record_attachment_ambiguous", "local")
        parts = []
        for name in names:
            async with self.page.expect_download(timeout=self.timeout * 1000) as event:
                await box.get_by_role("button", name=name, exact=True).click(timeout=self.timeout * 1000)
            parts.append(await self._downloaded(await event.value, name))
        if not parts:
            sources = await box.locator("img").evaluate_all(_IMAGES_JS)
            for number, source in enumerate(dict.fromkeys(sources), 1):
                if f"{urlsplit(source).scheme}://{urlsplit(source).netloc}" != self.origin:
                    raise Fault("record_image_origin_changed", "unknown")
                result = await frame.evaluate(_FETCH_JS, {"url": source, "method": "GET", "headers": {}, "body": None,
                                                          "timeout": int(self.timeout * 1000), "limit": MAX_RESPONSE_BYTES})
                if int(result["status"]) != 200:
                    raise Fault("record_image_download_failed", "unknown", http_status=int(result["status"]))
                parts.append(Response(200, result["contentType"], base64.b64decode(result["base64"]), name=f"image{number}"))
        if not parts:
            text = await box.inner_text()
            rest = re.sub(r"[\s\ue000-\uf8ff]", "", text.replace(record["title"], "").replace(record["published"], ""))
            if not rest:
                raise Fault("record_detail_unavailable", "site_data")
            tables = await box.locator("table:visible").evaluate_all(_TABLES_JS)
            capture = {"title": record["title"], "published": record["published"], "text": text, "tables": tables}
            parts.append(Response(200, CAPTURE_TYPE, json.dumps(capture, ensure_ascii=False).encode(), name="body.json"))
        await ui.close_dialog(frame, selector, self.timeout)
        return Response(200, "application/x-pmos-parts", b"", parts=tuple(parts))


_IMAGES_JS = """images => images.filter(i => i.complete && i.naturalWidth >= 200 && i.naturalHeight >= 100
  && i.getClientRects().length).map(i => i.currentSrc)"""
_TABLES_JS = """tables => tables.map(t => [...t.querySelectorAll('tr')].map(r => [...r.querySelectorAll('th,td')].map(c => c.innerText.trim())))"""


def _page_error(exc: Exception, default: str) -> str:
    """The code a page script threw (``throw new Error('component_missing')``)."""
    match = re.search(r"Error: ([a-z][a-z0-9_]{2,63})\b", str(exc))
    return match.group(1) if match else default


DOWNLOAD_TYPES = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                  ".xls": "application/vnd.ms-excel", ".pdf": "application/pdf", ".json": "application/json",
                  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  ".doc": "application/msword", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".zip": "application/zip"}
