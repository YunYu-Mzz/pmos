"""Anhui 6d80: options whose request value is an internal ID, read from the dropdown (r22)."""
import json
import os
import unittest
from unittest.mock import patch

from pmos.engine import Engine, internal_options
from pmos.faults import Fault
from pmos.province import Province
from pmos.spec import DatasetSpec
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir

LABEL, FIELD = "机组名称", "unitId"
DAY = "2026-10-29"


def option(name, value, kind="string"):
    return {"label": name, "value": value, "value_type": kind}


def scope(name, value):
    return {LABEL: name, FIELD: value}


class ReadingTheList(unittest.TestCase):
    def test_known_options_prove_the_new_ones(self):
        new, verified, doubtful = internal_options(
            [scope("甲机", "101"), scope("乙机", "102")],
            [option("甲机", "101"), option("丙机", "103"), option("丁机", "104")], LABEL, FIELD)
        self.assertCountEqual(new, [scope("丙机", "103"), scope("丁机", "104")])
        self.assertEqual((verified, doubtful), (1, 0))

    def test_a_known_option_with_another_id_refuses_the_whole_list(self):
        with self.assertRaises(Fault) as caught:
            internal_options([scope("甲机", "101")], [option("甲机", "999"), option("丙机", "103")], LABEL, FIELD)
        self.assertEqual(caught.exception.code, "option_value_unproven")

    def test_a_list_naming_no_known_option_adds_nothing(self):
        self.assertEqual(internal_options([scope("甲机", "101")], [option("丙机", "103")], LABEL, FIELD), ([], 0, 1))

    def test_doubtful_options_stay_out(self):
        new, verified, doubtful = internal_options(
            [scope("甲机", "101")],
            [option("甲机", "101"),
             option("两号", "201"), option("两号", "202"),      # one name, two IDs
             option("同号一", "301"), option("同号二", "301"),   # one ID, two names
             option("占用", "101"),                             # a known option's ID
             option("数字", 3378262674776262248, "number"),     # digits already lost
             option("丙机", "103")], LABEL, FIELD)
        self.assertEqual(new, [scope("丙机", "103")])
        self.assertEqual(verified, 1)
        self.assertEqual(doubtful, 6)


class Discovery(unittest.IsolatedAsyncioTestCase):
    def engine(self, listed):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = tmp.path / "provinces" / "anhui"
        (directory / "datasets").mkdir(parents=True)
        (directory / "province.json").write_text(json.dumps({
            "id": "anhui", "name": "安徽", "origin": "https://pmos.ah.sgcc.com.cn",
            "policy": {"response_interval_seconds": 1, "renew_after_idle_seconds": 300, "probe_after_idle_seconds": 300},
            "session": {"keepalive_dataset": None, "probe": None}}))
        page = {"transport": "ui", "frame": "f", "action": "export", "menu": ["甲", "乙"],
                "fields": [{"kind": "date", "placeholder": ["选择日期"], "value": "{date}"}]}
        path = "/px-contract-extranet-daypart/queryPlatform/execute"
        DatasetSpec(**{
            "id": "units", "name": "机组", "status": "enabled",
            "period": {"kind": "day", "start": "2026-10-01", "end": "today", "lag_days": 2},
            "request": {"transport": "fetch", "method": "POST", "path": path,
                        "template_body_set": {"businessTime": "{date}", FIELD: "{scope[unitId]}"},
                        "bootstrap": {"transport": "ui", "frame": "f", "menu": ["甲", "乙"], "action": "query", "path": path,
                                      "method": "POST",
                                      "fields": [{"kind": "date", "placeholder": ["选择日期"], "value": "{date}"},
                                                 {"kind": "select", "label": LABEL, "value": "{scope[机组名称]}"}]}},
            "response": {"kind": "json", "echo": {"unit_id": FIELD}},
            "scopes": [scope("甲机", "101")],
            "source": {"ui_request": page, "roll_forward_options": True, "partial_options": True,
                       "discover_options": {"label": LABEL, "value_field": FIELD},
                       "identity_contract": {"request_data_path": ["template_body_set"], "date_fields": ["businessTime"],
                                             "dimension_fields": {"businessTime": "日期"}, "scope_dimensions": [LABEL]}},
        }).save(directory / "datasets" / "units.json")
        clock = Clock()
        body = json.dumps({"status": 0, "data": listed}).encode()
        transport = FakeTransport(clock, default=Response(200, "application/json", body))
        province = Province.load("anhui", tmp.path / "provinces")
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        self.addCleanup(engine.ledger.close)
        return engine, transport, province, tmp

    async def test_new_units_join_with_their_id_and_survive_a_restart(self):
        engine, transport, province, tmp = self.engine([option("甲机", "101"), option("丙机", "103")])
        engine._seeded_on = "already"
        result = await engine._discover_options(engine.province.datasets()["units"], DAY)
        self.assertEqual(result["new"], ["丙机"])
        self.assertEqual((transport.sent[0]["action"], transport.sent[0]["select"]["label"]), ("options", LABEL))
        self.assertIn(scope("丙机", "103"), engine.province.datasets()["units"].scopes)
        self.assertIsNone(engine._seeded_on, "the new unit gets every due day on the next tick")
        again = Engine(province, transport, instance=tmp.path / "var", clock=engine.clock)
        self.addCleanup(again.ledger.close)
        self.assertIn(scope("丙机", "103"), again.province.datasets()["units"].scopes)
        built = again.province.datasets()["units"].build(DAY, scope("丙机", "103"))
        self.assertEqual(built["template_body_set"], {"businessTime": DAY, FIELD: "103"})

    async def test_a_mismatch_holds_only_this_reports_discovery(self):
        engine, _, _, _ = self.engine([option("甲机", "999"), option("丙机", "103")])
        engine.enqueue({"action": "discover_options", "dataset": "units", "period": DAY})
        self.assertTrue(await engine.run_command())
        self.assertIsNone(engine.state.blocked())
        self.assertEqual(engine.state.read("discovery_holds")["units"]["reason"], "option_value_unproven")
        self.assertEqual(engine.province.datasets()["units"].scopes, [scope("甲机", "101")])


if __name__ == "__main__":
    unittest.main()
