"""status --mail: what a status mail bridge reads from a PMOS instance (read-only)."""
import time
import unittest

from pmos.engine import Engine
from pmos.periods import today
from pmos.reporting import status_metrics

from helpers import Clock, FakeTransport, TempDir, dataset, make_province

TODAY = today().isoformat()


class StatusMetrics(unittest.IsolatedAsyncioTestCase):
    async def test_counts_originals_requests_and_the_latest_success_from_a_running_instance(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock(time.time())
        first = today().fromordinal(today().toordinal() - 1).isoformat()
        province = make_province(tmp.path, [dataset("report", start=first)], policy={"response_interval_seconds": 15})
        engine = Engine(province, FakeTransport(clock, duration=1), instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        await engine.tick()
        clock.advance(20)
        await engine.tick()
        metrics = status_metrics(tmp.path / "var", now=clock())
        self.assertEqual((metrics["originals"], metrics["last_hour_files_mtime"], metrics["missing_originals"]), (2, 2, 0))
        self.assertGreater(metrics["bytes"], 0)
        self.assertEqual(metrics["requests_last_hour"], 2)
        self.assertEqual(metrics["tasks"], {"saved": 2})
        self.assertEqual((metrics["blocked"], metrics["held_datasets"]), (False, []))
        self.assertEqual(metrics["current_dataset"], "report")
        self.assertIsNotNone(metrics["business_last_success_at"])
        self.assertIsNone(metrics["keeper_last_success_at"])
        engine.state.block("auth_expired", category="auth", keepalive_allowed=False)
        engine.hold_dataset("report", "unsent:component_missing", "paused")
        metrics = status_metrics(tmp.path / "var", now=clock())
        self.assertEqual((metrics["blocked"], metrics["block_reason"], metrics["held_datasets"]),
                         (True, "auth_expired", ["report"]))

    def test_the_mail_bridge_contract_keeps_its_fields(self):
        """Shanxi's report_bridge runs `status --mail` from the newest release dir: never drop or rename a field."""
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        self.assertLessEqual({
            "originals", "bytes", "last_hour_files_mtime", "missing_originals", "requests_last_hour", "tasks",
            "attempts", "phase", "current_dataset", "status_updated_at", "worker_pid", "blocked", "block_reason",
            "held_datasets", "business_last_success_at", "keeper_last_success_at", "generated_at"},
            set(status_metrics(tmp.path)), "added fields are fine; none of these may go")

    def test_the_headline_says_paused_or_stalled_at_first_sight(self):
        from pmos.reporting import headline
        self.assertEqual(headline("collecting", {"reason": "auth_expired"}, 1000, 2000)["headline"], "已暂停：auth_expired")
        self.assertEqual(headline("paused", None, 1000, 1100)["headline"], "已暂停（paused）")
        self.assertEqual(headline("waiting", None, 1000, 1000 + 45 * 60), {"stalled_minutes": 45, "headline": "已停滞 45 分钟"})
        self.assertEqual(headline("collecting", None, 1000, 1060)["headline"], "正常")
        self.assertEqual(headline("quiet_window", None, 1000, 1000 + 40 * 60)["headline"], "正常")

    def test_an_empty_instance_reports_zeros(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        metrics = status_metrics(tmp.path)
        self.assertEqual((metrics["originals"], metrics["requests_last_hour"], metrics["phase"]), (0, 0, None))


if __name__ == "__main__":
    unittest.main()
