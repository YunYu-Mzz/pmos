"""Shanxi native lane: identity contracts from legacy contracts, parity with the legacy queue."""
import json
import sqlite3
import unittest
from pathlib import Path

from pmos.migration import shanxi as migration

from helpers import TempDir

PAGE_INFO = {"pageNum": 1, "pageSize": 20, "pageSizes": [20, 50], "total": 0}
CONTRACTS = {
    "menu_plain": {"source_query_path": "/px-phbsx-retail-shop/infoGrid/plain", "date_fields": ["date"],
                   "observed_request": {"body": {"data": {"date": "2026-10-20"}, "pageInfo": PAGE_INFO}}},
    "menu_section": {"source_query_path": "/px-phbsx-retail-shop/infoPub/section", "date_fields": ["infoDate"],
                     "required_scope_fields": ["strName"],
                     "observed_request": {"body": {"data": {"infoDate": "2026-10-14", "strName": "甲线"},
                                                   "pageInfo": PAGE_INFO}}},
    "flat_curve": {"source_query_path": "/px-settlement-infpubquery/tableCurve/curve", "date_fields": ["dateTime"],
                   "request_envelope": "flat",
                   "observed_request": {"body": {"infoCode": "c1", "dateTime": "2026-10-20", "marketId": "PHBSX"}}},
}


def spec(name, *, paged=True, scopes=None):
    value = {"id": name, "name": name, "status": "sampled", "period": {"kind": "day", "start": "2026-10-01"},
             "request": {"transport": "component", "frame": "#/info/x", "component": "*",
                         "match": {"queryUrl": CONTRACTS[name]["source_query_path"]},
                         "path": CONTRACTS[name]["source_query_path"], "set": {"date": "{date}"},
                         "call": "handleCurrentChange" if paged else "queryData", "args": [1] if paged else []},
             "response": {"kind": "json"}, "source": {"strategy": "plain"}}
    if paged:
        value["request"]["pagination"] = {"page": "args.0", "records": "data.list", "page_size": 20}
    if scopes:
        value["scopes"] = scopes
        value["request"]["set"]["selectValue"] = "{scope[strName]}"
    return value


def row(dataset, period, body, page, status="saved"):
    return (f"id-{dataset}-{period}-{page}", dataset, period, CONTRACTS[dataset]["source_query_path"],
            json.dumps(body, ensure_ascii=False), json.dumps(CONTRACTS[dataset], ensure_ascii=False), page, status)


class ShanxiNativeParity(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.legacy = self.tmp.path / "legacy"
        self.legacy.mkdir()
        (self.legacy / "native_menu_query_contracts.json").write_text(
            json.dumps({"records": CONTRACTS}, ensure_ascii=False))
        self.datasets = self.tmp.path / "provinces" / "shanxi" / "datasets"
        self.datasets.mkdir(parents=True)
        for value in (spec("menu_plain"), spec("menu_section", scopes=[{"strName": "甲线"}, {"strName": "乙线"}]),
                      spec("flat_curve", paged=False)):
            (self.datasets / f"{value['id']}.json").write_text(json.dumps(value, ensure_ascii=False))

    def queue(self, rows):
        path = self.legacy / "queue.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE requests(identity TEXT, dataset TEXT, period TEXT, path TEXT, body TEXT,"
                       " contract TEXT, page INTEGER, status TEXT)")
            db.executemany("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)", rows)
        return "queue.sqlite3"

    def test_engine_identity_equals_the_legacy_native_queue_identity(self):
        attached = migration.attach_contracts(self.legacy, self.datasets)
        self.assertEqual(attached["attached"], ["flat_curve", "menu_plain", "menu_section"])
        snapshot = self.queue([
            row("menu_plain", "2026-10-20", {"data": {"date": "2026-10-20"}, "pageInfo": PAGE_INFO}, 1),
            row("menu_plain", "2026-10-20", {"data": {"date": "2026-10-20"}, "pageInfo": {**PAGE_INFO, "pageNum": 2}},
                2, "pending"),
            row("menu_section", "2026-10-14", {"data": {"infoDate": "2026-10-14", "strName": "乙线"},
                                               "pageInfo": PAGE_INFO}, 1),
            row("flat_curve", "2026-10-20", {"infoCode": "c1", "dateTime": "2026-10-20", "marketId": "PHBSX"}, 1)])
        report = migration.native_parity(self.legacy, self.datasets, snapshots=[snapshot])
        self.assertEqual(report["summary"]["records"], report["summary"]["matched"], report["per_source"])
        self.assertEqual(report["eligible"], ["flat_curve", "menu_plain", "menu_section"])

    def test_an_extra_business_field_in_the_legacy_body_is_a_mismatch(self):
        migration.attach_contracts(self.legacy, self.datasets)
        snapshot = self.queue([
            row("menu_plain", "2026-10-20", {"data": {"date": "2026-10-20", "unit": "x"}, "pageInfo": PAGE_INFO}, 1)])
        report = migration.native_parity(self.legacy, self.datasets, snapshots=[snapshot])
        self.assertEqual(report["per_source"]["menu_plain"]["mismatched"], 1)
        self.assertEqual(report["eligible"], [])

    def test_contracts_are_only_written_into_a_pmos_shanxi_datasets_directory(self):
        with self.assertRaisesRegex(ValueError, "pmos_shanxi_datasets_directory_required"):
            migration.attach_contracts(self.legacy, self.legacy)


if __name__ == "__main__":
    unittest.main()


class _CutoverFixture(unittest.IsolatedAsyncioTestCase):
    """A small frozen Shanxi snapshot: native queue, unified plan, renewal state."""

    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.legacy = self.tmp.path / "legacy"
        (self.legacy / "state" / "native_json_collection").mkdir(parents=True)
        (self.legacy / "native_menu_query_contracts.json").write_text(json.dumps({"records": CONTRACTS}, ensure_ascii=False))
        self.datasets = self.tmp.path / "provinces" / "shanxi" / "datasets"
        self.datasets.mkdir(parents=True)
        for value in (spec("menu_plain"), spec("flat_curve", paged=False)):
            (self.datasets / f"{value['id']}.json").write_text(json.dumps(value, ensure_ascii=False))
        migration.attach_contracts(self.legacy, self.datasets)
        path = self.legacy / migration.NATIVE_QUEUE
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE requests(identity TEXT, dataset TEXT, period TEXT, path TEXT, body TEXT,"
                       " contract TEXT, page INTEGER, status TEXT)")
            db.executemany("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)", [
                row("menu_plain", "2026-10-20", {"data": {"date": "2026-10-20"}, "pageInfo": PAGE_INFO}, 1, "complete"),
                row("menu_plain", "2026-10-19", {"data": {"date": "2026-10-19"}, "pageInfo": PAGE_INFO}, 1, "pending"),
                row("menu_plain", "2026-10-18", {"data": {"date": "2026-10-18"}, "pageInfo": PAGE_INFO}, 1, "failed"),
                row("flat_curve", "2026-10-20", {"infoCode": "c1", "dateTime": "2026-10-20", "marketId": "PHBSX"}, 1,
                    "pending")])
        (self.legacy / migration.UNIFIED_PLAN).write_text(json.dumps({
            "groups": [{"lane": "native", "scope": "flat_curve"}, {"lane": "native", "scope": "menu_plain"}],
            "user_deferred_scopes": {"native:flat_curve": {"approved": True, "reason": "user deferred"}}}))
        (self.legacy / "state" / "server_session_renewal.json").write_text(json.dumps({"outcome": "success"}))

    def declare_markers(self):
        (self.legacy / "MANIFEST.json").write_text(json.dumps(
            {"inflight_markers": {m: "absent" for m in migration.INFLIGHT_MARKERS[:3]}}))


class ShanxiCutover(_CutoverFixture):
    """Take over exactly the legacy queue: tombstones for everything, tasks for its pending rows."""

    def test_the_freeze_must_prove_no_request_is_in_flight(self):
        plan = migration.plan_cutover(self.legacy, self.datasets)
        self.assertFalse(plan["ready"])
        self.assertEqual(len(plan["global_blockers"]), 3)
        self.declare_markers()
        (self.legacy / "state" / "server_session_renewal.json").write_text(json.dumps({"outcome": "pending"}))
        self.assertIn("state/server_session_renewal.json:pending",
                      migration.plan_cutover(self.legacy, self.datasets)["global_blockers"])

    async def test_only_legacy_pending_rows_are_worked_and_deferred_scopes_stay_out(self):
        from pmos.engine import Engine
        from pmos.province import Province
        from helpers import Clock, FakeTransport, envelope
        self.declare_markers()
        plan = migration.plan_cutover(self.legacy, self.datasets)
        self.assertTrue(plan["ready"], plan["global_blockers"])
        self.assertEqual(plan["scope"], ["menu_plain"])
        self.assertEqual(plan["excluded_pending"], {"flat_curve": "user_deferred"})
        instance = self.tmp.path / "instance"
        result = migration.apply_cutover(
            self.legacy, self.datasets, instance, plan, legacy_owner={"host": "h", "stopped_evidence": "fixture"},
            code_sha="c", approval="fixture", request_budget=5,
            cutover={"session": {"keepalive_dataset": None}, "seeding": "legacy_pending_only"})
        self.assertEqual(result["seeded"], {"menu_plain": 1})
        (self.tmp.path / "provinces" / "shanxi" / "province.json").write_text(json.dumps({
            "id": "shanxi", "name": "山西", "origin": "https://pmos.sx.sgcc.com.cn",
            "policy": {"response_interval_seconds": 15, "lane_intervals": {"native": 5}},
            "session": {"keepalive_dataset": None, "probe": None}}))
        clock = Clock()
        transport = FakeTransport(clock, default=envelope(data={"list": [1], "total": 1, "isLastPage": True}))
        engine = Engine(Province.load("shanxi", self.tmp.path / "provinces"), transport, instance=instance,
                        clock=clock, account_alias="shanxi-production")
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        for _ in range(4):
            await engine.tick()
            clock.advance(20)
        self.assertEqual([r["set"]["date"] for r in transport.sent], ["2026-10-19"],
                         "complete and failed legacy days are never sent, nothing new is seeded")
        self.assertEqual(engine.ledger.task("menu_plain", "2026-10-19")["status"], "saved")


class ShanxiAttachments(_CutoverFixture):
    """The announcement lane: pending fileIds become downloads, finished ones stay tombstones."""

    def setUp(self):
        super().setUp()
        doc = {"id": "pub_notices", "name": "公告", "status": "sampled", "period": {"kind": "snapshot"},
               "request": {"transport": "component", "frame": "/pxf-settlement-outnetpub/",
                           "component": "operationOrganizationQuery", "match": {"viewId": "m1"}, "call": "queryData",
                           "path": "/px-settlement-infpubquery/ipDisclosureApply/getData",
                           "set": {"queryParam.data.endDate": "{date}"},
                           "pagination": {"page": "queryParam.pageInfo.pageNum", "records": "data.list", "page_size": 20}},
               "response": {"kind": "json"}, "source": {"menu_guid": "m1", "lane": "publication"},
               "documents": {"fields": {"id": "guid"}, "attachments": {"path": "attachment", "json_string": True},
                             "period": [{"title_regex": r"(20\d{2})年\s*(1[0-2]|0?[1-9])月", "kind": "month"}],
                             "download": {"transport": "native", "frame": "/pxf-settlement-outnetpub/", "module": "63a1",
                                          "export": "a", "args": ["{attachment[id]}", None], "capture": "blob",
                                          "path": "/px-settlement-infpubmeex/fileService/download", "method": "GET"}}}
        (self.datasets / "pub_notices.json").write_text(json.dumps(doc, ensure_ascii=False))
        path = self.legacy / migration.PUBLICATION_QUEUE
        path.parent.mkdir(parents=True)

        def work(identity, file_id, name, status):
            command = {"arguments": [file_id, None], "native_module": "63a1", "native_export": "a",
                       "path": "/px-settlement-infpubmeex/fileService/download", "method": "GET",
                       "query": {"fileId": file_id}, "filename": name}
            return (identity, "attachment", file_id, json.dumps(command, ensure_ascii=False),
                    json.dumps({"menu_result": {"menu": {"guid": "m1"}}}), status)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE work(identity TEXT, kind TEXT, scope TEXT, command TEXT, binding TEXT, status TEXT)")
            db.executemany("INSERT INTO work VALUES(?,?,?,?,?,?)", [
                work("w1", "f-new", "2026年9月预计划.pdf", "pending"),
                work("w2", "f-done", "2026年8月预计划.pdf", "complete")])
        plan = json.loads((self.legacy / migration.UNIFIED_PLAN).read_text())
        plan["groups"].append({"lane": "publication", "scope": "m1"})
        (self.legacy / migration.UNIFIED_PLAN).write_text(json.dumps(plan))

    async def test_pending_attachments_are_downloaded_with_the_legacy_command_and_done_ones_never(self):
        from pmos.documents import item_scope
        from pmos.ledger import Ledger
        self.declare_markers()
        plan = migration.plan_cutover(self.legacy, self.datasets)
        self.assertEqual(plan["scope"], ["menu_plain", "pub_notices"])
        self.assertEqual(plan["attachments_in_scope"], 1)
        instance = self.tmp.path / "instance"
        result = migration.apply_cutover(
            self.legacy, self.datasets, instance, plan, legacy_owner={"host": "h", "stopped_evidence": "fixture"},
            code_sha="c", approval="fixture", request_budget=5,
            cutover={"session": {"keepalive_dataset": None}, "seeding": "legacy_pending_only"})
        self.assertEqual(result["seeded"], {"menu_plain": 1, "pub_notices": 1})
        ledger = Ledger(instance / "ledger.sqlite3")
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.task("pub_notices", "2026-09", item_scope("f-new"))["status"], "pending",
                         "the period comes from the file name")
        from pmos.contracts import TaskIdentity
        done = TaskIdentity("shanxi", "shanxi-production", "pub_notices", {"kind": "snapshot", "anchor": "undated"},
                            {"file_id": "f-done"}, operation="attachment")
        self.assertTrue(ledger.legacy_file_tombstone(done.file_key), "a finished legacy download is a file tombstone")


class AnnouncementDownloads(unittest.TestCase):
    """Production downloads a file from the announcement's detail dialog: unstructedMessage.fileDownload(attachment).

    The file helper (webpack module 63a1) arrives with the dialog's chunk, so
    calling it directly fails before anything is sent (2026-10-31 attempt 50).
    """

    def test_every_shanxi_announcement_file_downloads_through_its_detail_dialog(self):
        from pmos.spec import load_all
        specs = {k: v for k, v in load_all(Path(__file__).parents[1] / "provinces/shanxi/datasets").items()
                 if v.documents}
        self.assertEqual(len(specs), 77)
        attachment = {"id": "n4d47f896b9994f42bb25697eb4eb026b", "name": "2026-04考核补偿报表.pdf"}
        item = {"record": {}, "attachment": attachment, "title": attachment["name"], "record_id": "legacy:x",
                "published": None, "period": "undated"}
        for name, spec in specs.items():
            request = spec.build_download(item)
            guid = spec.source["menu_guid"]
            self.assertEqual((request["transport"], request["component"], request["call"], request["args"]),
                             ("component", "unstructedMessage", "fileDownload", [attachment]), name)
            self.assertEqual(request["match"], {"attachment": [attachment]}, name)
            self.assertEqual((request["path"], request["method"], request["capture"]),
                             ("/px-settlement-infpubmeex/fileService/download", "GET", "blob"), name)
            self.assertEqual(request["prepare"][:-2], spec.request["prepare"], "the listing's view is mounted first")
            self.assertEqual(request["prepare"][0], {"wait": {"component": "chHeader"}},
                             "a freshly opened frame mounts its header a moment after load")
            dialog, shown = request["prepare"][-2:]
            self.assertEqual(dialog["match"], {"viewId": guid})
            self.assertEqual(list(dialog["set"]), ["detailComponent", "isfullscreen", "messagePropData",
                                                   "dialogTableVisible"])
            self.assertEqual(dialog["set"]["messagePropData"]["attachment"], [attachment])
            self.assertEqual(shown, {"wait": {"component": "unstructedMessage", "match": {"attachment": [attachment]}}})


class AltTabSpecs(unittest.TestCase):
    def test_each_alt_tab_spec_waits_for_its_tab_component_before_switching(self):
        """2026-10-31: the tab component mounts after the route change; acting at once was component_missing."""
        from pmos.spec import load_all
        specs = {k: v for k, v in load_all(Path(__file__).parents[1] / "provinces/shanxi/datasets").items()
                 if k.startswith("alt_tab_")}
        self.assertEqual(len(specs), 4)
        for name, spec in specs.items():
            steps = spec.request["prepare"]
            self.assertEqual(steps[0], {"wait": {"component": spec.request["component"]}}, name)
            self.assertEqual(steps[1]["call"], "handleClick", name)
