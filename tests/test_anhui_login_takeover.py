"""Login takeover and template-expiry regressions; synthetic data only."""
import functools
import http.server
import os
import threading
import unittest
from pathlib import Path

from pmos.browser.attach import work_page, reenter_work_page
from pmos.browser.transport import PageTransport, Templates
from pmos.browser import ui
from pmos.engine import Engine
from pmos.faults import Fault
from pmos.periods import today
from helpers import Clock, FakeTransport, TempDir, dataset, envelope, excel, make_province


class SessionTakeover(unittest.IsolatedAsyncioTestCase):
    def engine(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        load = dataset("load", keepalive=True, start=today().isoformat())
        load["request"]["bootstrap"] = {"transport": "ui", "frame": "load", "action": "query",
                                            "path": "/px-test/load", "fields": []}
        province = make_province(tmp.path, [load], session={"verify_on_attach": True,
                                      "renewal_bootstrap_retry_seconds": 600})
        clock = Clock()
        transport = FakeTransport(clock)
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        engine.archive.store("load", today().isoformat(), excel().body, ".xlsx")
        self.addCleanup(engine.ledger.close)
        return engine, transport, clock

    async def test_attach_verifies_once_before_normal_collection(self):
        engine, transport, clock = self.engine()
        engine.state.set_control(collect=True)
        await engine.tick()
        self.assertEqual(engine.ledger.last_attempt("keepalive")["outcome"], "success")
        self.assertFalse(engine._session_bootstrap_pending)
        clock.advance(31)
        await engine.tick()
        self.assertEqual(engine.ledger.last_attempt("collect")["outcome"], "success")
        self.assertEqual(len(transport.sent), 2)

    async def test_proven_unsent_navigation_failure_retries_only_after_cooldown(self):
        engine, transport, clock = self.engine()
        transport.unready.add("/px-test/load")
        transport.queue.append(Fault("menu_navigation_timeout", "local", unsent=True))
        self.assertIsNone(await engine.keep_session(force=True))
        clock.advance(31)
        self.assertIsNone(await engine.keep_session(force=True))
        self.assertEqual(len(transport.sent), 1)
        clock.advance(601)
        self.assertEqual(await engine.keep_session(force=True), "renewal")
        self.assertIsNone(engine.state.read("renewal_bootstrap_failed"))

    async def test_historical_latch_after_success_does_not_disable_a_new_login(self):
        engine, transport, clock = self.engine()
        self.assertEqual(await engine.keep_session(force=True), "renewal")
        engine.state.write("renewal_bootstrap_failed", {"code": "menu_navigation_timeout", "path": "/px-test/load"})
        clock.advance(31)
        transport.unready.add("/px-test/load")
        self.assertEqual(await engine.keep_session(force=True), "renewal")
        self.assertIsNone(engine.state.read("renewal_bootstrap_failed"))

    async def test_auth_failure_blocks_and_is_not_retried(self):
        engine, transport, clock = self.engine()
        transport.queue.append(Fault("auth_expired", "auth"))
        await engine.tick()
        self.assertEqual(engine.state.blocked()["reason"], "auth_expired")
        clock.advance(700)
        await engine.tick()
        self.assertEqual(len(transport.sent), 1)

    async def baseline(self, *, changed=False):
        engine, transport, _ = self.engine()
        spec = engine.province.datasets()["load"]
        spec.response = {"kind": "json"}
        spec.save(engine.province.datasets_dir / "load.json")
        engine.province.session["verify_points_on_attach"] = 96
        body = envelope(data=list(range(96)))
        engine.archive.store("load", today().isoformat(), body.body, ".json")
        transport.default = envelope(data=list(range(95)) + [999]) if changed else body
        await engine.tick()
        return engine, transport

    async def test_attach_checks_the_archived_96_point_business_baseline(self):
        engine, _ = await self.baseline()
        self.assertIsNone(engine.state.blocked())
        self.assertFalse(engine._session_bootstrap_pending)

    async def test_changed_baseline_keeps_collection_blocked(self):
        engine, transport = await self.baseline(changed=True)
        self.assertEqual(engine.state.blocked()["reason"], "session_baseline_mismatch")
        self.assertTrue(engine._session_bootstrap_pending)
        self.assertEqual(len(transport.sent), 1)

    def test_expired_template_is_unready_before_dispatch(self):
        class Frame:
            def is_detached(self):
                return False
        clock = Clock()
        templates = Templates("https://example.test", clock, ttl=14400)
        templates.items["/px-test/query"] = {"at": clock(), "frame": Frame()}
        transport = PageTransport(None, "https://example.test", templates, 45)
        request = {"transport": "fetch", "path": "/px-test/query"}
        self.assertTrue(transport.ready(request))
        clock.advance(14401)
        self.assertFalse(transport.ready(request))
        self.assertNotIn("/px-test/query", templates.items)


FIXTURES = Path(__file__).parent / "fixtures"


class Site(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/column_page.html"):
            body = (FIXTURES / "anhui_tree_page.html").read_bytes()
            body = body.replace(b"show('\xe7\x8e\xb0\xe8\xb4\xa7\xe5\xb8\x82\xe5\x9c\xba');", "show('中长期市场');".encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


@unittest.skipUnless(os.environ.get("PMOS_LOCAL_UI_TEST") == "1", "local UI test is opt-in")
class LoginBrowser(unittest.IsolatedAsyncioTestCase):
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
        from playwright.async_api import async_playwright
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(channel="msedge", headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    async def test_waits_for_header_and_ignores_same_named_nested_label(self):
        await self.page.goto(self.origin + "/anhui_tree_page.html")
        await self.page.evaluate("""() => {
          show('中长期市场');
          const top = document.getElementById('top'); top.className = 'el-menu--horizontal';
          const duplicate = document.createElement('div'); duplicate.className = 'el-submenu__title';
          duplicate.textContent = '现货市场'; document.body.append(duplicate);
          top.style.display = 'none'; setTimeout(() => top.style.display = 'flex', 400);
        }""")
        await ui.select_section(self.page, "现货市场", 4)
        nodes = await self.page.locator(".el-tree-node").evaluate_all(ui._MENU_NODES_JS)
        self.assertTrue(any(n["path"] == ["现货市场"] for n in nodes))

    async def test_named_home_tab_is_not_mistaken_for_the_query_page(self):
        await self.page.goto(self.origin + "/anhui_portal.html#/dashboard")
        stale = await self.context.new_page()
        await stale.goto(self.origin + "/anhui_tree_page.html#/home")
        await stale.evaluate("() => { window.name = 'owned'; }")
        config = {"name": "owned", "open_from": "/anhui_portal.html#/dashboard",
                  "menu": ["信息披露", "综合查询"], "url_contains": "columnHomeLeftMenuNew",
                  "adopt_url_contains": "columnHomeLeftMenuNew", "initial_section": "现货市场"}
        page = await work_page(self.browser, self.origin, config, 5)
        self.assertIsNot(page, stale)
        self.assertIn("columnHomeLeftMenuNew", page.url)
        self.assertFalse(stale.is_closed(), "the original login tab stays")
        nodes = await page.locator(".el-tree-node").evaluate_all(ui._MENU_NODES_JS)
        self.assertTrue(any(n["path"] == ["现货市场"] for n in nodes))

    async def test_ambiguous_header_does_not_click_an_arbitrary_market(self):
        await self.page.goto(self.origin + "/anhui_tree_page.html")
        await self.page.evaluate("""() => { show('中长期市场');
          const top = document.getElementById('top'); top.className = 'el-menu--horizontal';
          top.append(top.firstElementChild.cloneNode(true)); }""")
        with self.assertRaises(Fault) as caught:
            await ui.select_section(self.page, "现货市场", 2)
        self.assertEqual(caught.exception.code, "menu_label_not_unique")

    async def test_relogin_opens_fresh_query_app_and_keeps_the_portal(self):
        await self.page.goto(self.origin + "/anhui_portal.html#/dashboard")
        stale = await self.context.new_page()
        await stale.goto(self.origin + "/column_page.html#/columnHomeLeftMenuNew")
        await stale.evaluate("() => { window.name = 'owned'; }")
        config = {"name": "owned", "open_from": "/anhui_portal.html#/dashboard",
                  "menu": ["信息披露", "综合查询"], "url_contains": "columnHomeLeftMenuNew",
                  "adopt_url_contains": "columnHomeLeftMenuNew", "initial_section": "现货市场"}
        fresh = await reenter_work_page(self.browser, self.origin, config)
        self.assertTrue(stale.is_closed())
        self.assertFalse(self.page.is_closed())
        self.assertIsNot(fresh, stale)
