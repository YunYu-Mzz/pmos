from datetime import datetime,timezone,timedelta
import hashlib,json,os,tempfile,time
from pathlib import Path
import unittest
from unittest.mock import patch
from pmos.contracts import canonical
from pmos.faults import Fault
from pmos.shaanxi_lifecycle import prepare_handoff,validate_handoff,check_source_quiescent,code_fingerprint
from pmos.ledger import Ledger
from pmos.shaanxi_transport import OriginalWorkerLock,atomic
from pmos.shaanxi_runner import Runner,candidates


def identity(pid):return {'pid':pid,'start_ticks':'fixture-'+str(pid),'boot_id':'fixture-boot'}


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.source=self.base/'legacy';self.runtime=self.base/'runtime';self.instance=self.base/'instance';self.code=self.base/'code'
        self.state=self.source/'state';q=self.state/'collection_queue';q.mkdir(parents=True);(q/'worker.lock').touch()
        (self.source/'shaanxi_crawler').mkdir();(self.source/'shaanxi_crawler/fixture.py').write_text('x=1\n')
        self.runtime.mkdir();(self.runtime/'queue_operations.mjs').write_text('export const fixture=1;\n')
        (self.code/'pmos').mkdir(parents=True);(self.code/'pmos/core.py').write_text('fixture=1\n')
        atomic(self.code/'provinces/shaanxi/province.json',{'id':'shaanxi','name':'fixture','origin':'https://offline.invalid','policy':{'response_interval_seconds':1}})
        self.job={'datasetId':'fixture-source','kind':'table','params':{'date':'2026-01-01','page':1,'size':50},'status':'pending','priority':100}
        self.job['key']=hashlib.sha256(canonical({k:self.job[k] for k in ['datasetId','kind','params']})).hexdigest()
        atomic(q/'journal.json',{'jobs':{self.job['key']:self.job}});atomic(q/'worker_owner.json',{'pid':99999999,'startedAt':0})
        atomic(q/'control.json',{'enabled':True});atomic(self.state/'keeper/control.json',{'enabled':True})
        now=datetime.now(timezone.utc)
        atomic(self.state/'keeper/status.json',{'pid':101,'at':now.isoformat(),'pending':None,'controlEnabled':True,'phase':'idle','nextAttemptAt':(now+timedelta(seconds=300)).isoformat()})
        atomic(self.state/'keeper/runtime.json',{'pending':None});atomic(self.state/'keeper/last_success.json',{'finishedAt':now.isoformat(),'passed':True,'fullSampleMatches':True,'browserPid':103})
        atomic(self.state/'browser_session/ready.json',{'pid':102})
        self.patch=patch('pmos.shaanxi_lifecycle.process_identity',side_effect=identity);self.patch.start();self.addCleanup(self.patch.stop)
        self.runner_patch=patch('pmos.shaanxi_runner.process_identity',side_effect=identity);self.runner_patch.start();self.addCleanup(self.runner_patch.stop)

    def prepare(self):return prepare_handoff(self.source,self.runtime,self.instance,self.code,approval='fixture goal authorizes integration')

    def test_frozen_snapshot_and_new_owner_generation_preserve_original(self):
        original=(self.state/'collection_queue/journal.json').read_bytes();c=self.prepare()
        self.assertEqual(c['phase'],'prepared_no_dispatch');self.assertTrue(c['generation'].startswith('pmos-shaanxi-'))
        self.assertEqual((self.state/'collection_queue/journal.json').read_bytes(),original)
        self.assertEqual(Path(c['snapshot_ref']).stat().st_ino,(self.state/'collection_queue/journal.json').stat().st_ino)
        validate_handoff(c,self.code)

    def test_existing_block_does_not_clear_or_prepare_handoff(self):
        atomic(self.state/'session_block.json',{'reason':'unknown_request_result','key':'a'*64})
        before=(self.state/'session_block.json').read_bytes()
        with self.assertRaises(Fault):self.prepare()
        self.assertFalse((self.instance/'handoff.json').exists());self.assertEqual((self.state/'session_block.json').read_bytes(),before)

    def test_pending_keeper_and_unanswered_observer_command_block_handoff(self):
        atomic(self.state/'browser_session/inbox/fixture.json',{'op':'queueStep'})
        with self.assertRaises(Fault):self.prepare()

    def test_new_code_cannot_reuse_old_release_receipt(self):
        c=self.prepare();(self.code/'pmos/core.py').write_text('fixture=2\n')
        with self.assertRaises(Fault):validate_handoff(c,self.code)

    def test_original_worker_lock_must_be_exclusively_obtained(self):
        lock=OriginalWorkerLock(self.source);lock.acquire()
        try:
            with self.assertRaises(Fault):self.prepare()
        finally:lock.close()

    def test_runner_imports_full_shadow_before_dispatch_and_owns_original_lock(self):
        c=self.prepare();r=Runner(c,self.code);r.acquire()
        try:
            self.assertIsNotNone(r.engine.ledger.handoff());self.assertEqual(r.engine.ledger.db.execute('SELECT COUNT(*) FROM legacy_records').fetchone()[0],1)
            other=OriginalWorkerLock(self.source)
            with self.assertRaises(Fault):other.acquire()
            jobs,excluded=candidates({'jobs':{self.job['key']:self.job}},{'datasets':[{'id':'fixture-source','enabled':True,'family':'grid'}]},r.engine.ledger,c['account_alias'])
            self.assertEqual([j['key'] for j in jobs],[self.job['key']])
        finally:r.engine.release();r.lock.close()

    def test_one_task_selection_still_excludes_consumed_first_without_hashing_whole_frontier(self):
        c=self.prepare();r=Runner(c,self.code);r.acquire()
        try:
            r.engine.ledger.db.execute('UPDATE legacy_records SET consumed=1')
            first={**self.job,'priority':0};jobs={first['key']:first};expected=None
            for n in (2,3):
                job={**self.job,'params':{'date':f'2026-01-0{n}','page':1,'size':50},'priority':n}
                job['key']=hashlib.sha256(canonical({k:job[k] for k in ['datasetId','kind','params']})).hexdigest();jobs[job['key']]=job
                if n==2:expected=job['key']
            with patch.object(r.engine.ledger,'consumed_identity',wraps=r.engine.ledger.consumed_identity) as consumed:
                selected,counts=candidates({'jobs':jobs},{'datasets':[{'id':'fixture-source','enabled':True,'family':'grid'}]},r.engine.ledger,c['account_alias'],limit=1)
            self.assertEqual([x['key'] for x in selected],[expected]);self.assertEqual(consumed.call_count,2);self.assertEqual(counts['consumed_identity'],1)
        finally:r.engine.release();r.lock.close()

    def test_code_upgrade_requires_recorded_chain_and_preserves_imported_facts(self):
        c=self.prepare();r=Runner(c,self.code);r.acquire();r.engine.release();r.lock.close()
        (self.code/'pmos/core.py').write_text('fixture=2\n');new={**c,'code_sha':code_fingerprint(self.code)}
        with self.assertRaisesRegex(Fault,'native_release_upgrade_not_recorded'):Runner(new,self.code).acquire()
        db=Ledger(self.instance/'ledger.sqlite3');receipt=db.handoff()
        db.db.execute('INSERT INTO native_release_upgrades(previous_code_sha,new_code_sha,handoff_receipt_sha,approval,at) VALUES(?,?,?,?,?)',(c['code_sha'],new['code_sha'],receipt['receipt_sha256'],'fixture explicit code upgrade',time.time()));db.close()
        upgraded=Runner(new,self.code);upgraded.acquire()
        try:
            self.assertEqual(upgraded.engine.ledger.db.execute('SELECT COUNT(*) FROM legacy_records').fetchone()[0],1)
            self.assertEqual(upgraded.engine.ledger.request_budget(),5)
        finally:upgraded.engine.release();upgraded.lock.close()
