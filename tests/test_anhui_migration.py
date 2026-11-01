"""Anhui cutover: native bindings become fetch specs, identity parity, in-flight guard, handoff and replay."""
import json
import os
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from pmos.browser.transport import _template_body
from pmos.engine import Engine
from pmos.faults import Fault
from pmos.migration import anhui as migration
from pmos.periods import today
from pmos.province import Province
from pmos.spec import DatasetSpec
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir

DAY = [(today() - timedelta(days=n)).isoformat() for n in range(4)]   # newest first
ENDPOINT = "/px-phdah-settlement-oinfpubgrid/fineReport/select1_2"


def ui_spec(name):
    return DatasetSpec(**{"id": name, "name": name, "status": "sampled",
                          "period": {"kind": "day", "start": DAY[-1], "end": "today"},
                          "request": {"transport": "ui", "frame": "infoCode=x", "action": "export",
                                      "fields": [{"kind": "date", "placeholder": "选择日期", "value": "{date}"}],
                                      "menu": ["现货市场", name]},
                          "response": {"kind": "excel"}})


def legacy_root(base: Path, *, in_flight=False) -> Path:
    root = base / "anhui"
    (root / "state").mkdir(parents=True)
    bindings = root / migration.BINDINGS
    bindings.mkdir(parents=True)
    registry = {"load": {"family": "daily", "title": "实际负荷", "phase": "native_verified"},
                "typed": {"family": "daily", "title": "分类型", "phase": "native_verified"},
                "listing": {"family": "publication", "title": "公告", "phase": "native_listing_verified"}}
    (root / migration.REGISTRY).write_text(json.dumps(registry, ensure_ascii=False))
    for name, selectors, shape in (("load", {"日期": DAY[1]}, {"businessTime": "str"}),
                                   ("typed", {"日期": DAY[1], "类型": "A"}, {"businessTime": "str", "type": "str"})):
        (bindings / f"{name}.json").write_text(json.dumps({
            "dataset": name, "method": "POST", "endpoint_path": ENDPOINT, "body_shape": shape,
            "parameter_path": ["businessTime"], "selectors": selectors, "success_code": 0}, ensure_ascii=False))
    attempts = {day: {"phase": "captured", "period": day, "selectors": {"日期": day},
                      "receipt": {"dataset": "load", "period": day, "selectors": {"日期": day}, "source_sha256": "a" * 64}}
                for day in DAY[1:]}
    # As two of the server's attempts: only the receipt names the query.
    attempts[DAY[3]] = {"phase": "captured", "receipt": attempts[DAY[3]]["receipt"]}
    if in_flight:
        attempts[DAY[0]] = {"phase": "attempting", "period": DAY[0], "selectors": {"日期": DAY[0]}}
    (root / migration.attempts_path("load")).write_text(json.dumps(attempts, ensure_ascii=False))
    return root


class Specs(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)

    def test_a_date_only_native_report_becomes_a_fetch_replay_opened_by_its_ui_spec(self):
        built = migration.build_specs(legacy_root(self.tmp.path), {"load": ui_spec("load"), "typed": ui_spec("typed")})
        self.assertEqual(sorted(built["specs"]), ["load"])
        self.assertEqual(built["deferred"], {"typed": "selectors_beyond_the_date",
                                             "listing": "legacy_phase_native_listing_verified"})
        request = built["specs"]["load"]["request"]
        self.assertEqual((request["transport"], request["path"], request["template_body_set"]),
                         ("fetch", ENDPOINT, {"businessTime": "{date}"}))
        self.assertEqual({k: request["bootstrap"][k] for k in ("transport", "action", "path", "menu")},
                         {"transport": "ui", "action": "query", "path": ENDPOINT, "menu": ["现货市场", "load"]})
        again = migration.build_specs(legacy_root(TempDir().path), {"load": DatasetSpec(**built["specs"]["load"])})
        self.assertEqual(again["specs"]["load"]["request"], request, "a generated spec rebuilds itself")

    def test_parity_and_an_open_legacy_send_blocks_the_plan(self):
        specs = {"load": ui_spec("load")}
        plan = migration.plan_cutover(legacy_root(self.tmp.path), specs)
        self.assertTrue(plan["ready"])
        self.assertEqual((plan["scope"], plan["per_source"]["load"]["matched"]), (["load"], 3))
        other = TempDir()
        self.addCleanup(other.cleanup)
        busy = migration.plan_cutover(legacy_root(other.path, in_flight=True), specs)
        self.assertEqual((busy["ready"], busy["global_blockers"]), (False, {"legacy_attempt_in_flight": 1}))


class Handoff(unittest.IsolatedAsyncioTestCase):
    async def test_after_the_handoff_only_new_days_are_replayed_with_the_page_body(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        lag = patch.dict(migration.PUBLISH_LAG_DAYS, {"load": 0})   # a report published as the day runs
        lag.start()
        self.addCleanup(lag.stop)
        root = legacy_root(tmp.path)
        built = migration.build_specs(root, {"load": ui_spec("load")})
        directory = tmp.path / "provinces" / "anhui"
        (directory / "datasets").mkdir(parents=True)
        (directory / "province.json").write_text(json.dumps({
            "id": "anhui", "name": "安徽", "origin": "https://pmos.ah.sgcc.com.cn",
            "policy": {"response_interval_seconds": 5, "renew_after_idle_seconds": 300, "probe_after_idle_seconds": 300},
            "session": {"keepalive_dataset": None, "probe": None}}))
        migration.write_specs(built, directory / "datasets", ["load"])
        province = Province.load("anhui", tmp.path / "provinces")
        plan = migration.plan_cutover(root, province.datasets())
        instance = tmp.path / "instance"
        migration.apply_cutover(root, instance, plan, province.datasets(), legacy_owner={"stopped_evidence": "fixture"},
                                code_sha="c" * 64, approval="同意", request_budget=10)
        clock = Clock()
        body = {"status": 0, "data": [{"v": 1}]}
        transport = FakeTransport(clock, default=Response(200, "application/json", json.dumps(body).encode()))
        engine = Engine(province, transport, instance=instance, clock=clock, account_alias="anhui-production")
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        transport.unready = {migration_endpoint(province)}
        await engine.tick()                      # no page request observed yet
        transport.unready = set()                # the page's own request is a template now
        for _ in range(5):
            clock.advance(10)
            await engine.tick()
        sent = [(r["transport"], r.get("template_body_set")) for r in transport.sent]
        self.assertEqual(sent[0][0], "ui", "no page request seen yet: the report opens through its UI once")
        periods = {r.get("template_body_set", {}).get("businessTime") for r in transport.sent if r["transport"] == "fetch"}
        self.assertFalse(periods & set(DAY[1:]), "days the legacy worker captured are never sent again")
        self.assertEqual(len(transport.sent), 1, "only the one day the legacy worker never sent")
        self.assertEqual(transport.sent[0]["fields"][0]["value"], DAY[0])


class PublishLag(unittest.IsolatedAsyncioTestCase):
    """PMOS saves an empty or partial answer for good: a day is asked for only after its report's lag."""

    def test_reports_of_what_happened_wait_two_days_and_the_rest_one(self):
        built = migration.build_specs(legacy_root(TempDir().path), {"load": ui_spec("load")})
        self.assertEqual(built["specs"]["load"]["period"]["lag_days"], 1, "never the running day")
        self.assertEqual((migration.publish_lag("actual_load"), migration.publish_lag("intertie_actual_huan_i"),
                          migration.publish_lag("queryRqxtfhyc")), (2, 2, 1))

    async def test_a_seeded_task_of_a_day_not_yet_due_waits_without_being_sent(self):
        from helpers import dataset, make_province
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        spec = dataset("load", start=DAY[3], response="json")
        spec["period"]["lag_days"] = 2
        province = make_province(tmp.path, [spec], policy={"response_interval_seconds": 1})
        transport = FakeTransport(clock, default=Response(200, "application/json", b'{"status":0,"data":[1]}'))
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        engine.ledger.seed("load", [DAY[0], DAY[1]], "")      # as a legacy owner's open candidates
        for _ in range(6):
            await engine.tick()
            clock.advance(2)
        sent = [r["body"]["date"] for r in transport.sent]
        self.assertEqual(sent, [DAY[2], DAY[3]], "due days only, newest first")
        self.assertEqual({engine.ledger.task("load", d)["status"] for d in DAY[:2]}, {"pending"})
        self.assertEqual(engine.state.read("status")["phase"], "waiting_publication")


def migration_endpoint(province):
    return province.datasets()["load"].request["path"]


class TemplateBody(unittest.TestCase):
    def test_only_the_declared_fields_of_the_pages_own_body_change(self):
        raw = json.dumps({"businessTime": "2026-10-01", "type": "A", "taskId": "t1"})
        self.assertEqual(json.loads(_template_body(raw, {"businessTime": "2026-10-30"})),
                         {"businessTime": "2026-10-30", "type": "A", "taskId": "t1"})
        with self.assertRaises(Fault) as caught:
            _template_body(raw, {"genName": ""})
        self.assertEqual((caught.exception.code, caught.exception.unsent), ("template_body_field_missing", True))
        with self.assertRaises(Fault):
            _template_body("not json", {"businessTime": "x"})


if __name__ == "__main__":
    unittest.main()
