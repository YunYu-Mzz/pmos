"""Sparse per-date selectors: preserve old facts, collect listed options only."""
import json
import unittest
from unittest.mock import patch

from helpers import Clock, FakeTransport, TempDir, dataset, envelope, make_province
from pmos.engine import Engine
from pmos.faults import Fault
from pmos.period_options import SCOPE, listed_scopes
from pmos.reporting import status_metrics
from pmos.spec import scope_key
from pmos.validate import Response

DAY = "2026-10-20"
LABEL = "断面名称"


def option(name):
    return {"label": name, "value": name, "value_type": "string"}


def key(name):
    return scope_key({LABEL: name})


class DateTransport(FakeTransport):
    async def send(self, request):
        result = await super().send(request)
        if request.get("action") == "options":
            value = json.loads(result.body)
            value.setdefault("observed_date", request["fields"][0]["value"])
            return Response(result.status, result.content_type, json.dumps(value).encode())
        return result


class DailySelectors(unittest.IsolatedAsyncioTestCase):
    def engine(self, *, start=DAY):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        spec = dataset("sections", start=start, response="json")
        spec["period"]["end"] = DAY
        spec["request"]["body"]["name"] = "{scope[断面名称]}"
        spec["scopes"] = [{LABEL: "old"}, {LABEL: "never_listed"}]
        spec["source"] = {"period_options": {"label": LABEL}, "roll_forward_options": True,
                          "discover_options": {"label": LABEL},
                          "ui_request": {"frame": "sections", "menu": ["market", "sections"],
                                         "fields": [{"kind": "date", "placeholder": "date", "value": "{date}"}]}}
        province = make_province(tmp.path, [spec], policy={"response_interval_seconds": 1})
        clock = Clock()
        transport = DateTransport(clock, default=envelope())
        engine = Engine(province, transport, instance=tmp.path / "var", clock=clock)
        engine.state.set_control(collect=True)
        self.addCleanup(engine.ledger.close)
        return engine, transport, clock

    async def step(self, engine, clock):
        clock.advance(5)
        await engine.tick()

    def test_invalid_or_empty_lists_cannot_authorize_filtering(self):
        for found in ([], None, {}, [option("x"), {"label": "y", "value": "internal", "value_type": "string"}]):
            with self.subTest(found=found), self.assertRaises(ValueError):
                listed_scopes(found, LABEL)

    async def test_seed_schedules_dates_instead_of_cartesian_product(self):
        engine, _, _ = self.engine(start="2026-10-19")
        engine.seed()
        self.assertEqual(engine.ledger.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(engine.ledger.db.execute("SELECT COUNT(*) FROM period_options").fetchone()[0], 2)
        task, _ = engine._next_sendable(engine.collection_specs())
        self.assertEqual((task["period"], task["date_options"]), (DAY, True))

    async def test_new_listed_option_is_collected_and_unlisted_task_stays_pending(self):
        engine, transport, clock = self.engine()
        engine.ledger.seed("sections", [DAY], key("never_listed"))
        transport.queue.append(envelope(data=[option("new")]))
        await engine.tick()
        self.assertEqual(transport.sent[0]["fields"][0]["value"], DAY)
        self.assertEqual(transport.sent[0]["action"], "options")
        await self.step(engine, clock)
        self.assertEqual(transport.sent[-1]["body"], {"date": DAY, "name": "new"})
        self.assertEqual(engine.ledger.task("sections", DAY, key("new"))["status"], "saved")
        unlisted = engine.ledger.task("sections", DAY, key("never_listed"))
        self.assertEqual((unlisted["status"], unlisted["attempts"]), ("pending", 0))
        await self.step(engine, clock)
        self.assertEqual(engine.state.read("status")["phase"], "waiting_scope_review")
        self.assertEqual(len(transport.sent), 2)

    async def test_saved_empty_and_unknown_business_facts_are_not_reopened(self):
        engine, transport, clock = self.engine()
        for name, status in (("old", "saved"), ("empty", "empty"), ("unknown", "failed")):
            engine.ledger.seed("sections", [DAY], key(name))
            engine.ledger.settle_task("sections", DAY, status, scope=key(name), reason="historical")
        before = [tuple(r) for r in engine.ledger.db.execute("SELECT * FROM tasks ORDER BY scope")]
        transport.queue.append(envelope(data=[option("old"), option("empty"), option("unknown")]))
        await engine.tick()
        after = [tuple(r) for r in engine.ledger.db.execute("SELECT * FROM tasks ORDER BY scope")]
        self.assertEqual(before, after)
        await self.step(engine, clock)
        self.assertEqual(len(transport.sent), 1, "no previously terminal business combination sent")

    async def test_moves_to_next_day_without_querying_unlisted_combinations(self):
        engine, transport, clock = self.engine(start="2026-10-19")
        for day in (DAY, "2026-10-19"):
            engine.ledger.seed("sections", [day], key("never_listed"))
        transport.queue += [envelope(data=[option("old")]), envelope(), envelope(data=[option("new")]), envelope()]
        for _ in range(4):
            await self.step(engine, clock)
        self.assertEqual([r["fields"][0]["value"] for r in transport.sent if r.get("action") == "options"],
                         [DAY, "2026-10-19"])
        self.assertEqual([r["body"]["name"] for r in transport.sent if r["transport"] == "fetch"], ["old", "new"])
        self.assertEqual(engine.ledger.db.execute("SELECT COUNT(*) FROM tasks WHERE scope=? AND status='pending'",
                                                  (key("never_listed"),)).fetchone()[0], 2)

    async def test_empty_dictionary_is_review_not_empty_business_or_fallback(self):
        engine, transport, clock = self.engine()
        engine.ledger.seed("sections", [DAY], key("old"))
        transport.queue.append(envelope(data=[]))
        await engine.tick()
        await self.step(engine, clock)
        self.assertEqual(engine.ledger.option_period("sections", DAY)["state"], "review")
        self.assertEqual(engine.ledger.task("sections", DAY, key("old"))["status"], "pending")
        self.assertEqual(len(transport.sent), 1)
        metrics = status_metrics(engine.instance, now=clock())
        self.assertEqual(metrics["scope_review_pending"], 1)
        self.assertIn("待核", metrics["headline"])

    async def test_crash_after_inventory_response_recovers_without_another_request(self):
        engine, transport, clock = self.engine()
        engine.seed()
        transport.queue.append(envelope(data=[option("new")]))
        await engine.request("sample", {"transport": "ui", "action": "options", "path": "/px-test/sections",
                                        "fields": [{"kind": "date", "value": DAY}]},
                             {"kind": "json"}, dataset="sections", period=DAY, scope=SCOPE)
        self.assertEqual(engine.ledger.option_period("sections", DAY)["state"], "pending")
        await self.step(engine, clock)
        self.assertEqual(engine.ledger.option_period("sections", DAY)["state"], "ready")
        self.assertEqual(len(transport.sent), 1)
        self.assertIsNotNone(engine.ledger.task("sections", DAY, key("new")))

    async def test_unknown_inventory_is_preserved_and_not_retried_after_review(self):
        engine, transport, clock = self.engine()
        transport.queue.append(Fault("network_error", "network"))
        await engine.tick()
        self.assertEqual(engine.state.blocked()["reason"], "network_error")
        engine.state.clear_block("network_error")
        await self.step(engine, clock)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(engine.ledger.option_period("sections", DAY)["state"], "review")
        self.assertEqual(engine.ledger.last_attempt("sample")["outcome"], "unknown")

    async def test_auth_fault_stops_before_any_business_combination(self):
        engine, transport, clock = self.engine()
        transport.queue.append(Fault("auth_expired", "auth"))
        await engine.tick()
        self.assertEqual(engine.state.blocked()["reason"], "auth_expired")
        await self.step(engine, clock)
        self.assertEqual(len(transport.sent), 1)

    async def test_inventory_and_collection_share_response_spacing(self):
        engine, transport, clock = self.engine()
        transport.queue.append(envelope(data=[option("old")]))
        await engine.tick()
        await engine.tick()
        self.assertEqual(len(transport.sent), 1)
        clock.advance(1)
        await engine.tick()
        self.assertEqual(len(transport.sent), 2)

    async def test_stale_date_evidence_is_reviewed_without_filtering_tasks(self):
        engine, transport, clock = self.engine()
        engine.ledger.seed("sections", [DAY], key("old"))
        raw = {"status": 0, "data": [option("new")], "observed_date": "2026-10-19"}
        transport.queue.append(Response(200, "application/json", json.dumps(raw).encode()))
        await engine.tick()
        row = engine.ledger.option_period("sections", DAY)
        self.assertEqual((row["state"], row["reason"]), ("review", "date_options_date_unverified"))
        self.assertEqual(engine.ledger.task("sections", DAY, key("old"))["status"], "pending")
        self.assertIsNone(engine.ledger.task("sections", DAY, key("new")))

    async def test_legacy_skip_batch_stops_when_next_day_needs_an_inventory(self):
        engine, transport, clock = self.engine(start="2026-10-19")
        engine.seed()
        engine.ledger.record_period_options("sections", DAY, [key("old")])
        with patch.object(engine.ledger, "legacy_states", return_value=["saved"]):
            await engine.tick()
        self.assertEqual(engine.ledger.task("sections", DAY, key("old"))["status"], "skipped")
        self.assertEqual(transport.sent, [], "skipping old work sends no requests")
        transport.queue.append(envelope(data=[option("new")]))
        await self.step(engine, clock)
        self.assertEqual(engine.ledger.option_period("sections", "2026-10-19")["state"], "ready")
        self.assertEqual(len(transport.sent), 1)

    async def test_disabling_date_mode_restores_full_queue_without_deleting_inventory(self):
        engine, transport, clock = self.engine()
        engine.seed()
        engine.ledger.record_period_options("sections", DAY, [key("old")])
        engine.ledger.settle_task("sections", DAY, "saved", scope=key("old"))
        engine.ledger.seed("sections", [DAY], key("never_listed"))
        spec = engine.province.datasets()["sections"]
        spec.source.pop("period_options")
        spec.source.pop("discover_options")
        spec.source.pop("roll_forward_options")
        spec.save(engine.province.datasets_dir / "sections.json")
        engine._seeded_on = None
        await self.step(engine, clock)
        self.assertEqual(transport.sent[-1]["body"]["name"], "never_listed")
        self.assertEqual(engine.ledger.task("sections", DAY, key("never_listed"))["status"], "saved")
        self.assertEqual(engine.ledger.option_period("sections", DAY)["state"], "ready")
        metrics = status_metrics(engine.instance, now=clock())
        self.assertEqual(metrics["period_option_mode"], "cartesian")
        self.assertEqual(metrics["scope_review_pending"], 0)
