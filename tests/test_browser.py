"""Browser boundary with fake Playwright objects (no browser, no network)."""
import asyncio
import base64
import unittest
from pathlib import Path

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


class Context:
    def __init__(self):
        self.listeners = []

    def on(self, event, listener):
        self.listeners.append((event, listener))

    def remove_listener(self, event, listener):
        self.listeners.remove((event, listener))


class OperationScopedObservation(unittest.IsolatedAsyncioTestCase):
    async def test_requests_are_observed_only_inside_pmos_operations(self):
        templates, context, spawned = Templates(ORIGIN), Context(), []
        templates.observe(context, spawned.append)
        self.assertEqual(context.listeners, [], "no driver objects for background traffic")
        with templates.window():
            with templates.window():
                self.assertEqual(len(context.listeners), 1)
            self.assertTrue(templates.listening, "nested windows keep listening")
            context.listeners[0][1](Request("/px-x/export", Frame(), {"X-Token": "t"}))
        self.assertEqual(context.listeners, [])
        for coroutine in spawned:
            await coroutine
        self.assertEqual(templates.get("/px-x/export")["headers"], {"x-token": "t"}, "template kept for replay")

    def test_always_mode_keeps_listening_and_detach_removes_everything(self):
        templates, context = Templates(ORIGIN), Context()
        templates.observe(context, lambda c: None, always=True)
        with templates.window():
            pass
        self.assertEqual(len(context.listeners), 1)
        templates.detach()
        self.assertEqual(context.listeners, [])


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


class ClosedPageTemplates(unittest.TestCase):
    def test_a_template_on_a_closed_page_counts_as_gone(self):
        from pmos.browser.transport import Templates

        class Page:
            def __init__(self, closed):
                self.closed = closed

            def is_closed(self):
                return self.closed

        class Frame:
            def __init__(self, page):
                self.page = page

            def is_detached(self):
                return False                      # Playwright keeps a closed page's main frame attached
        templates = Templates("https://x.test")
        templates.items["/p"] = {"frame": Frame(Page(True)), "headers": {}, "method": "POST", "body": None,
                                 "page_body": "{}", "at": templates.clock()}
        new_main = Frame(Page(False))
        self.assertIs(templates.get("/p", fallback_frame=new_main)["frame"], new_main)
        with self.assertRaises(Fault):
            templates.get("/p")


class BringForward(unittest.IsolatedAsyncioTestCase):
    """Anhui 2026-11-01 12:07: page work in a hidden Edge tab waited out every timeout."""

    async def test_a_hidden_tab_comes_to_the_front_before_page_work_and_a_visible_one_is_left(self):
        from pmos.browser.ui import bring_forward

        class Page:
            def __init__(self, state):
                self.state, self.fronted = state, 0

            async def evaluate(self, script):
                if isinstance(self.state, Exception):
                    raise self.state
                return self.state

            async def bring_to_front(self):
                self.fronted += 1
        for state, fronted in (("hidden", 1), ("visible", 0), (RuntimeError("navigating"), 1)):
            page = Page(state)
            await bring_forward(page)
            self.assertEqual(page.fronted, fronted, state)


class AttachTests(unittest.TestCase):
    def test_only_loopback_cdp(self):
        self.assertEqual(cdp_endpoint({"cdp_url": "http://127.0.0.1:9222"}), "http://127.0.0.1:9222")
        for bad in ("http://0.0.0.0:9222", "http://10.0.0.2:9222", "ws://example.com:9222"):
            with self.assertRaises(AttachError):
                cdp_endpoint({"cdp_url": bad})
        with self.assertRaisesRegex(AttachError, "missing"):
            cdp_endpoint({"profile_dir": "/nonexistent/profile"})

    def test_the_port_file_is_read_through_a_brief_eperm(self):
        import errno
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as profile:
            (Path(profile) / "DevToolsActivePort").write_text("9333\n/devtools/browser/ab-12\n", encoding="ascii")
            real, refused = Path.lstat, []

            def lstat(path):
                if path.name == "DevToolsActivePort" and len(refused) < 2:
                    refused.append(path)
                    raise PermissionError(errno.EPERM, "Operation not permitted")
                return real(path)
            current = "ws://127.0.0.1:9333/devtools/browser/cd-34"
            with patch.object(Path, "lstat", lstat), patch("pmos.fsretry.time.sleep"), \
                    patch("pmos.browser.attach._browser_socket", return_value=current):
                self.assertEqual(cdp_endpoint({"profile_dir": profile}), current)
            self.assertEqual(len(refused), 2)

    def test_the_running_browser_names_its_socket_and_the_port_must_belong_to_the_profile(self):
        """Anhui 2026-11-01: the file kept the GUID of an older launch (404); the port was right."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as profile:
            (Path(profile) / "DevToolsActivePort").write_text("49290\n/devtools/browser/old-guid\n", encoding="ascii")
            config = {"profile_dir": profile, "profile_process_check": True}
            mine = f"/Applications/Edge --remote-debugging-port=49290 --user-data-dir={Path(profile).resolve()} --flag\n"
            with patch("pmos.browser.attach._browser_socket", return_value="ws://127.0.0.1:49290/devtools/browser/new-guid"):
                with patch("pmos.browser.attach._listener_commands", return_value=[mine]):
                    self.assertEqual(cdp_endpoint(config), "ws://127.0.0.1:49290/devtools/browser/new-guid")
                for others in ([], [mine.replace(str(Path(profile).resolve()), "/tmp/another-profile")],
                               [mine, "/Applications/Other --user-data-dir=/tmp/x\n"]):
                    with patch("pmos.browser.attach._listener_commands", return_value=others):
                        with self.assertRaisesRegex(AttachError, "devtools_port_not_this_profile"):
                            cdp_endpoint(config)
            with patch("pmos.browser.attach._browser_socket", return_value="ws://127.0.0.1:9999/devtools/browser/x"):
                with self.assertRaisesRegex(AttachError, "devtools_version_invalid"):
                    cdp_endpoint({"profile_dir": profile})


if __name__ == "__main__":
    unittest.main()


class ExpectWithoutWaiter(unittest.IsolatedAsyncioTestCase):
    """transport._Expect: page.expect_* semantics with a plain listener that is always removed."""

    class Page:
        def __init__(self):
            self.listeners = {}

        def on(self, event, handler):
            self.listeners.setdefault(event, []).append(handler)

        def remove_listener(self, event, handler):
            self.listeners[event].remove(handler)

        def emit(self, event, value):
            for handler in list(self.listeners.get(event, [])):
                handler(value)

    async def test_the_first_matching_event_resolves_and_the_listener_goes(self):
        from pmos.browser.transport import _Expect
        page = self.Page()
        async with _Expect(page, "response", lambda r: r == "wanted", timeout=1000) as captured:
            page.emit("response", "other")
            page.emit("response", "wanted")
        self.assertEqual(await captured.value, "wanted")
        self.assertEqual(page.listeners["response"], [])

    async def test_timeout_and_an_exception_in_the_block_both_remove_the_listener(self):
        from pmos.browser.transport import _Expect
        page = self.Page()
        async with _Expect(page, "download", timeout=50) as event:
            pass
        with self.assertRaises(asyncio.TimeoutError):
            await event.value
        self.assertEqual(page.listeners["download"], [])
        with self.assertRaises(RuntimeError):
            async with _Expect(page, "response", timeout=1000):
                raise RuntimeError("page call failed")
        self.assertEqual(page.listeners["response"], [])
