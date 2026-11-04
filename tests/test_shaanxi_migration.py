import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from pmos.contracts import canonical
from pmos.ledger import Ledger
from pmos.migration.shaanxi import KINDS, audit, decode_journal, import_journal, task_identity


def job(kind='table',status='pending',params=None):
    params=params if params is not None else {'date':'2026-01-01','page':1,'size':20,'valueType':'合成分类'}
    value={'datasetId':'synthetic-source','kind':kind,'params':params,'status':status}
    key_doc={'kind':kind,'fileId':params['fileId']} if kind=='download' else {k:value[k] for k in ['datasetId','kind','params']}
    value['key']=hashlib.sha256(canonical(key_doc)).hexdigest()
    return value


class Migration(unittest.TestCase):
    def test_all_13_kinds_map_with_original_parameters(self):
        jobs={}
        for kind in KINDS:
            j=job(kind,params={'fileId':'fixture-file'} if kind=='download' else {'fixtureKind':kind})
            jobs[j['key']]=j
        rows=decode_journal({'jobs':jobs},'fixture-account')
        self.assertEqual(len(rows),13)
        self.assertTrue(all(r.identity is not None for r in rows))
        self.assertFalse(audit({'jobs':jobs},'fixture-account')['online_dispatch_authorized'])

    def test_open_week_has_no_inferred_end_or_snapshot(self):
        j=job(params={'startDate':'2026-01-01','endDate':None,'page':1,'size':10})
        self.assertEqual(task_identity(j,'fixture').document()['period'],{'kind':'native_open_range','start':'2026-01-01','end':None})

    def test_held_unknown_and_local_parent_facts_remain_consumed(self):
        for status in ['saved','empty','held','held_interrupted','held_request_failed_412','unknown','running','assembled','expanded','admitted']:
            j=job(status=status);r=decode_journal({'jobs':{j['key']:j}},'fixture')[0]
            self.assertTrue(r.consumed)

    def test_source_and_date_protection_retained(self):
        j=job();k=j['key'];v={'jobs':{k:j},'datasetBlocks':{j['datasetId']:{'reason':'fixture'}},'gridDateHolds':{j['datasetId']:{'2026-01-01':{'reason':'fixture'}}}}
        r=decode_journal(v,'fixture')[0];self.assertIn('original_source_protected',r.blockers);self.assertIn('original_grid_date_protected',r.blockers)

    def test_pending_with_reservation_is_not_eligible(self):
        j=job();j['reservedAt']='2026-01-01T00:00:00Z';r=decode_journal({'jobs':{j['key']:j}},'fixture')[0]
        self.assertTrue(r.consumed);self.assertEqual(r.state,'unknown')

    def test_bad_hash_preserved_unmapped_and_unknown_status_held(self):
        j=job(status='new-unreviewed-status');j['key']='0'*64
        r=decode_journal({'jobs':{j['key']:j}},'fixture')[0]
        self.assertIsNone(r.identity);self.assertTrue(r.consumed);self.assertEqual(r.state,'held')

    def test_import_fences_dispatch_and_prevents_consumed_replay(self):
        j=job(status='held_request_failed_412');v={'jobs':{j['key']:j}}
        with TemporaryDirectory() as tmp:
            db=Ledger(Path(tmp)/'ledger.sqlite3')
            try:
                import_journal(db,v,'fixture',snapshot_sha256='1'*64)
                self.assertEqual(db.dispatch_blocker(),'legacy_shadow_requires_owner_handoff')
                self.assertTrue(db.consumed_identity(task_identity(j,'fixture').key))
                self.assertEqual(db.legacy_states(task_identity(j,'fixture').key),['held'])
            finally:db.close()
