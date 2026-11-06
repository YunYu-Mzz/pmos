"""Anhui r25 (2026-11-01): items an "expired login" answer left isolated can be reopened by the user; ack resets stale failures."""
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from pmos import cli
from pmos.ledger import Ledger
from pmos.state import StateStore


class Province:
    id = "anhui"

    def __init__(self, root):
        self.root = Path(root)

    def instance(self):
        return self.root


class ReleaseAuthExpired(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "ledger.sqlite3")
        self.ledger.seed("report", ["2026-10-30"], "s1")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def expired(self, status=200, business="401", reason="auth_expired"):
        attempt = self.ledger.begin("collect", "report", "2026-10-30", "s1")
        self.ledger.finish(attempt, "failed", http_status=status, business_status=business, reason=reason)
        self.ledger.settle_task("report", "2026-10-30", "failed", scope="s1",
                                reason="isolated:consumed_identity_requires_review")
        return attempt

    def test_an_expired_login_answer_returns_its_item_to_pending(self):
        attempt = self.expired()
        self.assertTrue(self.ledger.consumed("report", "2026-10-30", "s1"))
        with self.assertRaisesRegex(ValueError, "release_approval_required"):
            self.ledger.release_auth_expired([attempt], " ")
        self.assertEqual(self.ledger.release_auth_expired([attempt], "同意"), [(attempt, "report", "2026-10-30", "s1")])
        task = self.ledger.task("report", "2026-10-30", "s1")
        self.assertEqual((task["status"], task["reason"]), ("pending", None))
        self.assertFalse(self.ledger.consumed("report", "2026-10-30", "s1"))
        with self.assertRaisesRegex(ValueError, "not_failed|not_an_expired_login"):
            self.ledger.release_auth_expired([attempt], "同意")      # not twice

    def test_other_answers_are_refused(self):
        for status, business, reason in ((200, "40302", "permission_denied"), (200, "401", "other"), (500, "401", "auth_expired")):
            self.ledger.seed("report", ["2026-10-31"], "s1")
            attempt = self.ledger.begin("collect", "report", "2026-10-31", "s1")
            self.ledger.finish(attempt, "failed", http_status=status, business_status=business, reason=reason)
            with self.assertRaisesRegex(ValueError, "not_an_expired_login"):
                self.ledger.release_auth_expired([attempt], "同意")

    def test_an_item_with_another_real_attempt_is_refused(self):
        first = self.ledger.begin("collect", "report", "2026-10-30", "s1")
        self.ledger.finish(first, "unknown", reason="network_error")
        attempt = self.expired()
        with self.assertRaisesRegex(ValueError, "item_has_other_attempts"):
            self.ledger.release_auth_expired([attempt], "同意")
        self.assertEqual(self.ledger.task("report", "2026-10-30", "s1")["status"], "failed")

    def test_the_command_records_an_event(self):
        attempt = self.expired()
        self.ledger.close()
        cli.cmd_release_auth_expired(Province(self.root), Namespace(attempts=str(attempt), approval="同意"))
        self.ledger = Ledger(self.root / "ledger.sqlite3")
        self.assertEqual(self.ledger.task("report", "2026-10-30", "s1")["status"], "pending")
        events = (self.root / "state" / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("auth_expired_released", events)


class AckResetsFailures(unittest.TestCase):
    def test_ack_of_page_operations_failing_clears_the_stale_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            province = Province(tmp)
            state = StateStore(province.instance() / "state")
            state.write("page_operation_failures", [{"dataset": "a", "reason": "menu_navigation_timeout", "at": "x"}] * 3)
            state.block("page_operations_failing", category="local", keepalive_allowed=True)
            cli.cmd_ack(province, Namespace(reason="page_operations_failing", approval="同意"))
            self.assertEqual(state.read("page_operation_failures"), [])
            self.assertIsNone(state.blocked())

    def test_ack_of_another_block_leaves_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            province = Province(tmp)
            state = StateStore(province.instance() / "state")
            state.write("page_operation_failures", [{"dataset": "a", "reason": "x", "at": "x"}])
            state.block("auth_expired", category="auth", keepalive_allowed=False)
            cli.cmd_ack(province, Namespace(reason="auth_expired", approval="同意"))
            self.assertEqual(len(state.read("page_operation_failures")), 1)


if __name__ == "__main__":
    unittest.main()
