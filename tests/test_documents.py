"""Disclosure documents: listing -> one download task per attachment -> archive."""
import io
import json
import unittest
from datetime import timedelta
from unittest.mock import patch
import zipfile
from pathlib import Path

from pmos import documents
from pmos.engine import Engine
from pmos.excel import ContainerError, check_document
from pmos.periods import today
from pmos.spec import SpecError, render
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir, dataset, make_province

TODAY = today().isoformat()
PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32 + b"IEND\xaeB`\x82"


def docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", b"types")
        archive.writestr("word/document.xml", b"<document/>")
    return buffer.getvalue()


def record(guid, title, files, operate=1_790_000_000_000, **fields):
    return {"guid": guid, "title": title, "operateDate": operate, "marketId": "PDBLN",
            "attachment": json.dumps([{"id": f, "name": f + ".pdf"} for f in files]), **fields}


def listing(rows, total=None) -> Response:
    body = {"status": 0, "data": {"list": rows, "total": len(rows) if total is None else total}}
    return Response(200, "application/json", json.dumps(body, ensure_ascii=False).encode())


DOCUMENTS = {
    "fields": {"id": "guid", "title": "title", "published": "operateDate"},
    "published_format": "epoch_ms",
    "expect": {"marketId": "PDBLN"},
    "attachments": {"path": "attachment", "json_string": True},
    "period": [{"fields": {"year": "fyear", "month": "fmonth"}},
               {"title_regex": r"(20\d{2})年\s*(1[0-2]|0?[1-9])月", "kind": "month"}],
    "stop_at_known": True,
    "download": {"transport": "native", "frame": "/pxf-settlement-outnetpub/", "module": "63a1", "export": "a",
                 "args": ["{attachment[id]}", None], "path": "/px-settlement-infpubmeex/fileService/download",
                 "method": "GET"},
}


def documents_spec(status="enabled", **changes) -> dict:
    value = {"id": "notices", "name": "公告", "status": status, "period": {"kind": "snapshot"},
             "request": {"transport": "component", "frame": "/pxf-settlement-outnetpub/",
                         "component": "operationOrganizationQuery", "call": "queryData",
                         "path": "/px-settlement-infpubquery/ipDisclosureApply/getData",
                         "set": {"queryParam.data.menuId": "m1", "queryParam.data.endDate": "{date}"},
                         "pagination": {"page": "queryParam.pageInfo.pageNum", "size": "queryParam.pageInfo.pageSize",
                                        "page_size": 2, "records": "data.list", "total": "data.total"}},
             "response": {"kind": "json"}, "documents": json.loads(json.dumps(DOCUMENTS))}
    value.update(changes)
    return value


class ExtractTests(unittest.TestCase):
    def test_items_per_attachment_with_business_period_or_publication_date(self):
        rows = [record("g1", "2026年9月市场报告", ["a", "b"]),
                record("g2", "通知", ["c"], fyear="2026", fmonth="8"),
                record("g3", "无日期公告", ["d"]),
                record("g4", "无附件", [])]
        items = documents.extract(rows, DOCUMENTS)
        self.assertEqual([i["attachment"]["id"] for i in items], ["a", "b", "c", "d"])
        self.assertEqual([i["period"] for i in items[:3]], ["2026-09", "2026-09", "2026-08"])
        self.assertEqual(items[3]["period"], "pub-2026-09-21")
        self.assertEqual(items[3]["period_basis"], "publication_date")
        self.assertEqual(items[0]["scope"], documents.item_scope("a"))
        self.assertNotEqual(items[0]["scope"], items[1]["scope"])

    def test_filter_skips_and_expectation_failure_is_a_site_change(self):
        rows = [record("g1", "a", ["a"], publishType="公众"), record("g2", "b", ["b"], publishType="会员")]
        kept = documents.extract(rows, {**DOCUMENTS, "filter": {"publishType": "公众"}})
        self.assertEqual([i["record_id"] for i in kept], ["g1"])
        with self.assertRaisesRegex(documents.DocumentError, "record_expectation_failed"):
            documents.extract([{**rows[0], "marketId": "PHBSX"}], DOCUMENTS)
        with self.assertRaisesRegex(documents.DocumentError, "attachment_json_invalid"):
            documents.extract([{**rows[0], "attachment": "[{"}], DOCUMENTS)

    def test_business_year_is_the_legacy_rule(self):
        """Liaoning's lncrawler/attachment_dimensions.expand_records: fields, then title; never the publication date."""
        block = {**DOCUMENTS, "business_year": 2026}
        def kept(title, name, **fields):
            row = {**record("g", title, []), "attachment": json.dumps([{"id": "f", "name": name}]), **fields}
            return bool(documents.extract([row], block))
        self.assertTrue(kept("公告", "a.pdf", fyear="2026"))
        self.assertTrue(kept("公告", "a.pdf", fyear=2026), "the site sends some as a number")
        self.assertTrue(kept("公告", "a.pdf", businessYear="2026"))
        self.assertTrue(kept("辽宁2026年9月公告", "a.pdf"))
        self.assertFalse(kept("公告", "a.pdf"), "a record naming no year is out, whenever it was published")
        self.assertFalse(kept("2025年公告", "2026年a.pdf"), "the title's first year decides")
        self.assertFalse(kept("公告", "a.pdf", fyear="2025"))
        # the legacy queue held these 9 for review and never asked them: the fields say 2026, the title does not
        self.assertFalse(kept("辽宁电网2025年12月交易总体情况", "a.pdf", fyear="2026"))
        self.assertFalse(kept("辽宁省2027年年度滚动撮合测试交易公告", "2026年a.pdf", fyear="2026"))
        self.assertFalse(kept("2026年与2025年公告", "a.pdf", fyear="2026"))
        # fyear can be the publication year: title and this file must both name only the business year
        self.assertTrue(kept("2026年公告", "2026年a.pdf", fyear="2025"))
        self.assertFalse(kept("2026年公告", "a.pdf", fyear="2025"))
        self.assertFalse(kept("2026年与2025年公告", "2026年a.pdf", fyear="2025"))
        self.assertFalse(kept("2026年公告", "2026年a.pdf", fyear="2025", businessYear="2025"))
        self.assertTrue(kept("2026年公告", "2026年a.pdf", fyear="2025", businessYear="2026"))
        self.assertEqual(len(documents.extract([record("g", "公告", ["a"], fyear="2025")], DOCUMENTS)), 1, "no rule, no filter")
        with self.assertRaisesRegex(documents.DocumentError, "business_year"):
            documents.check({**DOCUMENTS, "business_year": "2026"})

    def test_record_only_and_week_quarter_titles(self):
        block = {**DOCUMENTS, "record_only": True,
                 "period": [{"title_regex": r"(20\d{2})年第(\d+)周", "kind": "week"},
                            {"title_regex": r"(20\d{2})年第([1-4])季度", "kind": "quarter"}]}
        items = documents.extract([record("g1", "2026年第7周检修计划", []), record("g2", "2026年第3季度", [])], block)
        self.assertEqual([i["period"] for i in items], ["2026-W07", "2026-Q3"])
        self.assertEqual(items[0]["attachment"]["id"], "record:g1")


class EmptyListingTests(unittest.TestCase):
    def test_pmos_empty_listing_shapes_are_empty_pages(self):
        from pmos.paging import PagingError, page_result
        pagination = {"page": "pageInfo.pageNum", "records": "data.list", "total": "data.total"}
        for body in ({"status": 0, "data": None}, {"status": 0, "data": []},
                     {"status": 0, "data": {"list": None, "total": 0, "pageNum": 1}}):
            self.assertEqual(page_result(json.dumps(body).encode(), pagination, 1), (0, True))
        with self.assertRaisesRegex(PagingError, "records_missing"):
            page_result(json.dumps({"status": 0, "data": {"list": None, "total": 3}}).encode(), pagination, 1)


class FormatAndSpecTests(unittest.TestCase):
    def test_document_signatures_not_file_names(self):
        self.assertEqual(check_document(PDF)["format"], ".pdf")
        self.assertEqual(check_document(PNG)["format"], ".png")
        self.assertEqual(check_document(docx())["format"], ".docx")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", b"types")
            archive.writestr("ppt/presentation.xml", b"<presentation/>")
        self.assertEqual(check_document(buffer.getvalue())["format"], ".pptx", "Liaoning's 13 slide decks")
        for broken, reason in ((PDF[:-7], "pdf_container_invalid"), (b"<html>", "document_format_unreviewed"),
                               (b"PK\x03\x04broken", "zip_container_invalid")):
            with self.assertRaisesRegex(ContainerError, reason):
                check_document(broken)

    def test_whole_placeholder_passes_objects(self):
        values = {"attachment": {"id": "a", "name": "n.pdf"}, "title": "t"}
        self.assertEqual(render(["{attachment}", "{attachment[id]}", "{title}-x"], values),
                         [{"id": "a", "name": "n.pdf"}, "a", "t-x"])

    def test_spec_rules(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        province = make_province(tmp.path, [documents_spec()])
        spec = province.datasets()["notices"]
        item = documents.extract([record("g1", "t", ["f1"])], spec.documents)[0]
        self.assertEqual(spec.build_download(item)["args"], ["f1", None])
        for broken, message in (
                ({"period": {"kind": "day", "start": "2026-10-01"}}, "snapshot"),
                ({"documents": {**DOCUMENTS, "download": {**DOCUMENTS["download"], "args": ["{date}"]}}},
                 "download placeholders"),
                ({"documents": {**DOCUMENTS, "download": {**DOCUMENTS["download"], "transport": "ui"}}}, "ui")):
            with self.assertRaisesRegex(SpecError, message):
                make_province(tmp.path, [documents_spec(**broken)]).datasets()


class DocumentEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()

    def engine(self, specs):
        self.province = make_province(self.tmp.path, specs, session={"collection_order": [s["id"] for s in specs]})
        self.transport = FakeTransport(self.clock, default=Response(200, "application/pdf", PDF))
        engine = Engine(self.province, self.transport, None, instance=self.tmp.path / "var", clock=self.clock)
        self.addCleanup(engine.ledger.close)
        return engine

    async def tick(self, engine, on=None):
        if on is None:
            await engine.tick()
        else:
            with patch("pmos.engine.today", return_value=on):
                await engine.tick()
        self.clock.advance(30)

    async def test_listing_queues_files_downloads_them_and_stops_at_known_records(self):
        engine = self.engine([documents_spec(), dataset("load", keepalive=True, start=TODAY)])
        engine.state.set_control(collect=True)
        self.transport.queue += [listing([record("g1", "2026年9月报告", ["a", "b"]), record("g2", "通知", ["c"])], 3),
                                 listing([record("g3", "公告", ["d"])], 3)]
        await self.tick(engine)
        self.assertEqual(self.transport.sent[0]["set"]["queryParam.pageInfo.pageNum"], 1)
        self.assertEqual(self.transport.sent[0]["set"]["queryParam.data.endDate"], TODAY)
        await self.tick(engine)
        self.assertEqual(self.transport.sent[1]["set"]["queryParam.pageInfo.pageNum"], 2)
        self.assertIn("new_files=4", engine.ledger.task("notices", TODAY)["reason"])
        for _ in range(4):
            await self.tick(engine)
        downloads = self.transport.sent[2:]
        self.assertEqual(sorted(r["args"][0] for r in downloads), ["a", "b", "c", "d"])
        self.assertTrue(all(r["transport"] == "native" and r["method"] == "GET" for r in downloads))
        files = [r for r in engine.archive.records("notices") if r["scope"].startswith("f")]
        self.assertEqual(len(files), 4)
        first = next(r for r in files if r["attachment_id"] == "a")
        self.assertEqual((first["period"], first["record_id"], first["path"][-4:]), ("2026-09", "g1", ".pdf"))
        # next day: page 1 holds one new record and known ones -> keep listing; page 2 all known -> stop
        tomorrow = today() + timedelta(days=1)
        self.transport.queue += [listing([record("g9", "新公告", ["z"]), record("g1", "2026年9月报告", ["a", "b"])], 5),
                                 listing([record("g2", "通知", ["c"]), record("g3", "公告", ["d"])], 5)]
        sent = len(self.transport.sent)
        await self.tick(engine, tomorrow)
        await self.tick(engine, tomorrow)
        self.assertEqual(engine.ledger.task("notices", tomorrow.isoformat())["status"], "saved")
        self.assertIn("new_files=1", engine.ledger.task("notices", tomorrow.isoformat())["reason"])
        self.assertEqual(len(self.transport.sent) - sent, 2, "page 3 is never requested")

    async def test_records_without_files_still_stop_the_listing_and_old_listings_are_superseded(self):
        engine = self.engine([documents_spec(), dataset("load", keepalive=True, start=TODAY)])
        engine.state.set_control(collect=True)
        engine.ledger.seed("notices", ["2026-10-01", "2026-10-02"])  # listings missed while offline
        self.transport.queue += [listing([record("g1", "a", []), record("g2", "b", [])], 4),
                                 listing([record("g3", "c", []), record("g4", "d", [])], 4)]
        await self.tick(engine)
        await self.tick(engine)
        self.assertEqual(engine.ledger.task("notices", TODAY)["status"], "saved")
        self.assertEqual(engine.ledger.task("notices", "2026-10-01")["status"], "skipped")
        self.assertEqual(engine.ledger.task("notices", "2026-10-02")["reason"], "superseded_by_newer_listing")
        tomorrow = today() + timedelta(days=1)
        self.transport.queue.append(listing([record("g1", "a", []), record("g2", "b", [])], 4))
        sent = len(self.transport.sent)
        await self.tick(engine, tomorrow)
        self.assertEqual(engine.ledger.task("notices", tomorrow.isoformat())["status"], "saved")
        self.assertEqual(len(self.transport.sent) - sent, 1, "a page of known records ends the listing")

    async def test_known_records_stop_a_listing_only_after_one_listing_reached_the_end(self):
        engine = self.engine([documents_spec(), dataset("load", keepalive=True, start=TODAY)])
        engine.state.set_control(collect=True)
        broken = Response(200, "application/json", b'{"status": 0, "data": {"rows": []}}')
        self.transport.queue += [listing([record("g1", "a", []), record("g2", "b", [])], 6), broken]
        await self.tick(engine)
        await self.tick(engine)  # page 2 changed shape: dataset held mid-listing
        self.assertEqual(engine.dataset_holds()["notices"]["reason"], "pagination_records_missing")
        engine.state.remove("dataset_holds")
        tomorrow = today() + timedelta(days=1)
        self.transport.queue += [listing([record("g1", "a", []), record("g2", "b", [])], 6),
                                 listing([record("g3", "c", []), record("g4", "d", [])], 6),
                                 listing([record("g5", "e", []), record("g6", "f", [])], 6)]
        for _ in range(3):
            await self.tick(engine, tomorrow)
        self.assertEqual([r["set"]["queryParam.pageInfo.pageNum"] for r in self.transport.sent[2:]], [1, 2, 3],
                         "page 1 is known, but the list was never read to its end")
        self.assertEqual(engine.state.read("listing_complete"), {"notices": tomorrow.isoformat()})

    async def test_a_bad_file_or_changed_record_quarantines_only_this_dataset(self):
        engine = self.engine([documents_spec(), dataset("load", keepalive=True, start=TODAY)])
        engine.state.set_control(collect=True)
        self.transport.queue += [listing([record("g1", "t", ["a"])]), Response(200, "application/octet-stream", b"<html>")]
        await self.tick(engine)
        await self.tick(engine)
        self.assertEqual(engine.dataset_holds()["notices"]["reason"], "document_format_unreviewed")
        self.assertIsNone(engine.state.blocked())
        # A separate fixture tests another failure; never retry bad attachment a.
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        other = self.engine([documents_spec(), dataset("load", keepalive=True, start=TODAY)])
        other.state.set_control(collect=True)
        self.transport.queue += [listing([{**record("g2", "t", ["b"]), "marketId": "PHBSX"}])]
        await self.tick(other)
        self.assertEqual(other.dataset_holds()["notices"]["reason"], "record_expectation_failed")

    async def test_ui_articles_one_task_per_record_and_one_file_per_part(self):
        menu = ["中长期市场", "市场运行信息", "市场信息披露报告(月)"]
        spec = {"id": "ah_notices", "name": "公告", "status": "enabled", "period": {"kind": "snapshot"},
                "request": {"transport": "ui", "action": "list", "frame": "/pxf-settlement-outnetpub/", "menu": menu,
                            "pagination": {"page": "page", "records": "data.list", "total": "data.total",
                                           "last": "data.last", "page_size": 10}},
                "response": {"kind": "json"},
                "documents": {"fields": {"id": "id", "title": "title", "published": "published"},
                              "published_format": "date", "record_only": True, "stop_at_known": True,
                              "period": [{"title_regex": r"(20\d{2})年第([一二三四1-4])季度", "kind": "quarter"}],
                              "download": {"transport": "ui", "action": "record", "frame": "/pxf-settlement-outnetpub/",
                                           "menu": menu, "record": "{record}"}}}
        engine = self.engine([spec, dataset("load", keepalive=True, start=TODAY)])
        engine.state.set_control(collect=True)
        rows = [{"id": "r1", "title": "2026年第三季度市场报告", "published": "2026-10-10", "page": 1},
                {"id": "r2", "title": "运行通知", "published": "2026-10-09", "page": 1}]
        body = json.dumps({"status": 0, "data": {"list": rows, "total": 2, "page": 1, "last": True}}, ensure_ascii=False)
        # tasks run newest period first: "pub-2026-10-09" (no period in the title) sorts before "2026-Q3"
        self.transport.queue += [Response(200, "application/json", body.encode()),
                                 Response(200, "application/x-pmos-parts", b"", parts=(
                                     Response(200, "application/x-pmos-capture+json", b'{"text": "body"}', name="body.json"),)),
                                 Response(200, "application/x-pmos-parts", b"", parts=(
                                     Response(200, "application/pdf", PDF, name="报告.pdf"),
                                     Response(200, "application/octet-stream", docx(), name="附表.docx")))]
        for _ in range(3):
            await self.tick(engine)
        self.assertEqual(self.transport.sent[0]["page"], 1)
        self.assertEqual([r["record"]["id"] for r in self.transport.sent[1:]], ["r2", "r1"])
        files = sorted((r["period"], r["scope"][-3:], r["attachment_name"], Path(r["path"]).suffix)
                       for r in engine.archive.records("ah_notices") if r["scope"].startswith("f"))
        self.assertEqual(files, [("2026-Q3", ".01", "报告.pdf", ".pdf"), ("2026-Q3", ".02", "附表.docx", ".docx"),
                                 ("pub-2026-10-09", ".01", "body.json", ".json")])
        self.assertIn("files=2", engine.ledger.task("ah_notices", "2026-Q3", documents.item_scope("record:r1"))["reason"])

    async def test_sampling_a_listing_then_one_file_verifies(self):
        engine = self.engine([documents_spec(status="candidate")])
        self.transport.queue.append(listing([record("g1", "2026年9月报告", ["a"])]))
        engine.enqueue({"action": "sample", "dataset": "notices", "period": TODAY})
        await self.tick(engine)
        self.assertEqual(self.province.datasets()["notices"].status, "sampled")
        self.assertEqual(self.transport.sent[0]["set"]["queryParam.pageInfo.pageNum"], 1)
        engine.enqueue({"action": "sample", "dataset": "notices", "period": TODAY, "scope": "file"})
        await self.tick(engine)
        results = list(engine.state.read("command_results").values())
        self.assertTrue(results[-1]["ok"], results[-1])
        self.assertEqual((results[-1]["status"], results[-1]["format"]), ("verified", ".pdf"))
        self.assertEqual(self.transport.sent[1]["args"], ["a", None])


if __name__ == "__main__":
    unittest.main()
