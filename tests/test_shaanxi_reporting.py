from types import SimpleNamespace
from pathlib import Path
import hashlib,json,tempfile
import unittest
from unittest.mock import patch
from pmos.shaanxi_reporting import augment_metrics,wrap_existing_snapshot,owner_evidence


class Report(unittest.TestCase):
    def metrics(self):return {'collector_state':'inactive','collector_pid':0,'phase':'legacy_old_status','blocked':False,'collection_paused':False,'ordinary_originals':100,'counts_note':'fixture'}
    def evidence(self,phase='completed'):
        return {'owned_live':True,'status':{'pid':123,'phase':phase},'control':{'collect':True,'paused':False,'stop_all':False}}

    def test_only_current_owner_fields_change_and_originals_do_not(self):
        m=self.metrics();keys=set(m);self.assertTrue(augment_metrics(m,self.evidence()))
        self.assertEqual(set(m),keys);self.assertEqual(m['ordinary_originals'],100);self.assertEqual(m['collector_pid'],123);self.assertFalse(m['collection_paused'])

    def test_old_protection_and_human_pause_are_never_cleared_by_reporting(self):
        for flag in ['blocked','collection_paused']:
            m=self.metrics();m[flag]=True;augment_metrics(m,self.evidence());self.assertTrue(m[flag]);self.assertTrue(m['collection_paused'])

    def test_waiting_original_result_is_not_presented_as_healthy_collection(self):
        m=self.metrics();augment_metrics(m,self.evidence('waiting_original_native_result'))
        self.assertTrue(m['blocked']);self.assertTrue(m['collection_paused'])

    def test_no_live_owner_preserves_original_sender_view(self):
        m=self.metrics();original=dict(m);augment_metrics(m,{'owned_live':False});self.assertEqual(m,original)

    def test_same_atomic_task_counts_pass_to_existing_formatter_unchanged(self):
        called=[]
        def note(metrics,**kwargs):
            called.append(kwargs);return '陕西由旧系统采集：'+str(kwargs['saved'])
        module=SimpleNamespace(status_mail=SimpleNamespace(legacy_note=note))
        def snapshot(root):
            metrics=self.metrics();metrics['counts_note']=module.status_mail.legacy_note(metrics,saved=7,pending=9,held=3);return {'metrics':metrics,'coverage':{'fixture_original_sha':'0'*64}}
        module.snapshot=snapshot
        with patch('pmos.shaanxi_reporting.owner_evidence',return_value=self.evidence()):
            wrapped=wrap_existing_snapshot(module,'unused');r=wrapped('unused')
        self.assertEqual(called,[{'saved':7,'pending':9,'held':3}]);self.assertIn('由PMOS采集',r['metrics']['counts_note']);self.assertEqual(r['coverage']['fixture_original_sha'],'0'*64)
        self.assertIs(module.status_mail.legacy_note,note)

    def test_pmos_links_are_partitioned_inside_existing_total_without_adding_again(self):
        calls=[]
        def formatter(**kwargs):calls.append(kwargs);return '陕西由PMOS采集，旧系统冻结90份、PMOS10份'
        module=SimpleNamespace(status_mail=SimpleNamespace(legacy_note=lambda *a,**k:'old',counts_note=formatter,headline=lambda m:'正常'))
        def snapshot(root):
            metrics=self.metrics();metrics['counts_note']=module.status_mail.legacy_note(metrics,saved=7,pending=9,held=3);return {'metrics':metrics}
        module.snapshot=snapshot;evidence={**self.evidence(),'pmos_unique_originals':10}
        with patch('pmos.shaanxi_reporting.owner_evidence',return_value=evidence):
            result=wrap_existing_snapshot(module,'unused')('unused')
        self.assertEqual(result['metrics']['ordinary_originals'],100)
        self.assertEqual((calls[0]['legacy_originals'],calls[0]['pmos_originals']),(90,10))
        self.assertEqual((calls[0]['saved'],calls[0]['pending'],calls[0]['held']),(7,9,3))
        self.assertNotIn('冻结',result['metrics']['counts_note'])

    def test_shared_original_deduplicates_by_sha_and_bad_bytes_are_not_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive=root/'original';archive.mkdir();p=archive/'fixture.json';p.write_bytes(b'{"fixture":1}')
            item={'path':'fixture.json','sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            (archive/'manifest.jsonl').write_text((json.dumps(item)+'\n')*2)
            self.assertEqual(owner_evidence(root)['pmos_unique_originals'],1)
            p.write_bytes(b'changed');self.assertIsNone(owner_evidence(root)['pmos_unique_originals'])
