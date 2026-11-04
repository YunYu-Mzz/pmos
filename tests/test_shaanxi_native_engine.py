import asyncio
import hashlib
import json
from datetime import datetime,timezone,timedelta
from pathlib import Path
import tempfile
import time
import unittest
from pmos.contracts import canonical
from pmos.faults import Fault
from pmos.migration.shaanxi import task_identity
from pmos.province import Province
from pmos.shaanxi_native import NativeTaskTransport,ShaanxiEngine
from pmos.shaanxi_transport import OriginalWorkerLock,atomic,load
from pmos.shaanxi_lab import ROOT


class NativeEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.source=self.base/'legacy';self.state=self.source/'state';self.instance=self.base/'pmos'
        folder=self.state/'collection_queue';folder.mkdir(parents=True);(folder/'worker.lock').touch()
        self.lock=OriginalWorkerLock(self.source);self.lock.acquire();self.addCleanup(self.lock.close)
        self.job={'datasetId':'fixture-source','kind':'table','params':{'date':'2026-01-01','page':1,'size':50},'status':'pending'}
        self.job['key']=hashlib.sha256(canonical({k:self.job[k] for k in ['datasetId','kind','params']})).hexdigest()
        atomic(folder/'journal.json',{'jobs':{self.job['key']:self.job}})
        atomic(folder/'control.json',{'enabled':True});atomic(self.state/'keeper/control.json',{'enabled':True})
        now=datetime.now(timezone.utc);stamp=now.isoformat()
        atomic(self.state/'keeper/status.json',{'at':stamp,'phase':'idle','controlEnabled':True,'pending':None,'nextAttemptAt':(now+timedelta(seconds=300)).isoformat()})
        atomic(self.state/'keeper/last_success.json',{'finishedAt':stamp,'passed':True,'fullSampleMatches':True})
        province=self.base/'provinces/shaanxi';province.mkdir(parents=True)
        atomic(province/'province.json',{'id':'shaanxi','name':'合成陕西','origin':'https://offline.invalid','policy':{'response_interval_seconds':1,'request_timeout_seconds':10}})
        self.transport=NativeTaskTransport(self.source,self.instance,self.lock,generation='fixture',runtime_pins={})
        self.engine=ShaanxiEngine(Province.load('shaanxi',self.base/'provinces'),self.transport,instance=self.instance,account_alias='fixture-account')
        self.engine.acquire();self.addCleanup(self.engine.release)

    async def observer(self,status='saved',transition=False):
        inbox=self.state/'browser_session/inbox'
        for _ in range(100):
            files=list(inbox.glob('*.json')) if inbox.exists() else []
            if files:break
            await asyncio.sleep(.01)
        else:raise AssertionError('PMOS never published')
        command=load(files[0]);self.assertEqual(command,{'op':'queueStep','preferredTaskKey':self.job['key']})
        result_job=dict(self.job,status='expanded' if transition else status)
        if not transition:
            f=json.loads((ROOT/'tests/fixtures/shaanxi_grid_fixture.json').read_text());body=json.dumps(f['body'],ensure_ascii=False).encode()
            payload=self.source/'fixture.response';payload.write_bytes(body);self.raw_path=payload
            destination=self.state/'collection_queue/tasks'/self.job['key'];destination.mkdir(parents=True)
            request={'dataset':{**f['contract']['dataset'],'endpoint':'/fixture'},'params':self.job['params'],
                'turn':{**f['contract']['turn'],'responses':[{'kind':'data','path':'/fixture','status':200,'responseFile':str(payload),'sha256':hashlib.sha256(body).hexdigest()}]}}
            atomic(destination/'validation_input.json',request)
            result_job['validation']=str(destination/'validated.json');atomic(destination/'validated.json',{'fixture':True,'response_sha256':hashlib.sha256(body).hexdigest()})
        atomic(self.state/'collection_queue/journal.json',{'jobs':{self.job['key']:result_job}})
        atomic(self.state/'browser_session/results'/files[0].name,{'ok':True,'result':{'phase':'expanded_into_native_types' if transition else 'completed','job':result_job}})

    async def test_pmos_owns_exact_attempt_archive_and_tombstone(self):
        observer=asyncio.create_task(self.observer())
        result=await self.engine.collect_native_job(self.job);await observer
        self.assertTrue(self.engine.archive.verify(result.record))
        self.assertTrue(self.engine.ledger.consumed_identity(task_identity(self.job,'fixture-account').key))
        target=self.engine.archive.root/result.record['path'];self.assertEqual(target.stat().st_ino,self.raw_path.stat().st_ino)
        with self.assertRaises(Fault):await self.engine.collect_native_job(self.job)
        self.assertEqual(len(list((self.state/'browser_session/inbox').glob('*.json'))),1)

    async def test_local_scope_expansion_records_no_fabricated_http_original(self):
        observer=asyncio.create_task(self.observer(transition=True))
        result=await self.engine.collect_native_job(self.job);await observer
        self.assertIsNone(result.record);self.assertFalse(self.engine.archive.records())
        row=self.engine.ledger.db.execute('SELECT outcome,http_status FROM attempts').fetchone()
        self.assertEqual(tuple(row),('local_transition',None))

    async def test_due_keeper_and_paused_control_prevent_publication(self):
        atomic(self.state/'keeper/control.json',{'enabled':False})
        with self.assertRaises(Fault):await self.engine.collect_native_job(self.job)
        self.assertEqual(self.engine.ledger.db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
        self.assertFalse((self.state/'browser_session/inbox').exists())
