"""Shaanxi runs from tools/shaanxi_owner.py only: the common CLI cannot put a second worker on its account."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pmos
from pmos.shaanxi_lab import ROOT


class LabBoundary(unittest.TestCase):
    def test_import_is_from_this_checkout(self):
        self.assertTrue(Path(pmos.__file__).resolve().is_relative_to(ROOT))
        self.assertFalse(any(p.is_symlink() for p in (ROOT/'pmos').rglob('*')))

    def test_online_commands_fail_before_creating_instance(self):
        with tempfile.TemporaryDirectory() as data:
            for command in ['run', 'start', 'accept', 'capture', 'serve']:
                p = subprocess.run([sys.executable,'-B','-m','pmos','shaanxi',command],cwd=ROOT,capture_output=True,text=True,
                                   env={**os.environ,'PMOS_DATA':data})
                self.assertEqual(p.returncode,2,command)
            self.assertEqual(os.listdir(data),[])

    def test_source_catalog_has_no_runnable_dataset_or_credentials(self):
        from pmos.province import Province
        p=Province.load('shaanxi')
        self.assertEqual(p.enabled(),[]);self.assertEqual(p.browser,{})
        value=json.loads((ROOT/'provinces/shaanxi/inventory.json').read_text())
        self.assertTrue(all(not x['online_enabled'] for x in value['entries']))
        self.assertEqual(value['sample_verified_entries'],0)
