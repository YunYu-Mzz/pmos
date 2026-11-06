"""The worker loop against a fake transport: collection, session keeping, faults."""
import unittest

from pmos.adapt import enable
from pmos.engine import Engine
from pmos.faults import Fault
from pmos.periods import today
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir, dataset, envelope, excel, make_province

TODAY = today().isoformat()


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()

    def engine(self, datasets, session_fault=None, **overrides):
        self.province = make_province(self.tmp.path, datasets, **overrides)
        self.transport = FakeTransport(self.clock)
        self.session_fault = session_fault

        async def check():
            return self.session_fault

        engine = Engine(self.province, self.transport, check, instance=self.tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        return engine

    async def test_collects_newest_first_and_waits_after_each_response_end(self):
        engine = self.engine([dataset("load", keepalive=True, start="2026-10-20")])
        engine.state.set_control(collect=True)
        await engine.tick()
        self.assertEqual(self.transport.sent[0]["body"], {"date": TODAY})
        await engine.tick()  # spacing not elapsed: nothing sent
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(engine.state.read("status")["phase"], "waiting")
        self.clock.advance(30)
        await engine.tick()
        self.assertEqual(len(self.transport.sent), 2)
        self.assertEqual(len(engine.archive.records("load")), 2)
        self.assertEqual(engine.ledger.summary()["load"]["saved"], 2)

    async def test_idle_queue_renews_login_with_business_request_before_logout_window(self):
        engine = self.engine([dataset("load", keepalive=True, start=TODAY)],
                             session={"probe": {"path": "/px-test/time"}})
        engine.state.set_control(collect=True)
        await engine.tick()  # the only task
        self.transport.queue.append(envelope(0))  # TIME answers JSON
        self.clock.advance(320)
        await engine.tick()  # complete; 5 idle minutes -> TIME probe only
        self.assertEqual(self.transport.sent[-1]["path"], "/px-test/time")
        self.transport.default = excel(b"renewal")
        self.clock.advance(285)
        await engine.tick()  # 10 business-idle minutes -> one real export renews
        sent = self.transport.sent[-1]
        self.assertEqual((sent["path"], sent["body"]), ("/px-test/load", {"date": TODAY}))
        self.assertEqual(engine.ledger.last_attempt("keepalive")["outcome"], "success")
        self.assertEqual(len(engine.archive.records("load")), 1, "renewals are never archived")
        self.assertEqual(engine.ledger.budget_starts(0).__len__(), 1, "renewals are not collection budget")

    async def test_probe_response_end_delays_the_next_collection(self):
        engine = self.engine([dataset("load", keepalive=True, start="2026-10-01")],
                             session={"probe": {"path": "/px-test/time"}})
        engine.ledger.seed("load", ["2026-10-01"])
        self.transport.duration = 45
        self.transport.default = envelope(0)
        await engine.keep_session()  # nothing collected yet: probe allowed
        self.assertEqual(self.transport.sent[-1]["path"], "/px-test/time")
        self.assertGreater(engine.spacing_wait("collect"), 29)

    async def test_missing_probe_or_renewal_template_waits_without_fault_or_attempt(self):
        # 10-23 live run: the probe fired before the page had polled TIME once.
        engine = self.engine([dataset("load", keepalive=True, start=TODAY)],
                             session={"probe": {"path": "/px-test/time"}})
        self.transport.unready = {"/px-test/time", "/px-test/load"}
        await engine.tick()
        self.assertIsNone(engine.state.blocked())
        self.assertEqual(self.transport.sent, [])
        self.assertIsNone(engine.ledger.last_attempt("probe"))
        self.assertIn("probe_template:/px-test/time", engine.state.read("status")["session_keeping_waiting"])
        self.transport.unready = set()
        self.transport.queue.append(envelope(0))
        await engine.tick()
        self.assertEqual(self.transport.sent[-1]["path"], "/px-test/time")
        # The probe is no longer waiting; renewal still needs one saved period.
        self.assertEqual(engine.state.read("status")["session_keeping_waiting"], "renewal_needs_one_saved_period")

    async def test_visible_logout_blocks_and_no_request_is_sent_afterwards(self):
        engine = self.engine([dataset("load", keepalive=True)], session_fault=Fault("auth_expired", "auth"))
        engine.state.set_control(collect=True)
        await engine.tick()
        self.session_fault = None
        for _ in range(3):
            self.clock.advance(900)
            await engine.tick()
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(engine.state.blocked()["reason"], "auth_expired")
        self.assertFalse(engine.state.read("hold")["keepalive"])

    async def test_local_fault_hold_keeps_login_alive_until_first_keepalive_failure(self):
        engine = self.engine([dataset("load", keepalive=True, start="2026-10-20")])
        engine.state.set_control(collect=True)
        await engine.tick()  # one saved period gives the keepalive something to re-read
        self.transport.queue.append(Fault("request_template_missing", "local"))
        self.clock.advance(30)
        await engine.tick()
        block = engine.state.blocked()
        self.assertEqual((block["reason"], block["keepalive_allowed"]), ("request_template_missing", True))
        self.clock.advance(601)
        await engine.tick()
        self.assertEqual(engine.ledger.last_attempt("keepalive")["outcome"], "success")
        self.transport.queue.append(envelope(401))
        self.clock.advance(601)
        await engine.tick()
        hold = engine.state.read("hold")
        self.assertEqual((hold["keepalive"], hold["stopped_by"]), (False, "auth_expired"))
        count = len(self.transport.sent)
        self.clock.advance(3600)
        await engine.tick()
        self.assertEqual(len(self.transport.sent), count, "no retry after the hold keepalive failed")

    async def test_permission_or_unknown_responses_stop_everything(self):
        for response in (envelope(40302), envelope(9999), Response(200, "text/html", b"<html>")):
            with self.subTest(body=response.body[:20]):
                tmp = TempDir()
                self.addCleanup(tmp.cleanup)
                self.tmp = tmp
                engine = self.engine([dataset("load", keepalive=True, start=TODAY)])
                engine.state.set_control(collect=True)
                self.transport.default = response
                await engine.tick()
                self.assertFalse(engine.state.blocked()["keepalive_allowed"])
                self.clock.advance(3600)
                await engine.tick()
                self.assertEqual(len(self.transport.sent), 1)

    async def test_broken_file_quarantines_only_that_dataset(self):
        engine = self.engine([dataset("a_report", start=TODAY), dataset("b_report", start=TODAY, keepalive=True)],
                             session={"collection_order": ["a_report", "b_report"]})
        engine.state.set_control(collect=True)
        self.transport.queue.append(Response(200, "application/vnd.ms-excel", b"PK\x03\x04broken"))
        await engine.tick()
        self.assertIn("a_report", engine.dataset_holds())
        self.assertIsNone(engine.state.blocked())
        self.clock.advance(30)
        await engine.tick()
        self.assertEqual(self.transport.sent[-1]["path"], "/px-test/b_report")

    async def test_network_failure_is_recorded_unknown_and_blocks(self):
        engine = self.engine([dataset("load", keepalive=True, start=TODAY)])
        engine.state.set_control(collect=True)
        self.transport.queue.append(Fault("network_error", "network"))
        await engine.tick()
        self.assertEqual(engine.ledger.last_attempt("collect")["outcome"], "unknown")
        self.assertEqual(engine.state.blocked()["category"], "network")
        self.assertEqual(engine.ledger.task("load", TODAY)["status"], "pending")

    async def test_unsettled_attempt_blocks_until_reviewed(self):
        engine = self.engine([dataset("load", keepalive=True)])
        engine.state.set_control(collect=True)
        engine.ledger.begin("collect", "load", TODAY)  # e.g. power loss while sending
        await engine.tick()
        self.assertEqual(engine.state.blocked()["reason"], "unsettled_attempt")
        self.assertEqual(self.transport.sent, [])

    async def test_two_empty_periods_pause_the_dataset(self):
        item = dataset("json_report", start="2026-10-20", response="json", keepalive=True)
        engine = self.engine([item])
        engine.state.set_control(collect=True)
        self.transport.default = envelope(0, data=[])
        await engine.tick()
        self.clock.advance(30)
        await engine.tick()
        self.assertEqual(engine.dataset_holds()["json_report"]["reason"], "consecutive_empty_periods")
        self.assertEqual(engine.ledger.summary()["json_report"]["empty"], 2)

    async def test_scoped_dataset_has_one_task_per_period_and_scope(self):
        item = {**dataset("sections", start=TODAY), "scopes": [{"断面名称": "甲"}, {"断面名称": "乙"}]}
        item["request"] = {"transport": "ui", "frame": "infoCode=x", "action": "export",
                           "fields": [{"kind": "date", "placeholder": "选择日期", "value": "{date}"}]}
        engine = self.engine([item, dataset("load", keepalive=True, start=TODAY)],
                             session={"collection_order": ["sections", "load"]})
        engine.state.set_control(collect=True)
        self.transport.default = excel()
        await engine.tick()
        self.clock.advance(30)
        self.transport.queue.append(excel(b"other"))
        await engine.tick()
        sent = [r["fields"][-1]["value"] for r in self.transport.sent]
        self.assertEqual(sorted(sent), ["乙", "甲"])
        records = engine.archive.records("sections")
        self.assertEqual(sorted(r["scope_values"]["断面名称"] for r in records), ["乙", "甲"])
        self.assertTrue(all(r["scope"] and r["path"].count("/") == 3 for r in records))
        self.assertEqual(engine.ledger.summary()["sections"], {"saved": 2})

    async def test_empty_scope_never_pauses_a_scoped_dataset(self):
        item = {**dataset("nodes", start="2026-10-20", response="json", keepalive=False),
                "scopes": [{"节点": "A"}]}
        engine = self.engine([item, dataset("load", keepalive=True, start=TODAY)],
                             session={"collection_order": ["nodes", "load"]})
        engine.state.set_control(collect=True)
        self.transport.default = envelope(0, data=[])
        for _ in range(3):
            await engine.tick()
            self.clock.advance(30)
        self.assertNotIn("nodes", engine.dataset_holds())
        self.assertEqual(engine.ledger.summary()["nodes"]["empty"], 3)

    async def test_paginated_listing_one_page_per_request_and_resumes_after_restart(self):
        import json as _json
        item = {**dataset("listing", start=TODAY, response="json"),
                "request": {"transport": "fetch", "method": "POST", "path": "/px-test/list",
                            "body": {"data": {"date": "{date}"}},
                            "pagination": {"page": "pageInfo.pageNum", "size": "pageInfo.pageSize",
                                           "page_size": 2, "records": "data.list", "total": "data.total"}}}
        engine = self.engine([item, dataset("load", keepalive=True, start=TODAY)],
                             session={"collection_order": ["listing", "load"]})
        engine.state.set_control(collect=True)
        page = lambda rows: Response(200, "application/json",
                                     _json.dumps({"status": 0, "data": {"list": rows, "total": 3}}).encode())
        self.transport.queue += [page([1, 2]), page([3])]
        await engine.tick()
        self.assertEqual(self.transport.sent[0]["body"]["pageInfo"], {"pageNum": 1, "pageSize": 2})
        self.assertEqual(engine.state.read("page_cursor")["page"], 2)
        self.assertEqual(engine.ledger.task("listing", TODAY)["status"], "pending")
        # a restarted worker continues at page 2, not page 1
        engine.ledger.close()
        restarted = Engine(self.province, self.transport, None, instance=self.tmp.path / "var", clock=self.clock)
        self.addCleanup(restarted.ledger.close)
        self.clock.advance(30)
        await restarted.tick()
        self.assertEqual(self.transport.sent[1]["body"]["pageInfo"]["pageNum"], 2)
        self.assertEqual(restarted.ledger.task("listing", TODAY)["status"], "saved")
        self.assertIsNone(restarted.state.read("page_cursor"))
        self.assertEqual(sorted(r["scope"] for r in restarted.archive.records("listing")), ["p0001", "p0002"])

    async def test_reports_without_template_wait_or_bootstrap_through_the_page(self):
        plain = dataset("plain", start=TODAY)
        booted = {**dataset("booted", start=TODAY),
                  "request": {**dataset("booted", start=TODAY)["request"],
                              "bootstrap": {"transport": "component", "frame": "#/info/booted", "open_url": "/info/#/info/booted",
                                            "component": "*", "match": {"exportUrl": "/px-test/booted"},
                                            "set": {"date": "{date}", "queryConfig.data": {"date": "{date}"}},
                                            "call": "exportData", "path": "/px-test/booted"}}}
        engine = self.engine([plain, booted, dataset("load", keepalive=True, start=TODAY)],
                             session={"collection_order": ["plain", "booted", "load"]})
        self.transport.unready |= {"/px-test/plain", "/px-test/booted"}
        engine.state.set_control(collect=True)
        await engine.tick()
        sent = self.transport.sent[0]
        self.assertEqual((sent["transport"], sent["call"], sent["set"]["queryConfig.data"]),
                         ("component", "exportData", {"date": TODAY}), "booted bootstraps; plain waits")
        self.assertEqual(engine.ledger.task("booted", TODAY)["status"], "saved")
        self.clock.advance(30)
        await engine.tick()  # load (template observed) goes on
        self.assertEqual(self.transport.sent[-1]["path"], "/px-test/load")
        self.clock.advance(30)
        await engine.tick()
        self.assertIsNone(engine.state.blocked())
        status = engine.state.read("status")
        self.assertEqual((status["phase"], status["templates"]), ("waiting_templates", ["/px-test/plain"]))
        self.assertEqual(engine.ledger.task("plain", TODAY)["status"], "pending")
        self.transport.unready.clear()  # the page showed the template
        await engine.tick()
        self.assertEqual(self.transport.sent[-1]["path"], "/px-test/plain")

    async def test_a_bootstrap_the_spacing_refused_is_not_isolated(self):
        """Anhui 2026-11-01 16:19: right after a slow keepalive an unsent item was isolated as site data."""
        booted = {**dataset("booted", start=TODAY),
                  "request": {**dataset("booted", start=TODAY)["request"],
                              "bootstrap": {"transport": "component", "frame": "#/info/booted", "component": "*",
                                            "match": {"exportUrl": "/px-test/booted"}, "call": "exportData",
                                            "path": "/px-test/booted"}}}
        engine = self.engine([booted, dataset("load", keepalive=True, start=TODAY)])
        self.transport.unready.add("/px-test/booted")
        self.transport.queue.append(Fault("spacing_not_elapsed", "local"))
        engine.state.set_control(collect=True)
        self.assertEqual(await engine.tick(), 1)
        row = engine.ledger.task("booted", TODAY, "")
        self.assertEqual((row["status"], row["reason"]), ("pending", None))
        self.assertEqual(engine.dataset_holds(), {})
        self.assertIsNone(engine.state.blocked())

    async def test_a_refused_bootstrap_can_be_reopened_and_nothing_else(self):
        engine = self.engine([dataset("load", keepalive=True, start=TODAY)])
        engine.ledger.seed("load", [TODAY], "")
        reason = "isolated:bootstrap_spacing_not_elapsed"
        engine.ledger.settle_task("load", TODAY, "failed", reason=reason)
        with self.assertRaises(ValueError):
            engine.ledger.reopen_refused_bootstrap("load", TODAY, "", " ")
        engine.ledger.reopen_refused_bootstrap("load", TODAY, "", "用户原话")
        self.assertEqual(engine.ledger.task("load", TODAY, "")["status"], "pending")
        engine.ledger.settle_task("load", TODAY, "failed", reason="isolated:network_error")
        with self.assertRaises(ValueError):
            engine.ledger.reopen_refused_bootstrap("load", TODAY, "", "用户原话")

    async def test_a_bootstrap_the_page_cannot_run_holds_only_that_report(self):
        booted = {**dataset("booted", start=TODAY),
                  "request": {**dataset("booted", start=TODAY)["request"],
                              "bootstrap": {"transport": "component", "frame": "#/info/booted", "component": "*",
                                            "match": {"exportUrl": "/px-test/booted"}, "call": "exportData",
                                            "path": "/px-test/booted"}}}
        engine = self.engine([booted, dataset("load", keepalive=True, start=TODAY)])
        self.transport.unready.add("/px-test/booted")
        self.transport.queue.append(Fault("component_missing", "local"))
        engine.state.set_control(collect=True)
        await engine.tick()
        self.assertEqual(engine.dataset_holds()["booted"]["reason"], "bootstrap_component_missing")
        self.assertIsNone(engine.state.blocked())

    async def test_renewal_bootstraps_once_and_a_failed_bootstrap_only_waits(self):
        load = {**dataset("load", keepalive=True, start=TODAY),
                "request": {**dataset("load", start=TODAY)["request"],
                            "bootstrap": {"transport": "component", "frame": "#/info/load", "component": "*",
                                          "match": {"exportUrl": "/px-test/load"}, "call": "exportData",
                                          "path": "/px-test/load"}}}
        engine = self.engine([load])
        engine.archive.store("load", TODAY, excel().body, ".xlsx")
        self.transport.unready.add("/px-test/load")
        self.transport.queue.append(Fault("component_missing", "local"))
        self.clock.advance(700)
        self.assertIsNone(await engine.keep_session())
        self.assertEqual(engine.state.read("renewal_bootstrap_failed")["code"], "component_missing")
        self.clock.advance(700)
        self.assertIsNone(await engine.keep_session())
        self.assertEqual(len(self.transport.sent), 1, "a failed bootstrap is not retried")
        self.assertIn("renewal_template", engine.state.read("status")["session_keeping_waiting"])
        self.transport.unready.clear()  # one 导出 click in the page
        self.assertEqual(await engine.keep_session(), "renewal")

    async def test_sample_lifecycle_candidate_to_enabled_and_denial(self):
        engine = self.engine([dataset("new_report", status="candidate", start="2026-10-01"),
                              dataset("locked_report", status="candidate", start="2026-10-01")])
        engine.enqueue({"action": "sample", "dataset": "new_report", "period": "2026-10-11"})
        await engine.tick()
        self.assertEqual(self.province.datasets()["new_report"].status, "sampled")
        self.clock.advance(30)
        engine.enqueue({"action": "sample", "dataset": "new_report", "period": "2026-10-11"})
        await engine.tick()
        self.assertEqual(self.province.datasets()["new_report"].status, "sampled", "same period proves nothing new")
        self.clock.advance(30)
        engine.enqueue({"action": "sample", "dataset": "new_report", "period": "2026-10-12"})
        await engine.tick()
        spec = self.province.datasets()["new_report"]
        self.assertEqual(spec.status, "verified")
        self.assertEqual(len(engine.samples.records("new_report")), 2)
        self.assertEqual(enable(spec).status, "enabled")
        self.transport.queue.append(envelope(40302))
        self.clock.advance(30)
        command = engine.enqueue({"action": "sample", "dataset": "locked_report", "period": "2026-10-11"})
        await engine.tick()
        self.assertEqual(self.province.datasets()["locked_report"].status, "denied")
        self.assertEqual(engine.state.read("command_results")[command]["category"], "permission")
        self.assertIsNone(engine.state.blocked(), "a denied candidate never stops the province")


if __name__ == "__main__":
    unittest.main()
