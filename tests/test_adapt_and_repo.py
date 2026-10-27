"""Adaptation helpers and the committed province configurations."""
import json
import unittest

from pmos import adapt
from pmos.province import PROVINCES, Province, province_ids
from pmos.spec import SpecError

from helpers import TempDir, make_province


class AdaptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)

    def test_scaffold_creates_a_loadable_province_once(self):
        root = self.tmp.path / "provinces"
        adapt.scaffold("hebei", "河北", "https://pmos.he.sgcc.com.cn", root)
        province = Province.load("hebei", root)
        self.assertEqual((province.name, province.datasets()), ("河北", {}))
        with self.assertRaises(SpecError):
            adapt.scaffold("hebei", "河北", "https://pmos.he.sgcc.com.cn", root)

    def test_templatize_replaces_only_period_values(self):
        body = {"data": {"date": "2026-10-11", "unit": "2026-10-11-not"}, "month": "2026-10", "n": 3}
        self.assertEqual(adapt.templatize(body, None, "day"),
                         {"data": {"date": "{date}", "unit": "2026-10-11-not"}, "month": "{month}", "n": 3})
        self.assertEqual(adapt.templatize(body, {"date"}, "day")["month"], "2026-10")

    def test_candidate_from_capture_writes_spec(self):
        province = make_province(self.tmp.path, [])
        entry = {"index": 7, "method": "POST", "path": "/px-x/export", "body": {"infoDate": "2026-10-11", "k": "v"},
                 "content_type": "application/vnd.ms-excel", "frame_url": "https://x/#/r"}
        spec = adapt.candidate_from_capture(province, entry, "new_report", "新报表", "day", "2026-01-01")
        self.assertEqual(spec.request["body"], {"infoDate": "{date}", "k": "v"})
        self.assertEqual((spec.status, spec.response["kind"]), ("candidate", "excel"))
        self.assertIn("new_report", province.datasets())


class RepositoryConfigTests(unittest.TestCase):
    def test_three_provinces_load_offline_with_no_enabled_datasets(self):
        self.assertTrue({"anhui", "liaoning", "shanxi"} <= set(province_ids()))
        for pid in ("anhui", "liaoning", "shanxi"):
            with self.subTest(province=pid):
                province = Province.load(pid)
                specs = province.datasets()
                self.assertGreater(len(specs), 10)
                keepalive = province.keepalive_spec()
                self.assertIsNone(keepalive)
                self.assertEqual(province.enabled(), [])
                for spec in specs.values():
                    due = spec.periods_to_collect()  # a complete-only year may have none yet
                    for _, values in spec.scope_items()[:1]:
                        spec.build(due[0] if due else spec.period["start"], values)
                    if spec.documents:
                        item = {"record": {}, "attachment": {"id": "f", "name": "n"}, "title": "t", "record_id": "r",
                                "published": "2026-01-01", "period": "2026-01", "period_basis": "title"}
                        spec.build_download(item)

    def test_no_secrets_in_committed_specs(self):
        for path in PROVINCES.glob("*/**/*.json"):
            text = path.read_text(encoding="utf-8").lower()
            for word in ("x-token", "x-ticket", "cookie", "password", "authorization"):
                self.assertNotIn(f'"{word}"', text, f"{path} contains {word}")
            json.loads(text)


if __name__ == "__main__":
    unittest.main()
