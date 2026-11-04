import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from pmos.faults import Fault
from pmos.shaanxi_transport import OriginalWorkerLock, CommandClient, atomic, load, verify_readonly_preflight


class Protocol(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'legacy';self.instance=self.root/'pmos'
        lockfile=self.source/'state/collection_queue/worker.lock';lockfile.parent.mkdir(parents=True);lockfile.touch()
        self.lock=OriginalWorkerLock(self.source);self.lock.acquire();self.addCleanup(self.lock.close)
        self.client=CommandClient(self.source,self.instance,self.lock,generation='fixture-owner')
        self.key='a'*64

    def test_original_worker_kernel_lock_excludes_second_worker(self):
        other=OriginalWorkerLock(self.source)
        with self.assertRaises(Fault):other.acquire()

    def test_replaced_lock_is_not_accepted_as_same_ownership(self):
        self.lock.path.unlink();self.lock.path.touch()
        with self.assertRaises(Fault):self.lock.check()

    def test_preparation_and_publication_are_durable_and_idempotent(self):
        plan=self.client.prepare(1,self.key);self.assertEqual(self.client.prepare(1,self.key),plan)
        command_id=self.client.publish(plan);self.assertEqual(self.client.publish(plan),command_id)
        paths=list((self.source/'state/browser_session/inbox').glob('*.json'));self.assertEqual(len(paths),1)
        self.assertEqual(load(paths[0]),{'op':'queueStep','preferredTaskKey':self.key})
        with self.assertRaises(Fault):self.client.prepare(1,'b'*64)

    async def test_observation_timeout_preserves_same_command_without_reissue(self):
        plan=self.client.prepare(1,self.key);command_id=self.client.publish(plan)
        with self.assertRaises(Fault) as e:await self.client.wait(plan,timeout=0)
        self.assertEqual(e.exception.code,'original_native_command_result_pending')
        self.assertEqual(self.client.prepare(1,self.key)['command_id'],command_id)
        self.assertEqual(len(list((self.source/'state/browser_session/inbox').glob('*.json'))),1)

    async def test_late_terminal_result_is_read_without_publishing_again(self):
        p=self.client.prepare(1,self.key);cid=self.client.publish(p)
        dest=self.source/'state/browser_session/results'/(cid+'.json')
        atomic(dest,{'ok':True,'result':{'phase':'completed','job':{'key':self.key,'status':'saved'}}})
        self.assertEqual((await self.client.wait(p,timeout=0))['phase'],'completed')
        self.assertTrue((self.instance/'native_commands/1.result_ref.json').is_file())
        self.assertEqual(self.client.publish(p),cid)

    def test_wrong_job_and_altered_preparation_fail_closed(self):
        p=self.client.prepare(1,self.key);cid=self.client.publish(p)
        atomic(self.source/'state/browser_session/results'/(cid+'.json'),{'ok':True,'result':{'phase':'completed','job':{'key':'b'*64}}})
        with self.assertRaises(Fault):self.client.result(p)
        p['command']['preferredTaskKey']='b'*64
        with self.assertRaises(Fault):self.client.publish(p)

    def test_code_drift_proves_unsent_and_creates_no_command(self):
        atomic(self.source/'code.json',{'fixture':1});digest='0'*64
        with self.assertRaises(Fault) as e:verify_readonly_preflight(self.source,self.key,runtime_pins={'code.json':digest},owner_lock=self.lock)
        self.assertTrue(e.exception.unsent)
        self.assertFalse((self.source/'state/browser_session/inbox').exists())
