"""Compatibility must preserve every consumed/unknown old identity offline."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest

from pmos.contracts import TaskIdentity, canonical
from pmos.engine import Engine
from pmos.faults import Fault
from pmos.ledger import Ledger
from pmos.queue_compat import (QueueContext, decode_journal, decode_jobs, decode_liaoning,
    decode_frontier, decode_native_request, decode_publication, import_shadow, read_json,
    read_native_sqlite, summarize)
from helpers import Clock, FakeTransport, TempDir, dataset, make_province


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.context = QueueContext("shanxi", "offline-account", "load", "day")
        self.ledger = Ledger(self.tmp.path / "shadow" / "ledger.sqlite3")
        self.addCleanup(self.ledger.close)

    def journal(self):
        return {"schema_version": 1, "dataset": "load", "granularity": "day",
                "dates": ["2026-10-01", "2026-10-02", "2026-10-03"],
                "outcomes": {"2026-10-01": "saved", "2026-10-02": "empty"},
                "batch": {"dates": ["2026-10-03"], "status": "acknowledged", "confirmed": {}}}

    def native_row(self, state="sent", page=1):
        return {"identity": "old-native-id", "dataset": "load", "period": "2026-10-18", "path": "/reviewed/source",
                "body": {"data": {"dateTime": "2026-10-18", "node": "synthetic-node"},
                         "pageInfo": {"pageNum": page, "pageSize": 20, "total": 50, "pages": 0}},
                "contract": {"date_fields": ["dateTime"], "reviewed_page_size": 20}, "page": page, "status": state}

    def test_equivalent_period_spellings_share_the_business_key(self):
        a = TaskIdentity("shanxi", "account", "load", {"kind": "day", "label": "2026-10-18"})
        b = replace(a, period={"kind": "day", "start": "2026-10-18", "end": "2026-10-18"})
        self.assertEqual(a.key, b.key)
        with self.assertRaises(ValueError):
            replace(a, period={"kind": "day", "label": "2026-10-18", "end": "2026-10-19"}).key

    def test_batch_ack_without_outcome_never_makes_reserved_day_pending(self):
        rows = decode_journal(self.journal(), self.context)
        self.assertEqual([row.state for row in rows], ["saved", "empty", "unknown"])
        self.assertTrue(rows[-1].consumed)
        self.assertFalse(summarize(rows)["dispatch_allowed"])

    def test_crawlee_month_and_year_are_not_daily_tasks(self):
        for kind, label in (("month", "2026-09"), ("year", "2026")):
            value = {**self.journal(), "dates": [label], "outcomes": {}, "batch": None, "granularity": kind}
            row = decode_journal(value, self.context)[0]
            self.assertEqual(row.identity.document()["period"], {"kind": kind, "label": label})

    def test_conflicting_confirmed_and_outcome_is_held_unknown(self):
        value = self.journal()
        value["batch"] = {"dates": ["2026-10-01"], "status": "completed", "confirmed": {"2026-10-01": "empty"}}
        row = decode_journal(value, self.context)[0]
        self.assertEqual(row.state, "unknown")
        self.assertIn("conflicting_legacy_outcomes", row.blockers)

    def test_anhui_attempt_index_overrides_pending_and_keeps_orphans(self):
        context = QueueContext("anhui", "offline-account", "curve", "day")
        value = {"jobs": {"a": {"period": "2026-10-18", "status": "pending"},
                           "b": {"period": "2026-10-19", "status": "pending", "attempted": True}}}
        attempts = {"a": {"phase": "failed", "period": "2026-10-18"},
                    "orphan": {"phase": "attempting", "period": "2026-10-20"}}
        rows = decode_jobs(value, context, attempts=attempts)
        self.assertEqual([row.state for row in rows], ["failed", "unknown", "unknown"])
        self.assertIn("orphan_attempt_preserved", rows[-1].blockers)
        self.assertIn("attempt_index_required", decode_jobs(value, context)[0].blockers)

    def test_liaoning_outcome_not_in_pending_is_still_preserved(self):
        context = QueueContext("liaoning", "offline-account", period_kind="day")
        value = {"sources": {"curve": {"pending": [{"key": "next", "dataset": "curve", "period": "2026-10-19", "params": {"type": 1}}],
                   "outcomes": {"old": {"outcome": "saved", "identity": {"period": "2026-10-18", "selection": 1}}}, "hold": "denied"}},
                 "active": {"dataset": "curve", "key": "next", "period": "2026-10-19"}}
        rows = decode_liaoning(value, context)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("legacy_source_hold" in row.blockers for row in rows))
        self.assertEqual(rows[0].state, "unknown")
        self.assertEqual(rows[1].state, "saved")

    def test_liaoning_engine_stage_frontier_retains_unfinished_job(self):
        value = {"dataset": "curve", "pending": [{"key": "j", "period": "2026-10-18"}], "outcomes": {},
                 "active": {"job": {"key": "j", "period": "2026-10-18"}, "stage": "load_report_content"}}
        row = decode_frontier(value, QueueContext("liaoning", "account", "curve", "day"))[0]
        self.assertEqual(row.state, "unknown")

    def test_native_ignores_ui_total_but_keeps_typed_scope_and_real_page(self):
        a = decode_native_request(self.native_row(), self.context)
        raw = self.native_row()
        raw["body"]["pageInfo"]["total"] = 999
        b = decode_native_request(raw, self.context)
        self.assertEqual(a.identity.key, b.identity.key)
        self.assertNotEqual(a.identity.key, decode_native_request(self.native_row(page=2), self.context).identity.key)
        raw["body"]["data"]["dateTime"] = "2026-10-17"
        with self.assertRaisesRegex(ValueError, "period_identity"):
            decode_native_request(raw, self.context)

    def test_attachment_capture_anchor_is_not_part_of_its_business_key(self):
        raw = {"identity": "publication-id", "kind": "attachment", "status": "denied_scope",
               "command": {"query": {"fileId": "fixture-file"}}}
        a = decode_publication(raw, self.context)
        self.assertEqual(a.identity.document()["period"], {"kind": "snapshot", "anchor": "undated"})
        self.assertTrue(a.consumed)
        b = decode_publication(raw, replace(self.context, snapshot_anchor="2026-10-18T12:00:00+08:00"))
        self.assertEqual(b.identity.document()["period"]["kind"], "snapshot")
        self.assertEqual(b.identity.operation, "attachment")
        self.assertEqual(a.identity.key, b.identity.key)

    def test_atomic_idempotent_shadow_import_preserves_all_states(self):
        rows = decode_journal(self.journal(), self.context)
        for _ in range(2):
            import_shadow(self.ledger, rows, self.context, source_id="old-queue", snapshot_sha256="a" * 64)
        self.assertEqual(self.ledger.legacy_summary()["states"], {"empty": 1, "saved": 1, "unknown": 1})
        self.assertTrue(self.ledger.consumed_identity(rows[2].identity.key))
        self.assertEqual(self.ledger.summary(), {}, "shadow never seeds runnable tasks")

    def test_new_snapshot_pending_cannot_erase_earlier_unknown_tombstone(self):
        rows = decode_journal(self.journal(), self.context)
        import_shadow(self.ledger, rows, self.context, source_id="old", snapshot_sha256="a" * 64)
        later = [replace(rows[-1], state="pending", consumed=False)]
        import_shadow(self.ledger, later, self.context, source_id="old", snapshot_sha256="b" * 64)
        self.assertTrue(self.ledger.consumed_identity(rows[-1].identity.key))
        self.assertEqual(self.ledger.legacy_summary()["snapshots"], 2)

    def test_shadow_import_cannot_take_over_an_already_dispatched_ledger(self):
        self.ledger.begin("collect", "load", "2026-10-18")
        with self.assertRaisesRegex(ValueError, "fresh_shadow"):
            import_shadow(self.ledger, [], self.context, source_id="old", snapshot_sha256="a" * 64)
        self.assertFalse(self.ledger.has_legacy_shadow())
        self.assertEqual(len(self.ledger.unsettled()), 1)

    def test_account_isolation_and_mapping_changes_are_rejected_atomically(self):
        rows = decode_journal(self.journal(), self.context)
        import_shadow(self.ledger, rows, self.context, source_id="old", snapshot_sha256="a" * 64)
        with self.assertRaisesRegex(ValueError, "mapping_changed"):
            import_shadow(self.ledger, [replace(rows[0], state="unknown")], self.context, source_id="old", snapshot_sha256="a" * 64)
        with self.assertRaisesRegex(ValueError, "account"):
            import_shadow(self.ledger, [], replace(self.context, account_alias="other"), source_id="other", snapshot_sha256="b" * 64)
        self.assertEqual(self.ledger.legacy_summary()["snapshots"], 1)

    def test_json_source_is_unchanged_and_expected_hash_is_required(self):
        path = self.tmp.path / "journal.json"
        raw = canonical(self.journal())
        path.write_bytes(raw)
        value, digest = read_json(path, expected_sha256=hashlib.sha256(raw).hexdigest())
        self.assertEqual(value, self.journal())
        self.assertEqual(path.read_bytes(), raw)
        with self.assertRaisesRegex(ValueError, "sha256_changed"):
            read_json(path, expected_sha256="0" * 64)

    def test_native_sqlite_reads_only_short_batches_without_source_mutation(self):
        path = self.tmp.path / "legacy.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE requests(identity,dataset,period,path,body,contract,page,status)")
        raw = self.native_row()
        db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)", (raw["identity"], raw["dataset"], raw["period"], raw["path"],
                   json.dumps(raw["body"]), json.dumps(raw["contract"]), raw["page"], raw["status"]))
        db.commit()
        db.close()
        before = path.read_bytes()
        rows = read_native_sqlite(path, self.context, batch_size=1)
        self.assertEqual(rows[0].state, "unknown")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_name(path.name + "-wal").exists())

    def test_live_wal_store_requires_a_separately_frozen_export(self):
        path = self.tmp.path / "legacy.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE requests(x)")
            db.commit()
            with self.assertRaisesRegex(ValueError, "requires_frozen_export"):
                read_native_sqlite(path, self.context)
        finally:
            db.close()


class EngineIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_index_cannot_dispatch_even_new_identity_or_keeper(self):
        tmp = TempDir()
        try:
            province = make_province(tmp.path, [dataset("load")])
            transport = FakeTransport(Clock())
            engine = Engine(province, transport, instance=tmp.path / "new-instance")
            # testland is a fake province: an actual migration owner is still
            # isolated in its ledger and all outbound paths are gated.
            context = QueueContext("shanxi", "account", "load", "day")
            row = decode_native_request(CompatibilityTests.native_row(self), context)
            import_shadow(engine.ledger, [row], context, source_id="old", snapshot_sha256="a" * 64)
            try:
                for kind in ("collect", "sample", "keepalive", "probe"):
                    with self.assertRaisesRegex(Fault, "requires_owner_handoff"):
                        await engine.request(kind, {"transport": "fetch", "path": "/offline"}, {"kind": "json"})
                self.assertEqual(transport.sent, [])
            finally:
                engine.ledger.close()
        finally:
            tmp.cleanup()
