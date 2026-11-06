"""An export labelled with an unexpected media type is judged by the workbook itself, on request."""
import unittest

from pmos.faults import Fault
from pmos.validate import Response, evaluate

from helpers import xlsx


class AnyMediaTypeTests(unittest.TestCase):
    def test_unexpected_label_is_refused_by_default(self):
        with self.assertRaises(Fault) as caught:
            evaluate({"kind": "xlsx"}, Response(200, "text/plain", xlsx()))
        self.assertEqual(caught.exception.code, "file_type_changed")

    def test_workbook_with_any_label_is_saved_when_the_spec_allows_it(self):
        outcome = evaluate({"kind": "xlsx", "any_media_type": True}, Response(200, "text/plain", xlsx()))
        self.assertEqual((outcome.result, outcome.extension), ("saved", ".xlsx"))

    def test_any_label_still_needs_a_sound_workbook(self):
        with self.assertRaises(Fault):
            evaluate({"kind": "xlsx", "any_media_type": True}, Response(200, "text/plain", b"PK\x03\x04broken"))

    def test_any_label_still_reads_a_json_answer_as_the_site_s(self):
        with self.assertRaises(Fault) as caught:
            evaluate({"kind": "xlsx", "any_media_type": True},
                     Response(200, "application/json", b'{"status":40302,"message":"no"}'))
        self.assertNotEqual(caught.exception.code, "file_type_changed")


if __name__ == "__main__":
    unittest.main()
