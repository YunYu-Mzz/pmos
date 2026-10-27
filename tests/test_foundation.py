"""Pure building blocks: periods, specs, validation, pacing, ledger, archive."""
import json
import unittest
from datetime import date

from pmos import periods
from pmos.archive import Archive
from pmos.faults import Fault, decide
from pmos.ledger import Ledger
from pmos.pacing import Policy, PolicyError, wait_seconds
from pmos.spec import DatasetSpec, SpecError, render
from pmos.validate import Response, evaluate

from helpers import Clock, TempDir, dataset, envelope, excel


class PeriodTests(unittest.TestCase):
    def test_canonical_values_only(self):
        for kind, value in (("day", "2026-1-01"), ("month", "2026-1"), ("year", "26"), ("day", "2026-02-30")):
            with self.assertRaises(periods.PeriodError):
                periods.parse(kind, value)
        self.assertEqual(periods.last_day("month", "2026-02"), date(2026, 2, 28))

    def test_expand_is_newest_first_with_lag_and_complete_months(self):
        on = date(2026, 10, 3)
        self.assertEqual(periods.expand("day", "2026-10-01", "today", on=on), ["2026-10-03", "2026-10-02", "2026-10-01"])
        self.assertEqual(periods.expand("day", "2026-10-01", "today", on=on, lag_days=1), ["2026-10-02", "2026-10-01"])
        self.assertEqual(periods.expand("month", "2026-08", "today", on=on), ["2026-10", "2026-09", "2026-08"])
        self.assertEqual(periods.expand("month", "2026-08", "today", on=on, complete_only=True), ["2026-09", "2026-08"])
        self.assertEqual(periods.expand("year", "2025", "today", on=on), ["2026", "2025"])
        self.assertEqual(periods.expand("day", "2026-10-05", "today", on=on), [])

    def test_future_period_is_refused(self):
        with self.assertRaisesRegex(periods.PeriodError, "future"):
            periods.validate("day", "2026-10-04", on=date(2026, 10, 3))

    def test_template_variables(self):
        values = periods.variables("month", "2026-02")
        self.assertEqual((values["start"], values["end"], values["month_compact"]), ("2026-02-01", "2026-02-28", "202602"))


class SpecTests(unittest.TestCase):
    def test_render_nested_templates(self):
        rendered = render({"data": {"date": "{date}", "k": ["{month}", 3]}, "x": "a{year}"},
                          periods.variables("day", "2026-10-11"))
        self.assertEqual(rendered, {"data": {"date": "2026-10-11", "k": ["2026-10", 3]}, "x": "a2026"})

    def test_rejects_unknown_placeholders_fields_and_bad_transport(self):
        value = dataset("good_one")
        DatasetSpec(**value).check()
        for change in ({"request": {**value["request"], "body": {"d": "{day}"}}},
                       {"request": {**value["request"], "transport": "curl"}},
                       {"request": {**value["request"], "path": "https://x/y"}},
                       {"status": "live"}, {"period": {"kind": "week", "start": "2026-01-01"}},
                       {"keepalive": True, "status": "verified"}):
            with self.assertRaises(SpecError):
                DatasetSpec(**{**value, **change}).check()

    def test_component_and_ui_transport_requirements(self):
        base = dataset("comp")
        with self.assertRaisesRegex(SpecError, "component"):
            DatasetSpec(**{**base, "request": {**base["request"], "transport": "component"}}).check()
        ui = {**base, "request": {"transport": "ui", "frame": "infoCode=x", "action": "export",
                                  "fields": [{"kind": "date", "placeholder": "选择日期", "value": "{date}"}]}}
        DatasetSpec(**ui).check()
        built = DatasetSpec(**ui).build("2026-10-11")
        self.assertEqual(built["fields"][0]["value"], "2026-10-11")
        self.assertNotIn("template", built)

    def test_build_fills_body_and_template_key(self):
        spec = DatasetSpec(**dataset("x_report"))
        request = spec.build("2026-10-11")
        self.assertEqual(request["body"], {"date": "2026-10-11"})
        self.assertEqual(request["template"], "/px-test/x_report")

    def test_scopes_have_stable_keys_and_become_ui_select_fields(self):
        ui = {**dataset("unit_status"), "scopes": [{"分类": "机组实际出力", "节点名称": "临涣厂#1机"}, {"节点名称": "B"}],
              "request": {"transport": "ui", "frame": "infoCode=jzzt", "action": "export",
                          "fields": [{"kind": "date", "placeholder": ["请选择日期", "选择日期"], "value": "{date}"}]}}
        spec = DatasetSpec(**ui)
        spec.check()
        (key, values), (other, _) = spec.scope_items()
        self.assertRegex(key, r"^s[0-9a-f]{15}$")
        self.assertEqual(spec.scope_values(key), values)
        fields = spec.build("2026-10-11", values)["fields"]
        self.assertEqual([f.get("label") for f in fields], [None, "分类", "节点名称"])
        self.assertEqual(fields[2]["value"], "临涣厂#1机")
        self.assertEqual(DatasetSpec(**dataset("plain")).scope_items(), [("", {})])

    def test_scope_placeholders_in_fetch_bodies_and_validation(self):
        value = {**dataset("section"), "scopes": [{"strName": "龙小线"}]}
        value["request"] = {**value["request"], "body": {"infoDate": "{date}", "strName": "{scope[strName]}"}}
        spec = DatasetSpec(**value)
        spec.check()
        self.assertEqual(spec.build("2026-10-11", {"strName": "龙小线"})["body"], {"infoDate": "2026-10-11", "strName": "龙小线"})
        with self.assertRaises(SpecError):
            DatasetSpec(**{**value, "scopes": []}).check()  # scope placeholder without scopes
        for bad in ([{}], [{"a": 1}], [{"a": "x"}, {"a": "x"}]):
            with self.assertRaises(SpecError):
                DatasetSpec(**{**value, "scopes": bad}).check()

    def test_load_requires_filename_equal_id(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        path = tmp.path / "other.json"
        path.write_text(json.dumps(dataset("x_report")))
        with self.assertRaisesRegex(SpecError, "file name"):
            DatasetSpec.load(path)


class ValidateTests(unittest.TestCase):
    def test_excel_saved_and_json_refusal_classified(self):
        self.assertEqual(evaluate({"kind": "xlsx"}, excel()).result, "saved")
        cases = [(envelope(401), "auth"), (envelope(10003), "auth"), (envelope(40302), "permission"),
                 (envelope(429), "rate_limit"), (envelope(500, message="访问过于频繁"), "rate_limit"),
                 (envelope(9999), "unknown"), (envelope(0), "unknown")]
        for response, category in cases:
            with self.subTest(response=response.body):
                with self.assertRaises(Fault) as caught:
                    evaluate({"kind": "xlsx"}, response)
                self.assertEqual(caught.exception.category, category)

    def test_http_and_html(self):
        for status, category in ((401, "auth"), (403, "permission"), (429, "rate_limit"), (502, "unknown")):
            with self.assertRaises(Fault) as caught:
                evaluate({"kind": "json"}, Response(status, "application/json", b"{}"))
            self.assertEqual(caught.exception.category, category)
        with self.assertRaises(Fault) as caught:
            evaluate({"kind": "xlsx"}, Response(200, "text/html", b"<html>login</html>"))
        self.assertEqual(caught.exception.code, "unexpected_html")

    def test_broken_workbook_is_site_data_quarantine(self):
        with self.assertRaises(Fault) as caught:
            evaluate({"kind": "xlsx"}, Response(200, "application/vnd.ms-excel", b"PK\x03\x04broken"))
        self.assertEqual(caught.exception.category, "site_data")
        self.assertEqual(decide(caught.exception, "collect").scope, "dataset")

    def test_json_empty_data_is_empty_not_saved(self):
        self.assertEqual(evaluate({"kind": "json"}, envelope(0, data=[])).result, "empty")
        self.assertEqual(evaluate({"kind": "json"}, envelope(0, data={"list": []})).result, "empty")
        self.assertEqual(evaluate({"kind": "json"}, envelope(0, data={"list": [1]})).result, "saved")

    def test_fault_decisions(self):
        self.assertEqual(decide(Fault("x", "permission"), "sample").dataset_status, "denied")
        self.assertEqual(decide(Fault("x", "permission"), "collect").scope, "province")
        self.assertTrue(decide(Fault("x", "local"), "collect").keepalive)
        for category in ("auth", "rate_limit", "network", "unknown"):
            self.assertFalse(decide(Fault("x", category), "collect").keepalive)


class PacingTests(unittest.TestCase):
    def test_policy_bounds(self):
        for change in ({"response_interval_seconds": 10}, {"max_per_hour": 0}, {"renew_after_idle_seconds": 2000}):
            with self.assertRaises(PolicyError):
                Policy.from_dict(change)
        self.assertEqual(Policy.from_dict({"unknown_key": 1}).extra, {"unknown_key": 1})

    def test_spacing_from_response_end_and_caps(self):
        policy = Policy.from_dict({"response_interval_seconds": 60, "max_per_hour": 2})
        self.assertEqual(wait_seconds(policy, 1000, 950), 10)
        self.assertEqual(wait_seconds(policy, 10_000, 900, [100, 200]), 0)  # both older than an hour
        self.assertEqual(wait_seconds(policy, 1000, 900, [500, 600]), 3600 + 500 + 1 - 1000)
        self.assertEqual(wait_seconds(policy, 1000, 900, [500, 600], charged=False), 0)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.ledger = Ledger(self.tmp.path / "l.sqlite3", self.clock)
        self.addCleanup(self.ledger.close)

    def test_seed_is_idempotent_and_newest_first(self):
        self.assertEqual(self.ledger.seed("a", ["2026-10-01", "2026-10-03", "2026-10-02"]), 3)
        self.assertEqual(self.ledger.seed("a", ["2026-10-03", "2026-10-04"]), 1)
        self.assertEqual(self.ledger.next_task(["a"])["period"], "2026-10-04")
        self.ledger.settle_task("a", "2026-10-04", "saved", sha256="f" * 64)
        self.assertEqual(self.ledger.next_task(["b", "a"])["period"], "2026-10-03")
        self.assertEqual(self.ledger.summary()["a"], {"pending": 3, "saved": 1})

    def test_attempt_is_durable_before_send_and_unsettled_until_finished(self):
        attempt = self.ledger.begin("collect", "a", "2026-10-01")
        reopened = Ledger(self.tmp.path / "l.sqlite3", self.clock)
        self.addCleanup(reopened.close)
        self.assertEqual([r["id"] for r in reopened.unsettled()], [attempt])
        self.clock.advance(5)
        reopened.settle_unknown(attempt)
        self.assertEqual(reopened.unsettled(), [])
        self.assertEqual(reopened.last_response_end(), self.clock())
        self.assertEqual(reopened.last_business_end(), 0)

    def test_probe_is_spacing_but_not_business_activity(self):
        a = self.ledger.begin("collect", "a", "p")
        self.clock.advance(3)
        self.ledger.finish(a, "success")
        business = self.clock()
        self.clock.advance(400)
        b = self.ledger.begin("probe")
        self.clock.advance(45)
        self.ledger.finish(b, "success")
        self.assertEqual(self.ledger.last_business_end(), business)
        self.assertEqual(self.ledger.last_response_end(), self.clock())
        self.assertEqual(self.ledger.budget_starts(0), [business - 3])


class ArchiveTests(unittest.TestCase):
    def test_store_dedupes_and_tracks_latest_period(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        archive = Archive(tmp.path, "testland")
        first = archive.store("a", "2026-10-01", b"one", ".xlsx")
        again = archive.store("a", "2026-10-01", b"one", ".xlsx")
        archive.store("a", "2026-10-02", b"two", ".xlsx")
        self.assertEqual(first["path"], again["path"])
        self.assertEqual(len(archive.records("a")), 2)
        self.assertEqual(archive.latest_period("a"), "2026-10-02")
        self.assertTrue(archive.verify(first))
        with self.assertRaises(ValueError):
            archive.store("a", "2026-10-01", b"", ".xlsx")


if __name__ == "__main__":
    unittest.main()
