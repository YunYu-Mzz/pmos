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


def daily_table() -> str:
    times = [f"{i // 4:02d}:{i % 4 * 15:02d}" for i in range(1, 97)]
    rows = []
    for start in range(0, 96, 16):
        rows.append("<tr>" + "".join(f"<td>{t}</td>" for t in times[start:start + 16]) + "</tr>")
        rows.append("<tr>" + "".join("<td>1.5</td>" for _ in range(16)) + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


def blank_table() -> str:
    return '<div class="pageContentDIV"><table class="x-table "><tr><td></td></tr><tr><td></td></tr></table></div>'


class Site(http.server.SimpleHTTPRequestHandler):
    received: list = []

    def log_message(self, *args):
        pass

    bodies: list = []

    finereport: list = []        # what the FineReport stand-in was asked, in order
    finereport_dead = False      # the session ended: parameters_d answers FineReport's error page
    finereport_blank = False
    finereport_denied: set = set()   # reports whose mapping answers 40302

    def finereport_answer(self, query: dict) -> None:
        from urllib.parse import parse_qs
        cmd = query.get("cmd", [""])[0]
        Site.finereport.append(cmd)
        if Site.finereport_dead and cmd == "parameters_d":
            self.reply(b"<html>Dec-Session_Lifecycle_End</html>", "text/html;charset=UTF-8")
        elif cmd == "parameters_d":
            self.reply(b'{"status":"success"}', "application/json")
        elif cmd == "combo_items":
            self.reply(json.dumps([{"text": "1号机", "value": "1号机"}, {"text": "2号机", "value": "2号机"}], ensure_ascii=False).encode(), "application/json")
        elif cmd == "load_content":
            self.reply(b'{"elementCases":[]}', "application/json")
        else:
            self.reply(json.dumps({"currentPage": 1, "totalPage": 1, "htmlTag": blank_table() if Site.finereport_blank else daily_table()}).encode(),
                       "application/json")

    def do_POST(self):
        from urllib.parse import parse_qs
        Site.received.append(self.path)
        Site.bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if self.path == "/px-settlement-infpubquery/spotgoods/queryIpSpotgoodsInfoBytableName":
            source = json.loads(Site.bodies[-1])["tableName"]
            Site.finereport.append("map:" + source)
            if source in Site.finereport_denied:
                return self.reply(json.dumps({"status": 40302, "message": "无权限"}).encode(), "application/json")
            url = f"/px-basesystem-reportform/decision/view/form?ref_t={len(Site.finereport)}&viewlet=dir%2F{source}.frm"
            if source == "lnsnxh_c":        # a .cpt report is served from another path, with no form of its own
                url = f"/px-basesystem-reportform/decision/view/report?viewlet=dir%2F{source}.cpt"
            return self.reply(json.dumps({"status": 0, "data": {"url": url}}).encode(), "application/json")
        if self.path == "/px-basesystem-reportform/decision/view/form":
            return self.finereport_answer(parse_qs(Site.bodies[-1].decode()))
        if self.path == "/px-x/export":
            body, kind = EXPORT, "application/vnd.ms-excel;charset=UTF-8"
        elif self.path == "/px-x/deniedExport":
            body, kind = json.dumps({"status": 40302, "message": "无权限"}).encode(), "application/json;charset=UTF-8"
        else:
            body, kind = json.dumps({"status": 0, "data": {"list": [1]}}).encode(), "application/json"
        self.reply(body, kind)

    challenges = 0          # the next document answers of /pxf-settlement-outnetpub/ that are a 412 challenge

    def do_GET(self):
        if self.path.startswith("/px-basesystem-reportform/decision/view/report"):
            return self.reply("<!doctype html><title>cpt</title><script>window.contentPane = {reportTotalPage: 1, currentPageIndex: 1};</script><table><tr><td>甲</td></tr></table>".encode(), "text/html;charset=UTF-8")
        if self.path.startswith("/px-basesystem-reportform/decision/view/form"):
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(self.path).query)
            if "cmd" in query:
                return self.finereport_answer(query)
            Site.finereport.append("document")
            Site.finereport_dead = False
            return self.reply((FIXTURES / "finereport_form.html").read_bytes(), "text/html;charset=UTF-8")
        if self.path == "/" or self.path.startswith("/?"):
            # The Anhui portal as on 2026-11-01: /?ACYxOEl1=…#/dashboard (a dynamic parameter before the hash).
            body = (FIXTURES / "anhui_portal.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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

    async def test_a_section_switched_in_the_top_menu_leaves_its_hover_menu_before_the_tree(self):
        """Anhui 2026-11-01: 月度输变电设备检修计划 (中长期市场) failed menu_label_not_visible; the legacy
        moves the pointer off the hover menu after a top-menu click (clear_hover_menu)."""
        from pmos.browser import ui
        await self.page.set_viewport_size({"width": 1000, "height": 700})
        await self.page.goto(self.origin + "/anhui_tree_page.html")
        await ui.select_menu(self.page, ["中长期市场", "市场边界信息", "月度输变电设备检修计划"], 5)
        self.assertEqual(await self.page.locator(".el-tree-node.is-current").count(), 1)
        self.assertEqual(await self.page.locator(".menuBox.menuBoxExpanded").count(), 0, "the hover menu was left")

    async def test_a_subtree_still_opening_is_waited_for(self):
        """Anhui r17 2026-11-01 15:00:47: 省内现货市场's 12x12 icon was clipped by the opening subtree."""
        from pmos.browser import ui
        await self.page.set_viewport_size({"width": 1000, "height": 700})
        await self.page.goto(self.origin + "/anhui_tree_page.html?animate=1")
        await ui.select_menu(self.page, ["中长期市场", "市场边界信息", "月度输变电设备检修计划"], 5)
        self.assertEqual(await self.page.locator(".el-tree-node.is-current").count(), 1)

    async def test_a_menu_label_with_no_visible_point_says_where_it_failed(self):
        from pmos.browser import ui
        await self.page.set_content("""<div class="el-tree"><div class="el-tree-node"><div class="el-tree-node__content">
          <span class="el-tree-node__expand-icon" style="display:inline-block;width:0;height:0;overflow:hidden"></span>
          <span class="el-tree-node__label">中长期市场</span></div>
          <div class="el-tree-node__children" style="display:none"><div class="el-tree-node"><div class="el-tree-node__content">
          <span class="el-tree-node__label">市场边界信息</span></div></div></div></div></div>""")
        with self.assertRaises(Fault) as caught:
            await ui.select_menu(self.page, ["中长期市场", "市场边界信息"], 1)
        self.assertEqual(caught.exception.code, "menu_label_not_visible")
        self.assertEqual({k: caught.exception.detail[k] for k in ("menu_depth", "menu_label", "menu_target", "box_width")},
                         {"menu_depth": 1, "menu_label": "中长期市场", "menu_target": "expand_icon", "box_width": 0})
        self.assertTrue(caught.exception.unsent)

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

    async def test_liaoning_disclosure_columns_open_without_their_first_query_and_ask_what_the_legacy_asked(self):
        """The real specs and page scripts, on a stand-in of the column page: nothing goes out at mount."""
        from pmos import paging
        directory = Path(__file__).parent.parent / "provinces" / "liaoning"
        specs = load_all(directory / "datasets")
        self.transport.scripts = {path.stem: path.read_text(encoding="utf-8") for path in (directory / "scripts").glob("*.js")}
        await self.page.goto(self.origin + "/disclosure_column.html")

        async def ask(name, page):
            spec = specs[name]
            request = paging.apply(spec.build("2026-11-01"), spec.request["pagination"], page)
            request = {**request, "frame": "/disclosure_column.html"}
            request.pop("open_url")
            Site.received.clear()
            Site.bodies.clear()
            response = await self.transport.send(request)
            self.assertEqual(json.loads(response.body)["status"], 0)
            self.assertEqual(Site.received, [spec.request["path"]], name + ": only the list request")
            return json.loads(Site.bodies[0])

        body = await ask("pub_LSTCFDJGCS", 1)
        self.assertEqual(body["data"]["menuId"], "69c8957d-f65b-4fab-b7d4-2d79cf980cd5")
        self.assertEqual(body["pageInfo"], {"pageNum": 1, "pageSize": 20})
        self.assertEqual((await ask("pub_LSTCFDJGCS", 2))["pageInfo"]["pageNum"], 2)
        record = {"guid": "g1", "title": "t", "attachment": "[]"}
        download = specs["pub_LSTCFDJGCS"].build_download({"record_id": "g1", "title": "t", "period": "2026-09", "record": record,
                                                           "attachment": {"id": "f2", "name": "f2.pdf"}})
        download = {**download, "frame": "/disclosure_column.html"}
        download.pop("open_url")
        Site.received.clear()
        self.assertEqual((await self.transport.send(download)).body, PDF)
        self.assertEqual(Site.received, [FILE_PATH + "?fileId=f2"], "the view's own fileDownload, the row narrowed to this file")
        await ask("pub_jyjgJbxx", 1)                    # getDataJY: ask() holds it to the spec's own path
        await ask("pub_dwqyqyjbxx", 1)
        name = "sitewide_8090c94f9cf7b256"
        body = await ask(name, 1)
        self.assertEqual(body, {"data": {"memberType": "20", "memberName": "", "infoCode": [], "time": [], "marketId": "PDBLN",
                                         "generatorType": [], "marketCode": "PDBLN"}, "pageInfo": {"pageNum": 1, "pageSize": 20}},
                         "exactly the fields the legacy observer saw on every page")
        self.assertEqual((await ask(name, 3))["pageInfo"]["pageNum"], 3)
        self.assertEqual((await ask("sitewide_f1051ebc83754fde", 1))["data"]["memberType"], "10")
        opened = await self.page.evaluate("window.opened")
        self.assertEqual(len(opened), 5, "each column opens once while it stays the one on screen")

    async def test_one_report_under_two_menu_paths_opens_and_two_reports_on_one_route_do_not(self):
        """Liaoning r14 19:29: the menu has 064 twice; the refusal took the worker down."""
        from pmos.browser import ui
        await self.page.goto(self.origin + "/column_page.html")
        frame = await ui.ensure_frame(self.page, {"frame": "/pdbln/report/PdblnReport064",
                                                  "site_menu": {"source": "lnsnxh_n", "route": "/x/pdbln/report/PdblnReport064"}}, 5)
        self.assertTrue(frame.url.endswith("/pdbln/report/PdblnReport064"))
        self.assertEqual(await self.page.evaluate("window.opened"), [["g-064a", "g-064a"]])
        with self.assertRaises(Fault) as caught:
            await ui.ensure_frame(self.page, {"frame": "/pdbln/report/PdblnReport098", "site_menu": {"route": "/x/pdbln/report/PdblnReport098"}}, 5)
        self.assertEqual(caught.exception.code, "site_menu_entry_missing")

    async def test_a_menu_still_loading_gets_its_time_before_an_entry_is_believed_missing(self):
        """Liaoning 20:11:42: one second after a recycle a report was paused for a missing menu entry."""
        from pmos.browser import ui
        await self.page.goto(self.origin + "/column_page.html")
        await self.page.evaluate("""() => { const whole = header.menuBoxObj; header.menuBoxObj = {};
                                            setTimeout(() => { header.menuBoxObj = whole; }, 1500); }""")
        request = {"frame": "/pdbln/report/PdblnReport043",
                   "site_menu": {"source": "lnsnxh_rqpjsbdj", "route": "/pxf-dif-settlement-outnetpub/pdbln/report/PdblnReport043"}}
        frame = await ui.ensure_frame(self.page, request, 5)
        self.assertTrue(frame.url.endswith("/pdbln/report/PdblnReport043"))

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

    async def test_the_portal_is_found_by_its_route_although_a_dynamic_parameter_precedes_the_hash(self):
        from pmos.browser.attach import work_page
        await self.page.goto(self.origin + "/?ACYxOEl1=abc123#/dashboard")
        config = {"name": "pmos-anhui-work", "open_from": "/#/dashboard", "menu": ["信息披露", "综合查询"],
                  "url_contains": "columnHomeLeftMenuNew"}           # the production value
        opened = await work_page(self.browser, self.origin, config, timeout=10)
        self.assertIn("columnHomeLeftMenuNew", opened.url)
        self.assertEqual(await opened.evaluate("() => window.name"), "pmos-anhui-work")

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

    async def test_a_query_whose_form_cannot_be_filled_is_unsent(self):
        await self.page.goto(self.origin + "/select_page.html")
        with self.assertRaises(Fault) as caught:
            await self.transport.send({"transport": "ui", "action": "query", "frame": "/select_page.html",
                                       "path": "/px-x/query", "method": "POST",
                                       "fields": [{"kind": "date", "placeholder": "选择日期", "value": "2026-10-28"},
                                                  {"kind": "select", "label": "断面名称", "value": "不存在的线"}]})
        self.assertEqual((caught.exception.code, caught.exception.unsent), ("select_option_missing", True))
        self.assertEqual({k: caught.exception.detail[k] for k in ("select_label", "select_option", "options_listed")},
                         {"select_label": "断面名称", "select_option": "不存在的线", "options_listed": 3})
        self.assertEqual(await self.page.evaluate("window.queries"), 0, "查询 was never clicked")

    def finereport_request(self, source="lnsnxh_a", day="2026-10-30"):
        return {"transport": "finereport", "method": "POST", "path": "/px-basesystem-reportform/decision/view/form", "frame": source,
                "site_menu": {"source": source, "route": "spotgoodsSum"}, "set": {"queryParam.mdate": day}, "date_field": "queryParam.mdate"}

    async def finereport_page(self):
        Site.finereport.clear()
        Site.finereport_dead = Site.finereport_blank = False
        await self.page.goto(self.origin + "/finereport_portal.html")
        from unittest.mock import patch
        no_pause = patch("pmos.browser.finereport.STAGE_PAUSE_SECONDS", 0)
        no_pause.start()
        self.addCleanup(no_pause.stop)

    async def test_a_finereport_day_opens_from_the_menu_and_asks_its_three_stages_one_by_one(self):
        from pmos.finereport import daily_values
        from pmos.validate import evaluate
        await self.finereport_page()
        response = await self.transport.send(self.finereport_request())
        self.assertEqual(Site.finereport, ["map:lnsnxh_a", "document", "form_getsource", "parameters_d", "load_content", "load_report_content"],
                         "the page mapped and loaded it; its own form asked the stages")
        self.assertEqual(len(daily_values(json.loads(response.body))), 96)
        self.assertEqual(evaluate({"kind": "finereport_daily"}, response).result, "saved")
        await self.transport.send(self.finereport_request(day="2026-10-29"))
        self.assertEqual(Site.finereport[6:], ["parameters_d", "load_content", "load_report_content"],
                         "the same session: no mapping, no document, and form_getsource stays unsent")

    async def test_a_probe_reads_the_mapping_and_the_form_and_asks_no_data(self):
        await self.finereport_page()
        site_menu = {"source": "lnsnxh_a", "route": "spotgoodsSum"}
        response = await self.transport.send({"transport": "finereport", "action": "probe", "frame": "lnsnxh_a", "site_menu": site_menu})
        found = json.loads(response.body)["data"]
        self.assertEqual(found["mapping"], {"http_status": 200, "business_status": 0, "mapped": True})
        self.assertEqual(found["viewlet"], "dir/lnsnxh_a.frm")
        self.assertTrue(found["form_ready"])
        self.assertEqual([(w["name"], w["value"]) for w in found["widgets"]], [("unit", "单位:MW")])
        self.assertEqual([c for c in Site.finereport if c in ("parameters_d", "load_content", "load_report_content")], [], "no stage")

    async def test_a_probe_opens_a_dropdown_once_and_reports_what_it_loads(self):
        await self.finereport_page()
        response = await self.transport.send({"transport": "finereport", "action": "probe", "frame": "lnsnxh_b",
                                              "site_menu": {"source": "lnsnxh_b", "route": "spotgoodsSum"},
                                              "form": {"combos": ["机组名称", "没有这个"], "probe_date": "2026-10-25"}})
        combos = json.loads(response.body)["data"]["combos"]
        self.assertEqual(combos[0]["queued"][0]["cmd"], "combo_items")
        self.assertIn("1号机", combos[0]["answer"])
        self.assertIn("combo_missing", combos[1]["error"])
        self.assertEqual([c for c in Site.finereport if c in ("parameters_d", "load_report_content")], [], "no data asked")
        self.assertEqual(await self.page.frames[-1].evaluate("window.__pmosFrQueue.length"), 0)

    async def test_a_report_with_no_form_shows_what_it_has_instead(self):
        await self.finereport_page()
        self.transport.timeout = 1
        response = await self.transport.send({"transport": "finereport", "action": "probe", "frame": "lnsnxh_c",
                                              "site_menu": {"source": "lnsnxh_c", "route": "spotgoodsSum"}})
        found = json.loads(response.body)["data"]
        self.assertEqual((found["path"], found["frame_found"], found["form_ready"]),
                         ("/px-basesystem-reportform/decision/view/report", True, False))
        self.assertEqual(found["page"]["contentPane"], "object")
        self.assertEqual(found["page"]["reportTotalPage"], 1)

    async def test_a_probe_can_follow_one_day_and_report_its_stages_and_table(self):
        await self.finereport_page()
        response = await self.transport.send({"transport": "finereport", "action": "probe", "frame": "lnsnxh_a",
                                              "site_menu": {"source": "lnsnxh_a", "route": "spotgoodsSum"},
                                              "form": {"follow_day": "2026-10-25"}})
        follow = json.loads(response.body)["data"]["follow"]
        self.assertEqual(follow["how"], "parameterCommit")
        self.assertEqual([s["cmd"] for s in follow["stages"]], ["parameters_d", "load_content", "load_report_content"])
        self.assertEqual((follow["last"]["rows"], follow["last"]["cols"], follow["last"]["totalPage"]), (12, [16], 1))
        self.assertEqual(follow["last"]["sample"][0][0], "00:15")

    async def test_a_refused_mapping_is_the_probes_answer_and_no_fault(self):
        await self.finereport_page()
        Site.finereport_denied = {"lnsnxh_b"}
        self.addCleanup(lambda: setattr(Site, "finereport_denied", set()))
        response = await self.transport.send({"transport": "finereport", "action": "probe", "frame": "lnsnxh_b",
                                              "site_menu": {"source": "lnsnxh_b", "route": "spotgoodsSum"}})
        found = json.loads(response.body)["data"]
        self.assertEqual(found["mapping"], {"http_status": 200, "business_status": 40302, "mapped": False})
        self.assertNotIn("widgets", found)

    async def test_a_probe_of_a_report_the_menu_does_not_have_says_so_and_sends_nothing(self):
        await self.finereport_page()
        Site.received.clear()
        with self.assertRaises(Fault) as caught:
            await self.transport.send({"transport": "finereport", "action": "probe", "frame": "nope",
                                       "site_menu": {"source": "nope", "route": "spotgoodsSum"}})
        self.assertEqual((caught.exception.code, caught.exception.unsent), ("site_menu_entry_missing", True))
        self.assertEqual(Site.received, [])

    async def test_inspecting_a_component_reads_its_query_fields_and_calls_nothing(self):
        Site.received.clear()
        response = await self.transport.send({"transport": "component", "action": "inspect", "frame": "/component_page.html",
                                              "component": "/pxf-x/report", "call": "queryPage", "path": "/px-x/query", "method": "POST"})
        found = json.loads(response.body)["data"]
        self.assertIn("queryPage", found.get("methods") or ["queryPage"])
        self.assertEqual(Site.received, [])

    async def test_a_nameless_view_is_found_by_what_it_can_do(self):
        response = await self.transport.send({"transport": "component", "frame": "/component_page.html", "component": "*",
                                              "match": {"queryPage": "*function", "exportData": "*function"}, "call": "queryPage",
                                              "method": "POST", "path": "/px-x/query"})
        self.assertEqual(response.status, 200)

    async def test_a_form_with_its_own_date_widget_and_no_unit_is_asked_and_reopened_by_its_menu_code(self):
        """The r14 probes: a report's file name differs from its menu code, and many forms show no unit."""
        from pmos.browser.finereport import REOPEN_IDLE_SECONDS
        from pmos.validate import evaluate
        await self.finereport_page()
        request = {**self.finereport_request(), "frame": "lnsnxh_a.frm", "form": {"date_widget": "交易日期", "unit": None}}
        response = await self.transport.send(request)
        self.assertEqual(evaluate({"kind": "finereport_table"}, response).result, "saved")
        Site.finereport.clear()
        self.transport.finereport_sessions["lnsnxh_a.frm"] -= REOPEN_IDLE_SECONDS + 1
        await self.transport.send(request)
        self.assertEqual(Site.finereport[0], "map:lnsnxh_a", "reopened by the menu code, though the frame is found by the file name")

    async def test_a_session_idle_too_long_is_opened_anew_before_it_is_asked(self):
        from pmos.browser.finereport import REOPEN_IDLE_SECONDS
        await self.finereport_page()
        await self.transport.send(self.finereport_request())
        Site.finereport.clear()
        self.transport.finereport_sessions["lnsnxh_a"] -= REOPEN_IDLE_SECONDS + 1
        await self.transport.send(self.finereport_request(day="2026-10-29"))
        self.assertEqual(Site.finereport, ["map:lnsnxh_a", "document", "form_getsource", "parameters_d", "load_content", "load_report_content"])

    async def test_a_session_found_ended_is_no_answer_and_the_next_try_opens_a_new_one(self):
        await self.finereport_page()
        await self.transport.send(self.finereport_request())
        Site.finereport.clear()
        Site.finereport_dead = True
        with self.assertRaises(Fault) as caught:
            await self.transport.send(self.finereport_request(day="2026-10-29"))
        self.assertEqual((caught.exception.code, caught.exception.unsent), ("finereport_session_ended", True))
        self.assertEqual(Site.finereport, ["parameters_d"], "nothing more was asked")
        response = await self.transport.send(self.finereport_request(day="2026-10-29"))
        self.assertEqual(response.status, 200)
        self.assertEqual(Site.finereport[1:3], ["map:lnsnxh_a", "document"])

    async def test_a_blank_report_is_an_empty_day(self):
        from pmos.validate import evaluate
        await self.finereport_page()
        Site.finereport_blank = True
        response = await self.transport.send(self.finereport_request())
        self.assertEqual(evaluate({"kind": "finereport_daily"}, response).result, "empty")

    async def test_another_report_opens_through_the_same_component(self):
        await self.finereport_page()
        await self.transport.send(self.finereport_request())
        Site.finereport.clear()
        await self.transport.send(self.finereport_request(source="lnsnxh_b"))
        self.assertEqual(Site.finereport[:2], ["map:lnsnxh_b", "document"])

    async def test_a_template_prime_chooses_the_first_listed_option(self):
        """Anhui 2026-11-01 14:49: the task's own option is not listed; any listed one shows the request."""
        from pmos.browser import ui
        await self.page.goto(self.origin + "/select_page.html")
        await self.page.evaluate("document.getElementById('pick').value = ''")
        await ui.fill(self.page.main_frame, {"kind": "select", "label": "断面名称", "value": "不存在的线",
                                             "any_listed": True}, 5)
        self.assertEqual(await self.page.input_value("#pick"), "甲线")
        self.assertEqual(await self.page.evaluate("window.queries"), 0, "filling never queries")
        self.assertEqual(Site.received, [])

    async def test_a_failed_choice_leaves_the_dropdown_closed_so_the_next_one_can_work(self):
        """Anhui r19 15:48:20: the bootstrap's missing option left it open; the prime's click closed it instead."""
        from pmos.browser import ui
        await self.page.goto(self.origin + "/select_page.html?toggle=1")
        frame = self.page.main_frame
        with self.assertRaises(Fault) as caught:
            await ui.fill(frame, {"kind": "select", "label": "断面名称", "value": "不存在的线"}, 1)
        self.assertEqual(caught.exception.code, "select_option_missing")
        await self.page.evaluate("document.getElementById('pick').value = ''")
        await ui.fill(frame, {"kind": "select", "label": "断面名称", "value": "不存在的线", "any_listed": True}, 5)
        self.assertEqual(await self.page.input_value("#pick"), "甲线")
        self.assertEqual(await self.page.evaluate("window.queries"), 0)

    async def test_a_dropdown_something_else_left_open_is_closed_first(self):
        from pmos.browser import ui
        await self.page.goto(self.origin + "/select_page.html?toggle=1")
        await self.page.click("#pick")
        await ui.fill(self.page.main_frame, {"kind": "select", "label": "断面名称", "value": "乙线"}, 5)
        self.assertEqual(await self.page.input_value("#pick"), "乙线")

    async def test_an_input_showing_the_options_own_value_has_committed(self):
        """Anhui r20 15:58:37, 6d80: wanted 安徽.长丰厂/20kV.长丰厂#1机, got 3378262674776262248, its internal ID."""
        from pmos.browser import ui
        await self.page.goto(self.origin + "/select_page.html?rawvalue=1")
        await ui.fill(self.page.main_frame, {"kind": "select", "label": "断面名称", "value": "丙机"}, 1)
        self.assertEqual(await self.page.input_value("#pick"), "3378262674776260623")

    async def test_a_value_that_never_commits_says_which_field_and_what_it_holds(self):
        from pmos.browser import ui
        await self.page.goto(self.origin + "/select_page.html")
        await self.page.evaluate("document.querySelectorAll('.el-select-dropdown__item').forEach(i => i.onclick = () => {})")
        with self.assertRaises(Fault) as caught:
            await ui.fill(self.page.main_frame, {"kind": "select", "label": "断面名称", "value": "乙线"}, 1)
        self.assertEqual(caught.exception.code, "form_value_not_committed")
        self.assertEqual(caught.exception.detail, {"field": "断面名称", "wanted": "乙线", "got": "甲线"})

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

    async def test_a_template_body_replay_sends_the_pages_own_body_with_only_the_date_changed(self):
        """Anhui 2026-11-01 12:5x: templates kept no body, so every template_body_set replay failed unsent."""
        import asyncio
        await self.page.goto(self.origin + "/component_page.html")
        page_body = {"businessTime": "2026-10-28", "taskId": "t-9", "genName": "甲线"}
        with self.transport.templates.window():
            await self.page.evaluate("b => fetch('/px-x/query', {method: 'POST', body: JSON.stringify(b)})", page_body)
            await asyncio.sleep(0.5)
        Site.received.clear()
        Site.bodies.clear()
        response = await self.transport.send({"transport": "fetch", "method": "POST", "path": "/px-x/query",
                                              "template_body_set": {"businessTime": "2026-10-26"}})
        self.assertEqual(json.loads(response.body)["status"], 0)
        self.assertEqual(Site.received, ["/px-x/query"])
        self.assertEqual(json.loads(Site.bodies[0]), {**page_body, "businessTime": "2026-10-26"})
        self.assertIsNone(self.transport.templates.items["/px-x/query"]["body"], "a plain replay still sends none")

    async def test_after_a_recycle_a_template_of_the_closed_page_is_sent_from_the_new_page(self):
        """Anhui 2026-11-01 13:42/13:55: replays hit the closed page's main frame and read as a lost driver."""
        import asyncio
        await self.page.goto(self.origin + "/component_page.html")
        with self.transport.templates.window():
            await self.page.evaluate("() => fetch('/px-x/query', {method: 'POST', body: JSON.stringify({businessTime: '2026-10-28'})})")
            await asyncio.sleep(0.5)
        fresh = await self.page.context.new_page()
        await fresh.goto(self.origin + "/component_page.html")
        old = self.transport.page
        self.transport.page, self.transport.main_frame_fallback = fresh, True
        await old.close()
        request = {"transport": "fetch", "method": "POST", "path": "/px-x/query", "template_body_set": {"businessTime": "2026-10-27"}}
        self.assertTrue(self.transport.ready(request))
        Site.received.clear()
        response = await self.transport.send(request)
        self.assertEqual(json.loads(response.body)["status"], 0)
        self.assertEqual(Site.received, ["/px-x/query"], "sent from the new page")
        self.transport.main_frame_fallback = False
        self.assertFalse(self.transport.ready(request), "without the fallback the closed page's template waits")

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
