import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


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

    def test_remaining_stage_checks_monday_before_province_remainders(self):
        def module(name):
            return SimpleNamespace(recover=lambda root: {"summary": {"module": name}, "website_requests": 0})
        stream = io.StringIO()
        with patch.object(TOOL.importlib, "import_module", side_effect=module) as imported, patch.object(TOOL.sys, "stdout", stream):
            self.assertEqual(TOOL.main(["all", "--stage", "remaining"]), 0)
        self.assertEqual([call.args[0] for call in imported.call_args_list], [
            "pmos.recovery.monday", "pmos.recovery.liaoning_remaining", "pmos.recovery.anhui_remaining", "pmos.recovery.shanxi_remaining"])
        report = json.loads(stream.getvalue())
        self.assertEqual(list(report["summaries"]), ["liaoning_monday", "liaoning", "anhui", "shanxi"])
        self.assertEqual(report["stage"], "remaining")

    def test_bridges_stage_checks_fixed_cohorts_then_one_followup(self):
        received = []
        def module(name):
            def recover(root, **options):
                received.append((name, options))
                return {"summary": {"module": name}, "website_requests": 0}
            return SimpleNamespace(recover=recover)
        with patch.object(TOOL.importlib, "import_module", side_effect=module), patch.object(TOOL.sys, "stdout", io.StringIO()):
            self.assertEqual(TOOL.main(["all", "--stage", "bridges"]), 0)
        self.assertEqual([name for name, _ in received], ["pmos.recovery.monday", "pmos.recovery.liaoning_bridges",
            "pmos.recovery.anhui_bridges", "pmos.recovery.evidence_followup"])
        self.assertEqual(received[-1][1], {"provinces": ["liaoning", "anhui", "shanxi"]})

    def test_single_province_followup_does_not_expand_selection(self):
        received = []
        def module(name):
            def recover(root, **options):
                received.append((name, options))
                return {"summary": {"scope": "fixture"}}
            return SimpleNamespace(recover=recover)
        with patch.object(TOOL.importlib, "import_module", side_effect=module), patch.object(TOOL.sys, "stdout", io.StringIO()):
            TOOL.main(["shanxi", "--stage", "bridges"])
        self.assertEqual(received, [("pmos.recovery.evidence_followup", {"provinces": ["shanxi"]})])


if __name__ == "__main__":
    unittest.main()
