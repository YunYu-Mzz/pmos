"""Full-year page size: a period keeps the size it was begun at and resumes after the legacy owner's pages."""
import json
import os
import unittest
from unittest.mock import patch

from pmos import paging
from pmos.engine import Engine, task_identity
from pmos.ledger import Ledger
from pmos.periods import today
from pmos.queue_compat import LegacyTask, QueueContext, import_shadow
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir, make_province

DAYS = [today().fromordinal(today().toordinal() - n).isoformat() for n in range(2)]   # newest first


def page(records, last):
    body = {"status": 0, "data": {"list": [{"id": n} for n in range(records)], "isLastPage": last}}
    return Response(200, "application/json", json.dumps(body).encode())


class PeriodPageSize(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(self.tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = {"id": "prices", "name": "prices", "status": "enabled",
                "period": {"kind": "day", "start": DAYS[-1], "end": "today"},
                "request": {"transport": "fetch", "method": "POST", "path": "/px-test/prices",
                            "body": {"data": {"date": "{date}"}, "pageInfo": {"pageNum": 1, "pageSize": 1000}},
                            "pagination": {"page": "pageInfo.pageNum", "size": "pageInfo.pageSize", "page_size": 1000,
                                           "records": "data.list", "last": "data.isLastPage", "max_pages": 50}},
                "response": {"kind": "json"},
                "source": {"identity_contract": {"body": {"data": {"date": "{date}"}}, "date_fields": ["date"],
                                                 "page_size": 1000}}}
        self.province = make_province(self.tmp.path, [spec], policy={"response_interval_seconds": 15})
        prices = self.province.datasets()["prices"].with_page_size(20)
        legacy = [LegacyTask(f"p{n}", "prices", "saved", "saved", True,
                             task_identity("testland", "acct", prices, DAYS[0], "", page=n)) for n in (1, 2)]
        instance = self.province.instance()
        instance.mkdir(parents=True)
        ledger = Ledger(instance / "ledger.sqlite3")
        with patch("pmos.queue_compat.PROVINCES", {"testland", "liaoning", "anhui", "shanxi"}):
            import_shadow(ledger, legacy, QueueContext("testland", "acct"), source_id="queue", snapshot_sha256="a" * 64)
        ledger.grant_handoff({"version": 1, "province": "testland", "account_alias": "acct",
                              "legacy_owner": {"stopped_evidence": "fixture"},
                              "snapshots": [{"source_id": "queue", "snapshot_sha256": "a" * 64}], "scope": ["prices"],
                              "request_budget": 100, "code_sha": "c", "approval": "fixture",
                              "session": {}, "seeding": "legacy_pending_only"})
        ledger.seed("prices", DAYS)
        ledger.close()
        self.transport = FakeTransport(self.clock, duration=1)
        self.engine = Engine(self.province, self.transport, instance=instance, clock=self.clock, account_alias="acct")
        self.addCleanup(self.engine.ledger.close)
        self.engine.state.set_control(collect=True)

    async def ticks(self, count):
        for _ in range(count):
            await self.engine.tick()
            self.clock.advance(20)

    async def test_a_legacy_begun_period_resumes_at_its_size_and_a_new_one_uses_the_spec_size(self):
        self.transport.queue += [page(3, True), page(7, True)]
        await self.ticks(2)
        asked = [(r["body"]["data"]["date"], r["body"]["pageInfo"]["pageNum"], r["body"]["pageInfo"]["pageSize"])
                 for r in self.transport.sent]
        self.assertEqual(asked, [(DAYS[0], 3, 20), (DAYS[1], 1, 1000)],
                         "legacy pages 1-2 at size 20 are not asked again; the untouched day uses 1000")
        self.assertEqual(self.engine.ledger.task("prices", DAYS[0])["status"], "saved")
        self.assertEqual(self.engine.ledger.task("prices", DAYS[1])["reason"], "pages=1,records=7")
        self.assertIsNone(self.engine.state.blocked())

    def test_a_site_answering_fewer_records_than_asked_while_more_exist_is_an_error(self):
        pagination = {"page": "p", "page_size": 1000, "records": "data.list", "last": "data.isLastPage"}
        with self.assertRaisesRegex(paging.PagingError, "page_size_capped"):
            paging.page_result(page(500, False).body, pagination, 1)
        self.assertEqual(paging.page_result(page(500, True).body, pagination, 1), (500, True))
        totals = {"page": "p", "page_size": 1000, "records": "data.list", "total": "data.total"}
        body = json.dumps({"data": {"list": [{}] * 500, "total": 1500}}).encode()
        with self.assertRaisesRegex(paging.PagingError, "page_size_capped"):
            paging.page_result(body, totals, 1)


if __name__ == "__main__":
    unittest.main()
