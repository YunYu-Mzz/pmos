import unittest
from types import SimpleNamespace

from pmos.browser.transport import _matches_request_body
from pmos.spec import DatasetSpec
from helpers import dataset


class QueryBodyBindingTests(unittest.TestCase):
    def test_bootstrap_binding_renders_the_actual_requested_date(self):
        value = dataset("report", response="json")
        value["request"]["bootstrap"] = {"transport": "ui", "action": "query", "frame": "report",
            "match_request_body": {"businessTime": "{date}"}}
        spec = DatasetSpec(**value)
        self.assertEqual(spec.build("2026-11-02")["bootstrap"]["match_request_body"],
                         {"businessTime": "2026-11-02"})

    def test_only_the_requested_date_and_dimensions_match(self):
        expected = {"businessTime": "2026-11-02", "dimensions.name": "A"}
        for day, name, accepted in (("2026-11-02", "A", True),
                                    ("2026-11-04", "A", False),
                                    ("2026-11-02", "B", False)):
            request = SimpleNamespace(post_data_json={"businessTime": day, "dimensions": {"name": name}})
            self.assertEqual(_matches_request_body(request, expected), accepted)
        self.assertFalse(_matches_request_body(SimpleNamespace(post_data_json={}), expected))
        self.assertFalse(_matches_request_body(SimpleNamespace(post_data_json="invalid"), expected))

    def test_unconfigured_reports_keep_their_original_matching(self):
        self.assertTrue(_matches_request_body(object(), None))


if __name__ == "__main__":
    unittest.main()
