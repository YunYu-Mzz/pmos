import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from pmos.adapters.native import decode, FIELDS
from pmos.archive import Archive, ArchiveError
from pmos.browser.transport import Templates
from pmos.cli import main
from pmos.contracts import TaskIdentity, canonical
from pmos.engine import Engine
from pmos.faults import Fault, decide
from pmos.legacy import review_manifest
from pmos.offline import demo, load_contract, replay
from pmos.reporting import coverage_item, ledger_snapshot, read_batch
from pmos.resources import MemoryObservation, admission
from pmos.validate import Response, evaluate
from helpers import Clock, FakeTransport, TempDir, dataset, make_province

FIXTURES = Path(__file__).parent / "fixtures"


class OfflineContracts(unittest.TestCase):
    def test_province_and_typed_dimensions_are_part_of_identity(self):
        base = TaskIdentity("liaoning", "account-a", "load", {"kind": "day", "start": "2026-10-18"})
        cases = [base, replace(base, province="anhui"), replace(base, account_alias="account-b"),
                 replace(base, dimensions={"node": 1}), replace(base, dimensions={"node": "1"}),
                 replace(base, page=1), replace(base, operation="export")]
        self.assertEqual(len({v.key for v in cases}), len(cases))
        self.assertEqual(replace(base, dimensions={"b": 1, "a": 2}).key,
                         replace(base, dimensions={"a": 2, "b": 1}).key)

    def test_identity_never_accepts_credentials(self):
        with self.assertRaisesRegex(ValueError, "credential"):
            TaskIdentity("liaoning", "a", "load", {"kind": "day"}, {"access_token": "synthetic"}).key

    def test_three_shapes_keep_zero_negative_missing_and_do_not_claim_full_coverage(self):
        for pid in ("liaoning", "anhui", "shanxi"):
            value = decode((FIXTURES / (pid + "_native.json")).read_bytes(), load_contract(pid))
            self.assertFalse(value.summary()["full_dataset_coverage_verified"])
            if pid != "shanxi":
                self.assertEqual(len(value.rows), 96)
                self.assertIn("0", [row["value"] for row in value.rows])
                self.assertTrue(any(row["value"].startswith("-") for row in value.rows))
                self.assertEqual(value.rows[-1]["time"], "24:00")
            else:
                self.assertIsNone(value.rows[-1]["ELECTRICITY_VALUE"])
                self.assertEqual(value.pagination["expected_pages"], 1)
                self.assertEqual(value.pagination["reported_pages"], 0)

    def test_nested_http200_auth_denial_and_unknown_fail_closed(self):
        for status, category in ((401, "auth"), (40302, "permission"), (500, "unknown"), (False, "unknown")):
            raw = canonical({"status": 0, "data": {"status": status, "message": "", "data": None}})
            with self.assertRaises(Fault) as caught:
                evaluate({"kind": "json"}, Response(200, "application/json", raw))
            self.assertEqual(caught.exception.category, category)

    def test_capture_cannot_bypass_http_auth(self):
        with self.assertRaises(Fault) as caught:
            evaluate({}, Response(401, "application/x-pmos-capture+json", b"{}"))
        self.assertEqual(caught.exception.category, "auth")

    def test_json_business_error_with_binary_mime_cannot_be_saved_as_excel(self):
        with self.assertRaises(Fault) as caught:
            evaluate({"kind": "excel"}, Response(200, "application/octet-stream", b'{"status":40302,"data":null}'))
        self.assertEqual(caught.exception.category, "permission")

    def test_missing_curve_point_or_boolean_is_rejected(self):
        original = json.loads((FIXTURES / "anhui_native.json").read_text())
        for mutation in ("missing", "boolean"):
            value = json.loads(json.dumps(original))
            if mutation == "missing":
                value["data"].pop(FIELDS[0])
            else:
                value["data"][FIELDS[0]] = False
            with self.assertRaises(Fault):
                decode(canonical(value), load_contract("anhui"))

    def test_short_nonterminal_page_is_rejected(self):
        value = json.loads((FIXTURES / "shanxi_native.json").read_text())
        value["data"]["total"] = 40
        with self.assertRaisesRegex(Fault, "page_count_mismatch"):
            decode(canonical(value), load_contract("shanxi"))

    def test_replay_requires_sha_and_does_not_modify_source(self):
        path = FIXTURES / "liaoning_native.json"
        before = path.read_bytes()
        result = replay("liaoning", path, hashlib.sha256(before).hexdigest())
        self.assertEqual(result["rows"], 96)
        self.assertEqual(result["website_requests"], 0)
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "sha256"):
            replay("liaoning", path, "0" * 64)

    def test_live_cli_commands_fail_before_browser_or_state_mutation(self):
        with patch("pmos.cli._browser", side_effect=AssertionError("must not attach")):
            for cmd in ("run", "capture", "start", "resume", "stop"):
                with self.subTest(command=cmd):
                    self.assertEqual(main(["shanxi", cmd]), 2)

    def test_memory_admission_retains_session_and_prioritizes_owned_request(self):
        value = MemoryObservation(100, 200, 10, 50)
        self.assertEqual(admission(value), "admit")
        self.assertEqual(admission(replace(value, charged_bytes=300)), "resource_hold_retain_session")
        self.assertEqual(admission(replace(value, charged_bytes=None)), "wait_measurement_unknown")
        self.assertEqual(admission(replace(value, in_flight=True, charged_bytes=300)), "wait_owned_operation")

    def test_permission_scope_needs_explicit_policy_and_generic_local_error_cannot_keepalive(self):
        self.assertEqual(decide(Fault("permission_denied", "permission"), "collect").scope, "province")
        exact = Fault("permission_denied", "permission", http_status=200, business_status="40302")
        self.assertEqual(decide(exact, "collect", permission_scope="dataset").scope, "dataset")
        self.assertEqual(decide(Fault("permission_denied", "permission", http_status=403), "collect", permission_scope="dataset").scope, "province")
        self.assertFalse(decide(Fault("internal_error", "local"), "collect").keepalive)


class DurableEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.province = make_province(self.tmp.path, [dataset("load", keepalive=True)])
        self.transport = FakeTransport(self.clock)
        self.engine = Engine(self.province, self.transport, instance=self.tmp.path / "instance", clock=self.clock)
        self.addCleanup(self.engine.ledger.close)
        self.spec = self.province.datasets()["load"]

    async def test_shared_engine_demo_all_provinces(self):
        for pid in ("liaoning", "anhui", "shanxi"):
            result = await demo(pid)
            self.assertTrue(result["archive_verified"])
            self.assertEqual(result["task_status"], "saved")
            self.assertEqual(result["fixture_dispatches"], 1)
            self.assertEqual(result["reseed_added"], 0)

    async def test_archive_checkpoint_crash_never_reissues_consumed_task(self):
        self.engine.ledger.seed("load", ["2026-10-18"])
        with patch.object(self.engine.archive, "store", side_effect=OSError("synthetic_disk_failure")):
            with self.assertRaises(OSError):
                await self.engine.collect(self.spec, "2026-10-18")
        self.assertEqual(len(self.engine.responses.records()), 1)
        self.clock.advance(30)
        with self.assertRaisesRegex(Fault, "consumed_identity"):
            await self.engine.collect(self.spec, "2026-10-18")
        self.assertEqual(len(self.transport.sent), 1)

    async def test_unknown_even_after_operator_settlement_is_not_retried(self):
        attempt = self.engine.ledger.begin("collect", "load", "2026-10-18")
        self.engine.ledger.settle_unknown(attempt)
        self.clock.advance(30)
        with self.assertRaisesRegex(Fault, "consumed_identity"):
            await self.engine.request("collect", self.spec.build("2026-10-18"), self.spec.response,
                                      dataset="load", period="2026-10-18")
        self.assertEqual(self.transport.sent, [])

    async def test_template_missing_does_not_create_dispatched_attempt(self):
        self.transport.unready.add("/px-test/load")
        with self.assertRaisesRegex(Fault, "template_missing"):
            await self.engine.request("collect", self.spec.build("2026-10-18"), self.spec.response,
                                      dataset="load", period="2026-10-18")
        self.assertEqual(self.engine.ledger.counts(0), {})

    async def test_decoder_failure_never_confirms_business_task(self):
        contract = load_contract("anhui")
        self.spec.response = {"kind": "json"}
        self.engine.decode_response = lambda body: decode(body, contract)
        self.transport.default = Response(200, "application/json", b'{"status":0,"data":{"v0015":0}}')
        self.engine.ledger.seed("load", ["2026-10-18"])
        with self.assertRaisesRegex(Fault, "curve_fields_changed"):
            await self.engine.collect(self.spec, "2026-10-18")
        self.assertEqual(self.engine.ledger.task("load", "2026-10-18")["status"], "pending")
        self.assertEqual(len(self.engine.quarantine.records()), 1)
        self.assertEqual(self.engine.archive.records(), [])

    async def test_pending_attempt_overrides_local_hold_keepalive(self):
        self.engine.ledger.begin("collect", "load", "2026-10-18")
        self.engine.state.block("request_template_missing", category="local", keepalive_allowed=True)
        await self.engine.tick()
        self.assertFalse(self.engine.state.read("hold")["keepalive"])
        self.assertEqual(self.transport.sent, [])

    async def test_validation_failure_preserves_quarantined_bytes(self):
        self.transport.default = Response(200, "application/vnd.ms-excel", b"broken workbook")
        with self.assertRaises(Fault):
            await self.engine.request("collect", self.spec.build("2026-10-18"), self.spec.response,
                                      dataset="load", period="2026-10-18")
        self.assertEqual(len(self.engine.quarantine.records()), 1)
        self.assertEqual(self.engine.archive.records(), [])

    async def test_one_worker_lock_rejects_second_owner(self):
        self.engine.acquire()
        other = Engine(self.province, self.transport, instance=self.engine.instance)
        try:
            with self.assertRaisesRegex(RuntimeError, "another worker"):
                other.acquire()
        finally:
            other.ledger.close()
            # Close only the descriptor; addCleanup owns this test's ledger.
            import os
            os.close(self.engine._lock_fd)
            self.engine._lock_fd = None

    async def test_snapshot_closes_read_connection_before_slow_file_work(self):
        self.engine.ledger.begin("probe")
        rows = read_batch(self.engine.ledger.path)
        # A long-lived returned Python list must not retain a SELECT cursor.
        self.engine.ledger.seed("load", ["2026-10-18"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(ledger_snapshot(self.engine.ledger.path)["tasks"]["load"]["pending"], 1)


class EvidenceAndResources(unittest.TestCase):
    def test_missing_status_does_not_create_database(self):
        with __import__("tempfile").TemporaryDirectory() as folder:
            path = Path(folder) / "missing" / "ledger.sqlite3"
            self.assertFalse(ledger_snapshot(path)["exists"])
            self.assertFalse(path.parent.exists())

    def test_coverage_counts_source_paths_and_keeps_empty_separate(self):
        a = {"path": "source.json", "sha256": "a" * 64, "size": 5, "role": "source",
             "sha256_verified": True, "task_key": "task", "empty": True}
        result = coverage_item("样本", ["load"], [a, a, {**a, "role": "parsed"}])
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["bytes"], 5)
        self.assertEqual(result["unique_periods"], 0)

    def test_archive_refuses_traversal(self):
        with __import__("tempfile").TemporaryDirectory() as folder:
            store = Archive(folder, "test")
            with self.assertRaises(ArchiveError):
                store.store("load", "../../escape", b"x", ".json")
            with self.assertRaises(ArchiveError):
                store.verify({"path": "../escape", "sha256": "x"})

    def test_legacy_manifest_hash_review_is_read_only_and_not_queue_admission(self):
        with __import__("tempfile").TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "old.json").write_bytes(b"{}")
            digest = hashlib.sha256(b"{}").hexdigest()
            manifest = root / "manifest.jsonl"
            manifest.write_text(json.dumps({"dataset": "load", "period": "2026-10-18", "path": "old.json", "sha256": digest}) + "\n")
            result = review_manifest(manifest, root, {k: k for k in ("dataset", "period", "path", "sha256")})
            self.assertFalse(result[0]["queue_import_ready"])
            self.assertEqual((root / "old.json").read_bytes(), b"{}")

    def test_template_cache_evicts_expired_detached_and_over_limit_refs(self):
        class Frame:
            def is_detached(self):
                return False
        clock = Clock()
        cache = Templates("https://offline.invalid", clock, max_items=2, ttl=10)
        for n in range(3):
            cache.items[str(n)] = {"at": clock(), "frame": Frame()}
            clock.advance(1)
        cache.prune()
        self.assertEqual(len(cache.items), 2)
        clock.advance(11)
        cache.prune()
        self.assertEqual(cache.items, {})

    def test_short_read_releases_rollback_journal_lock_before_file_io(self):
        with __import__("tempfile").TemporaryDirectory() as folder:
            path = Path(folder) / "queue.sqlite3"
            writer = sqlite3.connect(path, timeout=0.01)
            reader = sqlite3.connect(path, timeout=0.01)
            try:
                writer.execute("CREATE TABLE attempts(id INTEGER PRIMARY KEY, outcome TEXT)")
                writer.executemany("INSERT INTO attempts(outcome) VALUES(?)", [("success",)] * 500)
                writer.commit()
                cursor = reader.execute("SELECT * FROM attempts")
                cursor.fetchone()
                writer.execute("UPDATE attempts SET outcome='empty' WHERE id=1")
                with self.assertRaises(sqlite3.OperationalError):
                    writer.commit()
                writer.rollback()
                cursor.close()
                reader.close()
                rows = read_batch(path, limit=100)
                # Returned rows remain alive while the writer commits.
                writer.execute("UPDATE attempts SET outcome='empty' WHERE id=1")
                writer.commit()
                self.assertEqual(len(rows), 100)
            finally:
                reader.close()
                writer.close()
