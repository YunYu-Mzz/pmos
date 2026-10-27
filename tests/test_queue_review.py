"""Independent review regressions; fixtures only, no legacy directory writes."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pmos.ledger import Ledger
from pmos.contracts import TaskIdentity
from pmos.engine import Engine
from pmos.faults import Fault
from pmos.queue_compat import QueueContext, decode_liaoning, decode_native_request, decode_publication, import_shadow
from pmos.spec import DatasetSpec
from helpers import FakeTransport, make_province


class QueueReviewTests(unittest.TestCase):
    def setUp(self):
        self.context = QueueContext("liaoning", "review-fixture", period_kind="day")

    def test_liaoning_nested_failed_and_attempted_jobs_cannot_become_pending(self):
        for markers in ({"status": "failed"}, {"status": "denied"}, {"attempted": True},
                        {"issued": True}, {"sent": True}, {"attempted_at": 123}):
            with self.subTest(markers=markers):
                value = {"sources": {"curve": {"tasks": {"old": {
                    "period": "2026-10-18", "params": {}, **markers}}, "outcomes": {}}}}
                rows = decode_liaoning(value, self.context)
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0].consumed)
                self.assertNotEqual(rows[0].state, "pending")

    def test_liaoning_orphan_active_source_remains_unknown(self):
        value = {"sources": {}, "active": {"dataset": "orphan", "key": "already-issued",
                 "period": "2026-10-18", "params": {}}}
        rows = decode_liaoning(value, self.context)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].consumed)
        self.assertEqual(rows[0].state, "unknown")
        self.assertTrue(rows[0].blockers)

    def test_shadow_fence_is_enforced_by_the_atomic_begin_not_only_engine_precheck(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "new-offline-shadow.sqlite3")
            try:
                rows = decode_liaoning({"sources": {"curve": {"tasks": {}, "outcomes": {}}}}, self.context)
                # Simulates another owner importing after Engine's precheck.
                import_shadow(ledger, rows, self.context, source_id="fixture-export", snapshot_sha256="a" * 64)
                for kind in ("collect", "sample", "keepalive", "probe"):
                    with self.subTest(kind=kind):
                        with self.assertRaises(ValueError):
                            ledger.begin(kind)
                self.assertEqual(ledger.counts(0), {})
            finally:
                ledger.close()

    def test_old_directory_guard_runs_before_any_mkdir_or_sqlite_open(self):
        root = Path(__file__).resolve().parents[2]
        # Both possible writes are mocked: this regression remains harmless
        # even against an implementation that has lost its path guard.
        with patch.object(Path, "mkdir", side_effect=AssertionError("mkdir_before_path_guard")), \
             patch("sqlite3.connect", side_effect=AssertionError("sqlite_before_path_guard")):
            with self.assertRaisesRegex(ValueError, "legacy_directory"):
                Ledger(root / "liaoning" / "state" / "not-an-offline-target.sqlite3")

    def liaison_fixture(self):
        return {"sources": {"curve": {
            "tasks": {"different-old-key": {"period": "2026-10-18", "params": {"mdate": "2026-10-18", "type": 1}}},
            "outcomes": {"different-old-key": {"outcome": "saved", "identity": {"period": "2026-10-18", "selection": 1}}}
        }}}

    def test_liaoning_unreviewed_param_aliases_are_unmapped_instead_of_two_false_keys(self):
        row = decode_liaoning(self.liaison_fixture(), self.context)[0]
        self.assertIsNone(row.identity)
        self.assertTrue(row.blockers)

    def test_liaoning_reviewed_param_aliases_align_tasks_and_orphan_outcomes(self):
        value = self.liaison_fixture()
        contracts = {"curve": {"period_field": "mdate", "selection_field": "type"}}
        first = decode_liaoning(value, self.context, source_contracts=contracts)[0]
        value["sources"]["curve"]["tasks"] = {}
        orphan = decode_liaoning(value, self.context, source_contracts=contracts)[0]
        self.assertIsNotNone(first.identity)
        self.assertIsNotNone(orphan.identity)
        self.assertEqual(first.identity.key, orphan.identity.key)
        self.assertNotIn("mdate", first.identity.dimensions)

    def native_fixture(self):
        return {"identity": "old-native-fixture", "dataset": "curve", "period": "2026-10-18",
                "path": "/reviewed/fixture", "page": 1, "status": "sent",
                "body": {"data": {"dateTime": "2026-10-18", "type": 1}, "pageInfo": {"pageNum": 1, "pageSize": 20}},
                "contract": {"date_fields": ["dateTime"], "observed_request": {"body": {"pageInfo": {"pageNum": 1, "pageSize": 20}}}}}

    def assert_rejected_or_unmapped(self, row):
        try:
            result = decode_native_request(row, QueueContext("shanxi", "review-fixture", "curve", "day"))
        except ValueError:
            return
        self.assertIsNone(result.identity)
        self.assertTrue(result.blockers)

    def test_native_page_is_bound_to_the_actual_body_page(self):
        row = self.native_fixture()
        valid = decode_native_request(row, QueueContext("shanxi", "review-fixture", "curve", "day"))
        self.assertIsNotNone(valid.identity)
        row["body"]["pageInfo"]["pageNum"] = 2
        self.assert_rejected_or_unmapped(row)

    def test_native_page_size_change_or_missing_size_proof_is_not_a_mapped_identity(self):
        row = self.native_fixture()
        row["body"]["pageInfo"]["pageSize"] = 50
        self.assert_rejected_or_unmapped(row)
        row = self.native_fixture()
        row["contract"].pop("observed_request")
        self.assert_rejected_or_unmapped(row)

    def engine_fixture(self, root, province="shanxi"):
        config = replace(make_province(root, []), id=province)
        return Engine(config, FakeTransport(), instance=root / "offline-instance", account_alias="review-fixture")

    def spec_fixture(self):
        return DatasetSpec("curve", "脱敏身份夹具", "candidate",
            {"kind": "day", "start": "2026-10-01", "end": "today"},
            {"transport": "fetch", "method": "POST", "path": "/reviewed/fixture",
             "body": {"data": {"dateTime": "{date}", "type": 1}, "pageInfo": {"pageNum": 1, "pageSize": 20}},
             "pagination": {"page_size": 20}}, {"kind": "json"},
            source={"identity_contract": {"request_data_path": ["body", "data"], "date_fields": ["dateTime"], "page_size": 20}})

    def test_native_decoder_and_engine_share_the_full_reviewed_page_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = self.engine_fixture(Path(folder))
            try:
                legacy = decode_native_request(self.native_fixture(), QueueContext("shanxi", "review-fixture", "curve", "day"))
                current = engine.task_identity(self.spec_fixture(), "2026-10-18", page=1)
                self.assertEqual(legacy.identity.key, current.key)
                self.assertEqual(current.dimensions["_page_size"], 20)
            finally:
                engine.ledger.close()

    def test_engine_rejects_conflicting_rule_and_actual_pagination_size(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = self.engine_fixture(Path(folder))
            try:
                spec = self.spec_fixture()
                spec.request["pagination"]["page_size"] = 50
                with self.assertRaises(Fault):
                    engine.task_identity(spec, "2026-10-18", page=1)
            finally:
                engine.ledger.close()

    def test_liaoning_decoder_and_engine_share_the_reviewed_selection_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = self.engine_fixture(Path(folder), "liaoning")
            try:
                legacy = decode_liaoning(self.liaison_fixture(), self.context,
                    source_contracts={"curve": {"period_field": "mdate", "selection_field": "type"}})[0]
                spec = self.spec_fixture()
                spec.source = {}
                spec.request.pop("pagination")
                spec.scopes = [{"type": 1}]
                scope = spec.scope_items()[0][0]
                current = engine.task_identity(spec, "2026-10-18", scope)
                self.assertEqual(legacy.identity.key, current.key)
            finally:
                engine.ledger.close()

    def test_file_identity_is_shared_with_legacy_and_independent_of_title_period_or_record(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = self.engine_fixture(Path(folder))
            try:
                spec = self.spec_fixture()
                spec.documents = {"record_only": False}
                item = {"record_id": "first", "period": "2026-09", "attachment": {"id": "fixture-file"}}
                legacy = decode_publication({"identity": "old-file", "kind": "attachment", "status": "denied_scope",
                    "command": {"query": {"fileId": "fixture-file"}}}, QueueContext("shanxi", "review-fixture", "curve", "day"))
                current = engine.document_identity(spec, item)
                later = engine.document_identity(spec, {**item, "record_id": "another", "period": "2026-10"})
                self.assertEqual(legacy.identity.key, current.key)
                self.assertEqual(current.key, later.key)
            finally:
                engine.ledger.close()

    def test_unknown_file_tombstone_blocks_an_alias_dataset_but_does_not_cross_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "offline-files.sqlite3")
            try:
                first = TaskIdentity("shanxi", "review-fixture", "first-source", {"kind": "snapshot", "anchor": "undated"},
                                     {"file_id": "fixture-file"}, operation="attachment")
                alias = replace(first, dataset="alias-source")
                other_account = replace(first, account_alias="other-account")
                attempt = ledger.begin("collect", first.dataset, "2026-09", identity=first)
                ledger.finish(attempt, "unknown")
                self.assertNotEqual(first.key, alias.key)
                self.assertEqual(first.file_key, alias.file_key)
                self.assertNotEqual(first.file_key, other_account.file_key)
                self.assertTrue(ledger.consumed_file(alias.file_key))
                with self.assertRaisesRegex(ValueError, "file_already_consumed"):
                    ledger.begin("collect", alias.dataset, "2026-10", identity=alias)
                self.assertEqual(ledger.counts(0), {"collect": 1})
            finally:
                ledger.close()

    def test_consumed_legacy_file_creates_tombstone_but_pending_file_does_not(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "offline-shadow.sqlite3")
            try:
                context = QueueContext("shanxi", "review-fixture", "curve", "day")
                def row(file_id, status):
                    return decode_publication({"identity": file_id, "kind": "attachment", "status": status,
                        "command": {"query": {"fileId": file_id}}}, context)
                denied, pending = row("fixture-denied", "denied_scope"), row("fixture-pending", "pending")
                import_shadow(ledger, [denied, pending], context, source_id="fixture-files", snapshot_sha256="b" * 64)
                self.assertTrue(ledger.consumed_file(denied.identity.file_key))
                self.assertFalse(ledger.consumed_file(pending.identity.file_key))
            finally:
                ledger.close()
