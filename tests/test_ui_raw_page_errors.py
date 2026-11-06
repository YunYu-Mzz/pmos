"""A page step that gives up with a raw Playwright error is that report's page failure, not the province's network.

Anhui r23 2026-11-01 22:14: the first request after a restart (a whole-day template prime) threw a raw
timeout out of ``ensure_frame``; the engine recorded ``transport_exception`` (network) and blocked the
whole province. Before the report's own query nothing of ours was sent: it is a local, unsent fault, so
only that dataset waits.
"""
import unittest
from unittest.mock import AsyncMock, patch

from pmos.browser import ui
from pmos.browser.transport import PageTransport
from pmos.faults import Fault

CLOSED = "Target page, context or browser has been closed"


class SelectMenu(unittest.IsolatedAsyncioTestCase):
    async def run_menu(self, error, *, leaf_clicked=False):
        async def inner(page, labels, timeout, hover_exit, force, state):
            state["leaf_clicked"] = leaf_clicked
            raise error

        with patch.object(ui, "bring_forward", AsyncMock()), patch.object(ui, "_select_menu", inner):
            await ui.select_menu(object(), ["现货市场", "省内现货市场"], 1)

    async def test_raw_error_before_the_leaf_click_is_local_and_unsent(self):
        with self.assertRaises(Fault) as caught:
            await self.run_menu(TimeoutError("Timeout 45000ms exceeded."))
        fault = caught.exception
        self.assertEqual((fault.code, fault.category, fault.unsent), ("menu_click_failed", "local", True))
        self.assertEqual(fault.detail, {"error": "TimeoutError"})

    async def test_raw_error_after_the_leaf_click_stays_raw(self):
        with self.assertRaises(TimeoutError):
            await self.run_menu(TimeoutError("Timeout 45000ms exceeded."), leaf_clicked=True)

    async def test_a_dead_driver_stays_the_engines(self):
        with self.assertRaises(Exception) as caught:
            await self.run_menu(RuntimeError(CLOSED))
        self.assertNotIsInstance(caught.exception, Fault)

    async def test_faults_keep_their_code(self):
        with self.assertRaises(Fault) as caught:
            await self.run_menu(Fault("menu_navigation_timeout", "local"))
        self.assertEqual((caught.exception.code, caught.exception.unsent), ("menu_navigation_timeout", True))


class FrameStep(unittest.IsolatedAsyncioTestCase):
    def transport(self):
        transport = PageTransport.__new__(PageTransport)
        transport.page, transport.timeout = object(), 1
        return transport

    async def run_ui(self, error):
        with patch.object(ui, "bring_forward", AsyncMock()), patch.object(ui, "ensure_frame", AsyncMock(side_effect=error)):
            await self.transport()._ui({"action": "list"})

    async def test_raw_error_while_opening_the_frame_is_local_and_unsent(self):
        with self.assertRaises(Fault) as caught:
            await self.run_ui(TimeoutError("Timeout 45000ms exceeded."))
        fault = caught.exception
        self.assertEqual((fault.code, fault.category, fault.unsent), ("page_step_failed", "local", True))

    async def test_a_dead_driver_stays_the_engines(self):
        with self.assertRaises(Exception) as caught:
            await self.run_ui(RuntimeError(CLOSED))
        self.assertNotIsInstance(caught.exception, Fault)

    async def test_faults_pass_through(self):
        with self.assertRaises(Fault) as caught:
            await self.run_ui(Fault("report_frame_missing", "local"))
        self.assertEqual(caught.exception.code, "report_frame_missing")


if __name__ == "__main__":
    unittest.main()
