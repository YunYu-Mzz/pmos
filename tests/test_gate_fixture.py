"""Owned-frame request gate and Blob capture in a throwaway headless browser (local page only)."""
import functools
import http.server
import json
import os
import threading
import unittest
from pathlib import Path

from pmos.browser.transport import PageTransport, Templates
from pmos.faults import Fault
from pmos.spec import load_all

FIXTURES = Path(__file__).parent / "fixtures"
EXPORT = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xFF, 0x00, 0x80, 0x0A])  # binary that a charset decode would corrupt
FILE_PATH = "/px-settlement-infpubmeex/fileService/download"
PDF = b"%PDF-1.4\n" + bytes([0xFF, 0xFE, 0x00, 0x80])


class Site(http.server.SimpleHTTPRequestHandler):
    received: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        Site.received.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/px-x/export":
            body, kind = EXPORT, "application/vnd.ms-excel;charset=UTF-8"
        elif self.path == "/px-x/deniedExport":
            body, kind = json.dumps({"status": 40302, "message": "无权限"}).encode(), "application/json;charset=UTF-8"
        else:
            body, kind = json.dumps({"status": 0, "data": {"list": [1]}}).encode(), "application/json"
        self.reply(body, kind)

    challenges = 0          # the next document answers of /pxf-settlement-outnetpub/ that are a 412 challenge

    def do_GET(self):
        if self.path.startswith("/pxf-settlement-outnetpub/"):
            if Site.challenges > 0:
                Site.challenges -= 1
                # The challenge solves itself and reloads the plain address (the 首页 column).
                body = b"<script>setTimeout(() => location.replace('/pxf-settlement-outnetpub/'), 300)</script>"
                self.send_response(412)
            else:
                body = "<!doctype html><meta charset='utf-8'><title>x</title><p>安徽电力交易平台</p>".encode()
                self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith(FILE_PATH):
            return super().do_GET()
        Site.received.append(self.path)
        self.reply(PDF, "application/octet-stream;charset=UTF-8")

    def reply(self, body, kind):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(os.environ.get("PMOS_LOCAL_UI_TEST") == "1", "local UI browser test is opt-in")
class GateFixtureTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Site, directory=FIXTURES))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    async def asyncSetUp(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("playwright not installed")
        self.playwright = await async_playwright().start()
        self.browser = None
        for options in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                self.browser = await self.playwright.chromium.launch(headless=True, **options)
                break
            except Exception:
                continue
        if self.browser is None:
            await self.playwright.stop()
            self.skipTest("no Chromium-family browser available")
        self.page = await (await self.browser.new_context()).new_page()
        await self.page.goto(self.origin + "/component_page.html")
        templates = Templates(self.origin)
        import asyncio
        templates.observe(self.page.context, asyncio.ensure_future)
        self.transport = PageTransport(self.page, self.origin, templates, 5, gate=True)
        Site.received.clear()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_gate_lets_one_business_request_and_auth_through_and_blocks_the_rest(self):
        request = {"transport": "component", "frame": "/component_page.html", "component": "/pxf-x/report",
                   "path": "/px-x/query", "method": "POST", "set": {"date": "2026-10-20"}, "call": "queryPage"}
        await self.transport.preflight(request)
        response = await self.transport.send({**request, "_preflighted": True})
        self.assertEqual(json.loads(response.body)["status"], 0)
        self.assertEqual({k: response.operation[k] for k in ("business", "auth", "blocked")},
                         {"business": 1, "auth": 1, "blocked": 1})
        self.assertEqual(response.operation["blocked_paths"], ["POST /px-x/extra"])
        self.assertNotIn("/px-x/extra", Site.received, "the blocked request never reached the server")
        self.assertEqual(sorted(Site.received), ["/px-common-authcenter/sso/token", "/px-x/query"])

    async def test_a_failed_page_call_is_unsent_only_if_no_business_request_got_out(self):
        base = {"transport": "component", "frame": "/component_page.html", "component": "/pxf-x/report",
                "path": "/px-x/query", "method": "POST", "_preflighted": True}
        with self.assertRaises(Fault) as caught:
            await self.transport.send({**base, "call": "failBeforeQuery"})
        self.assertEqual((caught.exception.code, caught.exception.unsent), ("page_method_failed", True))
        with self.assertRaises(Fault) as caught:
            await self.transport.send({**base, "call": "failAfterQuery"})
        await self.page.wait_for_timeout(300)
        self.assertEqual(caught.exception.unsent, "/px-x/query" not in Site.received,
                         "unsent exactly when the query never reached the server")

    async def test_repeated_operations_leave_no_protocol_callbacks_or_handles_behind(self):
        connection = self.page._impl_obj._connection
        request = {"transport": "component", "frame": "/component_page.html", "component": "/pxf-x/report",
                   "path": "/px-x/query", "method": "POST", "set": {"date": "2026-10-20"}, "call": "queryPage"}
        for _ in range(8):
            await self.transport.preflight(request)
            await self.transport.send({**request, "_preflighted": True})
        await self.page.wait_for_timeout(300)
        self.assertEqual(len(connection._callbacks), 0, "Playwright's Waiter left __waitInfo__ callbacks")
        handles = [o for o in connection._objects.values() if type(o).__name__ == "JSHandle"]
        self.assertEqual(handles, [], "wait_for_function handles are disposed")

    async def test_reset_removes_only_pmos_frames_and_the_next_call_reopens_a_fresh_one(self):
        from pmos.browser import ui
        await self.page.evaluate("() => { const f = document.createElement('iframe'); f.id = 'site-own'; document.body.append(f); }")
        request = {"frame": "/publication_page.html", "open_url": "/publication_page.html#/x"}
        first = await ui.ensure_frame(self.page, request, 5)
        self.assertEqual(await self.transport.reset_frames(), 1)
        self.assertTrue(first.is_detached())
        self.assertEqual(await self.page.locator("iframe#site-own").count(), 1, "the site's own frames stay")
        again = await ui.ensure_frame(self.page, request, 5)
        self.assertIsNot(again, first)

    async def test_a_page_that_sends_nothing_times_out_as_unsent_not_unknown(self):
        self.transport.timeout = 2
        request = {"transport": "component", "frame": "/component_page.html", "component": "/pxf-x/report",
                   "path": "/px-x/query", "method": "POST", "call": "refuseBlank", "_preflighted": True}
        with self.assertRaises(Fault) as caught:
            await self.transport.send(request)
        self.assertEqual((caught.exception.code, caught.exception.category, caught.exception.unsent),
                         ("page_sent_nothing", "local", True))
        self.assertEqual(Site.received, [])

    async def test_export_bytes_come_from_the_page_blob_exactly(self):
        request = {"transport": "component", "frame": "/component_page.html", "component": "*",
                   "match": {"exportUrl": "/px-x/export"}, "path": "/px-x/export", "method": "POST",
                   "set": {"queryConfig.data": {"beginDate": "2026-10-30"}}, "call": "exportData", "capture": "blob"}
        response = await self.transport.send(request)
        self.assertEqual(response.body, EXPORT)
        self.assertEqual(response.name, "export.xls")
        self.assertEqual(await self.page.locator("a[download]").count(), 0)

    async def test_a_json_reply_to_an_export_is_returned_at_once_not_awaited_as_a_file(self):
        request = {"transport": "component", "frame": "/component_page.html", "component": "*",
                   "match": {"exportUrl": "/px-x/export"}, "path": "/px-x/deniedExport", "method": "POST",
                   "set": {"exportUrl": "/px-x/deniedExport", "queryConfig.data": {"tdate": "2026-10-14"}},
                   "call": "exportData", "capture": "blob"}
        response = await self.transport.send(request)
        self.assertEqual(json.loads(response.body)["status"], 40302)
        self.assertEqual(Site.received, ["/px-x/deniedExport"])
        self.assertEqual(response.operation["business"], 1)

    async def test_portal_reentry_opens_the_route_from_the_menu_tree_and_closes_pmos_frames(self):
        from pmos.browser.ui import route_of
        await self.page.goto(self.origin + "/portal_page.html")
        await self.page.wait_for_function("() => document.getElementById('pmos-x').contentDocument.readyState === 'complete'")
        from unittest.mock import patch
        settle = patch("pmos.browser.transport.PORTAL_REENTRY_SETTLE_SECONDS", 0)
        settle.start()
        self.addCleanup(settle.stop)
        result = await self.transport.portal_reentry("/pxf-phbsx-infomation/#/pxf-x/report")
        self.assertEqual(result, {"menu": ["信息披露", "现货日前信息", "日前备用总量"], "reloaded": True, "frames_closed": 1})
        shown = [f for f in self.page.frames if f.parent_frame is not None and route_of(f.url) == "/pxf-x/report"]
        self.assertEqual(await shown[0].evaluate("() => performance.getEntriesByType('navigation')[0].type"), "reload",
                         "the app document loaded anew, as after a person's entry")
        self.assertEqual([await (await f.frame_element()).get_attribute("id") for f in shown], ["pxf-x"],
                         "only the portal's own frame shows it; PMOS's old frame is gone")
        await self.transport.portal_reentry("/pxf-x/report")
        self.assertEqual(await self.page.evaluate("window.clicks"), 2, "the current leaf is clicked again")
        with self.assertRaises(Fault) as caught:
            await self.transport.portal_reentry("/pxf-x/nowhere")
        self.assertEqual(caught.exception.code, "portal_menu_route_missing")
        self.assertEqual(Site.received, [])

    async def test_a_menu_step_that_fails_before_the_leaf_is_clicked_is_proven_unsent(self):
        from pmos.browser import ui
        await self.page.goto(self.origin + "/portal_page.html")
        with self.assertRaises(Fault) as caught:
            await ui.select_menu(self.page, ["信息披露", "现货日前信息", "不存在的报表"], 1)
        self.assertEqual((caught.exception.code, caught.exception.unsent), ("menu_navigation_timeout", True))
        self.assertEqual(Site.received, [])

    async def test_a_liaoning_report_opens_from_the_column_pages_own_menu(self):
        from pmos.browser import ui
        await self.page.goto(self.origin + "/column_page.html")
        request = {"frame": "/pdbln/report/PdblnReport043",
                   "site_menu": {"source": "lnsnxh_rqpjsbdj", "route": "/pxf-dif-settlement-outnetpub/pdbln/report/PdblnReport043"}}
        frame = await ui.ensure_frame(self.page, request, 5)
        self.assertTrue(frame.url.endswith("/pdbln/report/PdblnReport043"))
        self.assertEqual(await self.page.evaluate("window.opened"), [["g-043", "g-043"]])
        self.assertIs(await ui.ensure_frame(self.page, request, 5), frame, "an open report frame is reused")
        with self.assertRaises(Fault) as caught:
            await ui.ensure_frame(self.page, {"frame": "/pdbln/report/PdblnReport999",
                                              "site_menu": {"source": "nope", "route": "/x/pdbln/report/PdblnReport999"}}, 5)
        self.assertEqual(caught.exception.code, "site_menu_entry_missing")
        self.assertEqual(Site.received, [])

    async def test_the_work_page_opens_once_from_the_portal_and_is_found_by_its_name_after(self):
        from pmos.browser.attach import work_page
        await self.page.goto(self.origin + "/portal_page.html")
        config = {"name": "pmos-work", "open_from": "/portal_page.html", "url": "/column_page.html"}
        first = await work_page(self.browser, self.origin, config, timeout=10)
        self.assertTrue(first.url.endswith("/column_page.html"))
        self.assertEqual(await first.evaluate("() => window.name"), "pmos-work")
        again = await work_page(self.browser, self.origin, config, timeout=10)
        self.assertIs(again, first)
        self.assertEqual(len(self.page.context.pages), 2)

    async def test_a_renderer_crash_stops_sends_until_a_fresh_page_replaces_it(self):
        import asyncio
        try:
            await self.page.goto("chrome://crash", timeout=5000)
        except Exception:
            pass                                  # the navigation itself dies with the renderer
        for _ in range(50):
            if self.transport.crashed:
                break
            await asyncio.sleep(0.1)
        self.assertTrue(self.transport.crashed)
        with self.assertRaises(Fault) as caught:
            await self.transport.send({"transport": "fetch", "method": "POST", "path": "/px-x/query", "body": {}})
        self.assertEqual((caught.exception.code, caught.exception.unsent), ("page_crashed", True))
        origin = self.origin

        async def reopen(old):
            fresh = await old.context.new_page()
            await fresh.goto(origin + "/component_page.html")
            return fresh
        self.transport.reopen = reopen
        self.assertEqual(await self.transport.recover_page(), {"reopened": True, "frames_closed": 0})
        self.assertFalse(self.transport.crashed)
        self.assertIsNot(self.transport.page, self.page)
        response = await self.transport.send({"transport": "component", "frame": "/component_page.html",
                                              "component": "/pxf-x/report", "path": "/px-x/query", "method": "POST",
                                              "set": {"date": "2026-10-20"}, "call": "queryPage"})
        self.assertEqual(json.loads(response.body)["status"], 0, "the fresh page works")

    async def test_the_anhui_work_page_opens_by_the_portal_menu_or_adopts_the_one_existing_page(self):
        from pmos.browser.attach import work_page
        await self.page.goto(self.origin + "/anhui_portal.html#/dashboard")
        config = {"name": "pmos-anhui-work", "open_from": "#/dashboard", "menu": ["信息披露", "综合查询"],
                  "url_contains": "columnHomeLeftMenuNew", "adopt_url_contains": "columnHomeLeftMenuNew"}
        opened = await work_page(self.browser, self.origin, config, timeout=10)
        self.assertIn("?ts=", opened.url, "the site's own click handler opened it")
        self.assertEqual(await opened.evaluate("() => window.name"), "pmos-anhui-work")
        self.assertIs(await work_page(self.browser, self.origin, config, timeout=10), opened)
        await opened.evaluate("() => { window.name = ''; }")       # as the legacy worker's own page
        self.assertIs(await work_page(self.browser, self.origin, config, timeout=10), opened, "adopted, no new tab")
        self.assertEqual(len(self.page.context.pages), 2)

    async def test_a_planned_recycle_through_a_412_challenge_settles_once_and_reopens_the_column(self):
        from pmos.browser.attach import work_page
        await self.page.goto(self.origin + "/anhui_challenge_portal.html#/dashboard")
        config = {"name": "pmos-anhui-work", "open_from": "#/dashboard", "menu": ["信息披露", "综合查询"],
                  "url_contains": "columnHomeLeftMenuNew"}
        rule = {"status": 412, "settle_seconds": 10, "path": "/pxf-settlement-outnetpub/", "text": "安徽",
                "url_contains": "columnHomeLeftMenuNew"}
        browser, origin = self.browser, self.origin

        async def reopen(old):
            return await work_page(browser, origin, config, timeout=10, exclude=(old,))
        work = await reopen(None)
        self.transport.page, self.transport.reopen = work, reopen
        spent = []
        Site.challenges = 1
        with self.assertRaises(Fault) as caught:          # a crash recovery (no rule) never judges by itself
            await self.transport.recover_page()
        self.assertEqual((caught.exception.code, caught.exception.http_status), ("security_verification", 412))
        self.assertIs(self.transport.page, work, "the old page stays in use for a person to look at")
        for page in [p for p in self.page.context.pages if p not in (self.page, work)]:
            await page.close()
        Site.challenges = 1
        with self.assertRaises(Fault):                    # the window's one judgement is spent
            await self.transport.recover_page(rule=rule, admit=lambda: False)
        for page in [p for p in self.page.context.pages if p not in (self.page, work)]:
            await page.close()
        Site.challenges = 1
        info = await self.transport.recover_page(rule=rule, admit=lambda: spent.append(1) or True)
        self.assertEqual((info["challenge"]["status"], info["challenge"]["reopened_from_portal"], spent), (412, True, [1]))
        self.assertIn("columnHomeLeftMenuNew", self.transport.page.url, "the 首页 page was replaced by the column")
        self.assertTrue(work.is_closed())
        self.assertEqual(len(self.page.context.pages), 2, "the portal and the one work page")
        Site.challenges = 2                               # a second refused document: a person decides
        with self.assertRaises(Fault):
            await self.transport.recover_page(rule=rule, admit=lambda: True)

    async def test_page_health_reads_the_pages_age_and_js_heap_without_a_request(self):
        health = await self.transport.page_health()
        self.assertGreaterEqual(health["age_seconds"], 0)
        self.assertIsInstance(health["js_heap_mb"], int, "Chromium exposes performance.memory")
        self.assertEqual(Site.received, [])

    async def test_options_are_read_from_the_dropdown_without_choosing_one_or_querying(self):
        await self.page.goto(self.origin + "/select_page.html")
        response = await self.transport.send({"transport": "ui", "action": "options", "frame": "/select_page.html",
                                              "fields": [{"kind": "date", "placeholder": "选择日期", "value": "2026-10-30"}],
                                              "select": {"kind": "select", "label": "断面名称"}})
        found = json.loads(response.body)["data"]
        self.assertEqual([(o["label"], o["value"], o["value_type"]) for o in found],
                         [("甲线", "甲线", "string"), ("乙线", "乙线", "string"), ("丙机", "3378262674776260623", "string")])
        self.assertEqual(await self.page.input_value("#pick"), "甲线", "nothing was chosen")
        self.assertEqual(await self.page.evaluate("window.queries"), 0, "查询 never clicked")
        self.assertFalse(await self.page.locator(".el-select-dropdown__item").first.is_visible(), "closed again")
        self.assertEqual(Site.received, [])

    async def test_a_filterable_select_is_judged_by_its_value_after_it_has_settled(self):
        """Anhui 2026-11-01 11:31: the input was blank for a tick after Escape (select_value_changed_by_reading)."""
        request = {"transport": "ui", "action": "options", "frame": "/filterable_select_page.html",
                   "fields": [{"kind": "date", "placeholder": "选择日期", "value": "2026-10-29"}],
                   "select": {"kind": "select", "label": "电源类型"}}
        await self.page.goto(self.origin + "/filterable_select_page.html")
        response = await self.transport.send(request)
        self.assertEqual([o["label"] for o in json.loads(response.body)["data"]], ["火电", "水电", "风电"])
        self.assertEqual(await self.page.input_value("#pick"), "火电")
        await self.page.goto(self.origin + "/filterable_select_page.html?change=1")
        with self.assertRaises(Fault) as caught:
            await self.transport.send({**request, "frame": "/filterable_select_page.html?change=1"})
        self.assertEqual(caught.exception.code, "select_value_changed_by_reading", "a real change is still caught")
        await self.page.goto(self.origin + "/filterable_select_page.html?pick=1")
        response = await self.transport.send({**request, "frame": "/filterable_select_page.html?pick=1"})
        self.assertEqual(len(json.loads(response.body)["data"]), 3, "it took one of its own options: as the legacy, read")
        await self.page.goto(self.origin + "/filterable_select_page.html?reset=1")
        response = await self.transport.send({**request, "frame": "/filterable_select_page.html?reset=1"})
        self.assertEqual(len(json.loads(response.body)["data"]), 3, "read once the page's own reset has settled")
        self.assertEqual(await self.page.input_value("#pick"), "水电")
        self.assertEqual(Site.received, [])

    async def test_a_template_outlives_its_frame_only_where_the_main_frame_fallback_is_on(self):
        await self.page.goto(self.origin + "/portal_page.html")
        frame = next(f for f in self.page.frames if f.parent_frame is not None)
        await frame.wait_for_load_state("load")
        await frame.evaluate("() => fetch('/px-x/query', {method: 'POST', body: JSON.stringify({d: 1})})")
        import asyncio
        await asyncio.sleep(0.5)                  # the template is captured from the frame's own request
        self.transport.templates.items.setdefault("/px-x/query", {"frame": frame, "headers": {}, "body": "{}",
                                                                 "at": self.transport.templates.clock()})
        await self.page.evaluate("() => document.getElementById('pmos-x').remove()")
        request = {"transport": "fetch", "method": "POST", "path": "/px-x/query", "body": {"d": 2}}
        self.assertFalse(self.transport.ready(request), "without the fallback a detached template waits")
        self.transport.main_frame_fallback = True
        self.assertTrue(self.transport.ready(request))
        Site.received.clear()
        response = await self.transport.send(request)
        self.assertEqual(json.loads(response.body)["status"], 0)
        self.assertEqual(Site.received, ["/px-x/query"], "sent from the page's main frame")

    async def test_an_announcement_file_downloads_through_its_detail_dialog_with_the_shanxi_spec(self):
        await self.page.goto(self.origin + "/publication_page.html")
        bare = {"transport": "native", "frame": "/publication_page.html", "module": "63a1", "export": "a",
                "args": ["f-1", None], "path": FILE_PATH, "method": "GET", "capture": "blob"}
        with self.assertRaises(Fault) as caught:
            await self.transport.preflight(bare)
        self.assertEqual((caught.exception.code, caught.exception.category), ("native_module_missing", "local"),
                         "the bare helper is not loaded yet: deferred in preflight, nothing consumed")
        spec = load_all(Path(__file__).parents[1] / "provinces/shanxi/datasets")["pub_DDJG_DLBWYXGLKHHFHMX"]
        attachment = {"id": "f-1", "name": "2026-04考核补偿报表.pdf"}
        request = spec.build_download({"record": {}, "attachment": attachment, "title": attachment["name"],
                                       "record_id": "legacy:x", "published": None, "period": "undated"})
        request = {k: v for k, v in request.items() if k != "open_url"} | {"frame": "/publication_page.html"}
        await self.transport.preflight(request)
        self.assertEqual(Site.received, [], "mounting the view and opening the dialog sends nothing")
        response = await self.transport.send({**request, "_preflighted": True})
        self.assertEqual(response.body, PDF)
        self.assertEqual(response.name, "f-1.pdf")
        self.assertEqual(Site.received, [FILE_PATH + "?fileId=f-1"])
        self.assertEqual(response.operation["business"], 1)


if __name__ == "__main__":
    unittest.main()
