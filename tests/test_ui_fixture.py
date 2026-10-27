"""ui list/record actions against a local Element UI page in a throwaway headless browser.

Nothing here touches a real site or a user browser profile: the fixture is
served from 127.0.0.1 and the browser runs with a temporary profile. Skipped
when no Chromium-family browser is available to Playwright.
"""
import functools
import http.server
import json
import threading
import unittest
import os
from pathlib import Path

from pmos.browser import ui
from pmos.browser.transport import PageTransport, Templates
from pmos.faults import Fault
from pmos.validate import evaluate

FIXTURES = Path(__file__).parent / "fixtures"
MENU = ["中长期市场", "市场运行信息", "市场信息披露报告(月)"]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get("PMOS_LOCAL_UI_TEST") == "1", "local UI browser test is opt-in")
class UiFixtureTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=FIXTURES))
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
        context = await self.browser.new_context(accept_downloads=True)
        self.page = await context.new_page()
        await self.page.goto(self.origin + "/element_ui.html")
        self.transport = PageTransport(self.page, self.origin, Templates(self.origin), 4)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    def request(self, action, **fields):
        return {"transport": "ui", "action": action, "frame": "/element_ui.html", "menu": MENU, **fields}

    async def listing(self, page):
        response = await self.transport.send(self.request("list", page=page))
        return json.loads(response.body)["data"]

    async def test_menu_path_listing_pages_and_article_parts(self):
        first = await self.listing(1)
        self.assertEqual((len(first["list"]), first["total"], first["last"], first["page"]), (10, 23, False, 1))
        self.assertEqual(first["list"][0]["title"], "2026年1月市场信息披露报告0")
        self.assertEqual(len({row["id"] for row in first["list"]}), 10)
        last = await self.listing(3)
        self.assertEqual((len(last["list"]), last["last"], last["page"]), (3, True, 3))
        # the leaf is current now: selecting it again clicks nothing
        await ui.select_menu(self.page, MENU, 5)
        second = await self.listing(2)
        article = second["list"][0]  # article 10: two attachments and two 预览 buttons
        response = await self.transport.send(self.request("record", record=article))
        self.assertEqual([p.name for p in response.parts], ["报告10.pdf", "附表10.pdf"])
        outcome = evaluate({"kind": "document"}, response)
        self.assertEqual([o.extension for o in outcome.detail["parts"]], [".pdf", ".pdf"])
        self.assertEqual(await self.page.locator(".el-dialog:visible").count(), 0, "dialog closed")
        # an article that has moved off its recorded page is found on another one
        text_only = {**second["list"][1], "page": 3}
        body = await self.transport.send(self.request("record", record=text_only))
        capture = json.loads(body.parts[0].body)
        self.assertEqual((body.parts[0].name, capture["tables"]), ("body.json", [[["电量", "12.5"]]]))
        with self.assertRaises(Fault) as empty:
            await self.transport.send(self.request("record", record=second["list"][2]))
        self.assertEqual(empty.exception.code, "record_detail_unavailable")
        missing = {"title": "不存在的文章", "published": "2026-01-01", "page": 1}
        with self.assertRaises(Fault) as gone:
            await self.transport.send(self.request("record", record=missing))
        self.assertEqual(gone.exception.code, "record_not_listed")


if __name__ == "__main__":
    unittest.main()
