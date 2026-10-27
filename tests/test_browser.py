"""Browser boundary with fake Playwright objects (no browser, no network)."""
import base64
import unittest

from pmos.browser.attach import AttachError, cdp_endpoint
from pmos.browser.transport import PageTransport, Templates
from pmos.faults import Fault

ORIGIN = "https://pmos.test.sgcc.com.cn"


class Frame:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []
        self.url = ORIGIN + "/pxf-x/#/report"

    def is_detached(self):
        return False

    async def evaluate(self, script, args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result


class Request:
    def __init__(self, path, frame, headers, resource_type="xhr", method="POST"):
        self.url, self.frame, self._headers = ORIGIN + path, frame, headers
        self.resource_type, self.method, self.post_data = resource_type, method, '{"date":"2026-10-01"}'

    async def all_headers(self):
        return self._headers


class TemplateTests(unittest.IsolatedAsyncioTestCase):
    async def capture(self, templates, request):
        coroutines = []
        templates._request(request, coroutines.append)
        for coroutine in coroutines:
            await coroutine

    async def test_keeps_only_gateway_headers_and_skips_background_and_own_calls(self):
        templates = Templates(ORIGIN)
        frame = Frame()
        headers = {"X-Token": "t", "CurrentRoute": "/r", "Cookie": "secret", "Content-Type": "application/json"}
        await self.capture(templates, Request("/px-x/export", frame, headers))
        self.assertEqual(templates.get("/px-x/export")["headers"],
                         {"x-token": "t", "currentroute": "/r", "content-type": "application/json"})
        await self.capture(templates, Request("/px-common-authority/framework/login/refreshExpireTime", frame, headers))
        await self.capture(templates, Request("/static/app.js", frame, headers, resource_type="script"))
        templates.sending = 1
        await self.capture(templates, Request("/px-x/other", frame, headers))
        self.assertEqual(set(templates.items), {"/px-x/export"})
        with self.assertRaisesRegex(Fault, "request_template_missing"):
            templates.get("/px-x/other")


class FetchTransportTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, frame):
        templates = Templates(ORIGIN)
        templates.items["/px-x/export"] = {"frame": frame, "headers": {"x-token": "t"}, "method": "POST",
                                           "body": "{}", "at": templates.clock()}
        return PageTransport(None, ORIGIN, templates, 45)

    async def test_replays_template_headers_with_new_body(self):
        frame = Frame({"status": 200, "contentType": "application/vnd.ms-excel",
                       "base64": base64.b64encode(b"PK..").decode()})
        response = await self.transport(frame).send({"transport": "fetch", "method": "POST", "path": "/px-x/export",
                                                     "template": "/px-x/export", "body": {"date": "2026-10-02"}})
        self.assertEqual((response.status, response.body), (200, b"PK.."))
        self.assertEqual(frame.calls[0]["body"], '{"date":"2026-10-02"}')
        self.assertEqual(frame.calls[0]["headers"], {"x-token": "t"})
        self.assertEqual(frame.calls[0]["url"], ORIGIN + "/px-x/export")

    async def test_ready_reports_template_presence_without_sending(self):
        frame = Frame()
        transport = self.transport(frame)
        self.assertTrue(transport.ready({"transport": "fetch", "path": "/px-x/export"}))
        self.assertFalse(transport.ready({"transport": "fetch", "path": "/px-x/missing"}))
        self.assertTrue(transport.ready({"transport": "ui", "frame": "x"}))
        self.assertEqual(frame.calls, [])

    async def test_browser_side_failure_is_a_network_fault(self):
        transport = self.transport(Frame(error=RuntimeError("AbortError with token xyz")))
        with self.assertRaises(Fault) as caught:
            await transport.send({"transport": "fetch", "method": "POST", "path": "/px-x/export",
                                  "template": "/px-x/export", "body": {}})
        self.assertEqual((caught.exception.code, caught.exception.category), ("network_error", "network"))
        self.assertNotIn("xyz", str(caught.exception))


class AttachTests(unittest.TestCase):
    def test_only_loopback_cdp(self):
        self.assertEqual(cdp_endpoint({"cdp_url": "http://127.0.0.1:9222"}), "http://127.0.0.1:9222")
        for bad in ("http://0.0.0.0:9222", "http://10.0.0.2:9222", "ws://example.com:9222"):
            with self.assertRaises(AttachError):
                cdp_endpoint({"cdp_url": bad})
        with self.assertRaisesRegex(AttachError, "missing"):
            cdp_endpoint({"profile_dir": "/nonexistent/profile"})


if __name__ == "__main__":
    unittest.main()
