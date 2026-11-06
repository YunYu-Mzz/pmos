import unittest
from dataclasses import replace
from helpers import FakeTransport,TempDir,dataset,envelope,make_province
from pmos.engine import Engine
from pmos.faults import Fault

class ActualGapTests(unittest.IsolatedAsyncioTestCase):
 def create(self,data):
  t=TempDir();self.addCleanup(t.cleanup);item=dataset('generation_output_actual',response='json')
  p=replace(make_province(t.path,[item]),id='anhui');e=Engine(p,FakeTransport(default=envelope(0,data)),instance=t.path/'var');self.addCleanup(e.ledger.close);e.spacing_wait=lambda *a:0
  return e,p.datasets()[item['id']]
 async def test_96_actual_values_are_saved_and_existing_dates_are_not_repeated(self):
  times=[f'{h:02d}:{m:02d}' for h in range(24) for m in (0,15,30,45) if h or m]+['24:00']
  e,s=self.create({t:'1' for t in times});cmd={'period':'2026-10-14','approval':'user'}
  r=await e._collect_missing_actual_curve(s,cmd);self.assertEqual((r['result'],r['points']),('saved',96));self.assertTrue(e.archive.verify(e.archive.records(s.id)[-1]))
  with self.assertRaisesRegex(Fault,'actual_gap_task_already_present'):await e._collect_missing_actual_curve(s,cmd)
 async def test_incomplete_actual_response_is_kept_as_sample_only(self):
  e,s=self.create({'00:15':1})
  with self.assertRaisesRegex(Fault,'actual_gap_values_incomplete'):await e._collect_missing_actual_curve(s,{'period':'2026-10-14','approval':'user'})
  self.assertEqual(len(e.samples.records(s.id)),1);self.assertEqual(len(e.archive.records(s.id)),0)
