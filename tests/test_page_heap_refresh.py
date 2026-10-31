"""The site tab is reloaded between ticks once its JS heap passes the limit."""
import asyncio
import unittest
from unittest.mock import patch

from pmos import memory
from pmos.state import StateStore

from helpers import Clock, TempDir

MIB = 1024 ** 2


class FakePage:
    def __init__(self, heaps, fail_reload=False):
        self.heaps, self.reloads, self.fail_reload = list(heaps), 0, fail_reload

    async def evaluate(self, script):
        return self.heaps.pop(0) if len(self.heaps) > 1 else self.heaps[0]

    async def reload(self, **kwargs):
        if self.fail_reload:
            raise RuntimeError("navigation failed")
        self.reloads += 1


class FakeTemplates:
    def __init__(self):
        self.items = {"/path": {"frame": object()}}


class FakeTransport:
    def __init__(self):
        self.templates = FakeTemplates()


async def no_sleep(_):
    return None


class PageHeapRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.state = StateStore(self.tmp.path / "state")
        self.clock = Clock()

    def refresh(self, page, transport=None):
        return memory.PageHeapRefresh(self.state, page, transport or FakeTransport(), limit_mib=300,
                                      check_every=120, min_gap=600, settle=0, clock=self.clock)

    def run_once(self, refresh):
        with patch.object(memory.asyncio, "sleep", no_sleep):
            asyncio.run(refresh())

    def test_small_heap_is_left_alone(self):
        page = FakePage([100 * MIB])
        self.run_once(self.refresh(page))
        self.assertEqual(page.reloads, 0)
        self.assertIsNone(self.state.read("page_heap_refresh"))

    def test_large_heap_reloads_once_and_clears_templates(self):
        page, transport = FakePage([549 * MIB, 79 * MIB]), FakeTransport()
        refresh = self.refresh(page, transport)
        self.run_once(refresh)
        self.assertEqual(page.reloads, 1)
        self.assertEqual(transport.templates.items, {})
        record = self.state.read("page_heap_refresh")
        self.assertEqual((record["status"], record["before_mib"], record["after_mib"]), ("reloaded", 549, 79))
        self.assertEqual(record["website_business_requests"], 0)
        # Within the minimum gap nothing is checked or reloaded again.
        page.heaps = [600 * MIB]
        self.clock.advance(300)
        self.run_once(refresh)
        self.assertEqual(page.reloads, 1)
        self.clock.advance(301)
        self.run_once(refresh)
        self.assertEqual(page.reloads, 2)

    def test_checks_are_rate_limited(self):
        page = FakePage([100 * MIB])
        refresh = self.refresh(page)
        self.run_once(refresh)
        page.heaps = [700 * MIB]
        self.clock.advance(60)
        self.run_once(refresh)
        self.assertEqual(page.reloads, 0)
        self.clock.advance(61)
        self.run_once(refresh)
        self.assertEqual(page.reloads, 1)

    def test_failed_reload_turns_it_off_without_retry(self):
        page = FakePage([700 * MIB], fail_reload=True)
        refresh = self.refresh(page)
        self.run_once(refresh)
        self.assertEqual(self.state.read("page_heap_refresh")["status"], "failed_no_retry")
        page.fail_reload = False
        self.clock.advance(3600)
        self.run_once(refresh)
        self.assertEqual(page.reloads, 0)


if __name__ == "__main__":
    unittest.main()
