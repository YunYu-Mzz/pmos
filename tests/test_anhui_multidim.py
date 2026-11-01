"""Anhui second batch: candidate keys, phases, parity, one combined handoff, option replay and echo."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pmos.engine import Engine, echo_mismatch
from pmos.faults import Fault
from pmos.migration import anhui_multidim as multidim
from pmos.province import Province
from pmos.spec import DatasetSpec
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir
from test_anhui_migration import DAY, legacy_root, ui_spec

ENDPOINT = "/px-phdah-settlement-oinfpubgrid/fineReport/select5_11"


def candidate(dataset, day, line, phase):
    identity = {"dataset": dataset, "period": day, "selectors": {"日期": day, "断面名称": line},
                "identity_bindings": {"断面名称": {"display_label": line, "identity_sha256": "f" * 64}}}
    return multidim.candidate_key(identity), {"identity": identity, "phase": phase, "evidence": []}


def add_sections(root: Path, *, open_send=False):
    (root / multidim.BINDINGS / "sections.json").write_text(json.dumps({
        "dataset": "sections", "method": "POST", "endpoint_path": ENDPOINT, "body_shape": {"businessTime": "str", "genName": "str"},
        "parameter_path": ["businessTime"], "selectors": {"日期": DAY[2], "断面名称": "甲线"}, "success_code": 0,
        "option_parameter_paths": {"断面名称": ["genName"]}, "option_echo_fields": {"断面名称": ["sec_name"]},
        "option_value_sources": {"断面名称": {"kind": "display_label"}}}, ensure_ascii=False))
    jobs = dict([candidate("sections", DAY[2], "甲线", "captured"), candidate("sections", DAY[2], "乙线", "pending_review"),
                 candidate("sections", DAY[1], "甲线", "observed_candidate"),
                 candidate("sections", DAY[1], "乙线", "requesting" if open_send else "source_empty")])
    (root / multidim.candidates_path("sections")).write_text(json.dumps(
        {"revision": "dated_selector_tree_v1", "dataset": "sections", "jobs": jobs}, ensure_ascii=False))
    return root


def section_ui():
    spec = ui_spec("sections")
    spec.request["fields"] = [{"kind": "date", "placeholder": "选择日期", "value": "{date}"}]
    return spec


class Candidates(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)

    def test_keys_recompute_and_only_an_observed_candidate_is_open(self):
        root = add_sections(legacy_root(self.tmp.path))
        rows, _, open_sends = multidim.legacy_rows(root, "anhui-production", ["sections"])
        self.assertEqual(open_sends, [])
        states = sorted((row.legacy_state, row.state, row.consumed) for _, row in rows)
        self.assertEqual(states, [("captured", "saved", True), ("observed_candidate", "pending", False),
                                  ("pending_review", "held", True), ("source_empty", "empty", True)])
        self.assertTrue(all(row.identity is not None and not row.blockers for _, row in rows))
        self.assertEqual(rows[0][1].identity.dimensions["日期"], rows[0][1].identity.period["label"])

    def test_a_replay_spec_has_one_scope_per_option_and_matches_every_legacy_identity(self):
        root = add_sections(legacy_root(self.tmp.path))
        built = multidim.build_specs(root, {"sections": section_ui()})
        spec = built["specs"]["sections"]
        self.assertEqual(spec["request"]["template_body_set"], {"businessTime": "{date}", "genName": "{scope[断面名称]}"})
        self.assertEqual(spec["scopes"], [{"断面名称": "乙线"}, {"断面名称": "甲线"}])
        self.assertEqual(spec["response"]["echo"], {"sec_name": "断面名称"})
        self.assertEqual(spec["request"]["bootstrap"]["fields"][1], {"kind": "select", "label": "断面名称",
                                                                    "value": "{scope[断面名称]}"})
        report = multidim.parity(root, {"sections": section_ui()})
        self.assertEqual((report["eligible"], report["per_source"]["sections"]["matched"]), (["sections"], 4))
        rebuilt = multidim.build_specs(root, {"sections": DatasetSpec(**spec)})["specs"]["sections"]
        self.assertEqual(rebuilt["request"], spec["request"], "a generated spec rebuilds itself")

    def test_an_open_legacy_candidate_blocks_the_combined_plan(self):
        root = add_sections(legacy_root(self.tmp.path), open_send=True)
        plan = multidim.plan_cutover(root, {"load": ui_spec("load"), "sections": section_ui()})
        self.assertEqual((plan["ready"], plan["global_blockers"]), (False, {"legacy_candidate_in_flight": 1}))


class CombinedHandoff(unittest.IsolatedAsyncioTestCase):
    async def test_one_receipt_grows_the_daily_reports_and_replays_only_open_candidates(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        from pmos.migration import anhui as first
        lag = patch.dict(first.PUBLISH_LAG_DAYS, {"load": 0})   # a report published as the day runs
        lag.start()
        self.addCleanup(lag.stop)
        root = add_sections(legacy_root(tmp.path))
        directory = tmp.path / "provinces" / "anhui"
        (directory / "datasets").mkdir(parents=True)
        (directory / "province.json").write_text(json.dumps({
            "id": "anhui", "name": "安徽", "origin": "https://pmos.ah.sgcc.com.cn",
            "policy": {"response_interval_seconds": 5, "renew_after_idle_seconds": 300, "probe_after_idle_seconds": 300,
                       "isolate_items": ["site_data"]},
            "session": {"keepalive_dataset": None, "probe": None}}))
        from pmos.migration import anhui as first
        first.write_specs(first.build_specs(root, {"load": ui_spec("load")}), directory / "datasets", ["load"])
        multidim.write_specs(multidim.build_specs(root, {"sections": section_ui()}), directory / "datasets", ["sections"])
        province = Province.load("anhui", tmp.path / "provinces")
        plan = multidim.plan_cutover(root, province.datasets())
        self.assertTrue(plan["ready"])
        result = multidim.apply_cutover(root, tmp.path / "instance", plan, province.datasets(),
                                        legacy_owner={"stopped_evidence": "fixture"}, code_sha="c" * 64,
                                        approval="一次切换吧", request_budget=20)
        self.assertEqual(result["seeded"], {"sections": 1})
        clock = Clock()
        echoed = {"status": 0, "data": [{"sec_name": "甲线", "v": 1}]}
        transport = FakeTransport(clock, default=Response(200, "application/json", json.dumps(echoed).encode()))
        engine = Engine(province, transport, instance=tmp.path / "instance", clock=clock, account_alias="anhui-production")
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        for _ in range(8):
            await engine.tick()
            clock.advance(10)
        replays = [r["template_body_set"] for r in transport.sent if r.get("path") == ENDPOINT]
        self.assertEqual(replays, [{"businessTime": DAY[1], "genName": "甲线"}], "the one open candidate, nothing else")
        rows = dict(((r["dataset"], r["period"]), r["status"]) for r in engine.ledger.db.execute(
            "SELECT dataset, period, status FROM tasks"))
        self.assertEqual(rows[("sections", DAY[1])], "saved")
        self.assertEqual(rows[("load", DAY[0])], "saved", "the date-only report still grows by day")
        self.assertNotIn(("sections", DAY[0]), rows, "today is not a per-option day yet (lag 1)")
        from datetime import timedelta
        from pmos.periods import today
        engine._roll_forward_options(set(engine.ledger.handoff()["seeded"]), today() + timedelta(days=1))
        rolled = sorted(r["scope"] for r in engine.ledger.db.execute(
            "SELECT scope FROM tasks WHERE dataset='sections' AND period=?", (DAY[0],)))
        self.assertEqual(len(rolled), 2, "the next day gets both known options")


class InternalOptionValues(unittest.TestCase):
    """6d80: the request carries a unit's internal ID, the legacy identity its display name."""

    def test_an_internal_value_is_taken_only_from_a_verified_original_with_the_candidates_own_identity(self):
        import hashlib
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        root = legacy_root(tmp.path)
        name, endpoint = "units", "/px-contract-extranet-daypart/queryPlatform/execute"
        original = json.dumps({"status": 0, "data": [{"NAME": "甲机", "unit_id": "3378262674776260623"},
                                                     {"NAME": "乙机", "unit_id": "999"}]}, ensure_ascii=False).encode()
        digest = hashlib.sha256(original).hexdigest()
        (root / "data/2026/native_tables/units").mkdir(parents=True)
        (root / "data/2026/native_tables/units/o.json").write_bytes(original)
        (root / multidim.BINDINGS / "units.json").write_text(json.dumps({
            "dataset": name, "method": "POST", "endpoint_path": endpoint, "parameter_path": ["businessTime"],
            "body_shape": {"businessTime": "str", "taskId": "str", "unitId": "number"}, "success_code": 0,
            "option_parameter_paths": {"机组名称": ["unitId"]}, "option_echo_fields": {},
            "option_value_sources": {"机组名称": {"kind": "archived_response_value"}}}, ensure_ascii=False))

        def job(day, unit, value, phase, receipt=None):
            identity = {"dataset": name, "period": day, "selectors": {"日期": day, "机组名称": unit},
                        "identity_bindings": {"机组名称": {"display_label": unit, "identity_sha256": multidim.option_identity(value)}}}
            return multidim.candidate_key(identity), {"identity": identity, "phase": phase, **({"receipt": receipt} if receipt else {})}
        receipt = {"source_path": "data/2026/native_tables/units/o.json", "source_sha256": digest}
        jobs = dict([job(DAY[2], "甲机", "3378262674776260623", "captured", receipt),
                     job(DAY[1], "甲机", "3378262674776260623", "observed_candidate"),
                     job(DAY[1], "乙机", "1000", "observed_candidate"),          # the original says 999: not proven
                     job(DAY[1], "丙机", "5", "observed_candidate")])            # in no original yet
        (root / multidim.candidates_path(name)).write_text(json.dumps({"revision": "r", "dataset": name, "jobs": jobs},
                                                                     ensure_ascii=False))
        spec = ui_spec(name)
        spec.request["fields"] = [{"kind": "date", "placeholder": "选择日期", "value": "{date}"}]
        built = multidim.build_specs(root, {name: spec})["specs"][name]
        self.assertEqual(built["scopes"], [{"机组名称": "甲机", "unitId": "3378262674776260623"}])
        self.assertEqual(built["request"]["template_body_set"], {"businessTime": "{date}", "unitId": "{scope[unitId]}"})
        report = multidim.parity(root, {name: spec})
        self.assertEqual(report["per_source"][name], {"records": 4, "consumed": 1, "matched": 2, "option_unresolved": 2})
        self.assertIn(name, report["eligible"])


class Echo(unittest.TestCase):
    def test_every_record_must_name_the_tasks_option(self):
        spec = DatasetSpec(**{"id": "sections", "name": "s", "status": "verified",
                              "period": {"kind": "day", "start": DAY[-1], "end": "today"},
                              "request": {"transport": "fetch", "method": "POST", "path": ENDPOINT,
                                          "template_body_set": {"businessTime": "{date}"}},
                              "response": {"kind": "json", "echo": {"sec_name": "断面名称"}}})
        echo_mismatch(spec, {"断面名称": "甲线"}, json.dumps({"data": [{"sec_name": "甲线"}]}).encode())
        echo_mismatch(spec, {"断面名称": "甲线"}, json.dumps({"data": {}}).encode())
        with self.assertRaises(Fault) as caught:
            echo_mismatch(spec, {"断面名称": "甲线"}, json.dumps({"data": [{"sec_name": "乙线"}]}).encode())
        self.assertEqual((caught.exception.code, caught.exception.category), ("native_option_echo_mismatch", "site_data"))


if __name__ == "__main__":
    unittest.main()


class WholeDay(unittest.IsolatedAsyncioTestCase):
    """Anhui node_price_whole_day: one blank-option answer per day and type, primed by a collected record."""

    def test_an_answer_must_hold_every_option_complete(self):
        from pmos.engine import whole_day_mismatch
        spec = DatasetSpec(**{"id": "nodes_whole_day", "name": "n", "status": "verified",
                              "period": {"kind": "day", "start": DAY[-1], "end": "today"},
                              "request": {"transport": "fetch", "method": "POST", "path": ENDPOINT,
                                          "template_body_set": {"businessTime": "{date}"}},
                              "response": {"kind": "json", "whole_day": {"option_field": "node_name", "time_field": "dataTime",
                                                                         "times": 2, "repeat": "single", "type_field": "price_type",
                                                                         "type_label": "数据类型"}}})
        good = [{"node_name": n, "dataTime": t, "price_type": "节点价格"} for n in ("A", "B") for t in ("00:15", "00:30")]
        whole_day_mismatch(spec, {"数据类型": "节点价格"}, json.dumps({"data": good}).encode())
        whole_day_mismatch(spec, {"数据类型": "节点价格"}, json.dumps({"data": []}).encode())
        for bad, code in ((good[:-1], "whole_day_times_changed"), (good + [good[0]], "whole_day_time_repeated"),
                          ([{**r, "price_type": "能量价格"} for r in good], "whole_day_type_mismatch")):
            with self.assertRaises(Fault) as caught:
                whole_day_mismatch(spec, {"数据类型": "节点价格"}, json.dumps({"data": bad}).encode())
            self.assertEqual(caught.exception.code, code)
        uniform = DatasetSpec(**{**spec.__dict__, "response": {"kind": "json", "whole_day": {
            "option_field": "node_name", "time_field": "dataTime", "times": 2, "repeat": "uniform"}}})
        twice = [{"node_name": "A", "dataTime": t, "v": 1} for t in ("00:15", "00:30") for _ in range(2)]
        whole_day_mismatch(uniform, {}, json.dumps({"data": twice}).encode())
        with self.assertRaises(Fault):
            whole_day_mismatch(uniform, {}, json.dumps({"data": twice[:-1] + [{**twice[-1], "v": 2}]}).encode())

    async def test_the_lane_is_primed_once_then_replays_the_blank_option_body(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        root = add_sections(legacy_root(tmp.path))
        rule = {"types": None, "option_field": "sec_name", "repeat": "uniform"}
        with patch.dict(multidim.WHOLE_DAY_SPECS, {"sections": rule}, clear=True):
            (root / multidim.whole_day_path("sections")).write_text(json.dumps({"dataset": "sections", "jobs": {
                f"{DAY[2]}|全部": {"period": DAY[2], "type": None, "phase": "captured"},
                f"{DAY[3]}|全部": {"period": DAY[3], "type": None, "phase": "pending"}}}, ensure_ascii=False))
            option = multidim.build_specs(root, {"sections": section_ui()})["specs"]
            built = multidim.build_whole_day_specs(root, option)
            spec = built["specs"]["sections_whole_day"]
            self.assertEqual(spec["request"]["template_body_set"], {"businessTime": "{date}", "genName": ""})
            self.assertEqual(spec["request"]["prime"]["period"], DAY[2], "the newest collected record primes it")
            self.assertEqual([f["value"] for f in spec["request"]["prime"]["request"]["fields"]], [DAY[2], "甲线"])
            specs = {"sections_whole_day": DatasetSpec(**spec)}
            rows, _, _ = multidim.whole_day_rows(root, "anhui-production", ["sections_whole_day"])
            self.assertEqual(sorted((r.state, r.consumed) for _, r in rows), [("pending", False), ("saved", True)])
            report = multidim.whole_day_parity(root, {**{k: DatasetSpec(**v) for k, v in option.items()}, **specs})
            self.assertEqual(report["per_source"]["sections_whole_day"]["matched"], 2)
        directory = tmp.path / "provinces" / "anhui"
        (directory / "datasets").mkdir(parents=True)
        (directory / "province.json").write_text(json.dumps({
            "id": "anhui", "name": "安徽", "origin": "https://pmos.ah.sgcc.com.cn",
            "policy": {"response_interval_seconds": 1, "renew_after_idle_seconds": 300, "probe_after_idle_seconds": 300},
            "session": {"keepalive_dataset": None, "probe": None}}))
        DatasetSpec(**{**spec, "status": "enabled", "period": {**spec["period"], "start": DAY[2]}}).save(
            directory / "datasets" / "sections_whole_day.json")
        province = Province.load("anhui", tmp.path / "provinces")
        clock = Clock()
        answer = {"status": 0, "data": [{"sec_name": "甲线", "dataTime": f"{i:02d}:00"} for i in range(96)]}
        transport = FakeTransport(clock, default=Response(200, "application/json", json.dumps(answer).encode()))
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        transport.unready = {ENDPOINT}
        await engine.tick()
        self.assertEqual((transport.sent[0]["transport"], transport.sent[0]["action"]), ("ui", "query"))
        clock.advance(5)
        await engine.tick()
        self.assertEqual(len(transport.sent), 1, "primed once per 10 minutes, not in a loop")
        transport.unready = set()
        clock.advance(5)
        await engine.tick()
        self.assertEqual(transport.sent[-1]["template_body_set"], {"businessTime": DAY[2], "genName": ""})
        self.assertEqual(engine.ledger.db.execute("SELECT status FROM tasks WHERE period=?", (DAY[2],)).fetchone()[0], "saved")


class Discovery(unittest.IsolatedAsyncioTestCase):
    async def test_new_text_valued_options_join_the_report_and_get_the_days_task(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        root = add_sections(legacy_root(tmp.path))
        spec = multidim.build_specs(root, {"sections": section_ui()})["specs"]["sections"]
        self.assertEqual(spec["source"]["discover_options"], {"label": "断面名称"})
        directory = tmp.path / "provinces" / "anhui"
        (directory / "datasets").mkdir(parents=True)
        (directory / "province.json").write_text(json.dumps({
            "id": "anhui", "name": "安徽", "origin": "https://pmos.ah.sgcc.com.cn",
            "policy": {"response_interval_seconds": 1, "renew_after_idle_seconds": 300, "probe_after_idle_seconds": 300},
            "session": {"keepalive_dataset": None, "probe": None}}))
        DatasetSpec(**{**spec, "status": "enabled"}).save(directory / "datasets" / "sections.json")
        province = Province.load("anhui", tmp.path / "provinces")
        clock = Clock()
        listed = {"status": 0, "data": [{"label": "甲线", "value": "甲线", "value_type": "string"},
                                        {"label": "丙线", "value": "丙线", "value_type": "string"},
                                        {"label": "丁机", "value": "99", "value_type": "string"}]}
        transport = FakeTransport(clock, default=Response(200, "application/json", json.dumps(listed).encode()))
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        result = await engine._discover_options(engine.province.datasets()["sections"], DAY[1])
        self.assertEqual(result["new"], ["丙线"], "known 甲线 skipped; 丁机 is not sent by its text")
        sent = transport.sent[0]
        self.assertEqual((sent["action"], sent["select"]["label"], sent["fields"][0]["value"]), ("options", "断面名称", DAY[1]))
        scopes = engine.province.datasets()["sections"].scopes
        self.assertIn({"断面名称": "丙线"}, scopes)
        again = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(again.ledger.close)
        self.assertIn({"断面名称": "丙线"}, again.province.datasets()["sections"].scopes, "kept across restarts")
        task = engine.ledger.db.execute("SELECT period FROM tasks WHERE dataset='sections'").fetchall()
        self.assertEqual([tuple(t) for t in task], [(DAY[1],)])


class DiscoveryFailure(unittest.IsolatedAsyncioTestCase):
    async def test_a_dropdown_that_could_not_be_read_holds_only_that_reports_discovery(self):
        """Anhui 2026-11-01 11:31: select_value_changed_by_reading stopped the whole province."""
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        root = add_sections(legacy_root(tmp.path))
        spec = multidim.build_specs(root, {"sections": section_ui()})["specs"]["sections"]
        directory = tmp.path / "provinces" / "anhui"
        (directory / "datasets").mkdir(parents=True)
        (directory / "province.json").write_text(json.dumps({
            "id": "anhui", "name": "安徽", "origin": "https://pmos.ah.sgcc.com.cn",
            "policy": {"response_interval_seconds": 1, "renew_after_idle_seconds": 300, "probe_after_idle_seconds": 300},
            "session": {"keepalive_dataset": None, "probe": None}}))
        DatasetSpec(**{**spec, "status": "enabled"}).save(directory / "datasets" / "sections.json")
        clock = Clock()
        transport = FakeTransport(clock)
        transport.queue = [Fault("select_value_changed_by_reading", "local")]
        province = Province.load("anhui", tmp.path / "provinces")
        engine = Engine(province, transport, instance=province.instance(), clock=clock)
        self.addCleanup(engine.ledger.close)
        engine.enqueue({"action": "discover_options", "dataset": "sections", "period": DAY[1]})
        self.assertTrue(await engine.run_command())
        self.assertIsNone(engine.state.blocked(), "a page read sent nothing: the province goes on")
        self.assertEqual(engine.state.read("discovery_holds")["sections"]["reason"], "select_value_changed_by_reading")
        engine._roll_forward_options(set(), __import__("pmos.periods", fromlist=["today"]).today())
        self.assertFalse([c for c in engine.state.read("commands", []) or [] if c.get("action") == "discover_options"],
                         "no new discovery for the held report")
        from argparse import Namespace
        from pmos import cli
        with self.assertRaises(ValueError):
            cli.cmd_release_discovery(engine.province, Namespace(dataset="sections", approval=" "))
        cli.cmd_release_discovery(engine.province, Namespace(dataset="sections", approval="用户原话"))   # reviewed
        self.assertEqual(engine.state.read("discovery_holds"), {})
        engine.enqueue({"action": "discover_options", "dataset": "sections", "period": DAY[1]})
        clock.advance(5)
        self.assertTrue(await engine.run_command())
        self.assertIsNone(engine.state.blocked(), "the day's read was consumed: held again, never a province stop")
        self.assertEqual(engine.state.read("discovery_holds")["sections"]["reason"], "consumed_identity_requires_review")
        self.assertEqual(len(transport.sent), 1, "the consumed read is never sent again")


class ReleaseEarly(unittest.TestCase):
    def test_an_answer_saved_before_its_day_was_published_waits_again_only_with_approval(self):
        from pmos.contracts import TaskIdentity
        from pmos.ledger import Ledger
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(tmp.path / "ledger.sqlite3")
        self.addCleanup(ledger.close)
        ledger.seed("load", [DAY[0]], "")
        identity = TaskIdentity("anhui", "anhui-production", "load", {"kind": "day", "label": DAY[0]}, {"日期": DAY[0]},
                                operation="query")
        attempt = ledger.begin("collect", "load", DAY[0], "", identity=identity)
        ledger.finish(attempt, "success", http_status=200, business_status="0")
        ledger.settle_task("load", DAY[0], "saved", sha256="a" * 64)
        self.assertTrue(ledger.consumed_identity(identity.key))
        with self.assertRaises(ValueError):
            ledger.release_early(attempt, " ")
        self.assertEqual(ledger.release_early(attempt, "用户原话"), (attempt, "load", DAY[0], ""))
        task = ledger.task("load", DAY[0])
        self.assertEqual((task["status"], task["reason"]), ("pending", f"released_early:{attempt}"))
        self.assertFalse(ledger.consumed_identity(identity.key), "the next send of that day is allowed")
        with self.assertRaises(ValueError):
            ledger.release_early(attempt, "用户原话")
