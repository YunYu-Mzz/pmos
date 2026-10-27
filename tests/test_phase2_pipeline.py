"""All province decoders use the same durable Engine; no copied workers."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import unittest

from pmos.cli import main
from pmos.contracts import canonical
from pmos.engine import Engine
from pmos.faults import Fault
from pmos.offline import province_decoder
from pmos.queue_compat import QueueContext, decode_jobs
from pmos.validate import Response
from helpers import Clock, FakeTransport, TempDir, dataset, make_province


class PhaseTwoPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_three_province_decoders_complete_the_same_engine_protocol(self):
        for province in ("liaoning", "anhui", "shanxi"):
            with self.subTest(province=province):
                temporary = TempDir()
                try:
                    spec = dataset("native_records", response="json")
                    config = make_province(temporary.path, [spec])
                    config.id = province
                    data = [{"value": 0, "negative": -2, "missing": None}]
                    if province == "shanxi":
                        data = {"list": data, "pageNum": 1, "pageSize": 20, "total": 1, "pages": 0}
                        contract = {"shape": "paged_records", "request_page": 1, "request_page_size": 20}
                    elif province == "anhui":
                        contract = {"model": {"kind": "observed_records", "path": ["data"]}, "success_code": 0}
                    else:
                        contract = {"shape": "records", "rows_path": ["data"], "success_statuses": [0]}
                    raw = canonical({"status": 0, "data": data})
                    clock = Clock()
                    transport = FakeTransport(clock, Response(200, "application/json", raw))
                    decoder = province_decoder(province)
                    engine = Engine(config, transport, instance=temporary.path / "instance", clock=clock,
                                    decode_response=lambda body: decoder(body, contract))
                    try:
                        engine.ledger.seed("native_records", ["2026-10-18"])
                        result = await engine.collect(config.datasets()["native_records"], "2026-10-18")
                        self.assertEqual(len(result.parsed.rows), 1)
                        self.assertTrue(engine.archive.verify(result.record))
                        self.assertEqual(engine.ledger.task("native_records", "2026-10-18")["status"], "saved")
                        self.assertEqual(engine.ledger.db.execute("SELECT COUNT(*) FROM attempt_identities").fetchone()[0], 1)
                        clock.advance(30)
                        with self.assertRaisesRegex(Fault, "consumed_identity"):
                            await engine.collect(config.datasets()["native_records"], "2026-10-18")
                        self.assertEqual(len(transport.sent), 1)
                    finally:
                        engine.ledger.close()
                finally:
                    temporary.cleanup()


class OfflineCliAndConservativeFacts(unittest.TestCase):
    def test_queue_review_is_readonly_and_prints_only_aggregate_qualification(self):
        temporary = TempDir()
        try:
            path = temporary.path / "batch_journal.json"
            raw = canonical({"schema_version": 1, "dataset": "curve", "granularity": "day",
                             "dates": ["2026-10-18"], "outcomes": {}, "batch": None})
            path.write_bytes(raw)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["legacy-review", "shanxi", "journal", str(path), "--sha256",
                             hashlib.sha256(raw).hexdigest(), "--account-alias", "offline-fixture"])
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(output.getvalue())["dispatch_allowed"])
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(list(temporary.path.iterdir()), [path])
        finally:
            temporary.cleanup()

    def test_pending_with_saved_response_or_failure_reason_is_not_fresh_work(self):
        context = QueueContext("anhui", "offline-fixture", "curve", "day")
        for marker in ({"reason": "prior_failure"}, {"native_receipt": {"source_sha256": "a" * 64}},
                       {"original": "old-source.json"}, {"source_path": "old-source.json"}):
            value = {"jobs": {"old": {"period": "2026-10-18", "status": "pending", **marker}}}
            task = decode_jobs(value, context, attempts={})[0]
            self.assertTrue(task.consumed)
            self.assertEqual(task.state, "unknown")
