"""FineReport daily answers and specs (no browser; the page work is in test_gate_fixture)."""
import json
import unittest
from pathlib import Path

from pmos.faults import Fault
from pmos.finereport import daily_values
from pmos.migration import liaoning as migration
from pmos.province import Province
from pmos.spec import SpecError, check_request
from pmos.validate import Response, evaluate

from test_gate_fixture import blank_table, daily_table

REPO = Path(__file__).resolve().parents[1]


def answer(html, **values):
    return {"currentPage": 1, "totalPage": 1, "htmlTag": html, **values}


class Values(unittest.TestCase):
    def test_a_day_has_96_times_in_six_rows_of_pairs(self):
        values = daily_values(answer(daily_table()))
        self.assertEqual((len(values), values[0], values[-1]), (96, {"time": "00:15", "value": "1.5"}, {"time": "24:00", "value": "1.5"}))

    def test_the_sites_blank_report_is_no_values(self):
        self.assertEqual(daily_values(answer(blank_table())), [])

    def test_a_blank_cell_stays_blank_and_a_changed_table_is_refused(self):
        self.assertIsNone(daily_values(answer(daily_table().replace("<td>1.5</td>", "<td></td>", 1)))[0]["value"])
        for body, code in ((answer("<table><tr><td>x</td></tr></table>"), "grid_changed"),
                           (answer(daily_table().replace("00:15", "00:16")), "times_changed"),
                           (answer(daily_table().replace("<td>1.5</td>", "<td>abc</td>", 1)), "not_numeric"),
                           (answer(daily_table().replace("<td>1.5</td>", "<td>NaN</td>", 1)), "not_finite"),
                           (answer(daily_table(), totalPage=2), "pagination")):
            with self.assertRaisesRegex(ValueError, code):
                daily_values(body)


class Evaluate(unittest.TestCase):
    def response(self, body):
        return Response(200, "application/json", json.dumps(body).encode())

    def test_data_is_saved_and_the_blank_report_is_empty(self):
        self.assertEqual(evaluate({"kind": "finereport_daily"}, self.response(answer(daily_table()))).result, "saved")
        self.assertEqual(evaluate({"kind": "finereport_daily"}, self.response(answer(blank_table()))).result, "empty")

    def test_a_table_whose_whole_value_rows_are_blank_is_refused_as_the_legacy_worker_did(self):
        """Its rows drop out of the 12: only the site's own blank report means no data."""
        with self.assertRaisesRegex(ValueError, "grid_changed"):
            daily_values(answer(daily_table().replace("<td>1.5</td>", "<td></td>")))

    def test_a_changed_table_faults_only_as_site_data(self):
        with self.assertRaises(Fault) as caught:
            evaluate({"kind": "finereport_daily"}, self.response(answer("<table></table>")))
        self.assertEqual((caught.exception.code, caught.exception.category), ("finereport_daily_grid_changed", "site_data"))


class Specs(unittest.TestCase):
    def test_the_three_reports_keep_their_identity_and_open_from_the_menu(self):
        from pmos.engine import task_identity
        specs = Province.load("liaoning", REPO / "provinces").datasets()
        sources = {"sitewide_e38351fe661a034a": "lnsnxh_fschjzsjcl", "sitewide_0353290a57044c3e": "lnsnxh_csdwfscfdzjyc",
                   "nonmarket_generation_forecast": "lnsnxh_fscjzzclyc"}
        self.assertEqual(set(sources), set(migration.ENGINE_LANE))
        for name, source in sources.items():
            built = specs[name].build("2026-10-30")
            self.assertEqual((built["transport"], built["frame"], built["site_menu"]), ("finereport", source, {"source": source, "route": "spotgoodsSum"}))
            self.assertEqual(built["set"][built["date_field"]], "2026-10-30")
            identity = task_identity("liaoning", "a", specs[name], "2026-10-30", "")
            self.assertEqual((identity.period["label"], identity.dimensions), ("2026-10-30", {}))

    def test_a_finereport_request_needs_its_frame_menu_and_date(self):
        good = {"transport": "finereport", "path": "/x", "frame": "s", "site_menu": {"route": "spotgoodsSum"},
                "set": {"d": "{date}"}, "date_field": "d"}
        check_request("t", good)
        for broken in ({"frame": None}, {"site_menu": None}, {"date_field": "other"}):
            with self.assertRaises(SpecError):
                check_request("t", {**good, **broken})


class TextGrid(unittest.TestCase):
    def grid(self, value):
        times = [f"{i // 4:02d}:{i % 4 * 15:02d}" for i in range(1, 97)]
        rows = []
        for start in range(0, 96, 16):
            rows.append("<tr>" + "".join(f"<td>{t}</td>" for t in times[start:start + 16]) + "</tr>")
            rows.append("<tr>" + "".join(f"<td>{value}</td>" for _ in range(16)) + "</tr>")
        return {"currentPage": 1, "totalPage": 1, "htmlTag": "<table>" + "".join(rows) + "</table>"}

    def test_a_grid_of_words_keeps_its_times_checked(self):
        """平衡裕度 and 可再生能源富余 answer words in the same 96-point grid (samples of 2026-10-25)."""
        from pmos.validate import Response, evaluate
        def run(body, **spec):
            return evaluate({"kind": "finereport_daily", **spec}, Response(200, "application/json", json.dumps(body, ensure_ascii=False).encode()))
        self.assertEqual(run(self.grid("平衡紧张"), text_values=True).result, "saved")
        with self.assertRaises(Fault) as caught:
            run(self.grid("平衡紧张"))
        self.assertEqual(caught.exception.code, "finereport_value_not_numeric")
        cut = self.grid("弃风")
        cut["htmlTag"] = cut["htmlTag"].replace("<td>00:15</td>", "<td>00:20</td>")
        with self.assertRaises(Fault) as caught:
            run(cut, text_values=True)
        self.assertEqual(caught.exception.code, "finereport_daily_times_changed")


class Table(unittest.TestCase):
    def evaluate(self, body):
        from pmos.validate import Response, evaluate
        return evaluate({"kind": "finereport_table"}, Response(200, "application/json", json.dumps(body, ensure_ascii=False).encode()))

    def test_a_table_of_any_shape_is_saved_and_a_blank_one_is_empty(self):
        html = "<table><tr><td>断面</td><td>值</td></tr><tr><td>甲</td><td>1</td></tr></table>"
        self.assertEqual(self.evaluate({"currentPage": 1, "totalPage": 1, "htmlTag": html}).result, "saved")
        blank = '<div class="pageContentDIV"><table class="x-table "><tr><td></td></tr><tr><td></td></tr></table></div>'
        self.assertEqual(self.evaluate({"currentPage": 1, "totalPage": 1, "htmlTag": blank}).result, "empty")

    def test_more_pages_are_not_saved_as_if_page_one_were_all(self):
        from pmos.faults import Fault
        with self.assertRaises(Fault) as caught:
            self.evaluate({"currentPage": 1, "totalPage": 3, "htmlTag": "<table><tr><td>甲</td></tr></table>"})
        self.assertEqual((caught.exception.code, caught.exception.category), ("finereport_table_more_pages", "site_data"))


if __name__ == "__main__":
    unittest.main()
