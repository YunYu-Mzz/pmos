import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from pmos.faults import Fault
from pmos.shaanxi_runner import Runner
from pmos.shaanxi_transport import atomic
from pmos.migration.shaanxi import task_identity
import test_shaanxi_native_engine as native_fixture


class Recovery(unittest.IsolatedAsyncioTestCase):
    setUp=native_fixture.NativeEngine.setUp
    observer=native_fixture.NativeEngine.observer
    def runner(self):
        r=object.__new__(Runner);r.source=self.source;r.instance=self.instance;r.engine=self.engine;r.transport=self.transport
        self.engine.ledger.db.execute('CREATE TABLE IF NOT EXISTS native_reconciliations(attempt_id INTEGER PRIMARY KEY,result_ref TEXT,disposition TEXT NOT NULL,artifact_sha TEXT,reconciled_at REAL NOT NULL)')
        return r

    async def test_late_reply_resolves_original_unknown_without_republishing(self):
        async def timeout(*a,**k):raise Fault('original_native_command_result_pending','network')
        with patch.object(self.transport.client,'wait',side_effect=timeout):
            with self.assertRaises(Fault):await self.engine.collect_native_job(self.job)
        r=self.runner();self.assertFalse(r.reconcile())
        await self.observer();self.assertTrue(r.reconcile());self.assertTrue(r.reconcile())
        row=self.engine.ledger.db.execute('SELECT outcome FROM attempts').fetchone();self.assertEqual(row[0],'unknown')
        identity=task_identity(self.job,'fixture-account');self.assertEqual(self.engine.ledger.task(identity.dataset,self.job['key'])['status'],'saved')
        self.assertEqual(len(list((self.state/'browser_session/inbox').glob('*.json'))),1)
        self.assertEqual(len(self.engine.archive.records()),1)
        self.assertEqual(self.engine.ledger.db.execute('SELECT disposition FROM native_reconciliations').fetchone()[0],'saved')

    async def test_crash_after_response_before_checkpoint_recovers_without_another_call(self):
        observer=asyncio.create_task(self.observer())
        with patch.object(self.engine.archive,'store',side_effect=OSError('fixture checkpoint interruption')):
            with self.assertRaises(OSError):await self.engine.collect_native_job(self.job)
        await observer;r=self.runner();self.assertTrue(r.reconcile())
        self.assertEqual(len(self.engine.archive.records()),1)
        self.assertEqual(self.engine.ledger.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],1)
        self.assertEqual(len(list((self.state/'browser_session/inbox').glob('*.json'))),1)

    async def test_pending_attempt_before_preparation_is_proven_unsent(self):
        identity=task_identity(self.job,'fixture-account');self.engine.ledger.seed(identity.dataset,[self.job['key']])
        self.engine.ledger.begin('collect',identity.dataset,self.job['key'],identity=identity)
        self.assertTrue(self.runner().reconcile())
        self.assertEqual(self.engine.ledger.db.execute('SELECT outcome FROM attempts').fetchone()[0],'not_sent')
        self.assertFalse((self.state/'browser_session/inbox').exists())

    async def test_late_unsent_reply_does_not_repeat_reconciliation_or_reopen_identity(self):
        async def timeout(*a,**k):raise Fault('original_native_command_result_pending','network')
        with patch.object(self.transport.client,'wait',side_effect=timeout):
            with self.assertRaises(Fault):await self.engine.collect_native_job(self.job)
        prepared=json.loads((self.transport.client.commands/'1.json').read_text())
        atomic(self.state/'browser_session/results'/(prepared['command_id']+'.json'),
            {'ok':True,'result':{'phase':'waiting_turn_lock'}})
        r=self.runner();self.assertTrue(r.reconcile());self.assertTrue(r.reconcile())
        self.assertEqual(self.engine.ledger.db.execute('SELECT outcome FROM attempts').fetchone()[0],'unknown')
        identity=task_identity(self.job,'fixture-account')
        self.assertEqual(self.engine.ledger.task(identity.dataset,self.job['key'])['status'],'skipped')
        self.assertEqual(self.engine.ledger.db.execute('SELECT COUNT(*) FROM native_reconciliations').fetchone()[0],1)

    async def test_error_result_is_isolated_with_its_original_digest(self):
        async def timeout(*a,**k):raise Fault('original_native_command_result_pending','network')
        with patch.object(self.transport.client,'wait',side_effect=timeout):
            with self.assertRaises(Fault):await self.engine.collect_native_job(self.job)
        prepared=json.loads((self.transport.client.commands/'1.json').read_text())
        atomic(self.state/'browser_session/results'/(prepared['command_id']+'.json'),
            {'ok':False,'error':'fixture interrupted after publication'})
        r=self.runner();self.assertTrue(r.reconcile());self.assertTrue(r.reconcile())
        self.assertTrue((self.transport.client.commands/'1.result_ref.json').is_file())
        self.assertEqual(self.engine.ledger.db.execute('SELECT outcome FROM attempts').fetchone()[0],'unknown')
        identity=task_identity(self.job,'fixture-account')
        self.assertEqual(self.engine.ledger.task(identity.dataset,self.job['key'])['status'],'failed')

    async def test_seed_command_is_file_only_and_uses_same_identity_after_wait(self):
        first=self.transport.client.prepare_seed('2026-11-02',1)
        self.assertEqual(first['command'],{'op':'queueSeed','startDate':'2026-11-01','endDate':'2026-11-01'})
        cid=self.transport.client.publish(first)
        self.assertEqual(self.transport.client.publish(first),cid)
        atomic(self.state/'browser_session/results'/(cid+'.json'),{'ok':True,'result':{'phase':'seeded_without_website_requests'}})
        self.assertEqual((await self.transport.client.wait(first,timeout=0))['phase'],'seeded_without_website_requests')
        self.assertEqual(len(list((self.state/'browser_session/inbox').glob('*.json'))),1)
