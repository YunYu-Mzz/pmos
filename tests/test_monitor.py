"""The stop alert: what counts as stopped, one mail per condition, and one when it clears."""
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pmos import monitor

from helpers import TempDir

NOW = 1_800_000_000.0


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class Conditions(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.instance = self.tmp.path
        (self.instance / "state").mkdir()
        self.write("status", {"phase": "collecting", "updated_at": iso(NOW - 10)})
        self.answered(NOW - 10)

    def write(self, name, value):
        (self.instance / "state" / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False))

    def answered(self, when):
        with sqlite3.connect(self.instance / "ledger.sqlite3") as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts(kind TEXT, outcome TEXT, finished_at REAL)")
            db.execute("DELETE FROM attempts")
            db.execute("INSERT INTO attempts VALUES('collect','success',?)", (when,))

    def found(self, unit="active"):
        return monitor.conditions(self.instance, unit, NOW)

    def test_a_working_collection_has_none(self):
        self.assertEqual(self.found(), {})

    def test_a_stopped_service_is_told_unless_it_was_stopped_on_purpose(self):
        self.assertIn("worker_down", self.found("inactive"))
        self.write("control", {"collect": True, "stop_all": True})
        self.assertEqual(self.found("inactive"), {})

    def test_a_block_and_a_report_that_needs_a_person_are_told_a_cooling_report_is_not(self):
        self.write("block", {"reason": "auth_expired", "at": "x", "keepalive_allowed": False})
        self.write("dataset_holds", {"a": {"reason": "r", "at": "x"}, "b": {"reason": "r", "at": "x", "until": NOW + 60}})
        found = self.found()
        self.assertIn("auth_expired", found["blocked"])
        self.assertIn("held:a", found)
        self.assertNotIn("held:b", found)

    def test_no_answer_for_half_an_hour_is_a_stall_unless_there_is_nothing_to_ask(self):
        self.answered(NOW - 31 * 60)
        self.assertIn("stalled", self.found())
        self.write("status", {"phase": "caught_up", "updated_at": iso(NOW - 10)})
        self.assertEqual(self.found(), {})
        self.write("status", {"phase": "collecting", "updated_at": iso(NOW - 10)})
        self.write("control", {"collect": True, "paused": True})
        self.assertEqual(self.found(), {}, "paused on purpose")

    def test_a_worker_that_stopped_writing_its_status_is_hung(self):
        self.write("status", {"phase": "collecting", "updated_at": iso(NOW - 301)})
        self.assertIn("worker_hung", self.found())

    def test_a_full_disk_is_told(self):
        self.write("status", {"phase": "disk_paused", "updated_at": iso(NOW - 10)})
        self.assertIn("disk_paused", self.found())


class Reconcile(unittest.TestCase):
    def test_one_mail_then_a_reminder_then_one_when_it_clears(self):
        state, mails = monitor.reconcile({}, {"blocked": "x"}, NOW)
        self.assertEqual([(m["kind"], m["code"]) for m in mails], [("alert", "blocked")])
        monitor.mark_sent(state, mails[0], NOW)
        state, mails = monitor.reconcile(state, {"blocked": "x"}, NOW + 300)
        self.assertEqual(mails, [], "not every five minutes")
        state, mails = monitor.reconcile(state, {"blocked": "x"}, NOW + monitor.REMIND_SECONDS)
        self.assertEqual(len(mails), 1)
        monitor.mark_sent(state, mails[0], NOW + monitor.REMIND_SECONDS)
        state, mails = monitor.reconcile(state, {}, NOW + monitor.REMIND_SECONDS + 300)
        self.assertEqual([m["kind"] for m in mails], ["cleared"])
        monitor.mark_sent(state, mails[0], NOW)
        self.assertEqual(monitor.reconcile(state, {}, NOW)[1], [])

    def test_a_mail_that_could_not_be_sent_is_tried_again(self):
        state, mails = monitor.reconcile({}, {"blocked": "x"}, NOW)
        state, again = monitor.reconcile(state, {"blocked": "x"}, NOW + 300)
        self.assertEqual(len(again), 1)
        self.assertEqual(state["active"]["blocked"]["since"], NOW, "it began when it began")
        state, gone = monitor.reconcile(state, {}, NOW + 600)
        self.assertEqual(gone, [], "never told: nothing to take back")

    def test_an_unsent_recovery_waits_for_the_next_check(self):
        state, mails = monitor.reconcile({}, {"blocked": "x"}, NOW)
        monitor.mark_sent(state, mails[0], NOW)
        state, mails = monitor.reconcile(state, {}, NOW + 300)
        state, mails = monitor.reconcile(state, {}, NOW + 600)
        self.assertEqual([m["kind"] for m in mails], ["cleared"])

    def test_the_mail_says_what_and_is_no_status_protocol_mail(self):
        item = monitor.message({"kind": "alert", "code": "blocked", "text": "全省已封锁", "since": NOW}, "辽宁", "a@x", "b@x")
        self.assertEqual(item["Subject"], "[PMOS告警] 辽宁：blocked")
        self.assertIn("全省已封锁", item.get_content())
        self.assertNotIn("CRAWLER-STATUS", item["Subject"])


if __name__ == "__main__":
    unittest.main()
