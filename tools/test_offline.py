"""Run all pure tests with network connections forbidden and UI opt-out."""
import os
from pathlib import Path
import socket
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.pop("PMOS_LOCAL_UI_TEST", None)
original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex


def guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError("network_forbidden_in_offline_suite")
    return original_connect(self, address)


def guarded_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError("network_forbidden_in_offline_suite")
    return original_connect_ex(self, address)


socket.socket.connect = guarded_connect
socket.socket.connect_ex = guarded_connect_ex
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
