"""Liaoning cutover: generated specs, identity parity, handoff gate, budget, skip and stop-all."""
import json
import unittest
from datetime import timedelta
from pathlib import Path

from pmos.engine import Engine
from pmos.migration import liaoning as migration
from pmos.periods import today
from pmos.province import Province
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir

DAY = [(today() - timedelta(days=n)).isoformat() for n in range(4)]   # newest first
MONTH = (today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
NODE = "辽宁.测试厂/220kV.一母母线"
ADAPTERS = """
class ReportAdapter: pass
ADAPTERS = [
    ReportAdapter('node_price', '节点电价', '005', 'mdate', selection_field='name'),
    ReportAdapter('actual_load', '实际负荷', '010', 'mdate', period='day'),
    ReportAdapter('monthly_balance', '月度平衡', '031', 'month', period='month'),
]
"""


def report_source(dataset, number, period, selections, title="t"):
    return {"dataset": dataset, "title": title, "kind": "report", "number": number, "period": period,
            "route": f"/pxf-dif-settlement-outnetpub/pdbln/report/PdblnReport{number}",
            "endpoint": f"/px-dif-settlement-infpubquery/pdblnReportController/pdblnReport{number}",
            "method": "POST", "selections": selections, "scope": "historical_dates_only_verified_selections",
            "proofs": [{"sha256": "a" * 64}], "prior_outcomes": {}}


def job(dataset, period, params):
    key = f"liaoning:2026:expanded:{dataset}:{period}:{len(params)}"
    return key, {"key": key, "dataset": dataset, "params": params, "period": period, "page": 1}


def legacy_root(base: Path, *, active=None, extra_param=False, hold=None) -> Path:
    root = base / "legacy"
    (root / "lncrawler").mkdir(parents=True)
    (root / "lncrawler/expanded_adapters.py").write_text(ADAPTERS)
    state = root / "state/server_verified_expansion_20261023"
    state.mkdir(parents=True)
    manifest = {"created_at": "x", "excluded": {}, "interval_seconds": 60, "retry_unknown": False, "sources": {
        "node_price": report_source("node_price", "005", "day", [NODE]),
        "actual_load": report_source("actual_load", "010", "day", [""]),
        "monthly_balance": report_source("monthly_balance", "031", "month", [""]),
        "member_list": {"dataset": "member_list", "kind": "member", "title": "成员"}}}
    (state / "active_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    sources = {}
    # node_price: newest day saved by legacy, the rest pending
    saved_key, _ = job("node_price", DAY[0], {"mdate": DAY[0], "name": NODE})
    pending = [job("node_price", d, {"mdate": d, "name": NODE, **({"extra": 1} if extra_param else {})})[1]
               for d in DAY[1:]]
    sources["node_price"] = {"pending": pending, "hold": hold, "empty_streak": 0, "outcomes": {
        saved_key: {"outcome": "saved", "identity": {"period": DAY[0], "selection": NODE}}}}
    # actual_load: one legacy unknown (in-flight lost) and one denied
    unknown_key, unknown_job = job("actual_load", DAY[0], {"mdate": DAY[0]})
    denied_key, _ = job("actual_load", DAY[1], {"mdate": DAY[1]})
    sources["actual_load"] = {"pending": [{**unknown_job, "status": "unknown", "sent": True}], "hold": None,
                              "outcomes": {denied_key: {"outcome": "denied", "identity": {"period": DAY[1]}}}}
    sources["monthly_balance"] = {"pending": [job("monthly_balance", MONTH, {"month": MONTH})[1]],
                                  "outcomes": {}, "hold": None}
    queue = {"sources": sources, "active": active, "cursor": 0, "created_at": "x", "source_order": list(sources),
             "seeded_date": DAY[0], "helper_hold": None}
    (state / "queue.json").write_text(json.dumps(queue, ensure_ascii=False))
    return root


def pmos_province(base: Path, specs: dict) -> Province:
    directory = base / "provinces" / "liaoning"
    (directory / "datasets").mkdir(parents=True)
    (directory / "province.json").write_text(json.dumps({
        "id": "liaoning", "name": "辽宁", "origin": "https://pmos.ln.sgcc.com.cn",
        "policy": {"response_interval_seconds": 15, "renew_after_idle_seconds": 300, "probe_after_idle_seconds": 300},
        "session": {"keepalive_dataset": None, "probe": None}}))
    for name, value in specs.items():
        (directory / "datasets" / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False))
    return Province.load("liaoning", base / "provinces")


def json_response(rows=1):
    body = {"status": 0, "data": [{"v": 1}] * rows}
    return Response(200, "application/json", json.dumps(body).encode())


OWNER = {"host": "test-host", "service": "legacy.service", "stopped_evidence": "systemctl inactive (fixture)"}


class GeneratedSpecs(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)

    def test_report_sources_become_the_legacy_component_call(self):
        built = migration.build_specs(legacy_root(self.tmp.path))
        self.assertEqual(sorted(built["specs"]), ["actual_load", "monthly_balance", "node_price"])
        self.assertEqual(built["deferred"], {"member_list": "legacy_kind_member_not_in_first_scope"})
        node = built["specs"]["node_price"]
        self.assertNotIn("site_menu", node["request"], "a source without its infoCode keeps the deep link")
        self.assertEqual(node["request"]["component"], "pdblnReport005")
        self.assertEqual(node["request"]["call"], "initData")
        self.assertEqual(node["request"]["set"], {"tableData": [], "queryParam.mdate": "{date}",
                                                  "queryParam.name": "{scope[name]}"})
        self.assertEqual(node["scopes"], [{"name": NODE}])
        self.assertEqual(node["source"]["identity_contract"]["dimension_fields"], {"queryParam.name": "name"})
        month = built["specs"]["monthly_balance"]
        self.assertEqual((month["period"]["kind"], month["request"]["set"]["queryParam.month"]), ("month", "{month}"))
        self.assertNotIn("scopes", built["specs"]["actual_load"])

    def test_a_report_with_its_menu_code_opens_from_the_sites_own_menu(self):
        root = legacy_root(self.tmp.path)
        path = root / "state/server_verified_expansion_20261023/active_manifest.json"
        manifest = json.loads(path.read_text())
        manifest["sources"]["actual_load"]["source"] = "lnsnxh_sjfh"
        path.write_text(json.dumps(manifest, ensure_ascii=False))
        request = migration.build_specs(root)["specs"]["actual_load"]["request"]
        self.assertEqual(request["site_menu"], {"source": "lnsnxh_sjfh",
                                                "route": "/pxf-dif-settlement-outnetpub/pdbln/report/PdblnReport010"})

    def test_parity_matches_every_legacy_record_and_flags_extra_parameters(self):
        report = migration.parity(legacy_root(self.tmp.path), bridges=False)
        self.assertEqual(report["summary"]["records"], report["summary"]["matched"])
        self.assertEqual(report["summary"]["unresolved_consumed"], 0)
        self.assertEqual(report["eligible"], ["actual_load", "monthly_balance", "node_price"])
        other = TempDir()
        self.addCleanup(other.cleanup)
        drifted = migration.parity(legacy_root(other.path, extra_param=True), bridges=False)
        self.assertIn("node_price", drifted["not_eligible"])
        self.assertEqual(drifted["not_eligible"]["node_price"]["unmapped"], 3)

    def test_a_consumed_outcome_without_its_query_is_resolved_from_the_workers_issued_job(self):
        """Server queue 2026-10-31: 7,564 consumed outcomes keep no query; operations/<id>/issued.json does."""
        root = legacy_root(self.tmp.path)
        state = root / "state/server_verified_expansion_20261023"
        queue = json.loads((state / "queue.json").read_text())
        key, issued = job("actual_load", DAY[2], {"mdate": DAY[2]})
        queue["sources"]["actual_load"]["outcomes"][key] = {"outcome": "saved", "sha256": "b" * 64, "rows": 1}
        (state / "queue.json").write_text(json.dumps(queue, ensure_ascii=False))
        self.assertEqual(migration.parity(root, bridges=False)["not_eligible"]["actual_load"]["unmapped_consumed"], 1)
        operation = state / "operations" / ("1" * 64)
        operation.mkdir(parents=True)
        (operation / "issued.json").write_text(json.dumps({"at": "x", "job": issued, "endpoint": "/e", "method": "POST"}))
        report = migration.parity(root, bridges=False)
        self.assertIn("actual_load", report["eligible"])
        self.assertEqual(report["per_source"]["actual_load"]["bridged"], 1)
        self.assertEqual(report["inputs"][-1]["files"], 1)
        other = state / "operations" / ("2" * 64)
        other.mkdir()
        (other / "issued.json").write_text(json.dumps({"job": {**issued, "params": {"mdate": DAY[3]}, "period": DAY[3]}}))
        conflicted = migration.parity(root, bridges=False)
        self.assertEqual(conflicted["inputs"][-1]["conflicting_keys"], 1)
        self.assertNotIn("actual_load", conflicted["eligible"], "a key issued with two different jobs stays unresolved")

    def test_in_flight_legacy_task_or_source_hold_blocks_the_plan(self):
        plan = migration.plan_cutover(legacy_root(self.tmp.path), bridges=False)
        self.assertTrue(plan["ready"])
        _, active = job("node_price", DAY[1], {"mdate": DAY[1], "name": NODE})
        other = TempDir()
        self.addCleanup(other.cleanup)
        busy = migration.plan_cutover(legacy_root(other.path, active=active), bridges=False)
        self.assertFalse(busy["ready"])
        self.assertIn("legacy_active_owner_unsettled", busy["global_blockers"])
        third = TempDir()
        self.addCleanup(third.cleanup)
        held = migration.plan_cutover(legacy_root(third.path, hold={"reason": "denied"}), bridges=False)
        self.assertNotIn("node_price", held["scope"])
        self.assertEqual(held["excluded"]["node_price"], ["legacy_source_hold"])


class PublishLag(unittest.TestCase):
    """Liaoning lesson 22 / Anhui 2026-11-01: an empty or partial running day would be saved for good."""

    def test_actual_reports_wait_two_days_and_forward_looking_ones_one(self):
        lag = migration.publish_lag
        self.assertEqual([lag("非市场机组总出力"), lag("实际负荷"), lag("发输变电设备检修计划执行情况（日）")], [2, 2, 2])
        self.assertEqual([lag("非市场机组总出力预测"), lag("日前各时段出清电力及平均电价"), lag("发输变电设备检修计划（日）")],
                         [1, 1, 1])
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        built = migration.build_specs(legacy_root(tmp.path))
        days = [spec for spec in built["specs"].values() if spec["period"]["kind"] == "day"]
        self.assertTrue(days and all(spec["period"]["lag_days"] in (1, 2) for spec in days))


class Handoff(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reports published as the day runs: these tests are about legacy tombstones, not publication lag.
        from unittest.mock import patch
        lag = patch.object(migration, "publish_lag", lambda title: 0)
        lag.start()
        self.addCleanup(lag.stop)
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.root = legacy_root(self.tmp.path)
        self.built = migration.build_specs(self.root)
        self.plan = migration.plan_cutover(self.root, bridges=False, requested=["actual_load", "node_price"])
        self.instance = self.tmp.path / "instance"

    def engine(self, budget=10):
        migration.apply_cutover(self.root, self.instance, self.plan, legacy_owner=OWNER, code_sha="c" * 40,
                                approval="fixture approval", request_budget=budget)
        province = pmos_province(self.tmp.path, self.built["specs"])
        self.transport = FakeTransport(self.clock, default=json_response())
        engine = Engine(province, self.transport, instance=self.instance, clock=self.clock,
                        account_alias="liaoning-production")
        self.addCleanup(engine.ledger.close)
        return engine

    async def run_ticks(self, engine, n):
        for _ in range(n):
            await engine.tick()
            self.clock.advance(20)

    def test_without_handoff_a_shadow_ledger_never_dispatches_and_receipt_must_match(self):
        from pmos.ledger import Ledger
        from pmos.queue_compat import QueueContext, import_shadow
        rows, _ = migration.legacy_rows(self.root, "liaoning-production", self.built)
        ledger = Ledger(self.tmp.path / "shadow" / "ledger.sqlite3")
        self.addCleanup(ledger.close)
        import_shadow(ledger, [r for _, r in rows], QueueContext("liaoning", "liaoning-production"),
                      source_id="queue", snapshot_sha256="b" * 64)
        self.assertEqual(ledger.dispatch_blocker(), "legacy_shadow_requires_owner_handoff")
        receipt = {"version": 1, "province": "liaoning", "account_alias": "liaoning-production", "legacy_owner": OWNER,
                   "snapshots": [{"source_id": "other", "snapshot_sha256": "b" * 64}], "scope": ["node_price"],
                   "request_budget": 5, "code_sha": "c", "approval": "a", "session": {}, "seeding": "spec_periods"}
        with self.assertRaisesRegex(ValueError, "snapshots_differ"):
            ledger.grant_handoff(receipt)
        with self.assertRaisesRegex(ValueError, "stop_evidence"):
            ledger.grant_handoff({**receipt, "legacy_owner": {"host": "h"}})
        granted = ledger.grant_handoff({**receipt, "snapshots": [{"source_id": "queue", "snapshot_sha256": "b" * 64}]})
        self.assertEqual(len(granted["receipt_sha256"]), 64)
        self.assertIsNone(ledger.dispatch_blocker())
        with self.assertRaisesRegex(ValueError, "no_import_after_owner_handoff"):
            import_shadow(ledger, [], QueueContext("liaoning", "liaoning-production"),
                          source_id="late", snapshot_sha256="d" * 64)

    async def test_legacy_consumed_tasks_are_skipped_and_only_scope_is_sent(self):
        engine = self.engine()
        engine.state.set_control(collect=True)
        await self.run_ticks(engine, 4)
        sent = [(r["component"], r["set"].get("queryParam.mdate")) for r in self.transport.sent]
        # actual_load (first in scope): the newest day is a lost in-flight
        # legacy request (unknown) and the next one was denied; neither is resent.
        self.assertEqual(sent[0], ("pdblnReport010", DAY[2]))
        self.assertNotIn(("pdblnReport010", DAY[0]), sent)
        self.assertNotIn(("pdblnReport010", DAY[1]), sent)
        task = engine.ledger.task("actual_load", DAY[0])
        self.assertEqual((task["status"], task["reason"]), ("skipped", "legacy_consumed:unknown"))
        self.assertEqual(engine.ledger.task("actual_load", DAY[1])["reason"], "legacy_consumed:denied")
        # node_price: the newest day was saved by legacy -> skipped; the next is new work
        node = engine.province.datasets()["node_price"]
        scope = node.scope_items()[0][0]
        self.clock.advance(30)
        self.assertIsNone(await engine.collect(node, DAY[0], scope))
        self.assertEqual(engine.ledger.task("node_price", DAY[0], scope)["reason"], "legacy_consumed:saved")
        await engine.collect(node, DAY[1], scope)
        self.assertEqual(self.transport.sent[-1]["set"], {"tableData": [], "queryParam.mdate": DAY[1],
                                                          "queryParam.name": NODE})
        self.assertFalse(any(r["component"] == "pdblnReport031" for r in self.transport.sent), "outside scope")
        monthly = engine.province.datasets()["monthly_balance"]
        with self.assertRaisesRegex(Exception, "outside_handoff_scope"):
            await engine.request("collect", monthly.build(MONTH), monthly.response, dataset="monthly_balance",
                                 period=MONTH)
        self.assertIsNone(engine.state.blocked())

    async def test_request_budget_is_a_hard_stop_and_stop_all_sends_nothing(self):
        engine = self.engine(budget=2)
        engine.state.set_control(collect=True)
        await self.run_ticks(engine, 10)
        self.assertEqual(len(self.transport.sent), 2)
        self.assertEqual(engine.state.read("status")["phase"], "handoff_request_budget_exhausted")
        with self.assertRaisesRegex(Exception, "budget_exhausted"):
            engine.ledger.begin("keepalive")

    async def test_stop_all_sends_nothing_not_even_keepalive(self):
        engine = self.engine()
        engine.state.set_control(collect=True, stop_all=True)
        await self.run_ticks(engine, 5)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(engine.state.read("status")["phase"], "stopped_all")

    def test_apply_refuses_a_changed_legacy_root_and_an_existing_instance(self):
        queue = self.root / "state/server_verified_expansion_20261023/queue.json"
        value = json.loads(queue.read_text())
        value["cursor"] = 99
        queue.write_text(json.dumps(value, ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, "legacy_root_changed_since_plan"):
            migration.apply_cutover(self.root, self.instance, self.plan, legacy_owner=OWNER, code_sha="c",
                                    approval="a", request_budget=3)
        self.instance.mkdir()
        fresh = migration.plan_cutover(self.root, bridges=False, requested=["node_price"])
        with self.assertRaisesRegex(ValueError, "new_instance_directory_required"):
            migration.apply_cutover(self.root, self.instance, fresh, legacy_owner=OWNER, code_sha="c",
                                    approval="a", request_budget=3)


if __name__ == "__main__":
    unittest.main()
