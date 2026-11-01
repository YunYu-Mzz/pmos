"""Production behaviours a province switch must keep: lane intervals and item isolation."""
import json
import os
import unittest
from argparse import Namespace
from unittest.mock import patch

from pmos import cli
from pmos.engine import ISOLATION_STREAK, Engine
from pmos.faults import Fault, decide
from pmos.pacing import Policy, PolicyError
from pmos.periods import previous as periods_previous, today
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir, dataset, envelope, excel, make_province

TODAY = today().isoformat()
PREVIOUS_DAY = periods_previous("day", TODAY)


def native(dataset_id):
    value = dataset(dataset_id, start=TODAY)
    value["source"] = {"lane": "native"}
    return value


class LaneIntervals(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()

    def engine(self, datasets, **policy):
        province = make_province(self.tmp.path, datasets, policy={"response_interval_seconds": 15,
                                                                  "lane_intervals": {"native": 5}, **policy},
                                 session={"collection_order": [d["id"] for d in datasets]})
        self.transport = FakeTransport(self.clock, duration=1)
        engine = Engine(province, self.transport, instance=self.tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        return engine

    async def test_native_lane_waits_five_seconds_others_fifteen_after_any_response(self):
        engine = self.engine([{**native("fast"), "status": "enabled"}, {**dataset("slow", start=TODAY)}])
        engine.state.set_control(collect=True)
        await engine.tick()                      # fast sent, response ends now
        self.assertEqual(len(self.transport.sent), 1)
        self.assertGreater(engine.spacing_wait("collect", "native"), 0)
        self.clock.advance(5)
        self.assertEqual(engine.spacing_wait("collect", "native"), 0)
        self.assertGreater(engine.spacing_wait("collect"), 0, "default lane still waits 15 s")
        self.clock.advance(10)
        self.assertEqual(engine.spacing_wait("collect"), 0)

    def test_lane_policy_is_validated(self):
        with self.assertRaises(PolicyError):
            Policy.from_dict({"lane_intervals": {"native": 0}})
        with self.assertRaises(PolicyError):
            Policy.from_dict({"isolate_items": ["auth"]})
        self.assertEqual(Policy.from_dict({"isolate_items": ["unknown"]}).isolate_items, ("unknown",))


class ItemIsolation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        days = [today().isoformat(), today().fromordinal(today().toordinal() - 1).isoformat()]
        self.days = days

    def engine(self, isolate):
        spec = dataset("report", start=self.days[1])
        province = make_province(self.tmp.path, [spec], policy={"response_interval_seconds": 15,
                                                               "isolate_items": isolate})
        self.transport = FakeTransport(self.clock, duration=1)
        engine = Engine(province, self.transport, instance=self.tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        return engine

    async def tick(self, engine):
        await engine.tick()
        self.clock.advance(20)

    async def test_non_auth_item_failure_is_isolated_and_the_next_item_continues(self):
        engine = self.engine(["unknown", "site_data", "permission"])
        self.transport.queue += [Response(500, "application/json", b"{}"), excel()]
        await self.tick(engine)
        await self.tick(engine)
        self.assertIsNone(engine.state.blocked())
        first = engine.ledger.task("report", self.days[0])
        self.assertEqual((first["status"], first["reason"]), ("failed", "isolated:http_error"))
        self.assertEqual(engine.ledger.task("report", self.days[1])["status"], "saved")
        self.assertEqual(engine.state.read("isolated_items")[0]["reason"], "isolated:http_error")
        self.assertEqual(len(self.transport.sent), 2, "the failed item is not retried")

    async def test_exact_40302_is_a_denied_item_but_a_lost_login_still_stops(self):
        engine = self.engine(["permission"])
        self.transport.queue += [envelope(40302), envelope(401)]
        await self.tick(engine)
        self.assertEqual(engine.ledger.task("report", self.days[0])["reason"], "denied:40302")
        self.assertIsNone(engine.state.blocked())
        await self.tick(engine)
        self.assertEqual(engine.state.blocked()["reason"], "auth_expired")

    async def test_after_a_cleared_login_block_the_app_frames_are_reopened(self):
        engine = self.engine([])
        resets = []

        async def reset_frames():
            resets.append(1)
            return 2
        self.transport.reset_frames = reset_frames
        self.transport.queue.append(envelope(401))
        await self.tick(engine)
        self.assertEqual(engine.state.blocked()["category"], "auth")
        await self.tick(engine)
        self.assertEqual(resets, [], "not while blocked")
        engine.state.clear_block("auth_expired")
        await self.tick(engine)
        self.assertEqual(resets, [1])
        await self.tick(engine)
        self.assertEqual(resets, [1], "once per cleared block")

    async def test_without_the_policy_failures_still_block_the_province(self):
        engine = self.engine([])
        self.transport.queue.append(Response(500, "application/json", b"{}"))
        await self.tick(engine)
        self.assertEqual(engine.state.blocked()["reason"], "http_error")
        self.assertEqual(engine.ledger.task("report", self.days[0])["status"], "pending")


class IsolationStreak(unittest.IsolatedAsyncioTestCase):
    """Repeated item failures are the dataset's fault: stop consuming its items."""

    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        first = today().fromordinal(today().toordinal() - 5).isoformat()
        province = make_province(self.tmp.path, [dataset("report", start=first), dataset("other", start=TODAY)],
                                 policy={"response_interval_seconds": 15, "isolate_items": ["unknown", "site_data"]},
                                 session={"collection_order": ["report", "other"]})
        self.transport = FakeTransport(self.clock, duration=1)
        self.engine = Engine(province, self.transport, instance=self.tmp.path / "var", clock=self.clock)
        self.addCleanup(self.engine.ledger.close)
        self.engine.state.set_control(collect=True)

    async def ticks(self, count):
        for _ in range(count):
            await self.engine.tick()
            self.clock.advance(20)

    async def test_three_isolated_failures_in_a_row_pause_only_that_dataset(self):
        self.transport.queue += [Response(500, "application/json", b"{}")] * ISOLATION_STREAK
        await self.ticks(ISOLATION_STREAK + 1)
        self.assertEqual(self.engine.dataset_holds()["report"]["reason"], "repeated_isolation:http_error")
        self.assertIsNone(self.engine.state.blocked())
        paths = [r["path"] for r in self.transport.sent]
        self.assertEqual(paths, ["/px-test/report"] * ISOLATION_STREAK + ["/px-test/other"])

    async def test_a_spec_may_hold_after_its_first_isolated_failure(self):
        path = self.engine.province.datasets_dir / "report.json"
        value = json.loads(path.read_text())
        value["source"] = {"isolation_streak": 1}   # e.g. a released user deferral the site may still refuse
        path.write_text(json.dumps(value))
        self.transport.queue.append(Response(500, "application/json", b"{}"))
        await self.ticks(2)
        self.assertEqual(self.engine.dataset_holds()["report"]["reason"], "repeated_isolation:http_error")
        self.assertEqual([r["path"] for r in self.transport.sent], ["/px-test/report", "/px-test/other"])

    async def test_five_40302_denials_in_a_row_pause_the_dataset(self):
        from pmos.engine import DENIAL_STREAK
        path = self.engine.province.datasets_dir / "report.json"
        value = json.loads(path.read_text())
        value["period"]["start"] = today().fromordinal(today().toordinal() - 9).isoformat()
        path.write_text(json.dumps(value))
        self.engine._seeded_on = None
        policy = {**self.engine.policy.__dict__}
        self.engine.policy = type(self.engine.policy).from_dict({**{k: v for k, v in policy.items() if v is not None},
                                                                 "isolate_items": ["permission"]})
        self.transport.queue += [envelope(40302)] * DENIAL_STREAK
        await self.ticks(DENIAL_STREAK + 1)
        self.assertEqual(self.engine.dataset_holds()["report"]["reason"], "repeated_denial:permission_denied")
        self.assertEqual(sum(r["path"] == "/px-test/report" for r in self.transport.sent), DENIAL_STREAK)

    async def test_a_released_denial_is_pending_again_and_sent_once_more(self):
        policy = {**self.engine.policy.__dict__}
        self.engine.policy = type(self.engine.policy).from_dict({**{k: v for k, v in policy.items() if v is not None},
                                                                 "isolate_items": ["permission"]})
        self.transport.queue.append(envelope(40302))
        await self.ticks(1)
        ledger = self.engine.ledger
        first = ledger.db.execute("SELECT dataset, period, scope FROM attempts WHERE id=1").fetchone()
        self.assertTrue(ledger.consumed(*first))
        with self.assertRaisesRegex(ValueError, "approval_required"):
            ledger.release_denied([1], " ")
        self.assertEqual(len(ledger.release_denied([1], "同意")), 1)
        self.assertFalse(ledger.consumed(*first))
        self.assertEqual(ledger.task(*first)["status"], "pending")
        with self.assertRaisesRegex(ValueError, "not_a_denial|not_denied"):
            ledger.release_denied([1], "同意")
        self.engine._seeded_on = None
        await self.ticks(3)
        self.assertEqual(ledger.task(*first)["status"], "saved", "sent once more after the release")

    def test_a_page_denial_releases_its_task(self):
        ledger = self.engine.ledger
        for scope, page_scope in (("s1", "s1.p0001"), ("", "p0001")):
            ledger.seed("report", ["2026-10-24"], scope)
            attempt = ledger.begin("collect", "report", "2026-10-24", page_scope)
            ledger.finish(attempt, "failed", http_status=200, business_status="40302", reason="permission_denied")
            ledger.settle_task("report", "2026-10-24", "failed", scope=scope, reason="denied:40302")
            self.assertEqual(ledger.release_denied([attempt], "同意"), [(attempt, "report", "2026-10-24", scope)])
            self.assertEqual(ledger.task("report", "2026-10-24", scope)["status"], "pending")
            self.assertFalse(ledger.consumed("report", "2026-10-24", page_scope))

    async def test_a_saved_item_between_failures_resets_the_streak(self):
        error = Response(500, "application/json", b"{}")
        self.transport.queue += [error, error, excel(), error]
        await self.ticks(4)
        self.assertNotIn("report", self.engine.dataset_holds())


class ReviewedLocalFailure(unittest.IsolatedAsyncioTestCase):
    """review-attempt: settle the local page failure that blocked the province, after review."""

    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(self.tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        first = today().fromordinal(today().toordinal() - 1).isoformat()
        self.province = make_province(self.tmp.path, [dataset("report", start=first)],
                                      policy={"response_interval_seconds": 15})
        self.transport = FakeTransport(self.clock, duration=0)

    async def blocked_engine(self, fault):
        engine = Engine(self.province, self.transport, instance=self.province.instance(), clock=self.clock)
        engine.state.set_control(collect=True)
        self.transport.queue.append(fault)
        await engine.tick()
        engine.ledger.close()
        return engine

    def review(self, attempt, decision="isolated", approval="同意"):
        cli.cmd_review_attempt(self.province, Namespace(attempt=attempt, decision=decision, approval=approval))

    def engine(self):
        engine = Engine(self.province, self.transport, instance=self.province.instance(), clock=self.clock)
        self.addCleanup(engine.ledger.close)
        return engine

    async def test_isolated_the_task_fails_the_block_clears_and_the_next_item_follows(self):
        engine = await self.blocked_engine(Fault("native_module_missing", "local"))
        self.assertEqual(engine.state.blocked()["reason"], "native_module_missing")
        self.review(1)
        self.assertIsNone(engine.state.blocked())
        engine = self.engine()
        row = engine.ledger.task("report", TODAY)
        self.assertEqual((row["status"], row["reason"]), ("failed", "isolated:native_module_missing"))
        self.assertEqual(engine.state.read("isolated_items")[-1]["attempt"], 1)
        self.clock.advance(20)
        await engine.tick()
        self.assertEqual(len(self.transport.sent), 2, "the isolated item is not retried; the next one is sent")
        self.assertEqual(engine.ledger.task("report", TODAY)["status"], "failed")

    async def test_not_sent_releases_the_identity_so_the_same_item_is_sent_again(self):
        await self.blocked_engine(Fault("native_module_missing", "local"))
        self.review(1, decision="not_sent")
        engine = self.engine()
        self.assertIsNone(engine.state.blocked())
        self.assertEqual(engine.ledger.db.execute("SELECT outcome FROM attempts WHERE id=1").fetchone()[0], "not_sent")
        self.assertFalse(engine.ledger.consumed("report", TODAY))
        self.clock.advance(20)
        await engine.tick()
        self.assertEqual(engine.ledger.task("report", TODAY)["status"], "saved")

    async def test_not_sent_only_for_an_error_raised_before_the_page_call(self):
        engine = await self.blocked_engine(Fault("page_call_failed", "local"))
        with self.assertRaisesRegex(ValueError, "not_a_pre_call_failure"):
            self.review(1, decision="not_sent")
        self.assertEqual(engine.state.blocked()["reason"], "page_call_failed")

    async def test_refuses_a_block_with_a_response_or_without_approval(self):
        engine = await self.blocked_engine(Fault("auth_expired", "auth", http_status=401))
        with self.assertRaisesRegex(ValueError, "unsent_local_failure_required|matching_local_block_required"):
            self.review(1)
        with self.assertRaisesRegex(ValueError, "approval_required"):
            self.review(1, approval=" ")
        self.assertEqual(engine.state.blocked()["reason"], "auth_expired")


class UnsentPageFailure(unittest.IsolatedAsyncioTestCase):
    """A page step the request gate proves unsent consumes nothing and holds only its dataset."""

    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(self.tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.province = make_province(self.tmp.path, [dataset("report", start=TODAY), dataset("other", start=TODAY)],
                                      policy={"response_interval_seconds": 15},
                                      session={"collection_order": ["report", "other"]})
        self.transport = FakeTransport(self.clock, duration=0)
        self.engine = Engine(self.province, self.transport, instance=self.province.instance(), clock=self.clock)
        self.addCleanup(self.engine.ledger.close)
        self.engine.state.set_control(collect=True)

    async def test_the_attempt_is_not_sent_the_item_stays_pending_and_the_others_continue(self):
        self.transport.queue.append(Fault("native_module_missing", "local", unsent=True))
        await self.engine.tick()
        ledger = self.engine.ledger
        self.assertEqual(ledger.db.execute("SELECT outcome FROM attempts WHERE id=1").fetchone()[0], "not_sent")
        self.assertFalse(ledger.consumed("report", TODAY))
        self.assertEqual(ledger.task("report", TODAY)["status"], "pending")
        self.assertIsNone(self.engine.state.blocked(), "one dataset's page step does not block the province")
        self.assertEqual(self.engine.dataset_holds()["report"]["reason"], "unsent:native_module_missing")
        self.clock.advance(20)
        await self.engine.tick()
        self.assertEqual(self.transport.sent[-1]["path"], "/px-test/other")
        cli.cmd_release_hold(self.province, Namespace(dataset="report", reason="unsent:native_module_missing",
                                                      approval="同意"))
        self.clock.advance(20)
        await self.engine.tick()
        self.assertEqual(ledger.task("report", TODAY)["status"], "saved", "released: the same item is sent")

    async def test_only_held_datasets_left_is_not_complete(self):
        self.engine.hold_dataset("report", "unsent:component_missing", "paused")
        self.engine.hold_dataset("other", "unsent:component_missing", "paused")
        await self.engine.tick()
        status = self.engine.state.read("status")
        self.assertEqual((status["phase"], status["held"]), ("waiting_holds", ["other", "report"]))

    def test_the_identity_lookup_uses_an_index(self):
        plan = self.engine.ledger.db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM attempts WHERE dataset=? AND period=? AND scope=?"
            " AND kind IN ('collect','sample') AND outcome!='not_sent' LIMIT 1", ("a", "b", "c")).fetchall()
        self.assertIn("attempts_identity", " ".join(str(tuple(r)) for r in plan))

    def test_release_hold_needs_the_exact_reason_and_approval(self):
        self.engine.hold_dataset("report", "unsent:component_missing", "paused")
        with self.assertRaisesRegex(ValueError, "hold_reason_mismatch"):
            cli.cmd_release_hold(self.province, Namespace(dataset="report", reason="other", approval="同意"))
        with self.assertRaisesRegex(ValueError, "approval_required"):
            cli.cmd_release_hold(self.province, Namespace(dataset="report", reason="unsent:component_missing",
                                                          approval=""))
        self.assertIn("report", self.engine.dataset_holds())

    def test_the_unsent_decision_keeps_the_login_renewed(self):
        decision = decide(Fault("component_missing", "local", unsent=True), "collect")
        self.assertEqual((decision.scope, decision.keepalive), ("dataset", True))
        self.assertEqual(decide(Fault("component_missing", "local"), "collect").keepalive, False)

    def test_a_document_file_tombstone_goes_with_a_not_sent_attempt(self):
        from pmos.contracts import TaskIdentity
        identity = TaskIdentity("testland", "default", "report", {"kind": "snapshot", "anchor": "undated"},
                                {"file_id": "f-1"}, operation="attachment")
        ledger = self.engine.ledger
        attempt = ledger.begin("collect", "report", "undated", "s1", identity=identity)
        self.assertTrue(ledger.consumed_file(identity.file_key))
        ledger.not_sent(attempt, "native_module_missing")
        self.assertFalse(ledger.consumed_file(identity.file_key))
        self.assertFalse(ledger.consumed_identity(identity.key))


class AcceptHold(unittest.TestCase):
    """Accept mode stays in the process for a keepalive-allowed block; it exits when renewal is not allowed."""

    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        from pmos.state import StateStore
        self.state = StateStore(self.tmp.path / "state")

    def test_hold_decisions(self):
        self.assertTrue(cli._held(self.state))
        block = self.state.block("request_template_missing", category="local", keepalive_allowed=True)
        self.assertTrue(cli._held(self.state), "renewing while a human reviews")
        self.state.write("hold", {"block_at": block["at"], "keepalive": False, "stopped_by": "auth_expired"})
        self.assertFalse(cli._held(self.state), "the renewal failed: nothing keeps the login, exit")
        self.state.clear_block("request_template_missing")
        self.state.block("auth_expired", category="auth", keepalive_allowed=False)
        self.assertFalse(cli._held(self.state))


if __name__ == "__main__":
    unittest.main()


class Preflight(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()

    async def test_a_failed_page_preflight_sends_nothing_and_only_defers_the_dataset(self):
        spec = dataset("page_report", start=TODAY)
        spec["request"] = {"transport": "component", "frame": "#/info/x", "component": "*",
                           "match": {"queryUrl": "/px-test/page_report"}, "path": "/px-test/page_report",
                           "set": {"date": "{date}"}, "call": "queryPage",
                           "preflight": [{"component": "*", "match": {"queryUrl": "/px-test/page_report"},
                                          "equals": {"exportUrl": "/px-test/page_reportExcel"}}]}
        spec["response"] = {"kind": "json"}
        province = make_province(self.tmp.path, [spec, dataset("other", start=TODAY)],
                                 session={"collection_order": ["page_report", "other"]})
        transport = FakeTransport(self.clock, default=envelope())
        checked = []

        async def preflight(request):
            checked.append(request["preflight"][0]["equals"])
            raise Fault("preflight_condition_unmet", "local")
        transport.preflight = preflight
        engine = Engine(province, transport, instance=self.tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        await engine.tick()
        self.assertEqual(checked, [{"exportUrl": "/px-test/page_reportExcel"}])
        self.assertEqual(transport.sent, [])
        self.assertEqual(engine.ledger.unsettled(), [])
        self.assertIsNone(engine.ledger.last_attempt("collect"), "no attempt: the identity is not consumed")
        self.assertEqual(engine.ledger.task("page_report", TODAY)["status"], "pending")
        self.assertEqual(engine.dataset_holds()["page_report"]["reason"], "preflight_deferred:preflight_condition_unmet")
        self.assertIsNone(engine.state.blocked())
        self.clock.advance(20)
        await engine.tick()
        self.assertEqual([r["path"] for r in transport.sent], ["/px-test/other"], "other datasets continue")

    async def test_a_passed_preflight_marks_the_request_so_send_does_not_prepare_twice(self):
        spec = dataset("page_report", start=TODAY)
        spec["request"] = {"transport": "component", "frame": "#/info/x", "component": "c", "path": "/px-test/p",
                           "set": {"date": "{date}"}, "call": "queryPage"}
        spec["response"] = {"kind": "json"}
        province = make_province(self.tmp.path, [spec])
        transport = FakeTransport(self.clock, default=envelope())

        async def preflight(request):
            return None
        transport.preflight = preflight
        engine = Engine(province, transport, instance=self.tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        await engine.tick()
        self.assertTrue(transport.sent[0]["_preflighted"])


class SubsystemKeepalive(unittest.IsolatedAsyncioTestCase):
    """The keepalive's subsystem is renewed while another subsystem is worked (Shanxi 2026-10-31 19:25)."""

    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        first = today().fromordinal(today().toordinal() - 40).isoformat()
        notice = dataset("notice", start=first, path="/px-a/notice")
        kept = dataset("kept", keepalive=True, start=TODAY, path="/px-b/kept")
        province = make_province(self.tmp.path, [notice, kept],
                                 policy={"response_interval_seconds": 15, "renew_after_idle_seconds": 300},
                                 session={"keepalive_period": TODAY, "collection_order": ["notice"]})
        self.transport = FakeTransport(self.clock, duration=1)
        self.engine = Engine(province, self.transport, instance=self.tmp.path / "var", clock=self.clock)
        self.addCleanup(self.engine.ledger.close)
        self.engine.ledger.seed("kept", [TODAY])
        self.engine.ledger.settle_task("kept", TODAY, "saved")      # only renewals from here on
        self.engine.state.set_control(collect=True)

    async def ticks(self, count):
        for _ in range(count):
            await self.engine.tick()
            self.clock.advance(16)

    async def test_a_due_renewal_takes_a_slot_while_another_subsystem_is_worked(self):
        from pmos.engine import app_of
        specs = self.engine.province.datasets()
        self.assertEqual((app_of(specs["notice"]), app_of(specs["kept"])), ("px-a", "px-b"))
        await self.ticks(30)                                          # 30 slots of 16 s = 480 s
        kinds = [r["path"] for r in self.transport.sent]
        renewals = [i for i, path in enumerate(kinds) if path == "/px-b/kept"]
        self.assertEqual(renewals[0], 0, "a subsystem never used in this run is renewed first")
        self.assertEqual(len(renewals), 2, "then again once it has idled 300 s")
        self.assertTrue(250 <= (renewals[1] - renewals[0]) * 16 <= 340)
        self.assertEqual(self.engine.ledger.last_attempt("keepalive")["kind"], "keepalive")
        self.assertIsNone(self.engine.state.blocked())

    async def test_a_failed_renewal_blocks_without_touching_the_waiting_task(self):
        self.transport.queue.append(envelope(40302))                  # the renewal: the subsystem refuses
        await self.ticks(2)
        self.assertEqual(self.engine.state.blocked()["reason"], "permission_denied")
        self.assertEqual([r["path"] for r in self.transport.sent], ["/px-b/kept"])
        statuses = {row["status"] for row in self.engine.ledger.db.execute("SELECT status FROM tasks WHERE dataset='notice'")}
        self.assertEqual(statuses, {"pending"}, "no notice item was sent or settled")


class UnpublishedWindow(unittest.IsolatedAsyncioTestCase):
    """User 2026-10-31: a 40302 on one of the last 7 days is "not yet published", tried again the next day."""

    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(self.tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.days = [today().fromordinal(today().toordinal() - n).isoformat() for n in (0, 1, 9)]
        spec = dataset("report", start=self.days[-1])
        province = make_province(self.tmp.path, [spec], policy={"response_interval_seconds": 15,
                                                               "isolate_items": ["permission"],
                                                               "unpublished_window_days": 7})
        self.transport = FakeTransport(self.clock, duration=1)
        self.engine = Engine(province, self.transport, instance=province.instance(), clock=self.clock)
        self.addCleanup(self.engine.ledger.close)
        self.engine.state.set_control(collect=True)
        self.engine.seed()
        self.engine.ledger.db.execute("DELETE FROM tasks WHERE period NOT IN (?,?,?)", tuple(self.days))
        self.engine.ledger.db.commit()

    async def ticks(self, count):
        for _ in range(count):
            await self.engine.tick()
            self.clock.advance(20)

    async def test_recent_denials_are_deferred_an_old_one_is_denied(self):
        self.transport.queue += [envelope(40302)] * 3
        await self.ticks(3)
        ledger = self.engine.ledger
        status = {day: (ledger.task("report", day)["status"], ledger.task("report", day)["reason"]) for day in self.days}
        self.assertEqual(status[self.days[0]], ("deferred", "unpublished:40302"))
        self.assertEqual(status[self.days[1]], ("deferred", "unpublished:40302"))
        self.assertEqual(status[self.days[2]], ("failed", "denied:40302"), "9 days old: a real denial")
        self.assertFalse(ledger.consumed("report", self.days[0]), "the deferred attempt consumes nothing")
        self.assertIsNone(self.engine.state.blocked())
        self.assertNotIn("report", self.engine.dataset_holds())

    async def test_the_next_day_a_deferred_item_is_sent_once_more(self):
        self.transport.queue.append(envelope(40302))
        await self.ticks(1)
        ledger = self.engine.ledger
        self.assertEqual(ledger.task("report", self.days[0])["status"], "deferred")
        self.assertEqual(ledger.reopen_deferred(before=self.clock() - 3600), 0, "not on the same day")
        self.assertEqual(ledger.reopen_deferred(before=self.clock() + 1), 1)
        await self.ticks(3)
        self.assertEqual(ledger.task("report", self.days[0])["status"], "saved")
        self.assertEqual(sum(r["path"] == "/px-test/report" and r["body"]["date"] == self.days[0]
                             for r in self.transport.sent), 2)

    def test_release_denied_can_defer(self):
        ledger = self.engine.ledger
        attempt = ledger.begin("collect", "report", self.days[1])
        ledger.finish(attempt, "failed", http_status=200, business_status="40302", reason="permission_denied")
        ledger.settle_task("report", self.days[1], "failed", reason="denied:40302")
        cli.cmd_release_denied(self.engine.province, Namespace(attempts=str(attempt), approval="同意", defer=True))
        self.assertEqual(ledger.task("report", self.days[1])["status"], "deferred")


class IncompleteMonth(unittest.IsolatedAsyncioTestCase):
    """A running month is not exported (partial data never redone); a recently ended one's 40302 is unpublished."""

    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(self.tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        now = today()
        self.current = f"{now.year:04d}-{now.month:02d}"
        self.previous = periods_previous("month", self.current)
        spec = dataset("monthly", kind="month", start=self.previous)
        province = make_province(self.tmp.path, [spec], policy={"response_interval_seconds": 15,
                                                               "isolate_items": ["permission"],
                                                               "unpublished_window_days": 7})
        self.transport = FakeTransport(self.clock, duration=1)
        self.engine = Engine(province, self.transport, instance=province.instance(), clock=self.clock)
        self.addCleanup(self.engine.ledger.close)
        self.engine.state.set_control(collect=True)

    async def test_the_running_month_waits_without_a_request(self):
        await self.engine.tick()
        self.clock.advance(20)
        await self.engine.tick()
        task = self.engine.ledger.task("monthly", self.current)
        self.assertEqual((task["status"], task["reason"]), ("deferred", "period_not_complete"))
        self.assertEqual([r["body"]["date"] for r in self.transport.sent], [self.previous], "only the ended month")

    def test_a_month_that_ended_within_the_window_is_unpublished(self):
        from pmos.periods import last_day
        ended = (today() - last_day("month", self.previous)).days
        self.assertEqual(self.engine._unpublished("monthly", self.previous), ended <= 7)
        self.assertTrue(self.engine._unpublished("monthly", self.current))
        self.assertFalse(self.engine._unpublished("monthly", "2026-01"))


class BlankOptionProbe(unittest.IsolatedAsyncioTestCase):
    async def test_one_blank_option_request_at_1000_per_page_is_kept_as_a_sample(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        spec = dataset("units", start=TODAY, response="json")
        spec["request"] = {"transport": "fetch", "method": "POST", "path": "/px-test/units",
                           "body": {"data": {"date": "{date}", "tdName": "{scope[tdName]}"},
                                    "pageInfo": {"pageNum": 1, "pageSize": 20}},
                           "pagination": {"page": "pageInfo.pageNum", "size": "pageInfo.pageSize", "page_size": 20,
                                          "records": "data.list", "total": "data.total", "last": "data.isLastPage"}}
        spec["scopes"] = [{"tdName": "unit A"}, {"tdName": "unit B"}]
        province = make_province(tmp.path, [spec], policy={"response_interval_seconds": 15})
        body = {"status": 0, "data": {"list": [{"tdName": "unit A"}, {"tdName": "unit B"}, {"tdName": "unit B"}],
                                      "total": 2500, "isLastPage": False}}
        transport = FakeTransport(clock, default=Response(200, "application/json", json.dumps(body).encode()))
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        command = engine.enqueue({"action": "probe_blank", "dataset": "units", "period": TODAY})
        await engine.tick()
        sent = transport.sent[0]["body"]
        self.assertEqual((sent["data"]["tdName"], sent["pageInfo"]["pageSize"], sent["pageInfo"]["pageNum"]), ("", 1000, 1))
        result = engine.state.read("command_results")[command]
        self.assertEqual((result["ok"], result["total"], result["pages_at_1000"]), (True, 2500, 3))
        self.assertEqual(result["distinct_options_on_page"], {"tdName": 2})
        self.assertEqual(engine.samples.records("units")[0]["acquisition"], "blank_option_probe")
        self.assertEqual(len(transport.sent), 1)

    async def test_a_probe_runs_between_requests_of_a_busy_five_second_lane(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        spec = dataset("units", start=today().fromordinal(today().toordinal() - 20).isoformat())
        spec["source"] = {"lane": "native"}
        spec["scopes"] = [{"tdName": "unit A"}]
        spec["request"]["body"]["tdName"] = "{scope[tdName]}"
        province = make_province(tmp.path, [spec], policy={"response_interval_seconds": 15,
                                                           "lane_intervals": {"native": 5}})
        transport = FakeTransport(clock, duration=1)
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        await engine.tick()
        command = engine.enqueue({"action": "probe_blank", "dataset": "units", "period": TODAY})
        for _ in range(3):
            clock.advance(6)                       # the native lane is due every 5 s; 15 s never pass
            await engine.tick()
        self.assertIn(command, engine.state.read("command_results", {}))
        self.assertIn("", [r["body"]["tdName"] for r in transport.sent])


class ApiBlankProbe(unittest.IsolatedAsyncioTestCase):
    """collect-one shows the page's request; probe-api-blank replays it once with the option blank."""

    async def test_collect_one_then_replay_the_page_query_with_a_blank_option(self):
        from types import SimpleNamespace
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        spec = dataset("sections", start=TODAY, response="json")
        spec["request"] = {"transport": "component", "frame": "#/info/s", "component": "c", "method": "POST",
                           "path": "/px-test/sections", "call": "queryPage",
                           "set": {"selectValue": "{scope[tdName]}",
                                   "queryConfig": {"data": {"tdName": "{scope[tdName]}", "tdate": "{date}"},
                                                   "pageInfo": {"pageNum": 1, "pageSize": 1000, "total": 0}}}}
        spec["scopes"] = [{"tdName": "A"}, {"tdName": "B"}]
        spec["source"] = {"lane": "native"}
        province = make_province(tmp.path, [spec], policy={"response_interval_seconds": 15,
                                                           "lane_intervals": {"native": 5}})
        body = {"status": 0, "data": {"list": [{"tdName": "A"}, {"tdName": "B"}], "total": 2}}
        transport = FakeTransport(clock, default=Response(200, "application/json", json.dumps(body).encode()))
        transport.templates = SimpleNamespace(items={})
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        engine.seed()
        probe = engine.enqueue({"action": "probe_api_blank", "dataset": "sections", "period": TODAY})
        await engine.tick()
        self.assertEqual(engine.state.read("command_results")[probe]["reason"], "api_probe_needs_a_page_request_first")
        first = engine.enqueue({"action": "collect_one", "dataset": "sections"})
        clock.advance(6)
        await engine.tick()
        self.assertEqual(engine.state.read("command_results")[first]["status"], "saved")
        transport.templates.items["/px-test/sections"] = {"headers": {}}
        probe = engine.enqueue({"action": "probe_api_blank", "dataset": "sections", "period": TODAY})
        clock.advance(6)
        await engine.tick()
        sent = transport.sent[-1]
        self.assertEqual((sent["transport"], sent["template"]), ("fetch", "/px-test/sections"))
        self.assertEqual(sent["body"]["data"], {"tdName": "", "tdate": TODAY})
        self.assertEqual(sent["body"]["pageInfo"]["pageSize"], 1000)
        result = engine.state.read("command_results")[probe]
        self.assertEqual((result["ok"], result["distinct_options_on_page"]), (True, {"tdName": 2}))
        self.assertEqual(len(transport.sent), 2)

    async def test_a_recent_40302_in_collect_one_is_deferred_like_normal_collection(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        spec = dataset("sections", start=TODAY)
        spec["scopes"] = [{"tdName": "A"}]
        spec["request"]["body"]["tdName"] = "{scope[tdName]}"
        province = make_province(tmp.path, [spec], policy={"response_interval_seconds": 15,
                                                           "isolate_items": ["permission"],
                                                           "unpublished_window_days": 7})
        transport = FakeTransport(clock, default=envelope(40302))
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        engine.seed()
        command = engine.enqueue({"action": "collect_one", "dataset": "sections"})
        await engine.tick()
        self.assertEqual(engine.state.read("command_results")[command]["reason"], "permission_denied")
        task = engine.ledger.db.execute("SELECT status, reason FROM tasks WHERE dataset='sections'").fetchone()
        self.assertEqual(tuple(task), ("deferred", "unpublished:40302"))


class QueryPermissionLapse(unittest.IsolatedAsyncioTestCase):
    """Shanxi 2026-10-31 22:03: page queries of one dataset after another answered 40302, the exports did not."""

    def engine(self, transport_default, *, reentry=True, **policy):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        start = today().fromordinal(today().toordinal() - 1).isoformat()

        def query(name):
            value = dataset(name, start=start, response="json", path=f"/px-shop/infoGrid/{name}")
            value["request"] = {"transport": "component", "frame": f"/pxf-shop/#/pxf-shop/{name}", "component": "*",
                                "match": {"queryUrl": f"/px-shop/infoGrid/{name}"},
                                "method": "POST", "path": f"/px-shop/infoGrid/{name}", "call": "queryData",
                                "set": {"queryConfig.data": {"date": "{date}"}}}
            return value
        datasets = [query("qa"), query("qb"), query("qc"), dataset("export", start=start, path="/px-shop/infoGrid/xExcel"),
                    {**query("other"), "request": {**query("other")["request"], "path": "/px-other/q"}}]
        province = make_province(self.tmp.path, datasets,
                                 policy={"response_interval_seconds": 1, "isolate_items": ["permission"], **policy},
                                 session={"collection_order": [d["id"] for d in datasets]})
        self.transport = FakeTransport(self.clock, default=transport_default, duration=1)
        self.reentries = []
        if reentry:
            async def portal_reentry(frame):
                self.reentries.append(frame)
                return {"menu": ["信息披露", frame], "frames_closed": 1}
            self.transport.portal_reentry = portal_reentry
        engine = Engine(province, self.transport, instance=self.tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        return engine

    async def run_ticks(self, engine, n):
        for _ in range(n):
            await engine.tick()
            self.clock.advance(2)

    def statuses(self, engine):
        return {(r["dataset"], r["period"]): (r["status"], r["reason"])
                for r in engine.ledger.db.execute("SELECT dataset, period, status, reason FROM tasks")}

    async def test_a_lapse_denies_nothing_reenters_from_the_portal_and_one_query_verifies_it(self):
        ok = envelope(0, {"list": [{"v": 1}]})
        engine = self.engine(ok)
        self.transport.queue = [envelope(40302)] * 3     # qa: two days denied, then qb's first day
        await self.run_ticks(engine, 3)
        self.assertNotIn(("failed", "denied:40302"), self.statuses(engine).values(), "a lapse is no denial")
        self.assertEqual({k: v["reason"] for k, v in engine.dataset_holds().items()},
                         {name: "query_lapse_suspected:px-shop" for name in ("qa", "qb", "qc")},
                         "the service's queries wait; its export and the other service do not")
        lapse = engine.state.read("query_lapse")
        self.assertEqual((lapse["dataset"], lapse["reopened"]), ("qb", 3))
        await self.run_ticks(engine, 1)                  # re-entry and the one verifying query
        self.assertEqual(self.reentries, ["/pxf-shop/#/pxf-shop/qb"])
        self.assertEqual(self.transport.sent[-1]["path"], "/px-shop/infoGrid/qb")
        self.assertEqual(len(self.transport.sent), 4)
        self.assertIsNone(engine.state.read("query_lapse"))
        self.assertEqual(engine.dataset_holds(), {})
        self.assertIsNone(engine.state.blocked())
        await self.run_ticks(engine, 5)                  # the five remaining query items
        self.assertEqual({status for (name, _), (status, _) in self.statuses(engine).items() if name in ("qa", "qb", "qc")},
                         {"saved"}, "the lapsed items are collected after the recovery")

    async def test_a_verifying_query_still_refused_holds_only_those_reports_and_the_rest_goes_on(self):
        engine = self.engine(envelope(40302))
        self.transport.queue = [envelope(40302)] * 4     # qa two days, qb, then the verifying query
        await self.run_ticks(engine, 4)
        self.assertIsNone(engine.state.blocked(), "a real denial of two reports never stops the province")
        self.assertEqual({k: v["reason"] for k, v in engine.dataset_holds().items()},
                         {"qa": "query_denied_after_portal_entry", "qb": "query_denied_after_portal_entry"})
        self.assertIsNone(engine.state.read("query_lapse"))
        self.assertEqual(len(self.reentries), 1)
        statuses = self.statuses(engine)
        self.assertEqual(statuses[("qb", TODAY)], ("failed", "denied:40302"), "the verified item is a denial")
        self.assertEqual({statuses[("qa", d)][0] for d in (TODAY, periods_previous("day", TODAY))}, {"pending"},
                         "the run's other items wait under the hold, never re-sent")
        self.transport.default = envelope(0, {"list": [{"v": 1}]})
        await self.run_ticks(engine, 2)
        self.assertEqual(self.transport.sent[-1]["path"], "/px-shop/infoGrid/qc", "the service's other queries go on")

    async def test_without_a_portal_reentry_the_province_holds_at_once(self):
        engine = self.engine(envelope(40302), reentry=False)
        await self.run_ticks(engine, 4)
        self.assertEqual(engine.state.blocked()["reason"], "query_lapse_needs_portal_entry")
        self.assertEqual(len(self.transport.sent), 3, "nothing is sent for the recovery")

    async def test_a_failed_reentry_blocks_with_a_fixed_code_and_keeps_its_cause(self):
        engine = self.engine(envelope(40302))

        async def broken(frame):
            raise Fault("menu_label_not_unique", "local")
        self.transport.portal_reentry = broken
        await self.run_ticks(engine, 4)
        block = engine.state.blocked()
        self.assertEqual((block["reason"], block["category"], block["keepalive_allowed"]),
                         ("query_lapse_reentry_failed", "auth", True))
        events = [json.loads(line) for line in (engine.state.root / "events.jsonl").read_text().splitlines()]
        self.assertIn({"reason": "query_lapse_reentry_failed", "cause": "menu_label_not_unique"},
                      [{k: e.get(k) for k in ("reason", "cause")} for e in events if e["phase"] == "query_lapse_needs_a_person"])

    async def test_per_option_reports_refused_one_after_another_are_no_lapse(self):
        engine = self.engine(envelope(40302))
        specs = engine.province.datasets()
        for name in ("qa", "qb"):
            specs[name].scopes = [{"unit": "A"}]
        with patch.object(engine.province, "datasets", lambda: specs):
            for name in ("qa", "qb"):
                engine._isolate_item(Fault("permission_denied", "permission", http_status=200, business_status="40302"),
                                     {"dataset": name, "period": TODAY, "scope": "s1"})
        self.assertIsNone(engine.state.read("query_lapse"))
        self.assertEqual(engine.state.read("query_denials", {}) or {}, {})

    def test_skip_lapse_is_a_persons_decision_that_holds_only_the_lapse_reports(self):
        engine = self.engine(envelope(40302))
        engine.state.write("query_lapse", {"dataset": "qb", "period": TODAY, "scope": "", "service": "px-shop",
                                           "run": [{"dataset": "qa", "period": TODAY, "scope": ""}]})
        engine.state.write("dataset_holds", {n: {"reason": "query_lapse_suspected:px-shop", "status": "paused"}
                                             for n in ("qa", "qb", "qc")})
        with patch.dict(os.environ, {"PMOS_DATA": str(self.tmp.path / "data")}):
            with patch.object(engine.province, "instance", lambda: self.tmp.path / "var"):
                with self.assertRaisesRegex(ValueError, "approval_required"):
                    cli.cmd_skip_lapse(engine.province, Namespace(approval=" "))
                cli.cmd_skip_lapse(engine.province, Namespace(approval="没有数据跳过吧"))
                with self.assertRaisesRegex(ValueError, "no_query_lapse"):
                    cli.cmd_skip_lapse(engine.province, Namespace(approval="没有数据跳过吧"))
        self.assertEqual({k: v["reason"] for k, v in engine.dataset_holds().items()},
                         {"qa": "query_lapse_skipped", "qb": "query_lapse_skipped"})
        self.assertIsNone(engine.state.read("query_lapse"))

    async def test_a_province_denial_streak_of_one_pauses_a_source_at_its_first_denial(self):
        engine = self.engine(envelope(40302), denial_streak=1)
        await self.run_ticks(engine, 1)
        self.assertEqual(engine.dataset_holds()["qa"]["reason"], "repeated_denial:permission_denied")

    async def test_recoveries_are_capped_per_hour(self):
        engine = self.engine(envelope(0, {"list": [{"v": 1}]}))
        engine.state.write("query_lapse_recoveries", [self.clock() - 60] * 3)
        engine.state.write("query_lapse", {"dataset": "qb", "period": TODAY, "scope": "", "service": "px-shop",
                                           "run": [{"dataset": "qa", "period": TODAY, "scope": ""}]})
        await self.run_ticks(engine, 1)
        self.assertIsNone(engine.state.blocked())
        self.assertEqual({k: v["reason"] for k, v in engine.dataset_holds().items()},
                         {"qa": "query_lapse_recoveries_exhausted", "qb": "query_lapse_recoveries_exhausted"})
        self.assertEqual(self.reentries, [])

    async def test_an_answered_query_between_two_datasets_40302s_is_no_lapse(self):
        engine = self.engine(envelope(40302))
        self.transport.default = None
        ok = envelope(0, {"list": []})
        self.transport.queue = [envelope(40302), ok, envelope(40302), ok, ok, ok, ok, ok]
        await self.run_ticks(engine, 5)
        self.assertEqual(engine.dataset_holds(), {})

    def test_release_hold_all_lets_every_dataset_of_that_hold_continue(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        with patch.dict(os.environ, {"PMOS_DATA": str(tmp.path / "data")}):
            province = make_province(tmp.path, [dataset("qa")])
            state = __import__("pmos.state", fromlist=["StateStore"]).StateStore(province.instance() / "state")
            state.write("dataset_holds", {"qa": {"reason": "query_lapse_suspected:px-shop"},
                                          "qb": {"reason": "query_lapse_suspected:px-shop"},
                                          "qc": {"reason": "repeated_denial:permission_denied"}})
            with self.assertRaisesRegex(ValueError, "approval_required"):
                cli.cmd_release_hold(province, Namespace(dataset=None, all=True, reason="query_lapse_suspected:px-shop",
                                                         approval=" "))
            cli.cmd_release_hold(province, Namespace(dataset=None, all=True, reason="query_lapse_suspected:px-shop",
                                                     approval="同意"))
            self.assertEqual(list(state.read("dataset_holds")), ["qc"])


class DiskGuard(unittest.IsolatedAsyncioTestCase):
    async def test_business_waits_while_the_disk_is_short_and_resumes_by_itself(self):
        from collections import namedtuple
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        province = make_province(tmp.path, [dataset("load", start=TODAY)],
                                 policy={"response_interval_seconds": 1, "min_free_disk_mb": 2048})
        transport = FakeTransport(clock)
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        usage = namedtuple("usage", "total used free")
        with patch("shutil.disk_usage", lambda path: usage(1, 1, 1024 * 1024 * 1024)):
            await engine.tick()
        self.assertEqual((engine.state.read("status")["phase"], transport.sent), ("disk_paused", []))
        self.assertIsNone(engine.state.blocked())
        with patch("shutil.disk_usage", lambda path: usage(1, 1, 3 * 1024 * 1024 * 1024)):
            clock.advance(5)
            await engine.tick()
        self.assertEqual(len(transport.sent), 1)


class PageHealth(unittest.IsolatedAsyncioTestCase):
    """Anhui 2026-10-31: a crashed tab failed 14 items in a row; a late CDP reply killed the driver."""

    def engine(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        province = make_province(tmp.path, [dataset("load", start=TODAY)], policy={"response_interval_seconds": 1})
        self.transport = FakeTransport(self.clock)
        engine = Engine(province, self.transport, instance=tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        return engine

    async def test_a_crashed_page_is_reloaded_before_anything_is_sent_and_repeated_crashes_stop(self):
        engine = self.engine()
        reloads = []

        async def recover_page():
            reloads.append(1)
            self.transport.crashed = False
            return {"frames_closed": 0}
        self.transport.recover_page = recover_page
        self.transport.crashed = True
        await engine.tick()
        self.assertEqual((len(reloads), self.transport.sent), (1, []))
        self.clock.advance(2)
        await engine.tick()
        self.assertEqual(len(self.transport.sent), 1, "collection goes on after the reload")
        for _ in range(3):
            self.transport.crashed = True
            self.clock.advance(60)
            await engine.tick()
        self.assertEqual(engine.state.blocked()["reason"], "page_crashed_repeatedly")
        self.assertEqual(len(reloads), 3, "at most 3 reloads an hour")

    async def test_an_old_or_heavy_page_is_recycled_between_actions_apart_from_the_crash_count(self):
        engine = self.engine()
        engine.province.browser_config = lambda: {"page_recycle": {"max_age_seconds": 3600, "max_js_heap_mb": 768}}
        health = {"age_seconds": 10, "js_heap_mb": 100}
        reopened = []

        async def page_health():
            return dict(health)

        async def recover_page():
            reopened.append(1)
            health.update(age_seconds=0, js_heap_mb=50)
            return {"reopened": True, "frames_closed": 0}
        self.transport.page_health, self.transport.recover_page = page_health, recover_page
        await engine.tick()
        self.assertEqual((reopened, len(self.transport.sent)), ([], 1), "a young, light page just works")
        health.update(age_seconds=3700)
        self.clock.advance(61)
        await engine.tick()
        self.assertEqual(len(reopened), 1)
        for _ in range(4):                       # hourly recycles never count as a crash loop
            health.update(js_heap_mb=900)
            self.clock.advance(61)
            await engine.tick()
        self.assertEqual(len(reopened), 5)
        engine._primed["load"] = self.clock()
        health.update(js_heap_mb=900)
        self.clock.advance(61)
        await engine.tick()
        self.assertEqual(engine._primed, {}, "a fresh page may prime its templates at once")
        self.assertIsNone(engine.state.blocked())
        self.assertEqual(engine.state.read("page_recoveries"), None)
        events = [json.loads(line) for line in (engine.state.root / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(e["phase"] == "page_recycled" for e in events), 6)

    async def test_a_planned_recycle_challenge_is_judged_once_in_two_hours_and_checked_by_96_points(self):
        """Anhui 2026-11-01 06:59 (user 10:5x 允许自动判断后继续): only a planned recycle's one document 412."""
        from pmos.faults import Fault
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        province = make_province(tmp.path, [dataset("load", start=TODAY, keepalive=True, response="json")],
                                 policy={"response_interval_seconds": 1})
        self.transport = FakeTransport(self.clock)
        engine = Engine(province, self.transport, instance=tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        rule = {"status": 412, "window_seconds": 7200, "path": "/pxf-settlement-outnetpub/", "text": "安徽",
                "verify_points": 96}
        engine.province.browser_config = lambda: {"page_recycle": {"max_age_seconds": 3600, "challenge": rule}}
        series = json.dumps({"status": 0, "data": [{"v": f"{n}.5"} for n in range(96)]}).encode()
        engine.archive.store("load", TODAY, series, ".json")
        health = {"age_seconds": 3700, "js_heap_mb": 100}
        seen = []

        async def page_health():
            return dict(health)

        async def recover_page(*, rule=None, admit=None):
            seen.append(rule)
            health.update(age_seconds=0)
            if not admit():
                raise Fault("security_verification", "auth", http_status=412)
            return {"reopened": True, "frames_closed": 0, "challenge": {"status": 412, "settled_after_seconds": 3.0}}
        self.transport.page_health, self.transport.recover_page = page_health, recover_page
        self.transport.default = Response(200, "application/json", series)
        await engine.tick()                      # the recycle meets the challenge and settles
        self.assertEqual((seen, self.transport.sent), ([rule], []))
        self.clock.advance(2)
        await engine.tick()                      # the 96-point check comes before anything else
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIsNone(engine.state.blocked())
        self.assertIsNone(engine.state.read("recycle_challenge_pending"))
        events = [json.loads(line) for line in (engine.state.root / "events.jsonl").read_text().splitlines()]
        settled = [e for e in events if e["phase"] == "recycle_challenge_settled"]
        self.assertEqual(len(settled), 1)
        self.assertGreaterEqual(settled[0]["points"], 96, "every number of the answer, the 96 values among them")
        health.update(age_seconds=3700)          # a second one within 2 h: a person decides
        self.clock.advance(1200)
        await engine.tick()
        self.assertEqual((engine.state.blocked()["reason"], engine.state.blocked()["http_status"]),
                         ("security_verification", 412))

    async def test_a_settled_challenge_whose_check_differs_stops_the_province(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        province = make_province(tmp.path, [dataset("load", start=TODAY, keepalive=True, response="json")],
                                 policy={"response_interval_seconds": 1})
        self.transport = FakeTransport(self.clock)
        engine = Engine(province, self.transport, instance=tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        engine.archive.store("load", TODAY, json.dumps({"status": 0, "data": list(range(96))}).encode(), ".json")
        engine.state.write("recycle_challenge_pending", {"status": 412, "verify_points": 96})
        self.transport.default = Response(200, "application/json", json.dumps({"status": 0, "data": [0] * 96}).encode())
        await engine.tick()
        self.assertEqual(engine.state.blocked()["reason"], "recycle_challenge_check_mismatch")
        self.assertEqual(len(self.transport.sent), 1, "only the check was sent")

    async def test_a_report_that_can_only_wait_never_costs_the_others_an_idle_round(self):
        """Liaoning 2026-11-01 lesson 21: an empty turn of a waiting lane slept the whole loop 10 s."""
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        province = make_province(tmp.path, [dataset("waits", start=TODAY), dataset("held", start=TODAY),
                                            dataset("works", start=PREVIOUS_DAY)], policy={"response_interval_seconds": 1})
        self.transport = FakeTransport(self.clock)
        engine = Engine(province, self.transport, instance=tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        self.transport.unready = {"/px-test/waits"}               # no template yet, no prime
        engine.hold_dataset("held", "unsent:x", "paused")
        sleeps = []
        for _ in range(4):
            sleeps.append(await engine.tick())
            self.clock.advance(1)
        self.assertEqual({r["path"] for r in self.transport.sent}, {"/px-test/works"})
        self.assertEqual(len(self.transport.sent), 2, "both days of the working report, back to back")
        self.assertLessEqual(max(sleeps[:2]), 1, "no idle round while one report can still send")

    async def test_page_operations_failing_in_a_row_stop_the_province_and_unsent_ones_cost_nothing(self):
        """Anhui 2026-11-01 12:07: 15 menu timeouts of 45 s each spent the acceptance budget."""
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        def ui(name):
            value = dataset(name, start=TODAY, response="json")
            value["request"] = {"transport": "ui", "frame": "infoCode=" + name, "action": "query", "path": "/px-test/" + name,
                                "fields": [{"kind": "date", "placeholder": "选择日期", "value": "{date}"}],
                                "menu": ["现货市场", name]}
            return value
        province = make_province(tmp.path, [ui("aa"), ui("bb"), ui("cc")], policy={"response_interval_seconds": 1})
        self.transport = FakeTransport(self.clock)
        engine = Engine(province, self.transport, instance=tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        self.transport.queue = [Fault("menu_navigation_timeout", "local", unsent=True) for _ in range(5)]
        for _ in range(6):
            await engine.tick()
            self.clock.advance(2)
        self.assertEqual(len(self.transport.sent), 3, "the third failure in a row stops it")
        self.assertEqual(engine.state.blocked()["reason"], "page_operations_failing")
        self.assertTrue(engine.state.blocked()["keepalive_allowed"], "the login is still renewed")
        self.assertEqual({engine.ledger.task(n, TODAY)["status"] for n in ("aa", "bb", "cc")}, {"pending"},
                         "unsent: nothing isolated, nothing consumed")
        self.assertIsNone(engine.state.read("isolated_items"))
        self.assertEqual(engine.ledger.attempts_since_handoff(), 0, "no handoff here; see the ledger test for the budget")
        engine.state.clear_block("page_operations_failing")
        engine.state.write("page_operation_failures", [])
        self.clock.advance(700)                    # the cooled-down reports come back
        # A driven page whose read check failed is no failing page (Anhui 12:32: three dropdown reads).
        self.transport.queue = [Fault("select_value_changed_by_reading", "local")]
        await engine.tick()
        self.assertEqual(engine.state.read("page_operation_failures"), [])
        self.assertNotEqual((engine.state.blocked() or {}).get("reason"), "page_operations_failing")

    def test_reviewed_navigation_failures_and_one_stopped_mid_navigation_consume_nothing(self):
        from pmos.contracts import TaskIdentity
        from pmos.ledger import Ledger
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(tmp.path / "ledger.sqlite3")
        self.addCleanup(ledger.close)
        ids = []
        for day, outcome, reason in ((TODAY, "failed", "menu_navigation_timeout"), (PREVIOUS_DAY, None, None),
                                     ("2026-10-01", "failed", "select_value_changed_by_reading")):
            ledger.seed("load", [day], "")
            identity = TaskIdentity("anhui", "anhui-production", "load", {"kind": "day", "label": day}, {"日期": day},
                                    operation="query")
            attempt = ledger.begin("collect", "load", day, "", identity=identity)
            if outcome:
                ledger.finish(attempt, outcome, reason=reason)
                ledger.settle_task("load", day, "failed", reason="isolated:bootstrap_" + reason)
            ids.append(attempt)
        with self.assertRaises(ValueError):
            ledger.review_unsent(ids, "用户原话")             # the third is no navigation failure: nothing changes
        self.assertEqual(ledger.db.execute("SELECT outcome FROM attempts WHERE id=?", (ids[0],)).fetchone()[0], "failed")
        self.assertEqual(len(ledger.review_unsent(ids[:2], "用户原话")), 2)
        self.assertEqual([r[0] for r in ledger.db.execute("SELECT outcome FROM attempts ORDER BY id")],
                         ["not_sent", "not_sent", "failed"])
        self.assertEqual(ledger.task("load", TODAY)["status"], "pending")
        self.assertFalse(ledger.unsettled())
        self.assertFalse(ledger.consumed("load", TODAY))

    async def test_a_failed_planned_recycle_keeps_the_login_renewed_and_names_its_cause(self):
        """Anhui 2026-11-01 12:39: the portal was not found (portal_page_missing); the old page still worked."""
        from pmos.browser.attach import AttachError
        engine = self.engine()
        engine.province.browser_config = lambda: {"page_recycle": {"max_age_seconds": 3600, "max_js_heap_mb": 768}}

        async def page_health():
            return {"age_seconds": 10, "js_heap_mb": 812}

        async def recover_page():
            raise AttachError("portal_page_missing")
        self.transport.page_health, self.transport.recover_page = page_health, recover_page
        await engine.tick()
        block = engine.state.blocked()
        self.assertEqual((block["reason"], block["keepalive_allowed"]), ("page_recovery_failed", True))
        events = [json.loads(line) for line in (engine.state.root / "events.jsonl").read_text().splitlines()]
        failed = [e for e in events if e["phase"] == "page_recovery_failed"]
        self.assertEqual((failed[0]["code"], failed[0]["planned"]), ("portal_page_missing", True))

    async def test_a_lost_driver_leaves_the_send_unknown_writes_no_block_and_ends_the_worker(self):
        from pmos.faults import DriverLost
        engine = self.engine()
        self.transport.queue = [DriverLost("Connection closed while reading from the driver")]
        with self.assertRaises(DriverLost):
            await engine.tick()
        self.assertIsNone(engine.state.blocked())
        attempt = engine.ledger.db.execute("SELECT outcome, reason FROM attempts").fetchone()
        self.assertEqual(tuple(attempt), ("unknown", "driver_lost"))
        events = [json.loads(line)["phase"] for line in (engine.state.root / "events.jsonl").read_text().splitlines()]
        self.assertIn("driver_lost", events)

    async def test_a_driver_dying_outside_a_guarded_call_is_still_no_business_block(self):
        from pmos.faults import DriverLost
        engine = self.engine()
        self.transport.queue = [RuntimeError("Target page, context or browser has been closed")]
        with self.assertRaises(DriverLost):
            await engine.tick()
        self.assertIsNone(engine.state.blocked(), "the restarted worker must be able to start (Anhui 02:08)")
        self.clock.advance(600)
        engine._driver_lost("Connection closed while reading from the driver")
        self.assertIsNone(engine.state.blocked())
        self.clock.advance(600)
        engine._driver_lost("Connection closed while reading from the driver")
        self.assertEqual(engine.state.blocked()["reason"], "driver_lost_repeatedly",
                         "the third loss within 2 h waits for a person, no restart loop")
        self.assertEqual(tuple(engine.ledger.db.execute("SELECT outcome, reason FROM attempts").fetchone()),
                         ("unknown", "driver_lost"))

    def test_a_closed_connection_is_a_lost_driver_not_a_network_outcome(self):
        from pmos.browser.transport import _lost_or_network
        from pmos.faults import DriverLost
        self.assertIsInstance(_lost_or_network(RuntimeError("Connection closed while reading from the driver")), DriverLost)
        self.assertIsInstance(_lost_or_network(RuntimeError("Target page, context or browser has been closed")), DriverLost)
        self.assertEqual(_lost_or_network(TimeoutError("timeout")).code, "network_error")


class MidnightAndUnsent(unittest.IsolatedAsyncioTestCase):
    """Liaoning 2026-11-01 00:03: the midnight SSO refresh met a page operation; nothing was sent, a lane paused 1 h."""

    def engine(self, **policy):
        from datetime import datetime
        from pmos.periods import CHINA
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock(datetime(2026, 11, 1, 0, 3, tzinfo=CHINA).timestamp())
        province = make_province(tmp.path, [dataset("load", start=TODAY)], policy={"response_interval_seconds": 1, **policy})
        self.transport = FakeTransport(self.clock)
        engine = Engine(province, self.transport, instance=tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        return engine

    async def test_no_business_request_starts_inside_the_midnight_window(self):
        engine = self.engine(quiet_windows=["23:58-00:10"])
        delay = await engine.tick()
        self.assertEqual((self.transport.sent, engine.state.read("status")["phase"]), ([], "quiet_window"))
        self.assertLessEqual(delay, 60)
        self.clock.advance(7 * 60 + 1)           # 00:10:01
        await engine.tick()
        self.assertEqual(len(self.transport.sent), 1)

    def test_windows_across_midnight_and_malformed_ones(self):
        from pmos.pacing import quiet_seconds_left
        self.assertEqual(quiet_seconds_left(["23:58-00:10"], 23 * 60 + 59), 11 * 60)
        self.assertEqual(quiet_seconds_left(["23:58-00:10"], 5), 5 * 60)
        self.assertEqual(quiet_seconds_left(["23:58-00:10"], 12 * 60), 0)
        with self.assertRaises(PolicyError):
            Policy.from_dict({"quiet_windows": ["24:00-01:00"]})

    async def test_nothing_sent_cools_down_and_is_tried_again_the_third_in_a_row_stays_held(self):
        engine = self.engine()
        for round_ in range(3):
            self.transport.queue = [Fault("page_sent_nothing", "local", unsent=True)]
            await engine.tick()
            hold = engine.dataset_holds()["load"]
            self.assertEqual((hold["reason"], hold["streak"]), ("unsent:page_sent_nothing", round_ + 1))
            self.clock.advance(601)
        self.assertIn("load", engine.dataset_holds(), "the third preparation failure in a row waits for review")
        self.assertNotIn("until", engine.dataset_holds()["load"])
        task = engine.ledger.db.execute("SELECT status FROM tasks WHERE dataset='load'").fetchone()[0]
        self.assertEqual(task, "pending", "never consumed: nothing was sent")


class ExportBlankProbe(unittest.IsolatedAsyncioTestCase):
    async def test_the_export_endpoint_is_asked_once_with_the_page_query_headers_and_a_blank_option(self):
        from types import SimpleNamespace
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        spec = dataset("units", start=TODAY, path="/px-test/unitsQuery")
        spec["scopes"] = [{"tdName": "A"}]
        province = make_province(tmp.path, [spec], policy={"response_interval_seconds": 15})
        transport = FakeTransport(clock)
        transport.templates = SimpleNamespace(items={"/px-test/unitsQuery": {"headers": {}}})
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        command = engine.enqueue({"action": "probe_export_blank", "dataset": "units", "period": TODAY,
                                  "export_path": "/px-test/unitsExport", "date_field": "tdate"})
        await engine.tick()
        sent = transport.sent[0]
        self.assertEqual((sent["path"], sent["template"], sent["body"]), ("/px-test/unitsExport", "/px-test/unitsQuery",
                                                                         {"tdate": TODAY, "tdName": ""}))
        result = engine.state.read("command_results")[command]
        self.assertTrue(result["ok"])
        self.assertGreater(result["bytes"], 0)

    async def test_without_a_page_query_the_page_itself_exports_the_given_option_once(self):
        """User 2026-10-31: the default option exported as legacy did (the page's exportData), one request."""
        from types import SimpleNamespace
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        spec = dataset("units", start=TODAY, path="/px-test/unitsQuery")
        spec["request"] = {"transport": "component", "frame": "/pxf-test/units", "open_url": "/pxf-test/#/units",
                           "component": "*", "match": {"queryUrl": "/px-test/unitsQuery"}, "method": "POST",
                           "path": "/px-test/unitsQuery", "call": "queryPage",
                           "set": {"date": "{date}", "selectValue": "{scope[tdName]}",
                                   "queryConfig": {"data": {"tdate": "{date}", "tdName": "{scope[tdName]}"}}}}
        spec["scopes"] = [{"tdName": "A"}, {"tdName": "B"}]
        province = make_province(tmp.path, [spec], policy={"response_interval_seconds": 15})
        transport = FakeTransport(clock, default=envelope(40302))
        transport.templates = SimpleNamespace(items={})
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        command = engine.enqueue({"action": "probe_export_blank", "dataset": "units", "period": TODAY, "option": "A",
                                  "export_path": "/px-test/unitsExport", "date_field": "tdate"})
        await engine.tick()
        self.assertEqual(len(transport.sent), 1)
        sent = transport.sent[0]
        self.assertEqual((sent["transport"], sent["call"], sent["path"], sent["capture"], sent["frame"]),
                         ("component", "exportData", "/px-test/unitsExport", "blob", "/pxf-test/units"))
        self.assertEqual(sent["match"], {"queryUrl": "/px-test/unitsQuery", "exportUrl": "/px-test/unitsExport"})
        self.assertEqual(sent["set"], {"selectValue": "A", "date": TODAY,
                                       "queryConfig.data": {"tdate": TODAY, "tdName": "A"}})
        result = engine.state.read("command_results")[command]
        self.assertEqual((result["ok"], result["reason"]), (False, "permission_denied"))
        self.assertIsNone(engine.state.blocked(), "a refused probe never blocks the province")
        attempt = engine.ledger.db.execute("SELECT scope, outcome FROM attempts").fetchone()
        self.assertEqual(tuple(attempt), ("exportoption", "failed"))
