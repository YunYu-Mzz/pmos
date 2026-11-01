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
from ..faults import DriverLost, Fault, driver_gone
from ..validate import CAPTURE_TYPE, Response
from . import challenge, finereport, ui

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
# While a PMOS call runs, the page's "save file" click is suppressed and the
# Blob it was about to save is handed to PMOS instead: the site builds it from
# its own arraybuffer response, which CDP's response.body() can corrupt for
# charset-labelled binary exports (Shanxi production saves the download for
# the same reason).
_QUIET_JS = """ms => {
  if (window.__pmosQuietInstalled !== 3) {
    const quiet = () => (window.__pmosQuietUntil || 0) > Date.now();
    const blobs = new Map();
    const create = URL.createObjectURL;
    URL.createObjectURL = function (object) {
      const url = create.apply(this, arguments);
      if (quiet() && object instanceof Blob) {
        blobs.set(url, object);
        while (blobs.size > 4) blobs.delete(blobs.keys().next().value);
      }
      return url;
    };
    const click = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () {
      if (quiet() && (this.hasAttribute('download') || /^blob:|^data:/.test(this.href || ''))) {
        const blob = blobs.get(this.href), name = this.getAttribute('download') || '';
        if (blob) {
          blobs.delete(this.href);
          // The page never revokes its download URLs: each file stayed in the
          // renderer for good. The Blob object itself stays readable here.
          try { URL.revokeObjectURL(this.href); } catch (e) {}
          window.__pmosBlob = {pending: true, name};
          blob.arrayBuffer().then(buffer => {
            const bytes = new Uint8Array(buffer); let raw = '';
            for (let i = 0; i < bytes.length; i += 16384) raw += String.fromCharCode(...bytes.subarray(i, i + 16384));
            window.__pmosBlob = {name, size: bytes.length, base64: btoa(raw)};
          }, () => { window.__pmosBlob = {name, error: 'blob_read_failed'}; });
        }
        return;
      }
      return click.apply(this, arguments);
    };
    const open = window.open;
    window.open = function () { return quiet() ? null : open.apply(this, arguments); };
    window.__pmosQuietInstalled = 3;
  }
  window.__pmosQuietUntil = Date.now() + ms;
  return true;
}"""

_NATIVE_RESOLVE = """
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
"""

_NATIVE_JS = "args => {" + _NATIVE_RESOLVE + """
  const result = fn(...(args.args || []));
  if (result && typeof result.catch === 'function') result.catch(() => {});
  return true;
}"""

# Preflight only: the helper must already be loaded in this frame. A module of
# a lazily loaded chunk (Shanxi's file helper 63a1 arrives with the announcement
# dialog) is missing until the page itself loads it.
_NATIVE_PROBE_JS = "args => {" + _NATIVE_RESOLVE + "  return true;\n}"


class _Expect:
    """``page.expect_response`` / ``expect_download`` without Playwright's Waiter.

    The Waiter sends ``__waitInfo__`` messages that expect no reply, yet the
    1.63 Python client keeps each one's callback (with its stack) in the
    connection and the driver never answers: two leaked per wait, for the life
    of the connection (654 after 327 Shanxi calls). A plain listener for the
    duration of the wait leaves nothing behind.
    """

    def __init__(self, page, event: str, predicate=None, *, timeout: float):
        self.page, self.event, self.timeout = page, event, timeout / 1000
        self.predicate = predicate or (lambda value: True)

    def _on(self, value) -> None:
        if self.future.done():
            return
        try:
            if self.predicate(value):
                self.future.set_result(value)
        except Exception as exc:
            self.future.set_exception(exc)

    def _close(self) -> None:
        self.page.remove_listener(self.event, self._on)
        if not self.future.done():
            self.future.cancel()

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        self.future, self.deadline = loop.create_future(), loop.time() + self.timeout
        self.page.on(self.event, self._on)
        return self

    async def __aexit__(self, exc_type, *exc):
        if exc_type is not None:
            self._close()
        return False

    @property
    def value(self):
        """Awaitable, like Playwright's: the first matching event within the timeout."""
        return self._value()

    async def _value(self):
        try:
            remaining = self.deadline - asyncio.get_running_loop().time()
            return await asyncio.wait_for(self.future, max(remaining, 0.001))
        finally:
            self._close()


class Templates:
    """Latest observed request per site path, captured from the page itself.

    Listening to the context's requests makes the Playwright driver keep an
    object per network request (the Shanxi collector's memory growth). By
    default PMOS therefore listens only inside :meth:`window`, i.e. while one
    of its own page operations runs (a bootstrap export, a component call):
    the page's own request is captured as a template there, and background
    traffic between operations creates nothing. ``always=True`` keeps the
    old behaviour for sources whose template only a human click provides.
    """

    def __init__(self, origin: str, clock=time.monotonic, *, max_items: int = 128, ttl: float = 900):
        self.origin = origin
        self.clock = clock
        self.items: dict[str, dict] = {}
        self.shapes: dict[str, dict[str, dict]] = {}     # path -> body shape -> newest template of it
        self.sending = 0
        self.pending: set = set()
        self.max_items, self.ttl = max_items, ttl
        self._context = self._listener = None
        self._spawn = None
        self._always = False
        self._windows = 0

    def observe(self, context, spawn, *, always: bool = False) -> None:
        self.detach()
        self._context, self._spawn, self._always = context, spawn, always
        if always:
            self._listen()

    @property
    def listening(self) -> bool:
        return self._listener is not None

    def _listen(self) -> None:
        if self._context is not None and self._listener is None:
            self._listener = lambda request: self._request(request, self._spawn)
            self._context.on("request", self._listener)

    def _unlisten(self) -> None:
        if self._context is not None and self._listener is not None:
            self._context.remove_listener("request", self._listener)
        self._listener = None

    def window(self):
        """Listen for the duration of one PMOS page operation (nests)."""
        templates = self

        class _Window:
            def __enter__(self):
                templates._windows += 1
                templates._listen()

            def __exit__(self, *exc):
                templates._windows -= 1
                if templates._windows == 0 and not templates._always:
                    templates._unlisten()
                return False
        return _Window()

    def detach(self) -> None:
        self._unlisten()
        self._context = self._spawn = None
        self._always, self._windows = False, 0
        self.items.clear()
        self.shapes.clear()

    def prune(self, *, keep_detached: bool = False) -> None:
        for path, template in list(self.items.items()):
            if self.clock() - template["at"] > self.ttl or (_frame_gone(template["frame"]) and not keep_detached):
                self.items.pop(path, None)
        for path, shapes in list(self.shapes.items()):
            for shape, template in list(shapes.items()):
                if self.clock() - template["at"] > self.ttl or (_frame_gone(template["frame"]) and not keep_detached):
                    shapes.pop(shape, None)
            if not shapes or path not in self.items:
                self.shapes.pop(path, None)
        while len(self.items) > self.max_items:
            self.items.pop(min(self.items, key=lambda key: self.items[key]["at"]))

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
            # Do not retain request objects or their driver backing buffers. The page's own JSON body
            # is kept as a short text copy for a template_body_set replay (Anhui: the date and options
            # change, the page's other fields stay); a replay without it never sends one ("body").
            try:
                text = request.post_data if request.method == "POST" else None
            except Exception:
                text = None                       # a binary body: the template is kept, without one
            page_body = text if isinstance(text, str) and len(text) <= MAX_PAGE_BODY_CHARS else None
            template = {"frame": request.frame, "headers": headers, "method": request.method,
                        "body": None, "page_body": page_body, "at": self.clock()}
            self.items[path] = template
            # Reports sharing an endpoint send different bodies (Anhui 2026-11-01 14:04: 6d80's replay found
            # another report's body, template_body_field_missing): the newest of each body shape is kept too.
            shape = _body_shape(page_body)
            if shape is not None:
                shapes = self.shapes.setdefault(path, {})
                shapes.pop(shape, None)
                shapes[shape] = template
                while len(shapes) > MAX_BODY_SHAPES:
                    shapes.pop(next(iter(shapes)))
            self.prune()
        except Exception:
            pass  # a vanished frame simply leaves no template

    def get(self, path: str, max_age: float | None = None, *, fallback_frame=None, fields=None) -> dict:
        """``fallback_frame``: a template whose frame is gone is sent from it (its headers are the session's).

        ``fields`` (a template_body_set's dotted paths): the newest template of this path whose page body
        has them all.
        """
        self.prune(keep_detached=fallback_frame is not None)
        template = self.items.get(path)
        if fields and template is not None and not _has_fields(template.get("page_body"), fields):
            fitting = [t for t in self.shapes.get(path, {}).values() if _has_fields(t.get("page_body"), fields)]
            template = max(fitting, key=lambda t: t["at"]) if fitting else template
        if template is None:
            raise Fault("request_template_missing", "local")
        if max_age is not None and self.clock() - template["at"] > max_age:
            raise Fault("request_template_stale", "local")
        if _frame_gone(template["frame"]):
            if fallback_frame is None:
                raise Fault("request_template_frame_detached", "local")
            return {**template, "frame": fallback_frame}
        return template


MAX_BODY_SHAPES = 4


def _body_shape(text):
    try:
        body = json.loads(text) if text else None
    except ValueError:
        return None
    return ",".join(sorted(body)) if isinstance(body, dict) else None


def _has_fields(text, fields) -> bool:
    try:
        body = json.loads(text) if text else None
    except ValueError:
        return False
    for path in fields:
        target = body
        for key in path.split("."):
            if not isinstance(target, dict) or key not in target:
                return False
            target = target[key]
    return True


def _frame_gone(frame) -> bool:
    """Detached, or on a page that was closed: Playwright keeps a closed page's main frame attached
    (Anhui 2026-11-01 13:42/13:55: replays after a page recycle hit the old page, "Target page … closed")."""
    try:
        if frame.is_detached():
            return True
        page = getattr(frame, "page", None)
        return bool(page is not None and page.is_closed())
    except Exception:
        return True


class RequestGate:
    """Owned-frame request gate for one PMOS page operation (Shanxi production's design).

    While a call runs, every request from the operation's frame (or its child
    frames) is decided here: the one expected business request (method +
    path) passes once, SSO/gateway exchanges and static assets pass, and
    everything else -- a component's own mount-time query, a second business
    request, another origin -- is aborted and recorded. One operation can
    therefore never send more business requests than the ledger recorded.
    """

    def __init__(self, page, origin: str, frame, expected: tuple[str, str] | None):
        self.page, self.origin, self.frame, self.expected = page, origin, frame, expected
        self.counts = {"business": 0, "auth": 0, "static": 0, "blocked": 0}
        self.blocked: list[str] = []
        self.closed = False

    def _ours(self, frame) -> bool:
        while frame is not None:
            if frame == self.frame:
                return True
            frame = frame.parent_frame
        return False

    async def handle(self, route) -> None:
        request = route.request
        try:
            frame = request.frame
        except Exception:
            frame = None  # service-worker requests belong to no frame
        if frame is None or not self._ours(frame):
            return await route.continue_()
        parsed = urlsplit(request.url)
        if parsed.scheme in ("data", "blob"):
            return await route.continue_()
        if f"{parsed.scheme}://{parsed.netloc}" == self.origin:
            if parsed.path in platform.GATE_AUTH_PATHS:
                self.counts["auth"] += 1
                return await route.continue_()
            if request.method in ("GET", "HEAD") and request.resource_type in platform.GATE_STATIC_TYPES:
                self.counts["static"] += 1
                return await route.continue_()
            if self.expected == (request.method, parsed.path) and self.counts["business"] == 0 and not self.closed:
                self.counts["business"] += 1
                return await route.continue_()
        self.counts["blocked"] += 1
        if len(self.blocked) < 20:
            self.blocked.append(f"{request.method} {parsed.path}")
        await route.abort("blockedbyclient")

    async def __aenter__(self):
        await self.page.context.route("**/*", self.handle)
        return self

    async def __aexit__(self, *exc):
        try:
            await self.page.context.unroute("**/*", self.handle)
        except Exception:
            pass
        return False

    def summary(self) -> dict:
        return {**self.counts, "blocked_paths": list(self.blocked)}

    async def unsent(self) -> bool:
        """After a failed page call: did no business request get out?

        The gate closes first, so a request the failed call may still have
        started is aborted when it reaches the route; then the count decides.
        """
        self.closed = True
        await asyncio.sleep(0.5)
        return self.counts["business"] == 0


class _NoGate:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def unsent(self) -> bool:
        return False  # without a gate nothing proves it

    def summary(self):
        return None


def _lost_or_network(exc: Exception):
    """A dead driver is no network outcome: the worker must restart (DriverLost); anything else may have been sent."""
    if driver_gone(exc):
        return DriverLost(str(exc)[:200])
    return Fault("network_error", "network")


def _template_body(raw, changes: dict) -> str:
    """The template's JSON body with ``changes`` (dotted paths that must already exist) applied."""
    try:
        body = json.loads(raw or "")
    except (TypeError, ValueError):
        raise Fault("template_body_not_json", "local", unsent=True) from None
    for path, value in changes.items():
        keys, target = path.split("."), body
        for key in keys[:-1]:
            target = target.get(key) if isinstance(target, dict) else None
        if not isinstance(target, dict) or keys[-1] not in target:
            raise Fault("template_body_field_missing", "local", unsent=True)
        target[keys[-1]] = value
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


# A page's own request body kept for a template_body_set replay (Anhui bodies are a few hundred bytes).
MAX_PAGE_BODY_CHARS = 65536
# After a portal re-entry the page finishes its own load requests (SSO token exchange) first.
PORTAL_REENTRY_SETTLE_SECONDS = 8


class PageTransport:
    def __init__(self, page, origin: str, templates: Templates, timeout_seconds: float,
                 scripts: dict[str, str] | None = None, *, gate: bool = False, main_frame_fallback: bool = False):
        self.page = page
        # Anhui (legacy node_price_whole_day / native_tables.fetch): a template outlives its page; it is
        # sent from the current page's main frame once its own frame is gone (a recycled or crashed page).
        self.main_frame_fallback = main_frame_fallback
        self.origin = origin
        self.templates = templates
        self.timeout = timeout_seconds
        self.scripts = scripts or {}
        self.gate = gate
        # A renderer crash: every later call would fail in 0 s on the dead page
        # (Anhui 2026-10-31 17:05: 14 items in a row), so nothing is sent until
        # the engine reloads it.
        self.crashed = False
        # (old page) -> a fresh page of the same role; set by whoever attached (cli._browser).
        self.reopen = None
        self.opened_at = time.monotonic()
        self.finereport_sessions: dict[str, float] = {}     # report -> when its session last answered
        self._watch(page)

    def _watch(self, page) -> None:
        try:
            page.on("crash", lambda *_: setattr(self, "crashed", True))
        except Exception:
            pass

    async def recover_page(self, *, rule: dict | None = None, admit=None) -> dict:
        """After a renderer crash or for a planned recycle: a fresh page of the same role (Playwright never
        revives a crashed one).

        As the Anhui legacy did (a new page, then the keeper's check). The
        page's old request templates belong to dead frames and are not used.
        A refused document of the new page raises its Fault, except the one
        case ``rule`` (``page_recycle.challenge``) and ``admit`` allow.
        """
        if self.reopen is None:
            raise Fault("page_reopen_unavailable", "local")
        old = self.page
        info = {}
        # A replacement page whose document the site refused (Anhui 412) is never used silently.
        with challenge.DocumentWatch(old.context, self.origin, ignore=(old,)) as watch:
            try:
                fresh = await self.reopen(old)
            except Exception:
                if not watch.failures or watch.failures[0]["page"] is None:
                    if watch.failures:
                        raise watch.fault() from None
                    raise
                fresh = watch.failures[0]["page"]   # the challenge's reload broke the open itself
            if watch.failures:
                fresh, info = await self._settle_challenge(old, fresh, watch, rule, admit)
        self.page = fresh
        self.crashed = False
        self.opened_at = time.monotonic()
        self._watch(self.page)
        try:
            await old.close()
        except Exception:
            pass
        return {"reopened": True, "frames_closed": await self.reset_frames(), **info}

    async def _settle_challenge(self, old, fresh, watch, rule, admit) -> tuple:
        """Only a planned recycle (``admit``), only one ``rule["status"]`` document, and only once per
        window (``admit()`` spends it): the page must settle on the site by itself; a page that settles
        outside the work column is closed and the work page opened once more from the portal."""
        if rule is None or admit is None or not watch.only(rule.get("status", 412)) or not admit():
            raise watch.fault()
        settled = await challenge.settle(fresh, watch, rule)
        if settled is None:
            raise watch.fault()
        if rule.get("url_contains") and rule["url_contains"] not in fresh.url:
            # Anhui 11-01: the reload landed on 首页 without the 现货市场 tree; the logged-in
            # portal's own menu opens 综合查询 again.
            await fresh.close()
            fresh = await self.reopen(old)
            if len(watch.failures) != 1 or rule["url_contains"] not in fresh.url:
                raise watch.fault()
            settled["reopened_from_portal"] = True
        return fresh, {"challenge": settled}

    async def page_health(self) -> dict:
        """How long PMOS has used this page and its JS heap (in-page only, nothing is sent)."""
        try:
            heap = await self.page.evaluate("() => performance.memory ? performance.memory.usedJSHeapSize : null")
        except Exception:
            heap = None
        return {"age_seconds": time.monotonic() - self.opened_at,
                "js_heap_mb": round(heap / 1048576) if isinstance(heap, (int, float)) else None}

    async def reset_frames(self) -> int:
        """At start and after a re-login: PMOS's own app frames reopen with the current session."""
        return await ui.close_owned_frames(self.page)

    async def portal_reentry(self, route: str) -> dict:
        """Open ``route`` from the portal's menu, as a person does, then let PMOS's own frames reopen.

        A lapsed subsystem answers its page queries 40302 while its exports
        still work; reopening PMOS's frames alone did not help, entering it
        from the portal menu did (Shanxi, 2026-10-31). The page's own load
        requests go out as they do for a person; no business call of ours.
        """
        with self.templates.window():
            path = await ui.portal_menu_path(self.page, route)
            await ui.select_menu(self.page, path, self.timeout, force=True)
            frame = await ui.portal_frame(self.page, route, self.timeout)
            # A report the portal keeps open is only re-shown by the click: load it anew (r25, 22:46).
            await ui.reload_frame(frame, self.timeout, settle=PORTAL_REENTRY_SETTLE_SECONDS)
        return {"menu": path, "reloaded": True, "frames_closed": await self.reset_frames()}

    def _gate(self, frame, expected):
        return RequestGate(self.page, self.origin, frame, expected) if self.gate else _NoGate()

    def ready(self, request: dict) -> bool:
        """Could ``send`` run now without a local failure? Never sends anything."""
        if request["transport"] != "fetch":
            return True
        path = request.get("template", request["path"])
        template = self.templates.items.get(path)
        if template is None:
            return False
        fields = list(request.get("template_body_set") or [])
        if fields and not _has_fields(template.get("page_body"), fields):
            # Only another report's body on this endpoint: not ready, the report's own page call bootstraps it.
            fitting = [t for t in self.templates.shapes.get(path, {}).values() if _has_fields(t.get("page_body"), fields)]
            if not fitting:
                return False
            template = max(fitting, key=lambda t: t["at"])
        max_age = request.get("max_template_age")
        if max_age is not None and self.templates.clock() - template["at"] > max_age:
            return False
        try:
            if not _frame_gone(template["frame"]):
                return True
            return self.main_frame_fallback and not self.crashed and not self.page.is_closed()
        except Exception:
            return False

    async def send(self, request: dict) -> Response:
        if self.crashed:
            raise Fault("page_crashed", "local", unsent=True)
        kind = request["transport"]
        if kind == "fetch":
            return await self._fetch(request)
        if kind in ("component", "native"):
            with self.templates.window():
                return await self._call(request)
        if kind == "ui":
            with self.templates.window():
                return await self._ui(request)
        if kind == "finereport":
            frame = await finereport.open_report(self.page, request, self.timeout, self.finereport_sessions)
            return await finereport.query_day(self.page, frame, request["set"][request["date_field"]], _Expect, self.timeout,
                                              self.finereport_sessions, request["frame"])
        raise Fault("transport_unknown", "local")

    async def _fetch(self, request: dict) -> Response:
        template = self.templates.get(request.get("template", request["path"]), request.get("max_template_age"),
                                      fallback_frame=self.page.main_frame if self.main_frame_fallback else None,
                                      fields=list(request.get("template_body_set") or []))
        url = self.origin + request["path"]
        if request.get("query"):
            url += "?" + urlencode(request["query"])
        if "body" in request:
            body = json.dumps(request["body"], ensure_ascii=False, separators=(",", ":"))
        elif "template_body_set" in request:
            # The page's own body with only these fields changed (Anhui legacy
            # replay: the date, never the page's other fixed values).
            body = _template_body(template.get("page_body"), request["template_body_set"])
        else:
            body = template["body"] if request["method"] == "POST" else None
        self.templates.sending += 1
        # A request may wait longer than the policy default (an Anhui whole-day answer: ~20 MB, 6-9 s server time).
        wait = max(float(request.get("timeout") or self.timeout), self.timeout)
        try:
            result = await template["frame"].evaluate(_FETCH_JS, {
                "url": url, "method": request["method"], "headers": template["headers"], "body": body,
                "timeout": int(wait * 1000), "limit": MAX_RESPONSE_BYTES})
        except Exception as exc:
            if _frame_gone(template["frame"]) and not self.page.is_closed():
                # The frame's page closed under the call while the current page lives: the script never
                # ran there, so nothing was sent and the driver is fine (not DriverLost).
                raise Fault("template_frame_closed", "local", unsent=True) from None
            # Timeout, abort, redirect or a dropped connection: the server may
            # or may not have handled it. The ledger keeps it unknown.
            raise _lost_or_network(exc) from None
        finally:
            self.templates.sending -= 1
        return Response(int(result["status"]), result["contentType"], base64.b64decode(result["base64"]))

    async def preflight(self, request: dict) -> None:
        """Bring the page to the call's state and check it, before any attempt is recorded.

        Opens the frame, runs ``prepare``, waits for the component (a native
        call: checks its helper module is loaded) and every ``preflight``
        condition (e.g. the live form's ``exportUrl`` / ``selectUrl``). A
        failure here means nothing was sent: the engine
        defers the dataset instead of consuming the task.
        """
        if request["transport"] not in ("component", "native"):
            return
        with self.templates.window():
            frame = await ui.ensure_frame(self.page, request, self.timeout)
            async with self._gate(frame, None) as gate:  # no business request during preflight
                if request.get("prepare"):
                    await self._prepare(frame, request["prepare"])
                if request["transport"] == "component":
                    await self._wait_for(frame, {"component": request["component"], "match": request.get("match")},
                                         "component_missing")
                else:
                    try:
                        await frame.evaluate(_NATIVE_PROBE_JS, {"module": request["module"],
                                                                "export": request.get("export")})
                    except Exception as exc:
                        raise Fault(_page_error(exc, "native_probe_failed"), "local") from None
                for condition in request.get("preflight", []):
                    await self._wait_for(frame, condition, "preflight_condition_unmet")
            self.last_preflight = gate.summary()

    async def _call(self, request: dict) -> Response:
        """Trigger the page's own code and keep the response it causes on ``path``."""
        target = {"component": request.get("component"), "match": request.get("match")}
        try:
            frame = await ui.ensure_frame(self.page, request, self.timeout)
            if request.get("prepare") and not request.get("_preflighted"):
                await self._prepare(frame, request["prepare"])
            if request["transport"] == "component":
                await self._wait_for(frame, target, "component_missing")
        except Fault as fault:
            # Before the business call: nothing of ours was sent, unless an
            # unguarded prepare step may have queried (no preflight ran).
            if fault.category != "local" or (request.get("prepare") and not request.get("_preflighted")):
                raise
            raise Fault(fault.code, "local", unsent=True) from None
        if request["transport"] == "component":
            script, args = _COMPONENT_JS, {**target, "set": request.get("set", {}), "call": request["call"],
                                           "args": request.get("args", [])}
        else:
            script, args = _NATIVE_JS, {"module": request["module"], "export": request.get("export"),
                                        "args": request.get("args", [])}
        path = request["path"]
        blob = request.get("capture") == "blob"
        # A request may wait longer than the policy default (e.g. a blank-option probe).
        wait = max(float(request.get("timeout") or self.timeout), self.timeout)
        gate = self._gate(frame, (request["method"], path))
        try:
            await frame.evaluate(_QUIET_JS, int(wait * 1000) + 5000)
            if blob:
                await frame.evaluate("() => { window.__pmosBlob = null; return true; }")
            async with gate:
                async with _Expect(self.page, "response",
                                   lambda r: urlsplit(r.url).path == path and r.request.method == request["method"],
                                   timeout=wait * 1000) as captured:
                    try:
                        await frame.evaluate(script, args)
                    except Exception as exc:
                        code = _page_error(exc, "page_call_failed")
                        raise Fault(code, "local", unsent=await gate.unsent()) from None
                try:
                    response = await captured.value
                except asyncio.TimeoutError:
                    # Still inside the gate: close it, then the count proves
                    # whether the page sent the business request at all (a form
                    # that refuses a blank option sends nothing).
                    if await gate.unsent():
                        raise Fault("page_sent_nothing", "local", unsent=True) from None
                    raise Fault("network_error", "network") from None
                # A JSON reply to an export is the site's answer (e.g. 40302), not a file to wait for.
                if blob and 200 <= response.status < 300 and "json" not in response.headers.get("content-type", "").lower():
                    saved = await self._saved_blob(frame)
                    body = base64.b64decode(saved["base64"])
                    try:  # its base64 copy would otherwise stay in the page until the next download
                        await frame.evaluate("() => { window.__pmosBlob = null; return true; }")
                    except Exception:
                        pass
                    return Response(response.status, response.headers.get("content-type", ""), body,
                                    name=saved.get("name") or "", operation=gate.summary())
                body = await response.body()
        except Fault:
            raise
        except Exception as exc:
            raise _lost_or_network(exc) from None
        finally:
            try:
                await frame.evaluate(_QUIET_JS, 3000)  # the save click follows the response
            except Exception:
                pass
        return Response(response.status, response.headers.get("content-type", ""), body, operation=gate.summary())

    async def _saved_blob(self, frame) -> dict:
        """The Blob the page tried to save after its response (see _QUIET_JS)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while loop.time() < deadline:
            saved = await frame.evaluate("() => window.__pmosBlob || null")
            if saved and not saved.get("pending"):
                if saved.get("error") or "base64" not in saved:
                    raise Fault("page_download_blob_unreadable", "unknown")
                return saved
            await asyncio.sleep(0.1)
        # The response arrived but the page never saved a file: result unknown.
        raise Fault("page_download_blob_missing", "unknown")

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
        await ui.bring_forward(self.page)
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
        if request["action"] == "options":
            # Discovery: the date set, the one dropdown read and closed; 查询 is never clicked.
            try:
                for field in request.get("fields", []):
                    await ui.fill(frame, field, self.timeout)
                options = await ui.select_options(frame, request["select"], self.timeout)
            except Fault:
                raise
            except Exception:
                raise Fault("network_error", "network") from None
            return Response(200, "application/json",
                            json.dumps({"status": 0, "data": options}, ensure_ascii=False).encode())
        try:
            for field in request.get("fields", []):
                await ui.fill(frame, field, self.timeout)
        except Fault as fault:
            # 查询 was never clicked: nothing of this operation was asked for.
            raise Fault(fault.code, fault.category, http_status=fault.http_status, unsent=True,
                        detail=fault.detail) from None

        def from_report(response) -> bool:
            req = response.request
            path = urlsplit(req.url).path
            return (req.frame == frame and req.resource_type in ("xhr", "fetch")
                    and path not in platform.NATIVE_BACKGROUND_PATHS
                    and (not request.get("path") or path == request["path"]))

        try:
            async with _Expect(self.page, "response", from_report, timeout=self.timeout * 1000) as captured:
                await (await ui.button(frame, request.get("query_button", "查询"))).click()
            query = await captured.value
            query_body = await query.body()
            await ui.settled(frame, self.timeout)
            if request["action"] == "query":
                return Response(query.status, query.headers.get("content-type", ""), query_body)
            async with _Expect(self.page, "download", timeout=self.timeout * 1000) as event:
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
            async with _Expect(self.page, "download", timeout=self.timeout * 1000) as event:
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
