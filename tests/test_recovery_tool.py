import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("recovery_tool", Path(__file__).resolve().parents[1] / "tools/recover_existing_evidence.py")
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


class RecoveryToolTests(unittest.TestCase):
    def test_output_requires_new_folder_under_project_var(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "pmos/var").mkdir(parents=True)
            expected = root / "pmos/var/new-evidence"
            self.assertEqual(TOOL.output_directory(root / "pmos", expected), expected)
            self.assertFalse(expected.exists())
            for path in (root / "pmos/var", root / "shanxi/state/new", root / "pmos/var/../new"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    TOOL.output_directory(root / "pmos", path)

    def test_output_rejects_var_and_nested_symlink_before_any_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            old = root / "shanxi/state"
            old.mkdir(parents=True)
            project = root / "pmos"
            project.mkdir()
            (project / "var").symlink_to(old, target_is_directory=True)
            with self.assertRaises(ValueError):
                TOOL.output_directory(project, project / "var/new")
            (project / "var").unlink()
            (project / "var").mkdir()
            (project / "var/nested").symlink_to(old, target_is_directory=True)
            with self.assertRaises(ValueError):
                TOOL.output_directory(project, project / "var/nested/new")
            self.assertEqual(list(old.iterdir()), [])

    def test_output_report_rejects_raw_credentials_and_query_fields(self):
        TOOL.validate_report({"source_sha256": "a" * 64, "website_requests": 0})
        for key in ("password", "cookie", "token", "raw", "selectors", "params", "model"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                TOOL.validate_report({"nested": [{key: "unwanted"}]})


if __name__ == "__main__":
    unittest.main()
