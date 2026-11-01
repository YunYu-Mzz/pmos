"""The 18 daily Liaoning reports join the handoff: their originals are the tombstones, their new days go first."""
import json
import os
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from pmos.engine import Engine
from pmos.ledger import Ledger
from pmos.migration import liaoning as migration
from pmos.periods import today
from pmos.province import Province
from pmos.queue_compat import QueueContext, import_shadow
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir

REPO = Path(__file__).resolve().parents[1]
REAL = Province.load("liaoning", REPO / "provinces")


def day(n):
    return (today() - timedelta(days=n)).isoformat()


def answer(numbers=2):
    return {"status": 0, "data": {"handleList": [{"time": "00:15", "v": 1.5}] * numbers}}


class Legacy:
    """A frozen legacy tree: Crawlee queue entries, originals, engine originals, raw load."""

    def __init__(self, base: Path):
        self.root = base / "legacy"

    def queue(self, name, when, handled=True):
        directory = self.root / migration.DAILY_QUEUES / ("liaoning-2026-daily-verified-20261014-" + name.replace("_", "-"))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}{when}.json").write_text(json.dumps(
            {"unique_key": name + when, "no_retry": True, "handled_at": "2026-10-01T00:00:00" if handled else None,
             "user_data": {"dataset": name, "date": when}}))
        (directory / "__metadata__.json").write_text("{}")

    def original(self, name, when, body, captured=None):
        directory = self.root / migration.DAILY_ORIGINALS / name / when / "abc"
        directory.mkdir(parents=True)
        (directory / "response.json").write_text(json.dumps(body))
        (directory / "metadata.json").write_text(json.dumps({"captured_at": (captured or day(0)) + "T00:02:00+08:00"}))

    def engine(self, name, when, points):
        directory = self.root / migration.DAILY_ENGINE / name / when / "abc"
        directory.mkdir(parents=True)
        (directory / "metadata.json").write_text(json.dumps({"date": when, "nonnull_points": points}))

    def load(self, when, values):
        directory = self.root / migration.DAILY_LOAD
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{when}.json").write_text(json.dumps({"record": {"date": when, "columns": [{"values": values}]}}))


class RealSpecs(unittest.TestCase):
    def test_all_eighteen_can_be_collected_with_the_lag_their_titles_call_for(self):
        specs = REAL.datasets()
        self.assertEqual(len(migration.DAILY_LANE), 18)
        for name in migration.DAILY_LANE:
            with self.subTest(name=name):
                self.assertEqual(specs[name].status, "verified")
                self.assertEqual(specs[name].period["lag_days"], migration.publish_lag(specs[name].name))

    def test_the_keepalive_has_a_day_before_any_pmos_original_exists(self):
        self.assertEqual(REAL.session["keepalive_period"], "2026-10-31")
        self.assertEqual(migration.DAILY_LANE[0], "system_load")


class Classification(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.legacy = Legacy(self.tmp.path)

    def lane(self, *names):
        return migration.daily_lane(self.legacy.root, REAL.datasets(), names=names)

    def test_only_an_answer_with_data_or_a_late_empty_one_completes_a_day(self):
        name = "hydro_daily"
        for n in range(1, 7):
            self.legacy.queue(name, day(n), handled=n != 5)
        self.legacy.original(name, day(1), answer(), captured=day(0))
        self.legacy.original(name, day(2), {"status": 0, "data": []}, captured=day(2))              # asked the same day
        self.legacy.original(name, day(3), {"status": 0, "data": {"handleList": []}}, captured=day(2))  # the next day
        self.legacy.original(name, day(4), {"status": 0, "data": []}, captured=day(0))              # 4 days later: truly empty
        result = self.lane(name)["per_dataset"][name]
        self.assertEqual(result["early_empty"], [day(3), day(2)], "user: 当天或第二天采到的空结果，当作未采处理")
        self.assertEqual(result["open"], [day(5)], "the queue never handled it, no original")
        self.assertEqual(result["handled_without_original"], [day(6)], "a legacy failure: not re-sent, listed")
        self.assertEqual(result["consumed"], 3, "data, a late empty answer, the legacy failure")

    def test_a_day_either_line_of_033_completed_is_done_and_an_interrupted_one_is_not(self):
        name = "nonmarket_generation_forecast"
        self.legacy.original(name, day(3), answer(), captured=day(0))
        self.legacy.engine(name, day(2), 96)
        self.legacy.engine(name, day(4), 0)
        self.legacy.queue(name, day(4), handled=False)
        result = self.lane(name)["per_dataset"][name]
        self.assertEqual((result["consumed"], result["open"]), (2, [day(4)]))

    def test_the_load_raw_records_are_its_originals(self):
        self.legacy.load(day(2), [1.0, 2.0])
        self.legacy.load(day(3), [])
        result = self.lane("system_load")["per_dataset"]["system_load"]
        self.assertEqual((result["consumed"], result["early_empty"]), (1, [day(3)]))

    def test_tombstones_carry_the_engines_own_identity_and_the_digest_follows_them(self):
        name = "centralized_solar_daily"
        self.legacy.original(name, day(2), answer(), captured=day(0))
        lane = self.lane(name)
        ((key, identity),) = lane["rows"]
        self.assertEqual(key, f"daily:{name}:{day(2)}")
        spec = REAL.datasets()[name]
        from pmos.engine import task_identity
        self.assertEqual(identity.key, task_identity("liaoning", "liaoning-production", spec, day(2), "").key)
        self.legacy.original(name, day(3), answer(), captured=day(0))
        self.assertNotEqual(self.lane(name)["snapshot_sha256"], lane["snapshot_sha256"])


class Handoff(unittest.IsolatedAsyncioTestCase):
    NAMES = ("system_load", "hydro_daily", "distributed_solar_actual")

    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(self.tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = self.tmp.path / "provinces" / "liaoning"
        (directory / "datasets").mkdir(parents=True)
        (directory / "province.json").write_text(json.dumps({
            "id": "liaoning", "name": "辽宁", "origin": "https://pmos.ln.sgcc.com.cn",
            "policy": {"response_interval_seconds": 1, "renew_after_idle_seconds": 300, "probe_after_idle_seconds": 300},
            "session": {"keepalive_dataset": None, "probe": None, "keepalive_period": day(5)}}))
        for name in ("actual", *self.NAMES):
            source = "tie_line_dayahead" if name == "actual" else name
            value = json.loads((REPO / "provinces/liaoning/datasets" / f"{source}.json").read_text(encoding="utf-8"))
            value["id"], value["period"] = name, {**value["period"], "start": day(5)}
            (directory / "datasets" / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False))
        self.province = Province.load("liaoning", self.tmp.path / "provinces")
        instance = self.province.instance()
        instance.mkdir(parents=True)
        ledger = Ledger(instance / "ledger.sqlite3")
        import_shadow(ledger, [], QueueContext("liaoning", "liaoning-production"), source_id="queue", snapshot_sha256="a" * 64)
        ledger.grant_handoff({"version": 1, "province": "liaoning", "account_alias": "liaoning-production",
                              "legacy_owner": {"stopped_evidence": "fixture"},
                              "snapshots": [{"source_id": "queue", "snapshot_sha256": "a" * 64}], "scope": ["actual"],
                              "request_budget": 100, "code_sha": "c", "approval": "fixture",
                              "session": {"keepalive_dataset": "system_load"}, "seeding": "spec_periods"})
        legacy = Legacy(self.tmp.path)
        for n in (2, 3, 4, 5):
            legacy.load(day(n), [1.0])
        for n in (3, 4, 5):
            legacy.original("hydro_daily", day(n), answer(), captured=day(0))
        for n in (3, 4, 5):
            legacy.original("distributed_solar_actual", day(n), answer(), captured=day(0))
        legacy.original("distributed_solar_actual", day(2), {"status": 0, "data": []}, captured=day(2))
        self.lane = migration.daily_lane(legacy.root, self.province.datasets(), names=self.NAMES)
        self.result = migration.apply_daily_lane(ledger, self.lane, "同意")
        self.again = migration.apply_daily_lane(ledger, self.lane, "同意")
        ledger.close()

    def engine(self):
        self.transport = FakeTransport(self.clock, duration=1, default=Response(
            200, "application/json", json.dumps(answer()).encode()))
        engine = Engine(self.province, self.transport, instance=self.province.instance(), clock=self.clock,
                        account_alias="liaoning-production")
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        return engine

    def test_the_order_puts_them_first_and_applying_twice_changes_nothing(self):
        handoff = self.engine().ledger.handoff()
        self.assertEqual(handoff["scope"], [*self.NAMES, "actual"])
        self.assertEqual(handoff["seeded"], ["actual", *self.NAMES])
        self.assertEqual(self.again["imported"]["added"], 0)

    async def test_what_legacy_completed_is_never_sent_and_the_rest_is_asked_once(self):
        engine = self.engine()
        for _ in range(14):
            await engine.tick()
            self.clock.advance(3)
        dates = {}
        for request in self.transport.sent:
            when = next((v for k, v in request.get("set", {}).items() if k in ("queryParam.mdate", "queryParam.date")), None)
            dates.setdefault(request["component"], []).append(when)
        hydro = engine.province.datasets()["hydro_daily"].request["component"]
        self.assertEqual(sorted(dates[hydro]), sorted([day(1), day(2)]), "legacy has 3, 4 and 5; lag 1 day; each once")
        load = engine.province.datasets()["system_load"].request["component"]
        self.assertEqual(dates[load], [day(1)], "the raw records count")
        solar = engine.province.datasets()["distributed_solar_actual"].request["component"]
        self.assertEqual(dates[solar], [day(2)], "its same-day empty answer is asked again, once published (lag 2)")
        self.assertIsNone(engine.state.blocked())

    async def test_an_idle_session_is_renewed_with_the_fixed_day_though_nothing_is_archived(self):
        """Liaoning 2026-11-01: outside the scope and without an original, keep_session could send nothing."""
        engine = self.engine()
        self.clock.advance(1000)
        self.assertEqual(await engine.keep_session(), "renewal")
        request = self.transport.sent[-1]
        self.assertEqual(request["component"], engine.province.datasets()["system_load"].request["component"])
        self.assertEqual(request["set"].get("queryParam.date"), day(5))
        self.assertFalse(engine.ledger.db.execute("SELECT 1 FROM tasks WHERE status='saved'").fetchone(), "no task's result")


if __name__ == "__main__":
    unittest.main()
