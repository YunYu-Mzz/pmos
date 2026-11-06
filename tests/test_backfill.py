"""Backfill: the early days nobody's task held (Anhui 2026-11-01, legacy_pending_only), after every new day."""
import json
import os
import unittest
from argparse import Namespace
from datetime import timedelta
from unittest.mock import patch

from pmos import cli
from pmos.contracts import TaskIdentity
from pmos.engine import BACKFILL_DISCOVERY_DAYS, Engine
from pmos.ledger import Ledger
from pmos.periods import today
from pmos.queue_compat import LegacyTask, QueueContext, import_shadow
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir, dataset, make_province


def day(n):
    return (today() - timedelta(days=n)).isoformat()


NEWEST, OLD_PENDING, CONSUMED, START = day(1), day(2), day(3), day(16)     # lag 1 day: day(1) is the newest due


def per_option(name, options):
    label = "线名"
    value = dataset(name, start=START, response="json")
    value["period"] = {"kind": "day", "start": START, "end": "today", "lag_days": 1}
    page = {"frame": "infoCode=" + name, "menu": ["现货市场", name],
            "fields": [{"kind": "date", "placeholder": "选择日期", "value": "{date}"}]}
    value["request"] = {"transport": "fetch", "method": "POST", "path": "/px-test/" + name,
                        "template_body_set": {"businessTime": "{date}", "genName": "{scope[线名]}"},
                        "bootstrap": {"transport": "ui", **page, "action": "query", "path": "/px-test/" + name, "method": "POST",
                                      "fields": page["fields"] + [{"kind": "select", "label": label, "value": "{scope[线名]}"}]}}
    value["scopes"] = [{label: option} for option in options]
    value["source"] = {"roll_forward_options": True, "discover_options": {"label": label}, "ui_request": page}
    return value


def identity(name, period):
    return TaskIdentity("testland", "acct", name, {"kind": "day", "label": period}, {"线名": "甲线"}, operation="query")


class Page(FakeTransport):
    """A template appears once a page query ran; a dropdown lists ``listed`` (date -> options)."""

    def __init__(self, clock, listed=None):
        super().__init__(clock, duration=1, default=Response(200, "application/json", json.dumps({"status": 0, "data": [{"v": 1}]}).encode()))
        self.listed = listed or {}

    async def send(self, request):
        if request.get("action") == "options":
            names = self.listed.get(request["fields"][0]["value"], [])
            self.sent.append(request)
            self.clock.advance(1)
            return Response(200, "application/json", json.dumps({"status": 0, "data": [
                {"label": n, "value": n, "value_type": "string"} for n in names]}, ensure_ascii=False).encode())
        if request["transport"] == "ui":
            self.unready = set()
        return await super().send(request)


class Fixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(self.tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        other = dataset("other", start=NEWEST, response="json")
        self.province = make_province(self.tmp.path, [per_option("big", ["甲线", "乙线", "丙线"]), other, per_option("small", ["甲线"])],
                                      policy={"response_interval_seconds": 1})
        instance = self.province.instance()
        instance.mkdir(parents=True)
        ledger = Ledger(instance / "ledger.sqlite3")
        rows = [LegacyTask("c", "big", "saved", "saved", True, identity("big", CONSUMED)),
                LegacyTask("p", "big", "pending", "pending", False, identity("big", OLD_PENDING)),
                LegacyTask("s", "small", "pending", "pending", False, identity("small", OLD_PENDING))]
        with patch("pmos.queue_compat.PROVINCES", {"testland", "liaoning", "anhui", "shanxi"}):
            import_shadow(ledger, rows, QueueContext("testland", "acct"), source_id="queue", snapshot_sha256="a" * 64)
        ledger.grant_handoff({"version": 1, "province": "testland", "account_alias": "acct",
                              "legacy_owner": {"stopped_evidence": "fixture"},
                              "snapshots": [{"source_id": "queue", "snapshot_sha256": "a" * 64}],
                              "scope": ["big", "other", "small"], "request_budget": 1000, "code_sha": "c",
                              "approval": "fixture", "session": {}, "seeding": "legacy_pending_only"})
        ledger.extend_scope(["big", "other", "small"], ["other"], "fixture")
        specs = self.province.datasets()
        for name in ("big", "small"):
            for key, _ in specs[name].scope_items():
                ledger.seed(name, [OLD_PENDING], key)
        ledger.close()

    def engine(self, listed=None):
        self.transport = Page(self.clock, listed)
        engine = Engine(self.province, self.transport, instance=self.province.instance(), clock=self.clock, account_alias="acct")
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        return engine

    def backfill(self, approval="同意", dry_run=False, datasets="big,small"):
        with patch.object(cli, "_print") as printed:
            cli.cmd_backfill(self.province, Namespace(datasets=datasets, approval=approval, dry_run=dry_run))
        return printed.call_args.args[0]

    def tasks(self, name):
        engine_ledger = Ledger(self.province.instance() / "ledger.sqlite3")
        try:
            return {(r["period"], r["scope"]): r["status"] for r in engine_ledger.db.execute(
                "SELECT period, scope, status FROM tasks WHERE dataset=?", (name,))}
        finally:
            engine_ledger.close()

    async def run_all(self, engine, ticks):
        for _ in range(ticks):
            await engine.tick()
            self.clock.advance(2)

    def sent_days(self, name):
        """(transport kind, day) of everything sent for one report, in order; dropdown reads as "options"."""
        result = []
        for request in self.transport.sent:
            if request["transport"] == "fetch" and request.get("path") == "/px-test/" + name:
                result.append(("fetch", request["template_body_set"]["businessTime"]))
            elif request["transport"] == "ui" and request.get("frame") == "infoCode=" + name:
                kind = "options" if request.get("action") == "options" else "ui"
                result.append((kind, request["fields"][0]["value"]))
        return result


class Command(Fixture):
    def test_a_dry_run_only_prints_the_plan(self):
        plan = self.backfill(approval=None, dry_run=True)
        self.assertEqual(plan["backfill"]["big"], {"before": CONSUMED, "days": 13, "options": 3, "requests_at_most": 39})
        self.assertEqual(plan["backfill"]["small"], {"before": OLD_PENDING, "days": 14, "options": 1, "requests_at_most": 14})
        self.assertEqual(plan["requests_at_most"], 53)
        engine = self.engine()
        self.assertEqual(engine.ledger.handoff()["seeded"], ["other"])
        self.assertIsNone(engine.state.read("backfill_before"))

    def test_it_needs_the_users_words_and_reports_in_scope(self):
        with self.assertRaisesRegex(ValueError, "approval_required"):
            self.backfill(approval=" ")
        with self.assertRaisesRegex(ValueError, "outside_scope:nope"):
            self.backfill(datasets="big,nope")
        self.assertEqual(self.engine().ledger.handoff()["seeded"], ["other"])

    def test_apply_records_the_seeding_and_each_reports_boundary(self):
        self.backfill(approval="同意出 r19")
        engine = self.engine()
        handoff = engine.ledger.handoff()
        self.assertEqual(handoff["scope"], ["big", "other", "small"], "the collection order stays")
        self.assertEqual(handoff["seeded"], ["other", "big", "small"])
        self.assertEqual(engine.state.read("backfill_before"), {"big": CONSUMED, "small": OLD_PENDING})


class Seeding(Fixture):
    def test_every_early_day_gets_every_option_and_what_legacy_completed_stays_out(self):
        self.backfill()
        engine = self.engine()
        engine.seed()
        tasks = self.tasks("big")
        self.assertEqual(sum(1 for period, _ in tasks if period < CONSUMED), 13 * 3)
        self.assertFalse([k for k in tasks if k[0] == CONSUMED], "the legacy owner completed that day")
        self.assertEqual(sum(1 for period, _ in tasks if period == OLD_PENDING), 3)

    def test_a_seeded_report_still_reads_its_new_options_from_the_page(self):
        """seed() gives it its days, but only _roll_forward_options reads the dropdown of the newest one."""
        self.backfill()
        engine = self.engine()
        engine.seed()
        queued = {(c["action"], c["dataset"]) for c in engine.state.read("commands", [])}
        self.assertEqual(queued, {("discover_options", "big"), ("discover_options", "small")})


class Order(Fixture):
    async def test_new_days_of_every_report_go_before_any_early_day_and_the_smallest_report_first(self):
        self.backfill()
        engine = self.engine()
        await self.run_all(engine, 40)
        order = [(r["path"], r["template_body_set"]["businessTime"]) if r["transport"] == "fetch" and "template_body_set" in r
                 else (r.get("path"), None) for r in self.transport.sent if r["transport"] == "fetch"]
        early = [i for i, (path, when) in enumerate(order) if when and when < CONSUMED]
        recent = [i for i, (path, when) in enumerate(order) if not when or when >= CONSUMED]
        self.assertTrue(early and recent)
        self.assertLess(max(recent), min(early), "other's day and the newer days of big and small come first")
        paths = [order[i][0] for i in early]
        self.assertEqual(paths[0], "/px-test/small", "the report with the fewest options first")
        self.assertEqual(paths.index("/px-test/big"), sum(1 for p in paths if p == "/px-test/small"), "then the next")
        days = [order[i][1] for i in early if order[i][0] == "/px-test/small"]
        self.assertEqual(days, sorted(days, reverse=True), "newest first within a report")

    async def test_an_early_day_without_a_template_primes_on_the_newest_day_not_its_own(self):
        self.backfill()
        engine = self.engine()
        engine.seed()
        specs = engine.province.datasets()
        for name in ("big", "small"):
            for key, _ in specs[name].scope_items():
                engine.ledger.settle_task(name, OLD_PENDING, "skipped", scope=key, reason="fixture")
                if engine.ledger.task(name, NEWEST, key):
                    engine.ledger.settle_task(name, NEWEST, "skipped", scope=key, reason="fixture")
        engine.ledger.settle_task("other", NEWEST, "skipped", reason="fixture")
        engine.state.write("commands", [])
        self.transport.unready = {"/px-test/small"}
        # This checks priming, not the separate weekly option-discovery lane.
        # A date divisible by BACKFILL_DISCOVERY_DAYS otherwise spends one of these ticks there.
        with patch.object(engine, "_backfill_discovery", return_value=False):
            await self.run_all(engine, 3)
        first = next(r for r in self.transport.sent if r["transport"] == "ui" and r.get("action") == "query")
        self.assertEqual(first["fields"][0]["value"], NEWEST, "the template does not depend on the day")
        self.assertTrue(first["fields"][1]["any_listed"])
        replay = next(r for r in self.transport.sent if r["transport"] == "fetch" and "template_body_set" in r)
        self.assertLess(replay["template_body_set"]["businessTime"], OLD_PENDING)
        self.assertIsNone(engine.state.blocked())
        self.assertEqual(engine.state.read("dataset_holds", {}) or {}, {})

    async def test_a_newer_day_keeps_bootstrapping_with_its_own_option(self):
        self.backfill()
        engine = self.engine()
        self.transport.unready = {"/px-test/big"}
        await self.run_all(engine, 2)
        first = next(r for r in self.transport.sent if r["transport"] == "ui" and r.get("action") == "query")
        self.assertEqual(first["frame"], "infoCode=big")
        self.assertFalse(first["fields"][1].get("any_listed"))
        self.assertGreaterEqual(first["fields"][0]["value"], CONSUMED)


class Discovery(Fixture):
    sampled = [day(n) for n in range(4, 17) if (today() - timedelta(days=n)).toordinal() % BACKFILL_DISCOVERY_DAYS == 0]

    async def test_an_early_days_dropdown_is_read_once_a_week_of_days_and_its_options_join(self):
        self.assertTrue(self.sampled)
        gone = self.sampled[0]
        self.backfill()
        engine = self.engine(listed={gone: ["甲线", "已撤销线"]})
        await self.run_all(engine, 120)
        reads = [when for kind, when in self.sent_days("big") if kind == "options" and when < CONSUMED]
        self.assertEqual(sorted(reads), sorted(p for p in self.sampled if p < CONSUMED), "once each, only those days")
        engine.ledger.close()
        names = {key: values for key, values in engine.province.datasets()["big"].scope_items()}
        self.assertIn({"线名": "已撤销线"}, list(names.values()), "the option found joined the report")
        self.assertTrue([k for k in self.tasks("big") if k[0] == gone and names[k[1]] == {"线名": "已撤销线"}],
                        "and got that day's task")

    async def test_a_held_discovery_stops_only_the_reading(self):
        self.backfill()
        engine = self.engine()
        engine.state.write("discovery_holds", {"big": {"reason": "fixture"}, "small": {"reason": "fixture"}})
        engine.seed()
        engine.state.write("commands", [])
        await self.run_all(engine, 30)
        self.assertFalse([1 for kind, when in self.sent_days("big") + self.sent_days("small") if kind == "options"])
        self.assertTrue([1 for kind, when in self.sent_days("small") if kind == "fetch" and when < OLD_PENDING])


if __name__ == "__main__":
    unittest.main()
