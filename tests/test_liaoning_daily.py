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
    def test_the_engine_lane_counts_days_without_any_original(self):
        """033's originals: power_reports and report_engine together; what neither has is asked too."""
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            legacy = Legacy(Path(directory))
            name = "nonmarket_generation_forecast"
            legacy.engine(name, day(3), 96)
            result = migration.daily_lane(legacy.root, REAL.datasets(), names=(name,))
        self.assertEqual(result["per_dataset"][name]["consumed"], 1)
        self.assertIn(day(4), result["per_dataset"][name]["no_original"])
        self.assertNotIn(day(3), result["per_dataset"][name]["no_original"])
        self.assertEqual(result["will_be_asked"], len(result["per_dataset"][name]["no_original"]))

    def test_033_is_not_in_this_lane_since_the_site_moved_it_to_finereport(self):
        """2026-10-23, route spotgoodsSum: the legacy worker excluded it here; its originals end on 10-16."""
        self.assertNotIn("nonmarket_generation_forecast", migration.DAILY_LANE)

    def test_all_seventeen_can_be_collected_with_the_lag_their_titles_call_for(self):
        specs = REAL.datasets()
        self.assertEqual(len(migration.DAILY_LANE), 17)
        for name in migration.DAILY_LANE:
            with self.subTest(name=name):
                self.assertEqual(specs[name].status, "verified")
                self.assertEqual(specs[name].period["lag_days"], migration.publish_lag(specs[name].name))

    def test_the_keepalive_has_a_day_before_any_pmos_original_exists(self):
        self.assertEqual(REAL.session["keepalive_period"], "2026-10-31")
        self.assertEqual(migration.DAILY_LANE[0], "system_load")


class PriorOriginal(unittest.TestCase):
    def legacy(self, directory, body=b'{"status":0,"data":[{"v":1}]}', sha=None, outcome="saved"):
        import hashlib
        name = "sitewide_f5d806b38bb8a248"
        root = Path(directory) / "legacy"
        response = root / "state/local_adaptation_20261020/responses/refresh/response.json"
        response.parent.mkdir(parents=True)
        response.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        manifest = root / migration.MANIFEST
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"sources": {name: {
            "prior_outcomes": {"k": {"outcome": outcome, "raw_sha256": digest}},
            "proofs": [{"sha256": sha or digest, "rows": 1, "params": {"date": day(4)},
                        "response_file": str(response.relative_to(root))}]}}}))
        return root, name

    def test_a_day_only_the_legacy_adaptation_saved_is_not_asked_again(self):
        """sitewide_f5d8's 2026-10-20: asked on 10-21, 200 and rows, its file outside the originals tree."""
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root, name = self.legacy(directory)
            lane = migration.daily_lane(root, REAL.datasets(), names=(name,))
        self.assertEqual(lane["per_dataset"][name]["original_in_adaptation"], [day(4)])
        self.assertNotIn(day(4), lane["per_dataset"][name]["no_original"])
        self.assertEqual(lane["per_dataset"][name]["consumed"], 1)
        self.assertEqual(lane["rows"][0][0], f"daily:{name}:{day(4)}")

    def test_a_prior_outcome_whose_file_does_not_prove_it_stays_open(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root, name = self.legacy(directory, sha="0" * 64)
            self.assertEqual(migration.prior_originals(root, name), [])
        with tempfile.TemporaryDirectory() as directory:
            root, name = self.legacy(directory, outcome="empty")
            self.assertEqual(migration.prior_originals(root, name), [])


class Weekly(unittest.TestCase):
    def test_a_monday_is_the_only_period_of_a_weekly_report(self):
        from datetime import date
        from pmos import periods
        kind = "week_start_monday"
        self.assertEqual(periods.parse(kind, "2026-10-26"), date(2026, 10, 26))
        with self.assertRaises(periods.PeriodError):
            periods.parse(kind, "2026-10-27")
        self.assertEqual(periods.value_of(kind, date(2026, 11, 1)), "2026-10-26", "a Sunday belongs to its week")
        self.assertEqual(periods.previous(kind, "2026-10-26"), "2026-10-19")
        self.assertEqual(periods.last_day(kind, "2026-10-26"), date(2026, 11, 1))
        self.assertEqual(periods.expand(kind, "2026-10-12", "today", on=date(2026, 11, 3), lag_days=2),
                         ["2026-10-26", "2026-10-19", "2026-10-12"], "11-02 is not due until 11-04")
        self.assertEqual(periods.expand(kind, "2026-10-12", "today", on=date(2026, 11, 4), lag_days=2)[0], "2026-11-02")
        self.assertEqual(periods.variables(kind, "2026-10-26")["date"], "2026-10-26")

    def test_both_specs_can_be_collected_and_keep_their_identity_bound_to_the_request(self):
        from datetime import date
        from pmos.engine import task_identity
        specs = REAL.datasets()
        self.assertEqual(len(migration.WEEKLY_LANE), 2)
        monday = specs["sitewide_d86eb498e4cf0eca"]
        self.assertEqual(monday.status, "verified")
        self.assertEqual(monday.build("2026-10-26")["set"]["queryParam.mdate"], "2026-10-26")
        self.assertEqual(monday.newest_due(on=date(2026, 11, 3)), "2026-10-26")
        self.assertEqual(task_identity("liaoning", "a", monday, "2026-10-26", "").document()["period"],
                         {"kind": "week_start_monday", "anchor": "2026-10-26"})
        daily = specs["sitewide_f5d806b38bb8a248"]
        self.assertEqual((daily.status, daily.period["lag_days"]), ("verified", migration.publish_lag(daily.name)))
        self.assertEqual(task_identity("liaoning", "a", daily, "2026-10-26", "").period["kind"], "day")

    def test_their_legacy_originals_become_tombstones_with_the_engines_identity(self):
        import tempfile
        from pmos.engine import task_identity
        name = "sitewide_d86eb498e4cf0eca"
        spec = REAL.datasets()[name]
        newest = spec.periods_to_collect()[0]
        with tempfile.TemporaryDirectory() as directory:
            legacy = Legacy(Path(directory))
            found = legacy.root / migration.DAILY_ADVANCED / name / newest / "m" / "s"
            found.mkdir(parents=True)
            (found / "metadata.json").write_text(json.dumps({"row_count": 59}))
            lane = migration.daily_lane(legacy.root, REAL.datasets(), names=(name,))
        ((key, identity),) = lane["rows"]
        self.assertEqual(identity.key, task_identity("liaoning", "liaoning-production", spec, newest, "").key)
        self.assertNotIn(newest, lane["per_dataset"][name]["no_original"])


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

    def test_the_users_choice_makes_early_empty_answers_tombstones_recorded_as_empty(self):
        """User 2026-11-01: "空结果不重采"."""
        name = "hydro_daily"
        self.legacy.original(name, day(1), answer(), captured=day(0))
        self.legacy.original(name, day(2), {"status": 0, "data": []}, captured=day(2))
        self.legacy.queue(name, day(3), handled=False)
        default = migration.daily_lane(self.legacy.root, REAL.datasets(), names=(name,))
        chosen = migration.daily_lane(self.legacy.root, REAL.datasets(), names=(name,), early_empty_consumed=True)
        never = len(default["per_dataset"][name]["no_original"])
        self.assertEqual((len(default["rows"]), default["will_be_asked"] - never, default["states"]), (1, 2, {}))
        self.assertEqual((len(chosen["rows"]), chosen["will_be_asked"] - never), (2, 1), "the day never answered is still asked")
        self.assertEqual(chosen["states"], {f"daily:{name}:{day(2)}": "empty"})
        self.assertNotEqual(default["snapshot_sha256"], chosen["snapshot_sha256"])

    def test_a_day_either_line_of_033_completed_is_done_and_an_interrupted_one_is_not(self):
        name = "nonmarket_generation_forecast"
        self.legacy.original(name, day(3), answer(), captured=day(0))
        self.legacy.engine(name, day(2), 96)
        self.legacy.engine(name, day(4), 0)
        self.legacy.queue(name, day(4), handled=False)
        result = self.lane(name)["per_dataset"][name]
        self.assertEqual((result["consumed"], result["open"]), (2, [day(4)]))

    def test_an_engine_report_counts_its_originals_from_before_the_route_moved(self):
        name = "sitewide_e38351fe661a034a"
        directory = self.legacy.root / migration.DAILY_ADVANCED / name / day(5) / "m" / "s"
        directory.mkdir(parents=True)
        (directory / "metadata.json").write_text(json.dumps({"row_count": 1}))
        empty = self.legacy.root / migration.DAILY_ADVANCED / name / day(6) / "m" / "s"
        empty.mkdir(parents=True)
        (empty / "metadata.json").write_text(json.dumps({"row_count": 0}))
        result = self.lane(name)["per_dataset"][name]
        self.assertEqual((result["consumed"], result["open"]), (1, [day(6)]))

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

    async def test_an_early_empty_day_the_user_chose_to_keep_is_no_original_and_says_it_was_empty(self):
        ledger = Ledger(self.province.instance() / "ledger.sqlite3")
        spec = self.province.datasets()["hydro_daily"]
        from pmos.engine import task_identity
        identity = task_identity("liaoning", "liaoning-production", spec, day(1), "")
        ledger.import_late_legacy("empties", "b" * 64, [("k", identity)], "同意", states={"k": "empty"})
        ledger.close()
        engine = self.engine()
        for _ in range(14):
            await engine.tick()
            self.clock.advance(3)
        hydro = spec.request["component"]
        self.assertEqual([r["set"].get("queryParam.mdate") for r in self.transport.sent if r["component"] == hydro], [day(2)])
        self.assertEqual(engine.ledger.task("hydro_daily", day(1))["reason"], "legacy_consumed:empty")

    async def test_an_idle_session_is_renewed_with_the_fixed_day_though_nothing_is_archived(self):
        """Liaoning 2026-11-01: outside the scope and without an original, keep_session could send nothing."""
        engine = self.engine()
        self.clock.advance(1000)
        self.assertEqual(await engine.keep_session(), "renewal")
        request = self.transport.sent[-1]
        self.assertEqual(request["component"], engine.province.datasets()["system_load"].request["component"])
        self.assertEqual(request["set"].get("queryParam.date"), day(5))
        self.assertFalse(engine.ledger.db.execute("SELECT 1 FROM tasks WHERE status='saved'").fetchone(), "no task's result")


PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def row(guid, title, *files, **fields):
    return {"guid": guid, "title": title, "operateDate": 1_790_000_000_000, "marketId": "PDBLN",
            "attachment": json.dumps([{"id": f, "name": f + ".pdf"} for f in files]), **fields}


def page(rows, total=None):
    body = {"status": 0, "message": "Success", "data": {"list": rows, "total": len(rows) if total is None else total}}
    return Response(200, "application/json", json.dumps(body, ensure_ascii=False).encode())


class RealColumns(unittest.TestCase):
    def test_the_63_columns_can_join(self):
        specs = {name: spec for name, spec in REAL.datasets().items() if name.startswith("pub_")}
        self.assertEqual(len(specs), 63)
        self.assertEqual(len({spec.source["legacy_dataset"] for spec in specs.values()}), 63)
        for name, spec in specs.items():
            with self.subTest(name=name):
                self.assertIn(spec.status, ("sampled", "verified"))
                self.assertIs(spec.period["daily"], True)
                self.assertEqual(spec.documents["business_year"], 2026)
                self.assertIs(spec.documents["undated_by_published"], True)
                self.assertIs(spec.documents["empty_file_settles"], True)
                self.assertIs(spec.documents["stop_at_known"], True)

    def test_the_three_member_lists_are_listed_every_day_with_their_member_type(self):
        specs = REAL.datasets()
        self.assertEqual({name: specs[name].request["set"]["queryParam.data.memberType"] for name in migration.MEMBER_LANE},
                         {"sitewide_8090c94f9cf7b256": "20", "sitewide_b03896324973acd7": "30", "sitewide_f1051ebc83754fde": "10"})
        for name in migration.MEMBER_LANE:
            self.assertIs(specs[name].period["daily"], True)
            self.assertEqual(specs[name].source["legacy_dataset"], name)

    def test_the_two_columns_the_legacy_manifest_corrected(self):
        specs = REAL.datasets()
        jy = specs["pub_jyjgJbxx"].request
        self.assertEqual((jy["path"].rpartition("/")[2], jy["set"]["tabFlag"]), ("getDataJY", True),
                         "the view's getIpc turns tabFlag on for jyjgJbxx")
        moved = json.dumps(specs["pub_dwqyqyjbxx"].request, ensure_ascii=False)
        self.assertNotIn("operationOrganizationQueryDwqy", moved, "the site moved this menu on 2026-10-23")
        self.assertEqual(specs["pub_dwqyqyjbxx"].request["component"], "operationOrganizationQuery")


class Columns(unittest.IsolatedAsyncioTestCase):
    """One real column spec on a handoff: its saved file is not asked, its queued one is, it is listed every day."""
    NAME = "pub_LSTCFDJGCS"
    LEGACY = "sitewide_ede1cd2300c54661"

    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(self.tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = self.tmp.path / "provinces" / "liaoning"
        (directory / "datasets").mkdir(parents=True)
        (directory / "scripts").mkdir()
        (directory / "scripts" / "open_column_menu.js").write_text("async () => true")
        (directory / "province.json").write_text(json.dumps({
            "id": "liaoning", "name": "辽宁", "origin": "https://pmos.ln.sgcc.com.cn",
            "policy": {"response_interval_seconds": 1, "renew_after_idle_seconds": 1500, "probe_after_idle_seconds": 1500},
            "session": {"keepalive_dataset": None, "probe": None}}))
        for name, source in (("actual", "tie_line_dayahead"), (self.NAME, self.NAME), ("pub_jyjgJbxx", "pub_jyjgJbxx")):
            value = json.loads((REPO / "provinces/liaoning/datasets" / f"{source}.json").read_text(encoding="utf-8"))
            value["id"] = name
            if name == "actual":
                value["period"] = {**value["period"], "start": day(1)}
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
                              "session": {}, "seeding": "spec_periods"})
        ledger.close()
        self.root = self.tmp.path / "legacy"

    def queue(self, outcomes=(), queued=(), asked=(), empty=(), withheld=()):
        def job(file_id, **more):
            return {"dataset": self.LEGACY, "attachment_id": file_id, "filename": file_id + ".pdf", "record_id": "g", **more}
        asked = (*asked, *empty)
        value = {"active": None, "global_hold": None, "source_holds": {},
                 "outcomes": {"k" + f: job(f, outcome="saved") for f in outcomes},
                 "pending": {"k" + f: job(f) for f in (*queued, *asked, *withheld)},
                 "isolated_job_reviews": {"k" + f: {"reason": "settled_empty_http200_attachment_preserved_no_retry"} for f in empty},
                 "scope_reviews": {"k" + f: {"reason": "native_title_year_conflicts_with_metadata", "title_years": ["2025"]}
                                   for f in withheld}}
        path = self.root / migration.ATTACHMENT_QUEUE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        for file_id in asked:
            operation = self.root / migration.ATTACHMENT_OPERATIONS / ("k" + file_id)
            operation.mkdir(parents=True)
            (operation / "issued.json").write_text("{}")
        listed = self.root / migration.NATIVE_LISTS / self.LEGACY / "2026-11-01" / "1" / "abc"
        listed.mkdir(parents=True, exist_ok=True)
        (listed / "metadata.json").write_text(json.dumps({"total": 41}))
        return self.root

    def engine(self):
        self.transport = FakeTransport(self.clock, duration=1, default=Response(
            200, "application/json", json.dumps(answer()).encode()))
        engine = Engine(self.province, self.transport, instance=self.province.instance(), clock=self.clock,
                        account_alias="liaoning-production")
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        return engine

    def test_what_the_legacy_queue_left_is_classified_and_a_0_byte_answer_is_resent_only_if_the_user_says(self):
        root = self.queue(outcomes=["f1"], queued=["f2"], asked=["f3"], empty=["f5"], withheld=["f6"])
        specs = self.province.datasets()
        lane = migration.list_lane(root, specs)
        self.assertEqual(sorted(key for key, _ in lane["rows"]), ["kf1", "kf3", "kf5"])
        self.assertEqual(lane["states"], {"kf3": "unknown", "kf5": "empty"})
        self.assertEqual([(i["file_id"], i["state"]) for i in lane["answered_without_a_file"]], [("f3", "unknown"), ("f5", "empty")])
        self.assertEqual([i["file_id"] for i in lane["withheld_year_conflict"]], ["f6"], "never asked, and never a tombstone")
        self.assertEqual(lane["files_left_to_download"], 1)
        self.assertEqual(lane["first_listing_pages"], 3 + 1, "41 records at 20 a page, and a column the legacy tree has no page of")
        self.assertEqual(lane["will_be_asked"], 4 + 1)
        self.assertEqual(migration.list_lane(root, specs, only=["pub_jyjgJbxx"])["will_be_asked"], 1, "this apply's numbers")
        self.assertEqual(migration.list_lane(root, specs, only=["pub_jyjgJbxx"])["first_listing_pages_all_columns"], 4)
        resend = migration.list_lane(root, specs, resend_empty=True)
        self.assertEqual(sorted(key for key, _ in resend["rows"]), ["kf1", "kf3"], "an unknown answer is not resent by this")
        self.assertEqual(resend["files_left_to_download"], 2)
        self.assertNotEqual(resend["snapshot_sha256"], lane["snapshot_sha256"])

    def test_a_pilot_joins_only_its_columns_but_every_tombstone(self):
        root = self.queue(outcomes=["f1"])
        lane = migration.list_lane(root, self.province.datasets(), only=["pub_jyjgJbxx"])
        self.assertEqual(lane["datasets"], ["pub_jyjgJbxx"])
        self.assertEqual(len(lane["rows"]), 1)
        with self.assertRaisesRegex(ValueError, "pub_nothing"):
            migration.list_lane(root, self.province.datasets(), only=["pub_nothing"])

    def test_a_pilot_scopes_only_its_columns_and_the_full_lane_imports_the_same_snapshot_once(self):
        root = self.queue(outcomes=["f1"], empty=["f3"])
        specs = self.province.datasets()
        ledger = Ledger(self.province.instance() / "ledger.sqlite3")
        pilot = migration.apply_daily_lane(ledger, migration.list_lane(root, specs, only=["pub_jyjgJbxx"]), "试点",
                                           source_id="liaoning-lists-lane")
        self.assertEqual((pilot["imported"]["added"], pilot["added"]), (2, ["pub_jyjgJbxx"]), "all tombstones, one column")
        self.assertEqual(ledger.handoff()["scope"], ["pub_jyjgJbxx", "actual"])
        self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM tasks WHERE dataset LIKE 'pub_%'").fetchone()[0], 0)
        full = migration.apply_daily_lane(ledger, migration.list_lane(root, specs), "全量", source_id="liaoning-lists-lane")
        self.assertTrue(full["imported"]["already_done"], "the same snapshot is no second import")
        self.assertEqual(full["added"], [self.NAME])
        with self.assertRaisesRegex(ValueError, "another_snapshot"):
            migration.apply_daily_lane(ledger, migration.list_lane(root, specs, resend_empty=True), "晚了", source_id="liaoning-lists-lane")
        self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM legacy_records WHERE source_id='liaoning-lists-lane'").fetchone()[0], 2)
        ledger.close()

    def test_an_unsettled_legacy_queue_is_refused(self):
        root = self.queue()
        path = root / migration.ATTACHMENT_QUEUE
        path.write_text(json.dumps({**json.loads(path.read_text()), "active": {"key": "x"}}))
        with self.assertRaisesRegex(ValueError, "not_settled"):
            migration.list_lane(root, self.province.datasets())

    def test_the_plan_counts_files_the_rule_takes_beyond_the_legacy_queue(self):
        root = self.queue(outcomes=["f1"])
        listed = root / migration.NATIVE_LISTS / self.LEGACY / "2026-11-01" / "1" / "abc" / "response.json"
        listed.write_text(json.dumps({"status": 0, "data": {"list": [row("g1", "2026年公告", "f1"), row("g2", "市场日志", "f9"),
                                                                       row("g3", "2025年公告", "f8")]}}))
        lane = migration.list_lane(root, self.province.datasets())
        self.assertEqual(lane["files_beyond_the_legacy_queue"], {self.NAME: 1}, "f9 only: f1 is saved, f8 is another year")
        self.assertEqual(lane["will_be_asked"], lane["first_listing_pages"] + 1)
        self.assertEqual(migration.list_lane(root, self.province.datasets(), only=["pub_jyjgJbxx"])["files_beyond_the_legacy_queue"], {})

    async def test_a_zero_byte_file_settles_alone_and_the_column_goes_on(self):
        root = self.queue(queued=["f1", "f2"])
        ledger = Ledger(self.province.instance() / "ledger.sqlite3")
        migration.apply_daily_lane(ledger, migration.list_lane(root, self.province.datasets(), only=[self.NAME]), "同意",
                                   source_id="liaoning-lists-lane")
        ledger.close()
        engine = self.engine()
        self.transport.queue += [page([row("g2", "2026年9月公告", "f1", "f2")]), Response(200, "application/pdf", b""),
                                 Response(200, "application/pdf", PDF)]
        for _ in range(8):
            await engine.tick()
            self.clock.advance(3)
        from pmos.documents import item_scope
        statuses = sorted((row_["status"], row_["reason"]) for row_ in engine.ledger.db.execute(
            "SELECT status, reason FROM tasks WHERE dataset=? AND scope IN (?, ?)", (self.NAME, item_scope("f1"), item_scope("f2"))))
        self.assertEqual(statuses, [("empty", "empty_file"), ("saved", None)])
        self.assertEqual(engine.dataset_holds(), {})
        self.assertIsNone(engine.state.blocked())
        self.assertEqual([item["reason"] for item in engine.state.read("isolated_items")], ["empty_file"], "listed for review")

    async def test_a_file_with_the_wrong_bytes_still_holds_the_column(self):
        root = self.queue(queued=["f1", "f2"])
        ledger = Ledger(self.province.instance() / "ledger.sqlite3")
        migration.apply_daily_lane(ledger, migration.list_lane(root, self.province.datasets(), only=[self.NAME]), "同意",
                                   source_id="liaoning-lists-lane")
        ledger.close()
        engine = self.engine()
        self.transport.queue += [page([row("g2", "2026年9月公告", "f1", "f2")]), Response(200, "text/html", b"<html>")]
        for _ in range(4):
            try:
                await engine.tick()
            except Exception:
                pass
            self.clock.advance(3)
        self.assertIn(self.NAME, {**engine.dataset_holds(), **({engine.state.blocked()["dataset"]: 1} if engine.state.blocked() else {})})

    async def test_only_the_files_left_are_asked_and_the_column_is_listed_again_tomorrow(self):
        root = self.queue(outcomes=["f1"], queued=["f2"], empty=["f3"])
        lane = migration.list_lane(root, self.province.datasets(), only=[self.NAME])
        ledger = Ledger(self.province.instance() / "ledger.sqlite3")
        applied = migration.apply_daily_lane(ledger, lane, "同意", source_id="liaoning-lists-lane")
        ledger.close()
        self.assertEqual((applied["imported"]["added"], applied["added"]), (2, [self.NAME]))
        engine = self.engine()
        self.transport.queue += [
            page([row("g2", "2026年9月公告", "f2", "f1", "f3"), row("g3", "2025年12月公告", "f6", fyear="2026"),
                  row("g1", "2025年公告", "f4", fyear="2025")]),
            Response(200, "application/pdf", PDF)]
        for _ in range(8):
            await engine.tick()
            self.clock.advance(3)
        tasks = {row_["scope"]: dict(row_) for row_ in engine.ledger.db.execute(
            "SELECT scope, status, reason FROM tasks WHERE dataset=? AND scope != ''", (self.NAME,))}
        from pmos.documents import item_scope
        self.assertEqual(tasks[item_scope("f1")]["reason"], "legacy_consumed:saved")
        self.assertEqual(tasks[item_scope("f3")]["reason"], "legacy_consumed:empty")
        self.assertEqual(tasks[item_scope("f2")]["status"], "saved")
        self.assertNotIn(item_scope("f4"), tasks, "a 2025 file is not this collection's")
        self.assertNotIn(item_scope("f6"), tasks, "the fields say 2026, the title 2025: the legacy queue held it, so does this")
        calls = [r for r in self.transport.sent if r.get("component") == "attchViewQueryJY"]
        self.assertEqual([r["call"] for r in calls], ["queryData", "fileDownload"])
        self.assertEqual([item["id"] for item in calls[1]["args"][0]["attachment"]], ["f2"], "the row narrowed to this file")
        tomorrow = today() + timedelta(days=1)
        self.transport.queue.append(page([row("g2", "2026年9月公告", "f2", "f1", "f3"), row("g3", "2025年12月公告", "f6", fyear="2026"),
                                          row("g1", "2025年公告", "f4", fyear="2025")]))
        sent = len(self.transport.sent)
        self.clock.advance(100)
        with patch("pmos.engine.today", return_value=tomorrow), patch("pmos.spec.periods.today", return_value=tomorrow):
            await engine.tick()
        self.assertEqual(engine.ledger.task(self.NAME, tomorrow.isoformat())["status"], "saved")
        self.assertEqual(len(self.transport.sent) - sent, 1, "page 1 is all known: the listing ends there")


class RealTrial(unittest.TestCase):
    FINEREPORT = 14

    def test_the_reports_on_trial_are_candidates_with_what_their_forms_ask(self):
        specs = REAL.datasets()
        for name in migration.TRIAL_LANE:
            self.assertIn(specs[name].status, ("candidate", "sampled", "quarantined"), name)
        self.assertEqual(specs["sitewide_5878dbf34cf4177a"].request["set"]["queryParam.date"], "{date}", "023 asks date, not mdate")
        self.assertEqual(specs["tie_line_intraday"].status, "sampled", "its 10-25 sample answered, r14 19:29")
        self.assertNotIn("complete_only", specs["sitewide_deb417edc0244802"].period, "or 2026 is never asked")
        self.assertEqual(specs["sitewide_f09e45e44534ce37"].request["site_menu"]["source"], "lnsnxh_ydfdsbjxjh", "the code on the 10-23 menu")
        self.assertEqual(specs["historical_electricity"].request["timeout"], 240, "the site took 161 s to answer in October")
        self.assertEqual(specs["additional_1410113473630834688"].request["component"], "AnnountTable")
        self.assertIs(specs["additional_1410113473630834688"].period["daily"], True)

    def test_the_fourteen_finereport_forms_are_candidates_to_probe(self):
        specs = [s for s in REAL.datasets().values() if s.request.get("transport") == "finereport" and s.status == "candidate"]
        self.assertEqual(len(specs), self.FINEREPORT - 8 - 2, "8 answered their samples, 2 are the site's own errors")
        self.assertEqual({s.request["site_menu"]["route"] for s in specs}, {"spotgoodsSum"})
        every = REAL.datasets().values()
        self.assertEqual(len([s for s in every if s.response["kind"] == "finereport_page"]), 5, "the five .cpt")
        self.assertEqual(REAL.datasets()["sitewide_31308b59070354ef"].status, "sampled", "its 5 pages were read")

    def test_the_nine_per_option_reports_read_their_dictionary_without_a_selection(self):
        specs = REAL.datasets()
        reading = {name for name, s in specs.items() if s.source.get("dictionary") and s.scopes and s.status != "candidate"}
        self.assertEqual(reading, {"sitewide_7d2f4e6e4598aba8", "sitewide_39c86b17616d219e", "sitewide_1c01651cb1051d47",
                                   "sitewide_931ac1f25ce9e62a", "sitewide_46fa5856d5315e4e", "sitewide_e6a2dcfefa4e7391",
                                   "realtime_node_price", "sitewide_0400f0e45de22dcd", "dayahead_power_and_average_price"})
        for name in reading:
            config = specs[name].source["dictionary"]
            with self.subTest(name=name):
                self.assertEqual(list(specs[name].scopes[0]), [config["field"]])
                self.assertIn("{date}", config["set"].values())
                self.assertIn(None, config["set"].values(), "no option selected: the form's own first read")
                self.assertTrue(config["path"].rsplit("/", 1)[1].startswith("pdblnReport"))

    def test_the_four_finereport_grids_are_held_to_their_shape(self):
        specs = REAL.datasets()
        for name, text in (("sitewide_546644ece23fec6a", False), ("sitewide_e87a7708ade1e187", False),
                           ("sitewide_3ebbdf40ca6ec2b1", True), ("sitewide_bd2561ac7f825385", True)):
            with self.subTest(name=name):
                self.assertEqual(specs[name].status, "sampled")
                self.assertEqual(specs[name].response["kind"], "finereport_daily")
                self.assertEqual(bool(specs[name].response.get("text_values")), text)

    def test_the_day_ahead_capacity_asks_every_channel_and_type_by_its_own_widgets(self):
        spec = REAL.datasets()["sitewide_49aa17d82da46bfb"]
        self.assertEqual(len(spec.scopes), 250, "125 channels x 2 types")
        built = spec.build("2026-10-25", spec.scopes[1])
        self.assertEqual(built["set"], {"日期": "2026-10-25", "交易日期": "2026-10-25", "通道名称": spec.scopes[1]["channel"], "数据类型": "正向可用"})
        identity = migration.task_identity("liaoning", "liaoning-production", spec, "2026-10-25", next(k for k, v in spec.scope_items() if v == spec.scopes[1]))
        self.assertEqual(identity.dimensions, {"channel": spec.scopes[1]["channel"], "type": "正向可用"})

    def test_the_aliases_are_retired(self):
        specs = REAL.datasets()
        for name in ("sitewide_055496fd163e494f", "sitewide_c23050b85c48fa15", "dayahead_constraints"):
            self.assertEqual(specs[name].status, "retired")


class Trial(unittest.IsolatedAsyncioTestCase):
    """Real specs on a handoff: a candidate waits for its sample; a look at a form asks no data and stops nothing."""

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
            "policy": {"response_interval_seconds": 1, "renew_after_idle_seconds": 1500, "probe_after_idle_seconds": 1500,
                       "isolate_items": ["unknown", "site_data", "permission"], "isolation_streak": 1, "denial_streak": 1},
            "session": {"keepalive_dataset": None, "probe": None}}))
        self.names = ("sitewide_46fa5856d5315e4e", "tie_line_intraday", "sitewide_49aa17d82da46bfb")
        for name in self.names:
            value = json.loads((REPO / "provinces/liaoning/datasets" / f"{name}.json").read_text(encoding="utf-8"))
            if name == "tie_line_intraday":
                value["period"], value["status"] = {**value["period"], "start": day(6)}, "candidate"
            if name == "sitewide_49aa17d82da46bfb":
                value["status"] = "candidate"
            if name == "sitewide_46fa5856d5315e4e":
                value["period"] = {**value["period"], "start": day(2)}
            (directory / "datasets" / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False))
        self.province = Province.load("liaoning", self.tmp.path / "provinces")
        instance = self.province.instance()
        instance.mkdir(parents=True)
        ledger = Ledger(instance / "ledger.sqlite3")
        import_shadow(ledger, [], QueueContext("liaoning", "liaoning-production"), source_id="queue", snapshot_sha256="a" * 64)
        ledger.grant_handoff({"version": 1, "province": "liaoning", "account_alias": "liaoning-production",
                              "legacy_owner": {"stopped_evidence": "fixture"},
                              "snapshots": [{"source_id": "queue", "snapshot_sha256": "a" * 64}], "scope": ["sitewide_46fa5856d5315e4e"],
                              "request_budget": 100, "code_sha": "c", "approval": "fixture",
                              "session": {}, "seeding": "spec_periods"})
        lane = migration.daily_lane(self.tmp.path / "legacy", self.province.datasets(), names=("tie_line_intraday",))
        migration.apply_daily_lane(ledger, lane, "都按你建议的来", source_id="liaoning-trial-lane")
        ledger.extend_scope(ledger.handoff()["scope"], ledger.handoff()["scope"], "fixture")
        ledger.close()

    def engine(self):
        self.transport = FakeTransport(self.clock, duration=1, default=Response(
            200, "application/json", json.dumps(answer()).encode()))
        engine = Engine(self.province, self.transport, instance=self.province.instance(), clock=self.clock,
                        account_alias="liaoning-production")
        self.addCleanup(engine.ledger.close)
        return engine

    async def run_commands(self, engine, limit=6):
        for _ in range(limit):
            self.clock.advance(5)
            await engine.run_command()
        return engine.state.read("command_results", {})

    def test_trying_a_report_needs_the_users_words_a_candidate_and_the_scope(self):
        from argparse import Namespace
        from pmos import cli
        args = Namespace(dataset="tie_line_intraday", period=day(3), approval="都按你建议的来", wait=0)
        cli.cmd_try_report(self.province, args)
        queued = self.engine().state.read("commands", [])
        self.assertEqual([(c["action"], c["dataset"], c["period"]) for c in queued], [("sample", "tie_line_intraday", day(3))])
        with self.assertRaisesRegex(ValueError, "approval_required"):
            cli.cmd_try_report(self.province, Namespace(**{**vars(args), "approval": " "}))
        with self.assertRaisesRegex(ValueError, "not_in_the_handoff_scope"):
            cli.cmd_try_report(self.province, Namespace(**{**vars(args), "dataset": "sitewide_49aa17d82da46bfb"}))
        with self.assertRaisesRegex(ValueError, "not_a_candidate"):
            cli.cmd_try_report(self.province, Namespace(**{**vars(args), "dataset": "sitewide_46fa5856d5315e4e"}))

    async def test_a_page_that_cannot_be_reached_answers_the_asker_and_the_worker_goes_on(self):
        """Liaoning r14 2026-11-01 19:29: sample -> preflight_deferred:... -> a block whose reason is no plain code -> exit 2."""
        from pmos.faults import Decision, Fault
        engine = self.engine()
        async def preflight(request):
            raise Fault("site_menu_entry_missing", "local")
        self.transport.preflight = preflight
        engine.enqueue({"action": "sample", "dataset": "tie_line_intraday", "period": day(3), "scope": None})
        results = await self.run_commands(engine, 1)
        result = next(iter(results.values()))
        self.assertEqual((result["ok"], result["reason"]), (False, "preflight_deferred:site_menu_entry_missing"))
        self.assertIsNone(engine.state.blocked())
        self.assertEqual(self.province.datasets()["tie_line_intraday"].status, "candidate", "unchanged")
        self.assertEqual(self.transport.sent, [], "nothing went out")
        engine.block(Fault("preflight_deferred:x", "local"), Decision("province", False))
        self.assertEqual(engine.state.blocked()["reason"], "preflight_deferred_x", "a composed code can never take the worker down")

    async def test_stop_all_meeting_a_request_is_no_block_and_the_next_start_is_not_refused(self):
        """Liaoning r15 19:45:44: stop-all met a request's own check, and the next accept was refused until an ack."""
        from pmos.faults import Fault
        engine = self.engine()
        engine.state.set_control(collect=True)
        with patch.object(engine, "collect", side_effect=Fault("stop_all_requested", "local")):
            engine.seed()
            engine.ledger.seed("sitewide_46fa5856d5315e4e", [day(2)], next(iter(self.province.datasets()["sitewide_46fa5856d5315e4e"].scope_items()))[0])
            for _ in range(3):
                self.clock.advance(5)
                await engine.tick()
        self.assertIsNone(engine.state.blocked())
        self.assertEqual(engine.dataset_holds(), {})

    async def test_a_report_can_allow_more_empty_periods_in_a_row_before_it_pauses(self):
        """A report that starts mid-year: asked newest first, two blank days at its start are no fault of the site's."""
        engine = self.engine()
        name = "tie_line_intraday"
        spec = self.province.datasets()[name]
        spec.source["max_consecutive_empty"] = 3
        spec.status = "sampled"
        blank = Response(200, "application/json", json.dumps({"status": 0, "data": []}).encode())
        for n in (3, 4):
            engine.ledger.seed(name, [day(n)])
            self.clock.advance(5)
            self.transport.queue.append(blank)
            await engine.collect(spec, day(n), "")
        self.assertEqual(engine.dataset_holds(), {}, "two empty days: the policy's 2 would have paused it")
        engine.ledger.seed(name, [day(5)])
        self.clock.advance(5)
        self.transport.queue.append(blank)
        await engine.collect(spec, day(5), "")
        self.assertEqual(engine.dataset_holds()[name]["reason"], "consecutive_empty_periods")

    async def test_a_dictionary_can_be_asked_by_get(self):
        engine = self.engine()
        spec = self.province.datasets()["sitewide_46fa5856d5315e4e"]
        spec.source["dictionary"] = {**spec.source["dictionary"], "method": "GET", "set": {}}
        self.clock.advance(5)
        self.transport.queue.append(Response(200, "application/json", json.dumps({"status": 0, "data": []}).encode()))
        await engine._read_dictionary(spec, day(1))
        self.assertEqual(self.transport.sent[-1]["method"], "GET")

    async def test_a_page_step_that_sent_nothing_keeps_the_report_as_it_was_and_tells_why(self):
        """r18 23:07: a dataset's status "paused" is no lifecycle state; saving it hid the fault as a SpecError."""
        from pmos.faults import Fault
        engine = self.engine()
        self.clock.advance(5)
        self.transport.queue.append(Fault("finereport_stage_changed", "local", unsent=True, detail={"stage": "parameters_d", "queued": [{"op": "widget"}]}))
        engine.enqueue({"action": "sample", "dataset": "tie_line_intraday", "period": day(3), "scope": None})
        results = await self.run_commands(engine, 1)
        result = next(iter(results.values()))
        self.assertEqual((result["ok"], result["reason"]), (False, "finereport_stage_changed"))
        self.assertEqual(result["detail"]["queued"], [{"op": "widget"}])
        self.assertEqual(self.province.datasets()["tie_line_intraday"].status, "candidate")
        self.assertIsNone(engine.state.blocked())

    async def test_asking_again_what_was_asked_answers_the_asker_and_stops_nothing(self):
        """Liaoning r19 23:17: try-report of a day an earlier sample had failed on blocked the province, and the worker left."""
        from argparse import Namespace
        from pmos import cli
        from pmos.faults import Fault
        engine = self.engine()
        self.clock.advance(5)
        self.transport.queue.append(Response(200, "application/json", b"not json at all"))
        engine.enqueue({"action": "sample", "dataset": "tie_line_intraday", "period": day(3), "scope": None})
        await self.run_commands(engine, 1)
        self.assertTrue(engine.ledger.consumed("tie_line_intraday", day(3), ""))
        engine.state.remove("block")
        sent = len(self.transport.sent)
        engine.enqueue({"action": "sample", "dataset": "tie_line_intraday", "period": day(3), "scope": None})
        results = await self.run_commands(engine, 1)
        last = list(results.values())[-1]
        self.assertEqual((last["ok"], last["reason"]), (False, "consumed_identity_requires_review"))
        self.assertIsNone(engine.state.blocked())
        self.assertEqual(len(self.transport.sent), sent)
        spec_status = self.province.datasets()["tie_line_intraday"].status
        if spec_status == "candidate":
            with self.assertRaisesRegex(ValueError, "already_asked"):
                cli.cmd_try_report(self.province, Namespace(dataset="tie_line_intraday", period=day(3), approval="同意", wait=0, scope=None))

    def test_a_sample_refused_for_its_pages_can_be_released_and_nothing_else_can(self):
        ledger = Ledger(self.province.instance() / "ledger.sqlite3")
        self.addCleanup(ledger.close)
        attempt = ledger.begin("sample", "tie_line_intraday", day(3), "")
        ledger.finish(attempt, "failed", http_status=200, reason="finereport_table_more_pages")
        other = ledger.begin("sample", "tie_line_intraday", day(4), "")
        ledger.finish(other, "failed", http_status=200, reason="unexpected_business_status")
        self.assertTrue(ledger.consumed("tie_line_intraday", day(3), ""))
        with self.assertRaisesRegex(ValueError, "approval"):
            ledger.release_sample(attempt, " ")
        with self.assertRaisesRegex(ValueError, "not_a_refused_sample"):
            ledger.release_sample(other, "同意")
        self.assertEqual(ledger.release_sample(attempt, "同意")[0], attempt)
        self.assertFalse(ledger.consumed("tie_line_intraday", day(3), ""))
        self.assertTrue(ledger.consumed("tie_line_intraday", day(4), ""))
        with self.assertRaisesRegex(ValueError, "already_released"):
            ledger.release_sample(attempt, "同意")

    async def test_a_candidate_is_not_seeded_until_its_sample_answers_and_that_day_is_not_asked_again(self):
        engine = self.engine()
        engine.seed()
        name = "tie_line_intraday"
        self.assertFalse(engine.ledger.has_tasks(name), "a candidate in the scope: nothing to collect yet")
        from pmos.adapt import sample
        self.clock.advance(5)
        self.transport.queue.append(Response(200, "application/json", json.dumps(answer()).encode()))
        result = await sample(engine, self.province.datasets()[name], day(3))
        self.assertEqual((result["result"], result["status"]), ("saved", "sampled"))
        sent = len(self.transport.sent)
        tomorrow = today() + timedelta(days=1)
        with patch("pmos.engine.today", return_value=tomorrow):
            engine.seed()
        spec = self.province.datasets()[name]
        self.assertTrue(engine.ledger.has_tasks(name), "the next day's seed takes it in")
        await engine.collect(spec, day(3), "")
        self.assertEqual(len(self.transport.sent), sent, "the sampled day: its sample's answer, no second request")
        row = engine.ledger.task(name, day(3))
        self.assertEqual((row["status"], row["reason"]), ("saved", "answered_by_sample"))
        self.assertEqual(len(engine.archive.records(name)), 1)

    async def test_a_sample_that_answered_nothing_keeps_the_candidate_and_its_day_settles_empty(self):
        engine = self.engine()
        engine.seed()
        name = "tie_line_intraday"
        from pmos.adapt import sample
        self.clock.advance(5)
        self.transport.queue.append(Response(200, "application/json", json.dumps({"status": 0, "data": []}).encode()))
        result = await sample(engine, self.province.datasets()[name], day(3))
        self.assertEqual((result["result"], result["status"]), ("empty", "candidate"))
        engine.ledger.seed(name, [day(3)])
        spec = self.province.datasets()[name]
        sent = len(self.transport.sent)
        await engine.collect(spec, day(3), "")
        self.assertEqual(len(self.transport.sent), sent)
        self.assertEqual(engine.ledger.task(name, day(3))["reason"], "answered_by_sample")

    async def test_the_dictionary_is_read_once_a_day_and_a_new_option_is_told_not_seeded(self):
        from pmos import monitor
        engine = self.engine()
        name = "sitewide_46fa5856d5315e4e"
        known = [item["name"] for item in self.province.datasets()[name].scopes]
        engine.seed()
        commands = engine.state.read("commands", [])
        self.assertEqual([(c["action"], c["dataset"]) for c in commands], [("read_dictionary", name)])
        tasks = engine.ledger.db.execute("SELECT COUNT(*) FROM tasks WHERE dataset=?", (name,)).fetchone()[0]
        self.transport.queue.append(Response(200, "application/json", json.dumps(
            {"status": 0, "data": [{"name": n} for n in (*known, "新节点")]}).encode()))
        await self.run_commands(engine, 1)
        request = self.transport.sent[-1]
        self.assertEqual(request["call"], "initDic")
        self.assertTrue(request["path"].endswith("pdblnReportDic001"))
        self.assertIsNone(request["set"]["queryParam.name"], "no option selected")
        self.assertRegex(request["set"]["queryParam.mdate"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(engine.state.read("new_options")[name]["new"], ["新节点"])
        self.assertEqual(engine.ledger.db.execute("SELECT COUNT(*) FROM tasks WHERE dataset=?", (name,)).fetchone()[0], tasks, "not seeded")
        found = monitor.conditions(self.province.instance(), "active", self.clock())
        self.assertIn("新节点", found["new_options:" + name])
        engine._seeded_on = None
        engine.seed()
        self.assertEqual(engine.state.read("commands", []), [], "read already today")
        self.clock.advance(5)
        self.transport.queue.append(Response(200, "application/json", json.dumps({"status": 0, "data": []}).encode()))
        await engine._read_dictionary(self.province.datasets()[name], day(1))
        self.assertIn(name, engine.state.read("new_options"), "an empty answer is a day not published: it says nothing")
        self.clock.advance(5)
        self.transport.queue.append(Response(200, "application/json", json.dumps({"status": 0, "data": [{"name": n} for n in known]}).encode()))
        await engine._read_dictionary(self.province.datasets()[name], day(2))
        self.assertEqual(engine.state.read("new_options"), {}, "cleared once the spec knows it, or the site dropped it")

    async def test_a_refused_look_is_that_reports_answer_and_collection_goes_on(self):
        engine = self.engine()
        engine.enqueue({"action": "read_dictionary", "dataset": "sitewide_46fa5856d5315e4e", "period": day(2)})
        self.transport.queue.append(Response(200, "application/json", json.dumps({"status": 40302, "message": "无权限"}).encode()))
        results = await self.run_commands(engine, 1)
        self.assertFalse(next(iter(results.values()))["ok"])
        self.assertIsNone(engine.state.blocked(), "the province goes on")
        self.assertEqual(engine.dataset_holds(), {})

    async def test_a_probe_reports_what_the_form_asks_for_and_costs_no_budget(self):
        engine = self.engine()
        before = engine.ledger.budget_starts(0) if hasattr(engine.ledger, "budget_starts") else None
        body = {"status": 0, "data": {"mapping": {"http_status": 200, "business_status": 0, "mapped": True},
                                      "widgets": [{"name": "日期", "type": "datetime", "value": "2026-10-01"}]}}
        self.transport.queue.append(Response(200, "application/json", json.dumps(body, ensure_ascii=False).encode()))
        engine.enqueue({"action": "probe_report", "dataset": "sitewide_49aa17d82da46bfb"})
        results = await self.run_commands(engine, 1)
        result = next(iter(results.values()))
        self.assertTrue(result["ok"])
        self.assertEqual(result["widgets"][0]["name"], "日期")
        self.assertEqual(result["name"], "日前省间联络线输电可用容量")
        self.assertEqual(self.transport.sent[-1]["action"], "probe")
        self.assertEqual(engine.ledger.budget_starts(0), before)


if __name__ == "__main__":
    unittest.main()
