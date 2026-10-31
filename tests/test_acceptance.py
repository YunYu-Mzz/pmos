"""Acceptance mode: a handoff receipt is the only way to run, and the run ends at its budget."""
import asyncio
import os
import unittest
from unittest.mock import patch

from pmos import cli
from pmos.ledger import Ledger
from pmos.periods import today
from pmos.queue_compat import QueueContext, import_shadow

from helpers import Clock, FakeTransport, TempDir, dataset, envelope, excel, make_province

TODAY = today().isoformat()


class Acceptance(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.data = self.tmp.path / "data"
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(self.data)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.province = make_province(self.tmp.path, [dataset("load", start=TODAY)],
                                      policy={"response_interval_seconds": 15})

    def handoff(self, budget):
        instance = self.data / "testland"
        instance.mkdir(parents=True)
        ledger = Ledger(instance / "ledger.sqlite3")
        with patch("pmos.queue_compat.PROVINCES", {"testland", "liaoning", "anhui", "shanxi"}):
            import_shadow(ledger, [], QueueContext("testland", "acct"), source_id="queue", snapshot_sha256="a" * 64)
        ledger.grant_handoff({"version": 1, "province": "testland", "account_alias": "acct",
                              "legacy_owner": {"stopped_evidence": "fixture"},
                              "snapshots": [{"source_id": "queue", "snapshot_sha256": "a" * 64}], "scope": ["load"],
                              "request_budget": budget, "code_sha": "c", "approval": "fixture",
                              "session": {}, "seeding": "legacy_pending_only"})
        days = [(today().fromordinal(today().toordinal() - n)).isoformat() for n in range(4)]
        ledger.seed("load", days)
        ledger.close()

    def test_without_a_receipt_accept_refuses_before_any_browser_attach(self):
        with patch("pmos.cli._browser", side_effect=AssertionError("must not attach")):
            self.assertEqual(cli.main(["testland", "accept"]), 2) if "testland" in cli.province_ids() else None
            with self.assertRaisesRegex(ValueError, "acceptance_requires_a_handoff_ledger"):
                cli.acceptance_gate(self.province)

    async def test_the_run_uses_the_receipt_alias_and_stops_at_the_budget(self):
        self.handoff(budget=2)
        gate = cli.acceptance_gate(self.province)
        self.assertEqual((gate["request_budget"], gate["used"], gate["account_alias"]), (2, 0, "acct"))
        clock = Clock()
        transport = FakeTransport(clock, default=excel())

        async def browser(province):
            async def check():
                return None

            class Playwright:
                async def stop(self):
                    pass
            return Playwright(), transport, check, None
        real_engine = cli.Engine

        def engine(*args, **kwargs):
            instance = real_engine(*args, clock=clock, **kwargs)

            async def fast_tick(original=instance.tick):
                delay = await original()
                clock.advance(20)
                return 0
            instance.tick = fast_tick
            return instance
        with patch("pmos.cli._browser", browser), patch("pmos.cli.Engine", engine), \
                patch("pmos.cli._stop_event", lambda: asyncio.Event()):
            await asyncio.wait_for(cli._accept(self.province, gate), timeout=10)
        self.assertEqual(len(transport.sent), 2, "exactly the budget, then the run ends by itself")
        with self.assertRaisesRegex(ValueError, "acceptance_not_dispatchable:handoff_request_budget_exhausted"):
            cli.acceptance_gate(self.province)
        # a user-approved extension lets the same handoff continue, recorded beside the receipt
        ledger = Ledger(self.data / "testland" / "ledger.sqlite3")
        with self.assertRaisesRegex(ValueError, "approval_required"):
            ledger.extend_handoff(3, " ")
        self.assertEqual(ledger.extend_handoff(3, "user: continue")["request_budget"], 5)
        ledger.close()
        self.assertEqual(cli.acceptance_gate(self.province)["request_budget"], 5)

    async def test_stop_all_ends_an_acceptance_run_without_sending(self):
        self.handoff(budget=5)
        from pmos.state import StateStore
        gate = cli.acceptance_gate(self.province)
        transport = FakeTransport(Clock(), default=envelope())

        async def browser(province):
            async def check():
                return None

            class Playwright:
                async def stop(self):
                    pass
            return Playwright(), transport, check, None
        original = StateStore.set_control

        def set_control(store, **changes):
            # the operator's stop-all arrives right after the run started
            original(store, **changes)
            if changes.get("collect"):
                original(store, stop_all=True)
        with patch("pmos.cli._browser", browser), patch.object(StateStore, "set_control", set_control), \
                patch("pmos.cli._stop_event", lambda: asyncio.Event()):
            await asyncio.wait_for(cli._accept(self.province, gate), timeout=10)
        self.assertEqual(transport.sent, [])


if __name__ == "__main__":
    unittest.main()


class PauseResume(unittest.TestCase):
    def test_pause_and_resume_toggle_collection_of_the_running_worker(self):
        from pmos.state import StateStore
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        with patch.dict(os.environ, {"PMOS_DATA": str(tmp.path / "data")}):
            self.assertEqual(cli.main(["shanxi", "pause"]), 0)
            state = StateStore(tmp.path / "data" / "shanxi" / "state")
            self.assertTrue(state.control()["paused"])
            self.assertEqual(cli.main(["shanxi", "resume"]), 0)
            self.assertFalse(state.control()["paused"])
