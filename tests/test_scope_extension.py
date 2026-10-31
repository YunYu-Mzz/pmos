"""Full-year scope after a handoff: approved order and daily seeding, late legacy tombstones, the Shanxi plan."""
import json
import os
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from pmos import cli
from pmos.contracts import TaskIdentity
from pmos.engine import Engine, task_identity
from pmos.ledger import Ledger
from pmos.periods import today
from pmos.province import Province
from pmos.queue_compat import LegacyTask, QueueContext, import_shadow

from helpers import Clock, FakeTransport, TempDir, dataset, make_province

REPO = Path(__file__).resolve().parents[1]
DAYS = [today().fromordinal(today().toordinal() - n).isoformat() for n in range(4)]   # newest first


def export_identity(name, day):
    return TaskIdentity("testland", "acct", name, {"kind": "day", "label": day}, {}, operation="export")


class Fixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(self.tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        start = DAYS[-1]
        native = dataset("native", start=start, response="json")
        native["request"] = {"transport": "fetch", "method": "POST", "path": "/px-test/native", "body": {"d": "{date}"}}
        self.province = make_province(self.tmp.path, [dataset("load", start=start), dataset("export", start=start),
                                                      native, dataset("strict", start=start)],
                                      policy={"response_interval_seconds": 15})
        instance = self.province.instance()
        instance.mkdir(parents=True)
        ledger = Ledger(instance / "ledger.sqlite3")
        rows = [  # the export indicator: newest day saved, next one still pending in the legacy queue
            LegacyTask("e0", "export", "saved", "saved", True, export_identity("export", DAYS[0])),
            LegacyTask("e1", "export", "pending", "pending", False, export_identity("export", DAYS[1])),
            # the native indicator: its newest day was exported by legacy (another operation)
            LegacyTask("n0", "native", "saved", "saved", True, export_identity("native", DAYS[0])),
            LegacyTask("s1", "strict", "pending", "pending", False, export_identity("strict", DAYS[1]))]
        with patch("pmos.queue_compat.PROVINCES", {"testland", "liaoning", "anhui", "shanxi"}):
            import_shadow(ledger, rows, QueueContext("testland", "acct"), source_id="queue", snapshot_sha256="a" * 64)
        ledger.grant_handoff({"version": 1, "province": "testland", "account_alias": "acct",
                              "legacy_owner": {"stopped_evidence": "fixture"},
                              "snapshots": [{"source_id": "queue", "snapshot_sha256": "a" * 64}], "scope": ["load"],
                              "request_budget": 100, "code_sha": "c", "approval": "fixture",
                              "session": {}, "seeding": "legacy_pending_only"})
        ledger.seed("load", DAYS[:1])
        ledger.close()

    def engine(self):
        engine = Engine(self.province, FakeTransport(self.clock, duration=1), instance=self.province.instance(),
                        clock=self.clock, account_alias="acct")
        self.addCleanup(engine.ledger.close)
        return engine


class ScopeExtension(Fixture):
    def test_the_order_only_grows_and_needs_the_users_words(self):
        ledger = self.engine().ledger
        self.assertEqual(ledger.handoff()["seeded"], [])
        with self.assertRaisesRegex(ValueError, "cannot_remove"):
            ledger.extend_scope(["export"], [], "同意")
        with self.assertRaisesRegex(ValueError, "approval_required"):
            ledger.extend_scope(["load", "export"], [], " ")
        with self.assertRaisesRegex(ValueError, "within_scope"):
            ledger.extend_scope(["load"], ["export"], "同意")
        ledger.extend_scope(["load", "export"], ["export"], "同意")
        self.assertEqual((ledger.handoff()["scope"], ledger.handoff()["seeded"]), (["load", "export"], ["export"]))
        with self.assertRaisesRegex(ValueError, "cannot_unseed"):
            ledger.extend_scope(["load", "export", "native"], [], "同意")
        self.assertEqual(len(ledger.handoff()["receipt_sha256"]), 64, "the receipt itself is unchanged")

    async def test_extended_datasets_seed_what_legacy_did_not_complete_and_new_days_daily(self):
        engine = self.engine()
        engine.ledger.extend_scope(["load", "export", "native", "strict"], ["export", "native", "strict"], "同意")
        spec = engine.province.datasets()["strict"]
        spec.source["skip_legacy_periods"] = True
        with patch.object(engine.province, "datasets", lambda: {**Province.datasets(engine.province), "strict": spec}):
            engine.seed()
        def seeded(name):
            return sorted(row["period"] for row in engine.ledger.db.execute("SELECT period FROM tasks WHERE dataset=?", (name,)))
        self.assertEqual(seeded("export"), sorted(DAYS[1:]), "the saved day is not seeded; the pending one is")
        self.assertEqual(seeded("native"), sorted(DAYS[1:]), "a day the export covered is complete for the query too")
        self.assertEqual(seeded("strict"), sorted([DAYS[0], DAYS[2], DAYS[3]]), "any legacy record skips it (page size)")
        self.assertEqual(seeded("load"), [DAYS[0]], "not seeded: only its legacy pending work")

    async def test_the_engine_sends_only_new_and_pending_periods_of_an_added_dataset(self):
        engine = self.engine()
        engine.ledger.extend_scope(["load", "export"], ["export"], "同意")
        engine.state.set_control(collect=True)
        for _ in range(6):
            await engine.tick()
            self.clock.advance(20)
        sent = [(r["path"], r["body"]["date"]) for r in engine.transport.sent]
        self.assertNotIn(("/px-test/export", DAYS[0]), sent, "saved by legacy: never sent")
        self.assertIn(("/px-test/export", DAYS[1]), sent)
        self.assertIsNone(engine.state.blocked())


class LateLegacyImport(Fixture):
    def test_consumed_tombstones_only_once_and_only_after_a_handoff(self):
        ledger = self.engine().ledger
        rows = [("m1", export_identity("export", DAYS[2]))]
        result = ledger.import_late_legacy("manifest:x", "b" * 64, rows, "同意")
        self.assertEqual(result["added"], 1)
        self.assertTrue(ledger.consumed_identity(export_identity("export", DAYS[2]).key))
        with self.assertRaisesRegex(ValueError, "already_done"):
            ledger.import_late_legacy("manifest:x", "b" * 64, rows, "同意")
        with self.assertRaisesRegex(ValueError, "approval_required"):
            ledger.import_late_legacy("manifest:y", "c" * 64, rows, "")
        fresh = Ledger(self.tmp.path / "fresh.sqlite3")
        self.addCleanup(fresh.close)
        with self.assertRaisesRegex(ValueError, "requires_a_handoff"):
            fresh.import_late_legacy("manifest:x", "b" * 64, rows, "同意")

    def test_a_manifest_line_becomes_the_fetch_specs_own_export_identity(self):
        from pmos.migration.shanxi import manifest_tombstones
        specs = Province.load("shanxi").datasets()
        manifest = self.tmp.path / "manifest.jsonl"
        lines = [{"dataset": "queryMarketClearingDA", "period": "2026-03-01", "sha256": "a" * 64, "path": "x/a.xlsx"},
                 {"dataset": "queryRetailMonthlyBalancingEnergy", "period": "2026-03", "sha256": "b" * 64, "path": "x/b.xlsx"},
                 {"dataset": "no_such_dataset", "period": "2026-03-01", "sha256": "c" * 64, "path": "x/c.xlsx"},
                 {"dataset": "queryMarketClearingDA", "period": "2026-03", "sha256": "d" * 64, "path": "x/d.xlsx"}]
        manifest.write_text("".join(json.dumps(x) + "\n" for x in lines) + '{"partial":')
        found = manifest_tombstones(manifest, specs)
        self.assertEqual(found["skipped"], {"no_spec": 1, "period_not_day": 1})
        keys = {identity.dataset: identity.key for _, identity in found["rows"]}
        for name, period in (("queryMarketClearingDA", "2026-03-01"), ("queryRetailMonthlyBalancingEnergy", "2026-03")):
            self.assertEqual(keys[name], task_identity("shanxi", "shanxi-production", specs[name], period, "").key, name)


class ShanxiFullYearPlan(unittest.TestCase):
    """provinces/shanxi/full_year_scope.json: the plan the user approved on 2026-10-31."""

    RECEIPT_SCOPE = ["menu_5d31d0fe28d6", "menu_60125ec016d3", "menu_fccfe1c6ef92", "pub_DDJG_DLBWYXGLKHHFHMX",
                     "pub_xxpl_sjjyjh", "pub_xxpl_gz_xxpl_qt_xxpl_qtdd", "pub_xxfb_yhsc", "pub_jyjg_zcqjycjztqk",
                     "candidate_df09f44e4a27", "candidate_9ed4f40e6fed", "candidate_495c3378e14d",
                     "candidate_6c19b801474c", "alt_tab_candidate_9cb0d3d052e5", "alt_tab_candidate_7440a7773d7e",
                     "alt_tab_candidate_f7296dd65da3", "alt_tab_candidate_b8c8f55fe60f", "querySectionConstraints",
                     "candidate_2e1f8d6d3b00", "pub_dwdlgdxx", "pub_sdgsjsztqk_26dce8de", "pub_jyjg_ydfhycjdldlphyc",
                     "pub_yddldlgxphyc", "pub_ydfsbdsbtctyjh_595a0a27"]

    def test_the_plan_keeps_the_receipt_scope_and_puts_the_heavy_per_object_work_last(self):
        plan = json.loads((REPO / "provinces/shanxi/full_year_scope.json").read_text(encoding="utf-8"))
        specs = Province.load("shanxi").datasets()
        self.assertTrue(set(self.RECEIPT_SCOPE) <= set(plan["order"]))
        self.assertTrue(all(name in specs for name in plan["order"]))
        self.assertTrue(all(specs[name].status in ("sampled", "verified", "enabled") for name in plan["seeded"]))
        self.assertEqual(plan["order"][-6:], plan["groups"]["per_object_legacy_pending_last"] + plan["groups"]["heavy_last"])
        self.assertFalse(set(plan["seeded"]) & set(plan["groups"]["per_object_legacy_pending_last"]),
                         "per-object history comes from the exports")
        self.assertFalse(set(plan["order"]) & set(plan["excluded"]))
        fetch = {n for n, s in specs.items() if s.request["transport"] == "fetch" and s.status == "verified"}
        self.assertEqual(fetch - set(plan["seeded"]), set(), "the whole export lane is seeded for the year")
        for name in plan["groups"]["released_deferred"]:
            self.assertEqual(specs[name].source.get("isolation_streak"), 1, name)


if __name__ == "__main__":
    unittest.main()
