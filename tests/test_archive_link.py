"""original/ and responses/ share one file for one answer; a store does not reread a year's manifest."""
import json
import os
import unittest
from unittest.mock import patch

from pmos.archive import Archive
from pmos.engine import Engine
from pmos.validate import Response

from helpers import Clock, FakeTransport, TempDir, dataset, make_province


class Link(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.responses = Archive(self.tmp.path / "responses", "testland")
        self.original = Archive(self.tmp.path / "original", "testland")
        self.original.link_from = self.responses

    def test_the_same_bytes_are_one_file_with_two_names(self):
        first = self.responses.store("load", "2026-10-01", b"answer", ".json", scope="attempt_1")
        second = self.original.store("load", "2026-10-01", b"answer", ".json")
        a, b = self.responses.root / first["path"], self.original.root / second["path"]
        self.assertEqual(a.stat().st_ino, b.stat().st_ino)
        self.assertEqual(a.stat().st_nlink, 2)
        self.assertTrue(self.original.verify(second))
        a.unlink()
        self.assertEqual(b.read_bytes(), b"answer", "removing responses/ leaves the original")
        self.assertTrue(self.original.verify(second))

    def test_other_bytes_are_their_own_file(self):
        self.responses.store("load", "2026-10-01", b"answer", ".json", scope="attempt_1")
        stored = self.original.store("load", "2026-10-02", b"another", ".json")
        self.assertEqual((self.original.root / stored["path"]).stat().st_nlink, 1)

    def test_a_link_that_cannot_be_made_falls_back_to_a_copy(self):
        self.responses.store("load", "2026-10-01", b"answer", ".json", scope="attempt_1")
        with patch("pmos.archive.os.link", side_effect=OSError("cross-device")):
            stored = self.original.store("load", "2026-10-01", b"answer", ".json")
        self.assertEqual((self.original.root / stored["path"]).stat().st_nlink, 1)
        self.assertTrue(self.original.verify(stored))
        self.assertEqual([p.name for p in (self.original.root / "load" / "2026-10-01").iterdir() if p.name.startswith(".incoming")], [])

    def test_the_manifest_gets_one_line_and_is_not_reread_for_every_store(self):
        self.original.store("load", "2026-10-01", b"answer", ".json")
        self.original.store("load", "2026-10-01", b"answer", ".json")
        self.assertEqual(len(self.original.records()), 1)
        with patch.object(Archive, "records", side_effect=AssertionError("reread")):
            self.original.store("load", "2026-10-02", b"next", ".json")
        self.assertEqual(len(self.original.records()), 2)

    def test_lines_another_process_appended_count_and_a_half_written_one_waits(self):
        other = Archive(self.tmp.path / "original", "testland")
        other.store("load", "2026-10-01", b"answer", ".json")
        self.original.store("load", "2026-10-01", b"answer", ".json")
        self.assertEqual(len(self.original.records()), 1, "the other process's line is known here")
        rest = json.dumps({"dataset": "load", "period": "2026-10-03", "scope": "", "sha256": "x"})
        with self.original.manifest.open("a") as stream:
            stream.write(rest[:10])
            stream.flush()
            self.assertFalse(self.original._known("load", "2026-10-03", "", "x"))
            stream.write(rest[10:] + "\n")
        self.assertTrue(self.original._known("load", "2026-10-03", "", "x"))


class Engined(unittest.IsolatedAsyncioTestCase):
    async def test_a_collected_answer_takes_one_file_on_disk(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        patcher = patch.dict(os.environ, {"PMOS_DATA": str(tmp.path / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        province = make_province(tmp.path, [dataset("load", start="2026-10-01", response="json")],
                                 policy={"response_interval_seconds": 15})
        body = json.dumps({"status": 0, "data": {"list": [1]}}).encode()
        engine = Engine(province, FakeTransport(clock, default=Response(200, "application/json", body), duration=1),
                        instance=province.instance(), clock=clock)
        self.addCleanup(engine.ledger.close)
        engine.state.set_control(collect=True)
        spec = province.datasets()["load"]
        engine.ledger.seed("load", ["2026-10-01"])
        await engine.collect(spec, "2026-10-01", "")
        originals = [p for p in engine.archive.root.rglob("*.json") if p.is_file()]
        responses = [p for p in engine.responses.root.rglob("*.json") if p.is_file()]
        self.assertEqual((len(originals), len(responses)), (1, 1))
        self.assertEqual(originals[0].stat().st_ino, responses[0].stat().st_ino)


if __name__ == "__main__":
    unittest.main()
