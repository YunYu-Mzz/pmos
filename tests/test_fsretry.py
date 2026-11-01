"""Anhui 2026-11-01 02:08: macOS briefly refused ~/Desktop with EPERM; a lost state write left a job stuck."""
import errno
import os
import unittest
from unittest.mock import patch

from pmos.archive import Archive
from pmos.fsretry import eperm_retry
from pmos.state import StateStore

from helpers import TempDir


def flaky(times, error=errno.EPERM):
    calls = {"n": 0}
    real = os.replace

    def replace(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= times:
            raise PermissionError(error, os.strerror(error))
        return real(*args, **kwargs)
    return replace, calls


class EpermRetry(unittest.TestCase):
    def test_only_eperm_is_retried_and_eacces_is_not(self):
        sleeps = []
        attempts = iter([PermissionError(errno.EPERM, "x"), PermissionError(errno.EPERM, "x"), "ok"])

        def function():
            value = next(attempts)
            if isinstance(value, Exception):
                raise value
            return value
        self.assertEqual(eperm_retry(function, sleep=sleeps.append), "ok")
        self.assertEqual(sleeps, [0.5, 0.5])

        def denied():
            raise PermissionError(errno.EACCES, "denied")
        with self.assertRaises(PermissionError):
            eperm_retry(denied, sleep=sleeps.append)
        self.assertEqual(len(sleeps), 2, "EACCES is a real permission problem: no retry")

    def test_a_state_write_and_an_original_survive_a_transient_eperm(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        state = StateStore(tmp.path / "state")
        replace, calls = flaky(2)
        with patch("pmos.fsretry.time.sleep", lambda s: None), patch("os.replace", replace):
            state.write("status", {"phase": "collecting"})
        self.assertEqual(state.read("status"), {"phase": "collecting"})
        self.assertEqual(calls["n"], 3)
        archive = Archive(tmp.path / "original", "testland")
        replace, calls = flaky(1)
        with patch("pmos.fsretry.time.sleep", lambda s: None), patch("os.replace", replace):
            record = archive.store("load", "2026-10-31", b'{"v":1}', ".json")
        self.assertTrue((tmp.path / "original" / record["path"]).exists())


if __name__ == "__main__":
    unittest.main()
